#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""Concurrent-call limits and drain against the bench (`make bench-up`).

The stack enforces ``call_max_calls`` on inbound INVITEs only (a
stateless 486 "Max Calls" before any call exists); outbound dialing is
the application's own budget. Draining refuses inbound on the SIP
thread itself and makes ``dial()`` raise, then resolves when the last
live call ends.
"""

import asyncio
import os

import pytest

native = pytest.importorskip("baresip._native")

from test_telephony_gates import fs_cli

from baresip import Account, DrainingError, Event
from baresip.call import CallState
from baresip.runtime import Runtime
from baresip.ua import UserAgent

pytestmark = pytest.mark.bench

DOMAIN = f"127.0.0.1:{os.environ.get('BENCH_SIP_PORT', '15060')}"


def config_text(tmp_path, limit: int) -> str:
    return (
        f"net_interface 127.0.0.1\ncall_max_calls {limit}\n"
        f"audio_source ausine,440\naudio_player aufile,{tmp_path}/rx.wav\n"
    )


async def registered_ua(runtime):
    ua = await UserAgent.create(runtime, Account(user="1003", password="bench1234", domain=DOMAIN))
    await ua.register()
    return ua


def auto_answer(ua, answered: list):
    def on_incoming(call):
        async def answer():
            await call.answer()
            answered.append(call)

        asyncio.get_running_loop().create_task(answer())

    ua.on_incoming(on_incoming)


async def test_max_calls_caps_inbound(tmp_path):
    runtime = Runtime()
    await runtime.start(config_text(tmp_path, limit=1))
    try:
        ua = await registered_ua(runtime)
        call = await ua.dial(f"sip:9196@{DOMAIN}")
        await call.wait_established()  # occupies the whole budget

        failed = []
        runtime.subscribe(lambda e: failed.append(e) if e.event is Event.SIPSESS_FAILED else None)
        out = await asyncio.to_thread(fs_cli, "originate user/1003 &park")
        assert "+OK" not in out, f"the over-limit call was accepted:\n{out}"

        deadline = asyncio.get_running_loop().time() + 5
        while not failed and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)
        assert failed, "expected the SIPSESS_FAILED event for the refused INVITE"
        assert (failed[0].text or "").startswith("486")

        await call.hangup()
    finally:
        await runtime.close()


async def test_limit_zero_means_unlimited(tmp_path):
    """Five simultaneous inbound calls — the stack's own compiled
    default of 4 would refuse the fifth, so this pins that the rendered
    0 truly lifts the limit."""
    runtime = Runtime()
    await runtime.start(config_text(tmp_path, limit=0))
    try:
        ua = await registered_ua(runtime)
        answered: list = []
        auto_answer(ua, answered)
        for i in range(5):
            out = await asyncio.to_thread(fs_cli, "originate user/1003 &park")
            assert "+OK" in out, f"call {i + 1} was refused:\n{out}"
        deadline = asyncio.get_running_loop().time() + 10
        while len(answered) < 5 and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.1)
        assert len(answered) == 5
        assert all(c.state is CallState.ESTABLISHED for c in answered)
        for call in answered:
            await call.hangup()
    finally:
        await runtime.close()


async def test_drain_refuses_new_work_and_resolves_when_idle(tmp_path):
    runtime = Runtime()
    await runtime.start(config_text(tmp_path, limit=0))
    try:
        ua = await registered_ua(runtime)
        calls = [await ua.dial(f"sip:9196@{DOMAIN}") for _ in range(2)]
        for call in calls:
            await call.wait_established()

        drain = asyncio.create_task(runtime.drain())
        await asyncio.sleep(0.3)
        assert not drain.done(), "drain resolved with live calls remaining"
        assert runtime.draining

        with pytest.raises(DrainingError):
            await ua.dial(f"sip:9196@{DOMAIN}")
        out = await asyncio.to_thread(fs_cli, "originate user/1003 &park")
        assert "+OK" not in out, f"an inbound call got through the drain:\n{out}"

        for call in calls:
            await call.hangup()
        await asyncio.wait_for(drain, 10)
    finally:
        await runtime.close()


async def test_drain_on_an_idle_runtime_resolves_immediately(tmp_path):
    runtime = Runtime()
    await runtime.start(config_text(tmp_path, limit=0))
    try:
        await asyncio.wait_for(runtime.drain(), 5)
        assert runtime.draining
    finally:
        await runtime.close()
