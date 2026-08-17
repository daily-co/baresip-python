#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""Shutdown-path tests that must run in a subprocess: interpreter-exit
behavior and death handling when no event loop remains cannot be observed
from inside the test process itself."""

import subprocess
import sys

import pytest

pytest.importorskip("baresip._native")

FORGOTTEN_CLOSE = """
import asyncio
from baresip._native import lib
from baresip.runtime import Runtime

async def main():
    runtime = Runtime()
    await runtime.start()
    ev, _ = await runtime.cmd(lib.BP_CMD_PING)
    assert ev == lib.BP_EV_PONG
    # deliberately no close()

asyncio.run(main())
print("MAIN-DONE", flush=True)
"""


def test_exit_without_close_terminates():
    """An application that forgets close() must still exit: the SIP thread
    is a daemon and the atexit hook force-stops it. A hang here means the
    interpreter is blocked joining a non-daemon thread before atexit runs."""
    proc = subprocess.run(
        [sys.executable, "-c", FORGOTTEN_CLOSE],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "MAIN-DONE" in proc.stdout


DEATH_WITH_LOOP_GONE = """
import asyncio, logging
logging.basicConfig(level=logging.INFO)

from baresip._native import ffi, lib
from baresip.runtime import Runtime

runtime = Runtime()

async def main():
    await runtime.start()

asyncio.run(main())  # returns with the loop closed; the SIP thread lives on

# Kill the loop behind the runtime's back, with no asyncio loop anywhere.
assert lib.bp_cmd(lib.BP_CMD_STOP, 0, ffi.NULL) == 0
runtime._thread.join(5)
assert not runtime._thread.is_alive()
assert Runtime._process_poisoned, "death must poison the process"
print("DEATH-MARKED", flush=True)
"""


def test_death_with_loop_gone_is_loud():
    """SIP-thread death after the asyncio loop is gone must still be marked
    and reported CRITICAL — death is never silent."""
    proc = subprocess.run(
        [sys.executable, "-c", DEATH_WITH_LOOP_GONE],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "DEATH-MARKED" in proc.stdout
    assert "exited unexpectedly" in proc.stderr, "the CRITICAL record must reach the logs"
