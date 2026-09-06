#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""T8 — shutdown roulette: teardown from every state, one process each.

Each iteration is a fresh subprocess so the interpreter-exit path runs
for real: the child brings a runtime to a seeded-random state (just
started, command in flight, mid-call, audio streaming from a foreign
thread) and then tears down — an explicit ``close()`` most of the time,
a bare interpreter exit (the atexit hook) the rest. The child asserts
its own hygiene after an explicit close (runtime threads gone, file
descriptors back to baseline); the parent asserts what only it can see:
exit code 0, bounded wall time, never a hang, never a crash.
"""

import os
import subprocess
import sys
import time

import pytest
from torture_helpers import Bounds, scaled

pytest.importorskip("baresip._native")

pytestmark = pytest.mark.torture

TOTAL = 500
CHECK_EVERY = 100
CHILD_TIMEOUT = 30

CHILD = """
import asyncio, os, random, threading, time

from baresip import Account, AudioNotActive, AudioRestarted, Config, Runtime, UserAgent
from baresip._native import lib

rng = random.Random(int(os.environ["T8_SEED"]))
OWN = "127.0.0.1:5083"


def fd_count():
    try:
        return len(os.listdir("/proc/self/fd"))
    except FileNotFoundError:
        return len(os.listdir("/dev/fd"))


fd0 = fd_count()
explicit_close = rng.random() < 0.7


def swallow(fut):
    if not fut.cancelled():
        fut.exception()


async def main():
    runtime = Runtime()
    await runtime.start(
        Config(
            net_interface="127.0.0.1",
            max_concurrent_calls=None,
            extra_config_text=f"sip_listen {OWN}\\n",
        )
    )

    roll = rng.random()
    if roll < 0.25:
        pass  # tear down straight after start
    elif roll < 0.5:
        fut = asyncio.ensure_future(runtime.cmd(lib.BP_CMD_PING))
        fut.add_done_callback(swallow)  # in flight at teardown
    else:
        ua_a = await UserAgent.create(
            runtime, Account(user="alice", password="", domain=OWN, reg_interval=0)
        )
        ua_b = await UserAgent.create(
            runtime, Account(user="bob", password="", domain=OWN, reg_interval=0)
        )
        incoming = asyncio.Queue()
        ua_b.on_incoming(incoming.put_nowait)
        call = await ua_a.dial(f"sip:bob@{OWN}")
        b_call = await asyncio.wait_for(incoming.get(), 5)
        await b_call.answer()
        await call.wait_established()
        if roll < 0.75:
            await asyncio.sleep(rng.random() * 0.2)  # tear down mid-call
        else:
            # Audio streaming from a foreign thread right through the
            # teardown; the writer must end via the typed audio errors.
            def writer():
                pcm = b"\\x00" * 320
                while True:
                    try:
                        call.audio.write(pcm)
                    except (AudioNotActive, AudioRestarted):
                        return
                    time.sleep(0.005)

            threading.Thread(target=writer, name="t8-writer", daemon=True).start()
            await asyncio.sleep(rng.random() * 0.3)

    if explicit_close:
        await asyncio.wait_for(runtime.close(), 10)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            lingering = [t.name for t in threading.enumerate() if t.name.startswith("baresip")]
            if not lingering:
                break
            time.sleep(0.05)
        assert not lingering, f"runtime threads survived close: {lingering}"
        fds = fd_count()
        assert fds <= fd0 + 8, f"fds grew {fd0} -> {fds} across the runtime's life"
    # else: no close at all — the atexit hook is the teardown under test.


asyncio.run(main())
print("OK", flush=True)
"""


def test_t8_shutdown_roulette(rng):
    bounds = Bounds()
    for i in range(scaled(TOTAL)):
        seed = rng.randrange(2**32)
        env = {**os.environ, "T8_SEED": str(seed)}
        if "ASAN_OPTIONS" in env:
            # The children exercise abrupt teardown, where end-of-process
            # leak reports are not a signal: the no-close variant leaves
            # memory live at exit by design, and close() during a live
            # call currently leaks that call's object graph — a real,
            # tracked bug (issue #2) that stays quarantined here until
            # the fix lands. Leak detection for the close paths runs
            # in-process: T1/T3/T4 and the unit suite under the ASan
            # lane cover them at full LSan strictness.
            env["ASAN_OPTIONS"] += ":detect_leaks=0"
        started = time.monotonic()
        try:
            proc = subprocess.run(
                [sys.executable, "-c", CHILD],
                env=env,
                capture_output=True,
                text=True,
                timeout=CHILD_TIMEOUT,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            pytest.fail(
                f"iteration {i + 1} seed {seed}: shutdown hung past {CHILD_TIMEOUT}s: {exc}"
            )
        elapsed = time.monotonic() - started
        assert proc.returncode == 0, (
            f"iteration {i + 1} seed {seed}: exit {proc.returncode}\n"
            f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
        )
        assert "OK" in proc.stdout, f"iteration {i + 1} seed {seed}: child never reached teardown"
        assert elapsed < 20, f"iteration {i + 1} seed {seed}: took {elapsed:.1f}s"

        if (i + 1) % CHECK_EVERY == 0:
            bounds.check(f"iteration {i + 1}")
