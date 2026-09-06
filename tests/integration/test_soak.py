#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""Long-running soaks: drift over minutes, leaks over a hundred calls.

Deliberately outside the ordinary bench run (`pytest -m soak` to run
them): a pacing bug shows up as drift, and drift needs minutes, not
seconds — a transmit clock built on relative sleeps loses about a
millisecond a minute, and the buffer depths are where that accumulates.
A per-call leak needs the opposite shape, many short calls: each one
allocates rings, a handle slot, and stack objects, and anything not
freed on close compounds into measurable RSS growth.
"""

import asyncio
import gc
import json
import math
import os
import struct
import subprocess

import pytest

native = pytest.importorskip("baresip._native")

from baresip import Account, Config
from baresip.call import CallState
from baresip.runtime import Runtime
from baresip.ua import UserAgent

pytestmark = [pytest.mark.bench, pytest.mark.soak]

DOMAIN = f"127.0.0.1:{os.environ.get('BENCH_SIP_PORT', '15060')}"
RAW_CONF = Config(net_interface="127.0.0.1", max_concurrent_calls=None)

SOAK_SECONDS = 600


async def test_ten_minute_echo_depth_stays_bounded():
    runtime = Runtime()
    await runtime.start(RAW_CONF)
    try:
        ua = await UserAgent.create(
            runtime, Account(user="1003", password="bench1234", domain=DOMAIN)
        )
        await ua.register()
        call = await ua.dial(f"sip:9196@{DOMAIN}")
        await call.wait_established()

        # Prime the loop with a second of tone, then echo whatever comes
        # back for the full duration, sampling the buffer depths 1/s.
        deadline = asyncio.get_running_loop().time() + 5
        info = None
        while asyncio.get_running_loop().time() < deadline:
            info = call.audio.info()
            if info.tx_ready and info.rx_ready:
                break
            await asyncio.sleep(0.05)
        assert info and info.tx_ready and info.rx_ready
        rate = info.tx_sample_rate
        call.audio.write(
            b"".join(
                struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / rate)))
                for i in range(rate)
            )
        )

        depths = []
        loop = asyncio.get_running_loop()
        start = loop.time()
        next_sample = start + 1
        while loop.time() - start < SOAK_SECONDS:
            pcm = call.audio.read(4096)
            if pcm:
                call.audio.write(pcm)
            else:
                await asyncio.sleep(0.01)
            if loop.time() >= next_sample:
                next_sample += 1
                info = call.audio.info()
                depths.append((info.tx_buffered, info.rx_buffered))

        assert call.state is CallState.ESTABLISHED, "the call must survive the soak"
        assert len(depths) >= SOAK_SECONDS * 9 // 10

        # Bounded means bounded the whole way: with the reader keeping
        # up, neither buffer should ever approach half its capacity —
        # a drifting transmit clock would show up here as a climb.
        max_tx = max(d[0] for d in depths)
        max_rx = max(d[1] for d in depths)
        assert max_tx < info.tx_capacity // 2, f"tx depth climbed to {max_tx}"
        assert max_rx < info.rx_capacity // 2, f"rx depth climbed to {max_rx}"

        await call.hangup()
        await ua.unregister()
    finally:
        await runtime.close()


WARMUP_CALLS = 10
MEASURED_CALLS = 100
RSS_GROWTH_LIMIT = 1.05


def rss_kb() -> int:
    """Current resident set size in KiB, without a third-party dep."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except FileNotFoundError:
        pass  # not Linux; ps below works on macOS
    out = subprocess.run(
        ["ps", "-o", "rss=", "-p", str(os.getpid())], capture_output=True, text=True, check=True
    )
    return int(out.stdout.strip())


async def one_call(ua) -> None:
    """Dial the echo extension, pass a little audio, hang up, wait closed."""
    call = await ua.dial(f"sip:9196@{DOMAIN}")
    await call.wait_established()
    loop = asyncio.get_running_loop()

    deadline = loop.time() + 5
    while loop.time() < deadline:
        info = call.audio.info()
        if info.tx_ready and info.rx_ready:
            break
        await asyncio.sleep(0.02)
    assert info.tx_ready and info.rx_ready
    rate = info.tx_sample_rate
    call.audio.write(
        b"".join(
            struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / rate)))
            for i in range(rate // 5)
        )
    )
    deadline = loop.time() + 0.3
    while loop.time() < deadline:
        call.audio.read(4096)
        await asyncio.sleep(0.02)

    await call.hangup()
    deadline = loop.time() + 5
    while call.state is not CallState.CLOSED and loop.time() < deadline:
        await asyncio.sleep(0.02)
    assert call.state is CallState.CLOSED


async def test_hundred_calls_leave_memory_and_handles_flat():
    """One hundred short calls: RSS stays flat, the handle table empties.

    Warmup calls first, so one-time pool growth (allocator arenas, jitter
    buffers, TLS sessions) doesn't count against the budget; after that,
    per-call state is supposed to be fully reclaimed on close, and five
    percent of a settled process is far more than a hundred leaked ring
    pairs would need to show up.
    """
    runtime = Runtime()
    await runtime.start(RAW_CONF)
    try:
        ua = await UserAgent.create(
            runtime, Account(user="1003", password="bench1234", domain=DOMAIN)
        )
        await ua.register()

        for _ in range(WARMUP_CALLS):
            await one_call(ua)
        gc.collect()
        settled = rss_kb()

        for _ in range(MEASURED_CALLS):
            await one_call(ua)
        gc.collect()
        grown = rss_kb()

        # Every call slot must be back: the count command tallies live
        # handle-table slots C-side, and calls are freed there on close,
        # so any remainder is a real leak.
        ev, payload = await runtime.cmd(native.lib.BP_CMD_TEST_HANDLE_COUNT)
        assert ev == native.lib.BP_EV_DONE
        counts = json.loads(payload)
        assert counts["call"] == 0 and counts["test"] == 0, counts
        assert counts["ua"] == 1

        assert grown <= settled * RSS_GROWTH_LIMIT, (
            f"RSS grew {settled} -> {grown} KiB over {MEASURED_CALLS} calls"
        )

        await ua.unregister()
    finally:
        await runtime.close()
