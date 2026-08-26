#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""T4 — race roulette: seeded adversarial interleavings.

Each iteration plays one hostile timing scenario between two agents in
one runtime (direct calls, no switch): the callee hangs up moments
after the INVITE while the caller proceeds, hangup during ringing,
dial-and-instant-close, double answers, and commands aimed at
just-closed calls. The invariant is absolute: **never a crash, never a
hang — always success or a typed error.** Anything else fails the run.
"""

import asyncio

import pytest
from torture_helpers import Bounds, drain_loop, expect_call_slots_empty, scaled

from baresip import Account, BaresipError, CallFailed, CallState, Runtime, UserAgent

native = pytest.importorskip("baresip._native")

pytestmark = pytest.mark.torture

TOTAL = 1000
CHECK_EVERY = 100
OWN = "127.0.0.1:5081"

#: The only acceptable outcomes: clean returns or the library's typed
#: errors (BaresipError covers Stale/Draining/CallFailed/...).
TYPED = (BaresipError,)


async def settle(calls, timeout: float = 5.0) -> None:
    """Every call ends CLOSED within the deadline, however the race went."""
    deadline = asyncio.get_running_loop().time() + timeout
    for call in calls:
        if call is None:
            continue
        while call.state is not CallState.CLOSED:
            try:
                await call.hangup()
            except TYPED:
                pass
            if call.state is CallState.CLOSED:
                break
            assert asyncio.get_running_loop().time() < deadline, "call never settled"
            await asyncio.sleep(0.01)


async def test_t4_race_roulette(rng):
    runtime = Runtime()
    await runtime.start(
        f"net_interface 127.0.0.1\nsip_listen {OWN}\n"
        "audio_source ausine,440\naudio_player aumem,default\n"
    )
    try:
        ua_a = await UserAgent.create(
            runtime, Account(user="alice", password="", domain=OWN, reg_interval=0)
        )
        ua_b = await UserAgent.create(
            runtime, Account(user="bob", password="", domain=OWN, reg_interval=0)
        )
        incoming: asyncio.Queue = asyncio.Queue()
        ua_b.on_incoming(incoming.put_nowait)

        async def take_incoming(timeout=2.0):
            try:
                return await asyncio.wait_for(incoming.get(), timeout)
            except TimeoutError:
                return None  # the race killed the call before it rang

        async def scenario_callee_hangs_early():
            # B hangs up 0-100 ms in while A drives toward established.
            a_call = await ua_a.dial(f"sip:bob@{OWN}")
            b_call = await take_incoming()
            if b_call is not None:
                await asyncio.sleep(rng.random() * 0.1)
                try:
                    await b_call.hangup()
                except TYPED:
                    pass
            try:
                await asyncio.wait_for(a_call.wait_established(), 5)
            except (CallFailed, TimeoutError):
                pass
            await settle([a_call, b_call])

        async def scenario_hangup_during_ringing():
            a_call = await ua_a.dial(f"sip:bob@{OWN}")
            await asyncio.sleep(rng.random() * 0.05)
            try:
                await a_call.hangup()
            except TYPED:
                pass
            b_call = await take_incoming(0.5)
            await settle([a_call, b_call])

        async def scenario_dial_instant_close():
            a_call = await ua_a.dial(f"sip:bob@{OWN}")
            try:
                await a_call.hangup()
            except TYPED:
                pass
            b_call = await take_incoming(0.5)
            await settle([a_call, b_call])

        async def scenario_double_answer():
            a_call = await ua_a.dial(f"sip:bob@{OWN}")
            b_call = await take_incoming()
            if b_call is not None:
                try:
                    await b_call.answer()
                    await b_call.answer()  # the second must be typed-or-clean
                except TYPED:
                    pass
            await settle([a_call, b_call])

        async def scenario_commands_on_closed():
            a_call = await ua_a.dial(f"sip:bob@{OWN}")
            b_call = await take_incoming()
            if b_call is not None:
                try:
                    await b_call.answer()
                except TYPED:
                    pass
            try:
                await a_call.hangup()
            except TYPED:
                pass
            await settle([a_call, b_call])
            for op in (a_call.hangup, a_call.reject):
                try:
                    await op()
                except TYPED:
                    pass
            try:
                await a_call.send_dtmf("5")
            except (*TYPED, ValueError):
                pass

        scenarios = [
            scenario_callee_hangs_early,
            scenario_hangup_during_ringing,
            scenario_dial_instant_close,
            scenario_double_answer,
            scenario_commands_on_closed,
        ]

        # Warm up before the baseline (first-call one-time costs).
        for _ in range(5):
            await scenario_dial_instant_close()
            while not incoming.empty():
                await settle([incoming.get_nowait()])

        bounds = Bounds()
        for i in range(scaled(TOTAL)):
            scenario = rng.choice(scenarios)
            # The hang detector: no scenario may take longer than this.
            await asyncio.wait_for(scenario(), 15)
            # Anything still queued from a killed-before-ring INVITE is
            # stale by now; drop it so the next iteration starts clean.
            while not incoming.empty():
                leftover = incoming.get_nowait()
                await settle([leftover])

            if (i + 1) % CHECK_EVERY == 0:
                await drain_loop()
                await expect_call_slots_empty(runtime, f"round {i + 1}", ua=2)
                bounds.check(f"round {i + 1}")
    finally:
        await runtime.close()
