#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""T3 — call storm: thousands of zero-talk-time calls, both directions.

A direct UA→UA call inside one runtime (the commit-31 pattern: pinned
``sip_listen``, no switch), so every iteration exercises the outbound
and inbound paths at once: dial → answer → established → immediate
hangup, the hanging-up side alternating by seed. Hunts handle churn and
reference imbalance — RSS flat and the handle table empty, checked
every hundred calls.
"""

import asyncio

import pytest
from torture_helpers import Bounds, drain_loop, expect_call_slots_empty, scaled

from baresip import Account, CallState, Runtime, UserAgent

native = pytest.importorskip("baresip._native")

pytestmark = pytest.mark.torture

TOTAL = 2000
CHECK_EVERY = 100
OWN = "127.0.0.1:5080"


async def wait_closed(call, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while call.state is not CallState.CLOSED:
        assert asyncio.get_running_loop().time() < deadline, "call never closed"
        await asyncio.sleep(0.005)


async def test_t3_call_storm(rng):
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

        async def one_call():
            async def answerer():
                call = await incoming.get()
                await call.answer()
                return call

            task = asyncio.create_task(answerer())
            a_call = await ua_a.dial(f"sip:bob@{OWN}")
            await a_call.wait_established()
            b_call = await asyncio.wait_for(task, 5)
            return a_call, b_call

        # Warm up before the baseline: the allocator grows arenas for
        # roughly the first several hundred calls, then the curve goes
        # flat (measured; it even shrinks as arenas consolidate).
        for _ in range(250):
            a_call, b_call = await one_call()
            await a_call.hangup()
            await wait_closed(a_call)
            await wait_closed(b_call)

        bounds = Bounds()
        for i in range(scaled(TOTAL)):
            a_call, b_call = await one_call()

            # Zero talk time: hang up immediately, either side.
            hanger = a_call if rng.random() < 0.5 else b_call
            await hanger.hangup()
            await wait_closed(a_call)
            await wait_closed(b_call)

            if (i + 1) % CHECK_EVERY == 0:
                await drain_loop()
                await expect_call_slots_empty(runtime, f"call {i + 1}", ua=2)
                bounds.check(f"call {i + 1}")
    finally:
        await runtime.close()
