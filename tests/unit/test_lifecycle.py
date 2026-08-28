#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""Lifecycle tests for the native shim: re-thread start/stop, command
marshaling, callback threading, and the command-queue failure contract.

These tests drive the raw C surface the way the Python runtime will: the re
loop on a dedicated thread, commands pushed from other threads via bp_cmd,
events observed through the bp_event_h callback. Requires the compiled
extension (``make native ext``); skipped otherwise.
"""

import errno
import os
import shutil
import tempfile
import threading
import time

import pytest

from baresip import CommandQueueFull

native = pytest.importorskip("baresip._native")
ffi = native.ffi
lib = native.lib

from baresip import _events  # import requires the built extension, hence after the skip

# Every event the sink receives: (event id, handle, thread ident).
EVENTS: list[tuple[int, int, int]] = []

CONFIG = b"# baresip-python tests\n"
CONF_DIR = b""  # a private directory for the stack, set up by the fixture


def _collect(ev, handle, payload):
    EVENTS.append((ev, handle, threading.get_ident()))


def loop_init():
    """bp_loop_init with the arguments every test here shares."""
    return lib.bp_loop_init(CONF_DIR, CONFIG, lib.BP_LOG_ERROR)


@pytest.fixture(scope="module", autouse=True)
def native_stack():
    global CONF_DIR
    conf_dir = tempfile.mkdtemp(prefix="baresip-test-")
    CONF_DIR = conf_dir.encode()
    assert lib.bp_init() == 0
    previous = _events.set_sink(_collect)
    yield
    _events.set_sink(previous)
    lib.bp_close()
    shutil.rmtree(conf_dir, ignore_errors=True)


def send(cmd, handle=0):
    """Push a command, mapping the C error contract to Python exceptions."""
    err = lib.bp_cmd(cmd, handle, ffi.NULL)
    if err == errno.EAGAIN:
        raise CommandQueueFull("command queue full; command was not sent")
    if err:
        raise OSError(err, os.strerror(err))


def pongs():
    return [e for e in EVENTS if e[0] == lib.BP_EV_PONG]


def wait_until(cond, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.001)
    return False


class ReLoop:
    """The re thread as the Python runtime will run it: init → run → done.

    With ``hold=True`` the thread initializes the loop (so bp_cmd accepts
    pushes) but waits before entering re_main — nothing drains the queue
    until release() is called. That is what makes the queue-full test
    deterministic.
    """

    def __init__(self, hold=False):
        self._hold = threading.Event()
        if not hold:
            self._hold.set()
        self._ready = threading.Event()
        self._init_err = None
        self.ident = None
        self._thread = threading.Thread(target=self._main, name="baresip-re")

    def _main(self):
        self.ident = threading.get_ident()
        self._init_err = loop_init()
        self._ready.set()
        if self._init_err:
            return
        self._hold.wait()
        lib.bp_loop_run()
        lib.bp_loop_done()

    def start(self):
        self._thread.start()
        assert self._ready.wait(timeout=5), "re thread never became ready"
        assert self._init_err == 0
        return self

    def release(self):
        self._hold.set()

    def stop(self):
        send(lib.BP_CMD_STOP)
        self._thread.join(timeout=5)
        assert not self._thread.is_alive(), "re thread failed to shut down"


def test_100_start_stop_cycles():
    """The M0 gate: 100 clean lifecycles, 5 PING/PONG round-trips each,
    every callback on the re thread."""
    main_ident = threading.get_ident()
    pings = 5

    for cycle in range(100):
        EVENTS.clear()
        loop = ReLoop().start()

        for i in range(pings):
            send(lib.BP_CMD_PING, handle=i)
        assert wait_until(lambda: len(pongs()) == pings), f"cycle {cycle}: {EVENTS}"

        loop.stop()

        assert sorted(p[1] for p in pongs()) == list(range(pings))
        callback_threads = {e[2] for e in EVENTS}
        assert callback_threads == {loop.ident}, "callback ran off the re thread"
        assert loop.ident != main_ident


def test_queue_full_raises_then_recovers():
    """The bp_cmd failure contract: a full pipe rejects loudly with
    CommandQueueFull, every accepted command survives, and the loop still
    answers once drained."""
    EVENTS.clear()
    loop = ReLoop(hold=True).start()  # queue open, nothing draining

    accepted = []
    full_hits = []

    def flood():
        count = 0
        for _ in range(100_000):
            try:
                send(lib.BP_CMD_PING, handle=1)
            except CommandQueueFull:
                full_hits.append(1)
                break
            count += 1
        accepted.append(count)

    threads = [threading.Thread(target=flood) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
        assert not t.is_alive()

    assert len(full_hits) == 4, "every flooding thread must see the full queue"
    total_accepted = sum(accepted)
    assert total_accepted > 0

    # Drain: every accepted command — and none of the rejected ones —
    # produces exactly one PONG.
    loop.release()
    assert wait_until(lambda: len(pongs()) == total_accepted, timeout=30)
    time.sleep(0.05)  # would catch stragglers beyond the expected count
    assert len(pongs()) == total_accepted

    # The loop is still healthy: a fresh PING gets answered.
    send(lib.BP_CMD_PING, handle=4242)
    assert wait_until(lambda: any(p[1] == 4242 for p in pongs()))

    loop.stop()
    assert {e[2] for e in EVENTS} == {loop.ident}


def test_cmd_rejected_when_loop_not_running():
    """Pushes are gated: after shutdown, bp_cmd refuses with ESHUTDOWN
    instead of writing into a freed queue."""
    EVENTS.clear()
    loop = ReLoop().start()
    send(lib.BP_CMD_PING, handle=7)
    assert wait_until(lambda: len(pongs()) == 1)
    loop.stop()

    assert lib.bp_cmd(lib.BP_CMD_PING, 8, ffi.NULL) == errno.ESHUTDOWN
    with pytest.raises(OSError):
        send(lib.BP_CMD_PING, handle=8)
    assert len(pongs()) == 1


def test_run_without_init_refuses():
    """bp_loop_run without bp_loop_init is refused, loudly."""
    assert lib.bp_loop_run() == errno.EINVAL


def test_done_without_init_refuses(capfd):
    """bp_loop_done without bp_loop_init is refused, loudly — running the
    teardown over a stack that never came up must not look like success."""
    assert lib.bp_loop_done() == errno.EINVAL
    assert "nothing to tear down" in capfd.readouterr().err

    # The refusal must leave the process able to run a real cycle.
    EVENTS.clear()
    loop = ReLoop().start()
    send(lib.BP_CMD_PING, handle=1)
    assert wait_until(lambda: len(pongs()) == 1)
    loop.stop()


def test_done_while_running_refuses(capfd):
    """bp_loop_done while the loop runs is the use-after-free case: it must
    refuse (EBUSY), say so on stderr, and leave the loop fully working."""
    EVENTS.clear()
    loop = ReLoop().start()

    # A round-trip first: start() only proves init finished, and done
    # between init and run is a *valid* teardown — the refusal under
    # test requires the thread to actually be inside the loop.
    send(lib.BP_CMD_PING, handle=1)
    assert wait_until(lambda: len(pongs()) == 1), "loop never started processing"

    assert lib.bp_loop_done() == errno.EBUSY
    assert "refusing to free" in capfd.readouterr().err

    send(lib.BP_CMD_PING, handle=2)
    assert wait_until(lambda: len(pongs()) == 2), "loop must survive the refused call"
    loop.stop()


def test_run_while_running_refuses():
    """A second bp_loop_run while the loop is live is refused."""
    EVENTS.clear()
    loop = ReLoop().start()
    # Same start-vs-refusal race as the done test: prove the loop is
    # actually running before expecting EALREADY.
    send(lib.BP_CMD_PING, handle=1)
    assert wait_until(lambda: len(pongs()) == 1), "loop never started processing"
    assert lib.bp_loop_run() == errno.EALREADY
    loop.stop()


def test_double_init_refuses():
    """A second bp_loop_init without bp_loop_done is refused on the re
    thread itself."""
    results = []

    def main():
        results.append(loop_init())
        results.append(loop_init())  # misuse: no bp_loop_done between
        lib.bp_loop_run()
        results.append(lib.bp_loop_done())

    thread = threading.Thread(target=main, name="baresip-re")
    thread.start()
    assert wait_until(lambda: len(results) >= 2)
    send(lib.BP_CMD_STOP)
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert results == [0, errno.EALREADY, 0]
