#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""T7 — audio churn: hostile PCM I/O and renegotiation under load.

Direct UA→UA calls with both legs on aumem, so Python feeds and reads
both ends. Two hunts: random-sized reads and writes (0 B, odd sizes,
huge) with hangups landing mid-write from another task, and hold/resume
cycles driving re-INVITEs at a leg doing live audio I/O — the epoch
mechanism fired over and over under load. The invariant is the audio
contract itself: every operation returns, raises AudioNotActive, or
raises AudioRestarted — never a crash, never a hang, never a stuck
stream afterwards.
"""

import asyncio

import pytest
from torture_helpers import Bounds, drain_loop, expect_call_slots_empty, scaled

from baresip import (
    Account,
    AudioNotActive,
    AudioRestarted,
    BaresipError,
    CallState,
    Config,
    Runtime,
    UserAgent,
)

native = pytest.importorskip("baresip._native")

pytestmark = pytest.mark.torture

IO_ROUNDS = 150
HOLD_CYCLES = 200
CHECK_EVERY = 25
OWN = "127.0.0.1:5082"
CONF = Config(
    net_interface="127.0.0.1",
    max_concurrent_calls=None,
    extra_config_text=f"sip_listen {OWN}\n",
)

AUDIO_GONE = (AudioNotActive, AudioRestarted)


async def wait_closed(call, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while call.state is not CallState.CLOSED:
        assert asyncio.get_running_loop().time() < deadline, "call never closed"
        await asyncio.sleep(0.005)


async def start_pair(runtime):
    ua_a = await UserAgent.create(
        runtime, Account(user="alice", password="", domain=OWN, reg_interval=0)
    )
    ua_b = await UserAgent.create(
        runtime, Account(user="bob", password="", domain=OWN, reg_interval=0)
    )
    incoming: asyncio.Queue = asyncio.Queue()
    ua_b.on_incoming(incoming.put_nowait)
    return ua_a, incoming


async def call_with_audio(ua_a, incoming):
    """One established self-call with the a-leg's audio up both ways."""

    async def answerer():
        call = await incoming.get()
        await call.answer()
        return call

    task = asyncio.create_task(answerer())
    a_call = await ua_a.dial(f"sip:bob@{OWN}")
    await a_call.wait_established()
    b_call = await asyncio.wait_for(task, 5)
    # answer() returns when the command completes; the b-leg's own
    # ESTABLISHED event lands asynchronously, and hold requires it.
    await b_call.wait_established()

    deadline = asyncio.get_running_loop().time() + 5
    while asyncio.get_running_loop().time() < deadline:
        try:
            info = a_call.audio.info()
            if info.tx_ready and info.rx_ready:
                return a_call, b_call
        except AUDIO_GONE:
            pass
        await asyncio.sleep(0.02)
    raise AssertionError("audio did not come up within 5 s")


def one_audio_op(rng, call) -> None:
    """One random audio operation; only the contract's outcomes allowed."""
    try:
        roll = rng.random()
        if roll < 0.4:
            # Writes: empty, tiny, odd-sized, frame-sized, or huge.
            size = rng.choice([0, 1, 2, rng.randrange(3, 320), 320 * rng.randrange(1, 13), 262144])
            call.audio.write(b"\x00" * size)
        elif roll < 0.8:
            # Reads: single byte up to far beyond any buffer.
            call.audio.read(rng.choice([1, 2, rng.randrange(3, 4096), 65536]))
        elif roll < 0.9:
            call.audio.info()
        else:
            call.audio.stats()
    except AUDIO_GONE:
        pass


