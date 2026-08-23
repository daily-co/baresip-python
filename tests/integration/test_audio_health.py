#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""Audio health against the FreeSWITCH bench: force each failure mode,
expect exactly the warning it deserves — and no other.

The echo extension returns whatever we transmit, silence included, so
received audio flows at wire rate for as long as the call stands. That
makes the receive-side faults easy to force: just stop reading.
Run with: pytest -m bench tests/integration
"""

import asyncio
import itertools
import math
import os
import struct

import pytest

native = pytest.importorskip("baresip._native")

from baresip import Account, AudioNotActive
from baresip.runtime import Runtime
from baresip.ua import UserAgent

pytestmark = pytest.mark.bench

DOMAIN = f"127.0.0.1:{os.environ.get('BENCH_SIP_PORT', '15060')}"
RAW_CONF = "net_interface 127.0.0.1\naudio_source aumem,default\naudio_player aumem,default\n"


def sine(rate: int, seconds: float, freq: int = 440, amp: int = 8000) -> bytes:
    n = int(rate * seconds)
    return b"".join(
        struct.pack("<h", int(amp * math.sin(2 * math.pi * freq * i / rate))) for i in range(n)
    )


@pytest.fixture
async def bench_ua():
    runtime = Runtime()
    await runtime.start(RAW_CONF)
    ua = await UserAgent.create(runtime, Account(user="1003", password="bench1234", domain=DOMAIN))
    await ua.register()
    try:
        yield ua
    finally:
        await runtime.close()


async def dial_ready(ua):
    """Dial the echo service and wait until both audio directions are up."""
    call = await ua.dial(f"sip:9196@{DOMAIN}")
    await call.wait_established()
    deadline = asyncio.get_running_loop().time() + 5
    while asyncio.get_running_loop().time() < deadline:
        try:
            info = call.audio.info()
            if info.tx_ready and info.rx_ready:
                return call, info
        except AudioNotActive:
            pass
        await asyncio.sleep(0.05)
    raise AssertionError("audio did not come up within 5 s")


def collect_warnings(call) -> list:
    """Arm on_audio_warning; returns a list of (time, AudioWarning)."""
    loop = asyncio.get_running_loop()
    got: list = []
    call.on_audio_warning(lambda w: got.append((loop.time(), w)))
    return got


async def test_starved_transmit_warns_once_per_second(bench_ua):
    call, info = await dial_ready(bench_ua)
    warnings = collect_warnings(call)

    # Feed audio steadily but far below the wire rate, in odd-sized
    # chunks: the pacing thread keeps catching partial frames — the
    # mid-stream starvation the transmit warning exists for.
    rate = info.tx_sample_rate
    for _ in range(35):
        call.audio.write(sine(rate, 0.025))  # 25 ms of audio per 100 ms
        await asyncio.sleep(0.1)

    tx = [(t, w) for t, w in warnings if w.direction == "tx"]
    assert tx, "3.5 s of starved transmit produced no warning"
    assert "feeding" in tx[0][1].message
    # Rate-limited: one per sampling window, so ~3.5 s allows at most 4.
    assert len(tx) <= 4
    for (t1, _), (t2, _) in itertools.pairwise(tx):
        assert t2 - t1 >= 0.8, "warnings closer together than the sampling window"

    stats = call.audio.stats()
    assert stats.tx_starved_frames >= 3
    await call.hangup()


async def test_abandoned_reader_warns_and_idle_transmit_does_not(bench_ua):
    call, _ = await dial_ready(bench_ua)
    warnings = collect_warnings(call)

    # One read declares the intent to consume; then stop. Transmit is
    # idle throughout (silence by design — never a warning); the echoed
    # silence keeps arriving at wire rate, overflows the abandoned 2 s
    # receive buffer, and the tap starts dropping — the receive warning.
    # The wait allows for the far end taking a moment to latch before
    # its echo starts flowing.
    call.audio.read(320)
    await asyncio.sleep(4.5)

    rx = [w for _, w in warnings if w.direction == "rx"]
    assert rx, "an abandoned receive buffer produced no warning"
    assert "reading" in rx[0].message
    assert not [w for _, w in warnings if w.direction == "tx"], (
        "idle transmit must not warn: silence is a choice, not a fault"
    )

    stats = call.audio.stats()
    assert stats.rx_dropped_bytes > 0
    assert stats.tx_silence_frames > 0  # idle was happening, and counted
    assert stats.tx_starved_frames == 0
    await call.hangup()


async def test_never_reading_is_counted_but_never_warned(bench_ua):
    call, _ = await dial_ready(bench_ua)
    warnings = collect_warnings(call)

    # Touch neither direction — the shape of an application that plays
    # and records through real audio devices. The unread receive buffer
    # overflows just the same, but a direction never used is a choice:
    # no warning may fire. The loss still shows in the stats on request.
    await asyncio.sleep(4.0)

    assert not warnings, f"an unused call warned: {[w.message for _, w in warnings]}"
    stats = call.audio.stats()
    assert stats.rx_dropped_bytes > 0
    await call.hangup()


async def test_late_reader_discards_are_counted(bench_ua):
    call, _ = await dial_ready(bench_ua)

    # Let echoed audio pile up unread until it is provably past half of
    # the 2 s buffer — media can take a moment to start flowing, so 2.5 s
    # of wall time banks at least ~2 s of audio — then read: the
    # catch-up skips forward and must say so.
    await asyncio.sleep(2.5)
    assert call.audio.read(4096) != b""

    stats = call.audio.stats()
    assert stats.rx_discarded_bytes > 0
    # After the skip the reader is current again: nowhere near half full.
    assert stats.rx_buffered < stats.rx_high_water
    await call.hangup()
