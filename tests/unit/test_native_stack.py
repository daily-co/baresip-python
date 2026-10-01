#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""Bringing the SIP stack up and down: the configuration it is given, what
it is allowed to touch, what it is allowed to print, and what it says."""

import asyncio
import contextlib
import contextvars
import errno
import logging
import os
import subprocess
import sys
import threading

import pytest

from baresip import BaresipError, Config

native = pytest.importorskip("baresip._native")
lib = native.lib

from baresip.runtime import Runtime  # imports the extension, hence after the skip

# An address the stack cannot listen on: enough to fail ua_init, which is
# the last step of bringing it up.
UNUSABLE_CONFIG = Config(extra_config_text="sip_listen 999.999.999.999:5060")

# Stands in for the request-scoped values an application sets around
# start() — a session id, a correlation id, a loguru contextualize field.
SESSION = contextvars.ContextVar("session")


@contextlib.contextmanager
def native_records():
    """Collect everything logged under ``baresip.native`` while running.

    Restores the logger afterwards: its level is process-wide, and leaving
    it turned up would follow every later test."""
    native_log = logging.getLogger("baresip.native")
    captured: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = captured.append
    previous = native_log.level
    native_log.addHandler(handler)
    native_log.setLevel(logging.DEBUG)
    try:
        yield captured
    finally:
        native_log.removeHandler(handler)
        native_log.setLevel(previous)


async def test_native_log_is_captured():
    """The stack talks during startup; all of it must arrive as log
    records, at the severity it used."""
    runtime = Runtime(native_log_level="debug")
    with native_records() as captured:
        await runtime.start()
        await runtime.close()

    assert captured, "the stack logs while starting; none of it was captured"
    assert {r.levelno for r in captured} <= {
        logging.DEBUG,
        logging.INFO,
        logging.WARNING,
        logging.ERROR,
    }
    assert any(r.levelno == logging.DEBUG for r in captured), "debug lines were asked for"


async def test_log_level_is_applied_from_the_start():
    """Filtering happens natively, so lines below the level never cross
    into Python at all — including the ones startup would produce."""
    runtime = Runtime(native_log_level="error")
    with native_records() as captured:
        await runtime.start()
        await runtime.close()

    assert not [r for r in captured if r.levelno < logging.ERROR], (
        f"levels below the configured one leaked through: {[r.getMessage() for r in captured]}"
    )


async def test_native_log_records_carry_the_starting_context():
    """Records are emitted on the log thread, which has no context of its
    own; a handler must still see what was set around start()."""
    seen: list = []
    native_log = logging.getLogger("baresip.native")
    handler = logging.Handler()
    handler.emit = lambda record: seen.append(SESSION.get(None))
    previous = native_log.level
    native_log.addHandler(handler)
    native_log.setLevel(logging.DEBUG)

    SESSION.set("session-7")
    try:
        runtime = Runtime(native_log_level="debug")
        await runtime.start()
        await runtime.close()
    finally:
        native_log.removeHandler(handler)
        native_log.setLevel(previous)

    assert seen, "the stack logs while starting; none of it was captured"
    assert set(seen) == {"session-7"}


async def test_event_callbacks_carry_the_starting_context():
    """Events reach the loop from the SIP thread, which has no context of
    its own; the callback must run in the one set around start()."""
    SESSION.set("session-8")
    runtime = Runtime()
    await runtime.start()
    try:
        seen = asyncio.get_running_loop().create_future()
        # What the SIP thread does with every event it hands over.
        threading.Thread(
            target=runtime._call_threadsafe, args=(lambda: seen.set_result(SESSION.get(None)),)
        ).start()
        assert await asyncio.wait_for(seen, 5) == "session-8"
    finally:
        await runtime.close()


async def test_unusable_config_is_rejected_and_leaves_the_process_usable():
    """A failure partway through startup must leave nothing behind: not the
    parts of the stack that did come up, and not the setting that caused
    it — configuration is process-wide, so a later run that says nothing
    about a key must still get the default rather than this run's value."""
    failed = Runtime()
    with pytest.raises(BaresipError):
        await failed.start(UNUSABLE_CONFIG)
    assert failed._conf_dir is None, "the private directory outlived the failed start"

    runtime = Runtime()
    await runtime.start()  # says nothing about sip_listen
    try:
        ev, _ = await runtime.cmd(lib.BP_CMD_PING)
        assert ev == lib.BP_EV_PONG
    finally:
        await runtime.close()