async def test_t7_audio_io_churn(rng):
    runtime = Runtime()
    await runtime.start(CONF)
    try:
        ua_a, incoming = await start_pair(runtime)

        async def one_round():
            a_call, b_call = await call_with_audio(ua_a, incoming)
            for _ in range(rng.randrange(20, 41)):
                one_audio_op(rng, rng.choice([a_call, b_call]))
                if rng.random() < 0.3:
                    await asyncio.sleep(rng.random() * 0.005)

            if rng.random() < 0.5:
                hanger = a_call if rng.random() < 0.5 else b_call
                await hanger.hangup()
            else:
                # The hangup lands while another task is mid-write; the
                # writer must end through the typed path, never hang.
                async def writer():
                    pcm = b"\x00" * 1280
                    try:
                        while True:
                            a_call.audio.write(pcm)
                            await asyncio.sleep(0.005)
                    except AUDIO_GONE:
                        return

                task = asyncio.create_task(writer())
                await asyncio.sleep(rng.random() * 0.05)
                await a_call.hangup()
                await asyncio.wait_for(task, 5)
            await wait_closed(a_call)
            await wait_closed(b_call)

        # Warm up before the baseline: the first calls pay one-time costs
        # (jitter buffers, codec state, allocator arenas).
        for _ in range(10):
            await one_round()

        bounds = Bounds()
        for i in range(scaled(IO_ROUNDS)):
            await asyncio.wait_for(one_round(), 20)
            if (i + 1) % CHECK_EVERY == 0:
                await drain_loop()
                await expect_call_slots_empty(runtime, f"round {i + 1}", ua=2)
                bounds.check(f"round {i + 1}")
    finally:
        await runtime.close()


async def test_t7_hold_reinvite_churn(rng):
    runtime = Runtime()
    await runtime.start(CONF)
    try:
        ua_a, incoming = await start_pair(runtime)
        a_call, b_call = await call_with_audio(ua_a, incoming)

        stop = asyncio.Event()

        async def churner():
            """Live audio I/O on the a-leg while re-INVITEs land on it."""
            pcm = b"\x00" * 640
            while not stop.is_set():
                try:
                    a_call.audio.write(pcm)
                    a_call.audio.read(4096)
                except AUDIO_GONE:
                    pass
                await asyncio.sleep(0.01)

        async def flip(verb: str) -> None:
            """One hold or resume, retrying through re-INVITE glare."""
            deadline = asyncio.get_running_loop().time() + 5
            while True:
                try:
                    await getattr(b_call, verb)()
                    return
                except BaresipError as exc:
                    still_time = asyncio.get_running_loop().time() < deadline
                    if "session refresh" in str(exc) and still_time:
                        await asyncio.sleep(0.02)
                        continue
                    raise

        # Warm the renegotiation path before the baseline, same idiom as
        # every other torture.
        churn = asyncio.create_task(churner())
        try:
            for _ in range(5):
                await flip("hold")
                await flip("resume")
            bounds = Bounds()
            for i in range(scaled(HOLD_CYCLES)):
                await flip("hold")
                if rng.random() < 0.5:
                    await asyncio.sleep(rng.random() * 0.03)
                await flip("resume")
                if (i + 1) % 50 == 0:
                    bounds.check(f"cycle {i + 1}")
        finally:
            stop.set()
            await asyncio.wait_for(churn, 5)

        assert a_call.state is CallState.ESTABLISHED, "a-leg died during hold churn"
        assert b_call.state is CallState.ESTABLISHED, "b-leg died during hold churn"

        # Audio must still flow after the last resume: the b-leg's source
        # transmits (silence when unfed), so the a-leg reads real bytes.
        deadline = asyncio.get_running_loop().time() + 5
        while True:
            try:
                if a_call.audio.read(4096):
                    break
            except AUDIO_GONE:
                pass
            assert asyncio.get_running_loop().time() < deadline, (
                "audio never recovered after hold churn"
            )
            await asyncio.sleep(0.02)

        await a_call.hangup()
        await wait_closed(a_call)
        await wait_closed(b_call)
        await drain_loop()
        await expect_call_slots_empty(runtime, "after hold churn", ua=2)
    finally:
        await runtime.close()
