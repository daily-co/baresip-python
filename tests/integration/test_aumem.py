#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""aumem end to end against the FreeSWITCH bench: real calls, real PCM.

The bench's 9196 extension echoes RTP back, so PCM written here should
come back as readable PCM — that closes the loop over the source's
pacing thread, the encoder, the wire, the decoder, and the receive tap.
Run with: pytest -m bench tests/integration
"""

import array
import asyncio
import math
import os
import struct
import subprocess

import pytest

native = pytest.importorskip("baresip._native")

from baresip import Account, AudioNotActive, Event
from baresip.runtime import Runtime
from baresip.ua import UserAgent

pytestmark = pytest.mark.bench

DOMAIN = f"127.0.0.1:{os.environ.get('BENCH_SIP_PORT', '15060')}"
CONTAINER = "baresip-bench-freeswitch"
RAW_CONF = "net_interface 127.0.0.1\naudio_source aumem,default\naudio_player aumem,default\n"

TONE_RMS_FLOOR = 1000  # a full-scale-ish tone survives a G.711 round trip way above this
SILENCE_RMS_CEIL = 300  # what "nothing was said" may measure, comfort noise included


def sine(rate: int, seconds: float, freq: int = 440, amp: int = 8000) -> bytes:
    n = int(rate * seconds)
    return b"".join(
        struct.pack("<h", int(amp * math.sin(2 * math.pi * freq * i / rate))) for i in range(n)
    )


def peak_window_rms(pcm: bytes, rate: int, window_ms: int = 100) -> float:
    """The loudest 100 ms anywhere in the capture — alignment-free."""
    samples = array.array("h", pcm[: len(pcm) - len(pcm) % 2])
    win = max(1, rate * window_ms // 1000)
    peak = 0.0
    for start in range(0, max(1, len(samples) - win), win):
        chunk = samples[start : start + win]
        if chunk:
            peak = max(peak, math.sqrt(sum(s * s for s in chunk) / len(chunk)))
    return peak


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


async def read_for(call, seconds: float) -> bytes:
    got = bytearray()
    deadline = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < deadline:
        pcm = call.audio.read(4096)
        if pcm:
            got += pcm
        else:
            await asyncio.sleep(0.01)
    return bytes(got)


async def test_echo_carries_our_pcm(bench_ua):
    call, info = await dial_ready(bench_ua)
    rate = info.tx_sample_rate
    assert rate and info.rx_sample_rate

    call.audio.write(sine(rate, 1.0))
    received = await read_for(call, 2.5)

    # At least most of a second of audio came back, and the tone is in it.
    assert len(received) >= int(0.8 * info.rx_sample_rate) * 2
    assert peak_window_rms(received, info.rx_sample_rate) > TONE_RMS_FLOOR
    await call.hangup()


async def test_two_calls_carry_independent_audio(bench_ua):
    call_a, info_a = await dial_ready(bench_ua)
    call_b, info_b = await dial_ready(bench_ua)

    # Only A speaks. If the per-call rings were crossed anywhere, B's
    # echo would carry A's tone.
    call_a.audio.write(sine(info_a.tx_sample_rate, 1.5))
    a_pcm, b_pcm = await asyncio.gather(read_for(call_a, 2.0), read_for(call_b, 2.0))

    assert peak_window_rms(a_pcm, info_a.rx_sample_rate) > TONE_RMS_FLOOR
    assert peak_window_rms(b_pcm, info_b.rx_sample_rate) < SILENCE_RMS_CEIL
    await call_a.hangup()
    await call_b.hangup()


async def test_remote_hangup_surfaces_not_active(bench_ua):
    call, _ = await dial_ready(bench_ua)

    closed = asyncio.get_running_loop().create_future()
    call.on(
        lambda e: (
            closed.set_result(None) if e.event is Event.CALL_CLOSED and not closed.done() else None
        )
    )
    await asyncio.to_thread(
        subprocess.run,
        ["docker", "exec", CONTAINER, "fs_cli", "-x", "hupall"],
        capture_output=True,
        timeout=30,
        check=False,
    )
    await asyncio.wait_for(closed, 10)

    # CLOSED processing drops the audio slot on the re thread; give the
    # race a moment rather than asserting on the exact interleaving.
    deadline = asyncio.get_running_loop().time() + 2
    while asyncio.get_running_loop().time() < deadline:
        try:
            call.audio.read(320)
            await asyncio.sleep(0.05)
        except AudioNotActive:
            return
    raise AssertionError("audio stayed active after the call closed")


async def test_local_hangup_surfaces_not_active(bench_ua):
    call, _ = await dial_ready(bench_ua)

    closed = asyncio.get_running_loop().create_future()
    call.on(
        lambda e: (
            closed.set_result(None) if e.event is Event.CALL_CLOSED and not closed.done() else None
        )
    )
    await call.hangup()
    await asyncio.wait_for(closed, 10)

    deadline = asyncio.get_running_loop().time() + 2
    while asyncio.get_running_loop().time() < deadline:
        try:
            call.audio.write(b"\x00" * 320)
            await asyncio.sleep(0.05)
        except AudioNotActive:
            return
    raise AssertionError("audio stayed active after the call closed")
