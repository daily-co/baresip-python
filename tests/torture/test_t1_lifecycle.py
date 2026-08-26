#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""T1 — lifecycle churn: thousands of runtime start/stop cycles.

Hunts init/teardown races, file-descriptor leaks, and join hangs. Some
cycles leave a command in flight at close, steered by the seed — the
invariant is that close always completes in bounded time and the
command resolves to a result or a typed error, never a hang.
"""

import asyncio
import os
import threading

import pytest
from torture_helpers import Bounds, scaled

from baresip import BaresipError, Runtime, RuntimeDead

native = pytest.importorskip("baresip._native")
lib = native.lib

pytestmark = pytest.mark.torture

TOTAL = 5000
CHECK_EVERY = 100


async def test_t1_lifecycle_churn(rng):
    # Warm up before the baseline: the first cycles pay one-time costs
    # (module loads, TLS contexts, allocator arenas) that would read as
    # growth. The soak suite established the idiom.
    for _ in range(5):
        runtime = Runtime()
        await runtime.start()
        await runtime.close()
    bounds = Bounds()
    threads0 = threading.active_count()

    for i in range(scaled(TOTAL)):
        runtime = Runtime()
        await runtime.start()

        pending = None
        roll = rng.random()
        if roll < 0.33:
            # A command completed before close.
            await runtime.cmd(lib.BP_CMD_PING)
        elif roll < 0.66:
            # A command in flight at close: fired, never awaited before
            # the teardown starts.
            pending = asyncio.ensure_future(runtime.cmd(lib.BP_CMD_PING))

        await asyncio.wait_for(runtime.close(), 10)

        if pending is not None:
            # Success or a typed error — never a hang, never a crash.
            try:
                await asyncio.wait_for(pending, 5)
            except (BaresipError, RuntimeDead):
                pass

        if (i + 1) % CHECK_EVERY == 0:
            # Cycling baresip_init/close retains ~2 KB/cycle upstream
            # (reachable growth, measured and bisected — issue #1); the
            # budget is double that, so a regression still fails.
            bounds.check(f"cycle {i + 1}", allowance_kb=(i + 1) * 4)
            # Two thread facts, separately checked. Runtime-owned
            # threads (named "baresip*") must be gone — a just-closed
            # one gets a moment to wind down first. The overall count
            # only has to stay bounded: asyncio's default executor
            # keeps a small worker pool alive by design, but a
            # per-cycle leak would blow past the cap within one
            # check window.
            deadline = asyncio.get_running_loop().time() + 2
            while asyncio.get_running_loop().time() < deadline:
                lingering = [t.name for t in threading.enumerate() if t.name.startswith("baresip")]
                if not lingering:
                    break
                await asyncio.sleep(0.05)
            assert not lingering, f"cycle {i + 1}: runtime threads leaked: {lingering}"
            # asyncio's default executor grows toward its own cap under
            # rapid sequential submits (a worker just finishing is not
            # yet "idle" when the next submit lands) and stops there; a
            # real per-cycle thread leak sails past it within one
            # check window.
            pool_cap = min(32, (os.cpu_count() or 1) + 4)
            assert threading.active_count() <= threads0 + pool_cap + 2, (
                f"cycle {i + 1}: unbounded threads ({threads0} -> {threading.active_count()})"
            )
