#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""The runtime: owns the SIP thread and the asyncio-facing command surface.

One Runtime drives one native event loop. Commands go in as awaitable
futures with a mandatory timeout; events come back on the asyncio loop.
Only one Runtime may be live at a time — libre/baresip state is process
global — and a runtime whose SIP thread died cannot be restarted: start a
new process.
"""

import asyncio
import atexit
import contextlib
import errno as _errno
import logging
import os
import shutil
import tempfile
import threading
import time

from baresip._events import set_sink
from baresip._native import ffi, lib
from baresip.errors import BaresipError, CommandQueueFull, CommandTimeout, RuntimeDead

logger = logging.getLogger("baresip.runtime")

_STOP_RETRIES = 100
_STOP_RETRY_DELAY = 0.01

#: Native log levels, by the name used in configuration.
LOG_LEVELS = {
    "debug": lib.BP_LOG_DEBUG,
    "info": lib.BP_LOG_INFO,
    "warning": lib.BP_LOG_WARN,
    "error": lib.BP_LOG_ERROR,
}

# Everything the native stack says arrives under these two, so an
# application can silence or route them independently of our own logging.
native_logger = logging.getLogger("baresip.native")
sip_logger = logging.getLogger("baresip.native.sip")

_TO_PYTHON_LEVEL = {
    lib.BP_LOG_DEBUG: logging.DEBUG,
    lib.BP_LOG_INFO: logging.INFO,
    lib.BP_LOG_WARN: logging.WARNING,
    lib.BP_LOG_ERROR: logging.ERROR,
}

# The stack rejects an empty configuration, so "apply no settings" still
# needs a line of text.
_DEFAULT_CONFIG = "# baresip-python defaults\n"


class Runtime:
    """Lifecycle owner for the native SIP stack.

    Usage::

        runtime = Runtime()
        await runtime.start()
        ...
        await runtime.close()
    """

    _class_lock = threading.Lock()
    _active = None  # the live instance, if any (one per process at a time)
    _process_poisoned = False  # a runtime died here; no restarts in-process

    def __init__(
        self,
        *,
        command_timeout: float = 5.0,
        watchdog_interval: float = 10.0,
        native_log_level: str = "warning",
    ):
        """Initialize the runtime.

        Args:
            command_timeout: Default seconds to wait for a command's
                completion before CommandTimeout.
            watchdog_interval: Seconds between health PINGs of the SIP
                thread; a miss is reported CRITICAL.
            native_log_level: Lowest severity to capture from the native
                stack: "debug", "info", "warning" or "error".

        Raises:
            ValueError: native_log_level is not one of the four names.
        """
        if native_log_level not in LOG_LEVELS:
            raise ValueError(
                f"native_log_level must be one of {sorted(LOG_LEVELS)}, got {native_log_level!r}"
            )
        self._command_timeout = command_timeout
        self._watchdog_interval = watchdog_interval
        self._native_log_level = LOG_LEVELS[native_log_level]
        self._conf_dir: str | None = None
        self._config_text = _DEFAULT_CONFIG
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._log_thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._stack_down = threading.Event()
        self._init_err: int | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._seq = 0
        self._state = "new"  # new -> running -> closing -> closed | dead
        self._dropped_events = 0
        self._push_cmd = lib.bp_cmd  # indirection point, patchable in tests
        self._watchdog_task: asyncio.Task | None = None
        self.on_dead = None  # optional callable, invoked once on SIP-thread death

    # -- lifecycle -----------------------------------------------------------

    async def start(self, config_text: str = _DEFAULT_CONFIG) -> None:
        """Start the SIP thread and wait for it to become ready.

        Args:
            config_text: Configuration for the native stack, in its own
                ``key value`` line format.
        """
        # The parser rejects an empty buffer, so "no settings" still needs
        # something to parse.
        self._config_text = config_text or _DEFAULT_CONFIG

        with Runtime._class_lock:
            if Runtime._process_poisoned:
                raise RuntimeDead("a runtime already died in this process; start a new process")
            if Runtime._active is not None:
                raise BaresipError("another Runtime is active; only one may run at a time")
            if self._state != "new":
                raise BaresipError(f"cannot start a runtime in state {self._state!r}")
            Runtime._active = self

        try:
            err = lib.bp_init()
            if err:
                raise BaresipError(f"native init failed: {os.strerror(err)}")

            self._loop = asyncio.get_running_loop()
            set_sink(self._on_native_event)
            # The stack is confined to this directory: whatever a module
            # decides to read or write, it cannot reach the invoking user's
            # home directory.
            self._conf_dir = tempfile.mkdtemp(prefix="baresip-")
            # Started before the stack comes up, so its own startup lines
            # are captured too.
            lib.bp_log_start()
            self._log_thread = threading.Thread(
                target=self._drain_native_log, name="baresip-log", daemon=True
            )
            self._log_thread.start()
            # Daemon: CPython joins non-daemon threads BEFORE running atexit
            # hooks, so a non-daemon SIP thread would hang interpreter exit
            # forever whenever an application forgets close() — and the
            # atexit safety net below could never run.
            self._thread = threading.Thread(
                target=self._re_thread_main, name="baresip-re", daemon=True
            )
            self._thread.start()

            ok = await self._loop.run_in_executor(None, self._ready.wait, 5)
            if not ok or self._init_err:
                if not ok:
                    # The thread may still come up and enter the loop; make
                    # a best effort to stop it before abandoning it.
                    self._send_stop()
                    self._thread.join(2)
                detail = os.strerror(self._init_err) if self._init_err else "no ready signal"
                raise BaresipError(f"SIP thread failed to start: {detail}")

            self._state = "running"
            self._watchdog_task = self._loop.create_task(self._watchdog())
            atexit.register(self._forced_teardown)
            logger.info("runtime started")
        except BaseException:
            with Runtime._class_lock:
                Runtime._active = None
            set_sink(None)
            self._stop_native_log()
            self._remove_conf_dir()
            raise

    async def close(self) -> None:
        """Stop the SIP thread and release the runtime."""
        if self._state in ("closed", "dead"):
            return
        if self._state != "running":
            raise BaresipError(f"cannot close a runtime in state {self._state!r}")
        self._state = "closing"

        if self._watchdog_task:
            self._watchdog_task.cancel()

        await self._loop.run_in_executor(None, self._send_stop)
        assert self._thread is not None
        await self._loop.run_in_executor(None, self._thread.join, 5)

        if self._thread.is_alive():
            # A wedged SIP thread is death, not closure: poison the process,
            # keep the atexit hook (the thread is a daemon, so exit still
            # works), and never run bp_close under a live stack.
            self._state = "dead"
            with Runtime._class_lock:
                Runtime._active = None
                Runtime._process_poisoned = True
            logger.critical(
                "SIP thread did not exit within 5s of STOP; runtime is dead. "
                "Restart requires a new process."
            )
            self._fail_pending(RuntimeDead("SIP thread failed to stop"))
            set_sink(None)
            await self._loop.run_in_executor(None, self._stop_native_log)
            self._remove_conf_dir()
            raise RuntimeDead("SIP thread did not stop within 5s")

        atexit.unregister(self._forced_teardown)
        set_sink(None)
        self._fail_pending(BaresipError("runtime closed"))
        # After the SIP thread is gone: no more lines can arrive, and the
        # reader hands over what is left before it stops.
        await self._loop.run_in_executor(None, self._stop_native_log)
        lib.bp_close()
        self._remove_conf_dir()
        self._state = "closed"
        with Runtime._class_lock:
            Runtime._active = None
        logger.info("runtime closed")

    def _drain_native_log(self) -> None:
        """LOG THREAD. Collect captured lines until the capture is stopped.

        Blocks inside the native read, which releases the GIL, so this
        thread costs nothing while the stack is quiet."""
        rec = ffi.new("struct bp_log_rec *")
        while lib.bp_log_read(rec):
            try:
                if rec.dropped:
                    native_logger.warning(
                        "%d native log line(s) dropped: the reader could not keep up", rec.dropped
                    )
                target = sip_logger if rec.channel == lib.BP_LOG_CH_SIP else native_logger
                # Bytes off the network reach us here, so neither valid
                # UTF-8 nor the absence of NULs can be assumed.
                text = ffi.buffer(rec.msg, rec.len)[:].decode("utf-8", "replace")
                target.log(_TO_PYTHON_LEVEL.get(rec.level, logging.INFO), "%s", text)
            except Exception:
                # A logging handler that raises must not end log capture.
                with contextlib.suppress(Exception):
                    logger.exception("native log line could not be delivered")

    def _stop_native_log(self) -> None:
        lib.bp_log_stop()
        if self._log_thread is not None:
            self._log_thread.join(2)
            if self._log_thread.is_alive():
                logger.error("native log reader did not stop")
            self._log_thread = None

    def _remove_conf_dir(self) -> None:
        """Discard the stack's private directory, unless the stack is still
        standing — a wedged SIP thread may still be reading from it."""
        if self._conf_dir is None:
            return
        if not self._stack_down.is_set() and self._thread is not None and self._thread.is_alive():
            logger.warning("leaving %s behind: the SIP stack is still up", self._conf_dir)
            return
        shutil.rmtree(self._conf_dir, ignore_errors=True)
        self._conf_dir = None

    def _re_thread_main(self) -> None:
        self._init_err = lib.bp_loop_init(
            self._conf_dir.encode(), self._config_text.encode(), self._native_log_level
        )
        self._ready.set()
        if self._init_err:
            # A failed init unwinds itself completely; nothing to tear down.
            self._stack_down.set()
            return
        err = 0
        try:
            err = lib.bp_loop_run()
        finally:
            lib.bp_loop_done()
            self._stack_down.set()
            closing = self._state in ("closing", "closed")
            if err:
                logger.log(
                    logging.ERROR if closing else logging.CRITICAL,
                    "SIP loop exited with error: %s",
                    os.strerror(err),
                )
            if not closing:
                self._die_on_re_thread()

    def _die_on_re_thread(self) -> None:
        """RE THREAD. Mark death here, synchronously: the asyncio loop may
        already be stopped or closed, and death must never be silent."""
        self._state = "dead"
        with Runtime._class_lock:
            Runtime._active = None
            Runtime._process_poisoned = True
        logger.critical(
            "SIP thread exited unexpectedly; runtime is dead, %d command(s) failed. "
            "Restart requires a new process.",
            len(self._pending),
        )
        set_sink(None)
        # Signal only — this runs on the SIP thread, and the log reader
        # stops itself once it has handed over the last lines.
        lib.bp_log_stop()
        self._remove_conf_dir()
        self._call_threadsafe(self._finish_death)

    def _finish_death(self) -> None:
        """Loop thread: the loop-affine part of dying."""
        self._fail_pending(RuntimeDead("SIP thread exited unexpectedly"))
        if self.on_dead is not None:
            self.on_dead()

    def _fail_pending(self, exc: BaseException) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()

    def _forced_teardown(self) -> None:
        """atexit safety net: no event loop remains, so skip the graceful
        drain — detach the callback, stop the loop, join. Correct
        applications close() first and never reach this."""
        set_sink(None)
        if self._thread is not None and self._thread.is_alive():
            logger.critical("runtime never closed; forcing SIP thread shutdown at interpreter exit")
            self._send_stop()
            self._thread.join(2)
        self._stop_native_log()
        self._remove_conf_dir()

    def _send_stop(self) -> None:
        for _ in range(_STOP_RETRIES):
            err = lib.bp_cmd(lib.BP_CMD_STOP, 0, ffi.NULL)
            if err in (0, _errno.ESHUTDOWN):
                return
            if err != _errno.EAGAIN:
                logger.critical("could not queue STOP: %s", os.strerror(err))
                return
            time.sleep(_STOP_RETRY_DELAY)
        logger.critical("could not queue STOP: command queue stayed full")

    # -- commands ------------------------------------------------------------

    async def cmd(self, cmd_id: int, *, args: str | None = None, timeout: float | None = None):
        """Send a command and await its completion event.

        Args:
            cmd_id: One of the BP_CMD_* ids.
            args: Argument text for commands that take one.
            timeout: Seconds to wait, defaulting to the runtime's.

        Returns:
            (event id, payload bytes or None) from the completing event.

        Raises:
            CommandQueueFull: the queue rejected the command (not sent).
            CommandTimeout: no completion within the timeout.
            RuntimeDead: the SIP thread died.
        """
        if self._state == "dead":
            raise RuntimeDead("SIP thread exited unexpectedly")
        if self._state != "running":
            raise BaresipError(f"cannot send commands in state {self._state!r}")

        seq = self._next_seq()
        future = self._loop.create_future()
        self._pending[seq] = future

        err = self._push_cmd(
            cmd_id, seq, ffi.NULL if args is None else ffi.new("char[]", args.encode())
        )
        if err:
            self._pending.pop(seq, None)
            if err == _errno.EAGAIN:
                logger.error(
                    "command queue full; command rejected",
                    extra={"cmd": cmd_id, "seq": seq},
                )
                raise CommandQueueFull(f"command {cmd_id} rejected: queue full")
            logger.error("command failed: %s", os.strerror(err), extra={"cmd": cmd_id, "seq": seq})
            raise BaresipError(f"command {cmd_id} failed: {os.strerror(err)}")

        timeout = timeout if timeout is not None else self._command_timeout
        try:
            return await asyncio.wait_for(future, timeout)
        except TimeoutError:
            self._pending.pop(seq, None)
            logger.error(
                "no completion in %.1fs — SIP thread stalled (check watchdog) or command lost",
                timeout,
                extra={"cmd": cmd_id, "seq": seq},
            )
            raise CommandTimeout(f"command {cmd_id} got no completion in {timeout}s") from None

    async def set_native_log_level(self, level: str) -> None:
        """Change how much the native stack logs, while it runs.

        Args:
            level: "debug", "info", "warning" or "error".

        Raises:
            ValueError: level is not one of the four names.
        """
        if level not in LOG_LEVELS:
            raise ValueError(f"level must be one of {sorted(LOG_LEVELS)}, got {level!r}")
        await self.cmd(lib.BP_CMD_SET_LOG_LEVEL, args=str(LOG_LEVELS[level]))

    async def set_sip_trace(self, enabled: bool) -> None:
        """Log every SIP message sent and received, under
        ``baresip.native.sip`` at DEBUG level."""
        await self.cmd(lib.BP_CMD_SET_SIP_TRACE, args="1" if enabled else "0")

    def _next_seq(self) -> int:
        self._seq = (self._seq + 1) & 0xFFFFFFFF or 1
        while self._seq in self._pending:
            self._seq = (self._seq + 1) & 0xFFFFFFFF or 1
        return self._seq

    # -- events --------------------------------------------------------------

    def _on_native_event(self, ev: int, handle: int, payload) -> None:
        """RE THREAD. Hop to the asyncio loop; never block, never raise."""
        self._call_threadsafe(self._dispatch, ev, handle, payload)

    def _call_threadsafe(self, fn, *args) -> None:
        loop = self._loop
        try:
            if loop is None or loop.is_closed():
                raise RuntimeError("no loop")
            loop.call_soon_threadsafe(fn, *args)
        except RuntimeError:
            self._dropped_events += 1
            if self._dropped_events == 1 or self._dropped_events % 100 == 0:
                logger.warning(
                    "event loop unavailable; %d event(s) dropped so far",
                    self._dropped_events,
                )

    def _dispatch(self, ev: int, handle: int, payload) -> None:
        future = self._pending.pop(handle, None)
        if future is not None and not future.done():
            future.set_result((ev, payload))
            return
        logger.debug("unmatched event", extra={"event": ev, "seq": handle})

    # -- watchdog --------------------------------------------------------------

    async def _watchdog(self) -> None:
        while True:
            await asyncio.sleep(self._watchdog_interval)
            try:
                await self.cmd(lib.BP_CMD_PING, timeout=min(2.0, self._watchdog_interval / 2))
            except CommandTimeout:
                logger.critical(
                    "watchdog PING unanswered: SIP thread unresponsive; SIP timers stalled"
                )
            except (RuntimeDead, BaresipError):
                return