async def test_private_directory_is_removed_on_close():
    runtime = Runtime()
    await runtime.start()
    conf_dir = runtime._conf_dir
    assert conf_dir and os.path.isdir(conf_dir)
    await runtime.close()
    assert not os.path.exists(conf_dir)


async def test_sip_trace_toggles():
    """Both directions of the switch are accepted while running.

    What it produces — the SIP messages themselves — needs a peer to talk
    to, so that is asserted where there is one to talk to."""
    runtime = Runtime()
    await runtime.start()
    try:
        await runtime.set_sip_trace(True)
        await runtime.set_sip_trace(False)
    finally:
        await runtime.close()


def test_unknown_log_level_is_rejected():
    with pytest.raises(ValueError):
        Runtime(native_log_level="verbose")


async def test_start_with_config():
    """A Config is declarative: its log level wins over the constructor's
    and is active from the stack's first line, and its sip_trace switch is
    applied once the stack is up."""
    runtime = Runtime()  # constructor default: warning
    with native_records() as captured:
        await runtime.start(Config(native_log_level="debug", sip_trace=True))
        try:
            ev, _ = await runtime.cmd(lib.BP_CMD_PING)
            assert ev == lib.BP_EV_PONG
        finally:
            await runtime.close()

    assert any(r.levelno == logging.DEBUG for r in captured), (
        "the Config's level did not reach the stack in time for startup"
    )


async def test_failure_after_ready_stops_the_thread():
    """A start that fails after the stack came up must take the stack back
    down and leave the process able to run a fresh runtime — it is a failed
    start, not a death."""
    runtime = Runtime()
    real_push = runtime._push_cmd

    def deny_sip_trace(cmd, seq, args):
        if cmd == lib.BP_CMD_SET_SIP_TRACE:
            return errno.EIO
        return real_push(cmd, seq, args)

    runtime._push_cmd = deny_sip_trace
    with pytest.raises(BaresipError):
        await runtime.start(Config(sip_trace=True))

    assert runtime._thread is not None and not runtime._thread.is_alive()
    assert runtime._conf_dir is None, "the private directory outlived the failed start"

    fresh = Runtime()
    await fresh.start()
    await fresh.close()


SILENT_RUN = """
import asyncio, pathlib, sys
from baresip.runtime import Runtime

async def main():
    runtime = Runtime(native_log_level="debug")
    await runtime.start()
    await runtime.set_sip_trace(True)
    await runtime.close()

asyncio.run(main())
pathlib.Path(sys.argv[1]).write_text("done")   # not stdout: that is under test
"""


def test_stack_keeps_to_itself(tmp_path):
    """Two promises a library has to keep, observed on one run: the host
    process owns stdout and stderr, and the user's home directory is not
    ours to write in."""
    marker = tmp_path / "marker"
    home = tmp_path / "home"
    home.mkdir()

    proc = subprocess.run(
        [sys.executable, "-c", SILENT_RUN, str(marker)],
        env={**os.environ, "HOME": str(home)},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert marker.read_text() == "done"
    assert proc.stdout == "", f"the stack wrote to stdout: {proc.stdout!r}"
    assert proc.stderr == "", f"the stack wrote to stderr: {proc.stderr!r}"
    assert list(home.iterdir()) == [], "the stack wrote into the user's home directory"
