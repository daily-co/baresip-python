#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""DTMF over the wire, against the FreeSWITCH bench.

The bench dialplan bridges 1003 back to its own registration, so one
process holds both legs of a real FS-bridged call — digits sent on one
leg must come out of the other, in both directions and in both modes
(RTP telephone-events and SIP INFO). Run with:
pytest -m bench tests/integration
"""

import asyncio
import logging
import os

import pytest

native = pytest.importorskip("baresip._native")

from baresip import Account
from baresip.runtime import Runtime
from baresip.ua import UserAgent

pytestmark = pytest.mark.bench

DOMAIN = f"127.0.0.1:{os.environ.get('BENCH_SIP_PORT', '15060')}"
RAW_CONF = "net_interface 127.0.0.1\naudio_source aumem,default\naudio_player aumem,default\n"


async def make_ua(runtime, dtmf_mode="rtpevent"):
    account = Account(user="1003", password="bench1234", domain=DOMAIN, dtmf_mode=dtmf_mode)
    ua = await UserAgent.create(runtime, account)
    await ua.register()
    return ua


async def self_call(ua):
    """Dial our own user: FS bridges the call back to us — two legs,
    one process, a real switch in the middle."""
    incoming: asyncio.Queue = asyncio.Queue()
    ua.on_incoming(incoming.put_nowait)
    outbound = await ua.dial(f"sip:1003@{DOMAIN}")
    inbound = await asyncio.wait_for(incoming.get(), 10)
    await inbound.answer()
    await outbound.wait_established()
    return outbound, inbound


async def wait_digits(collected: list, n: int, timeout: float = 8.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while len(collected) < n:
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"expected {n} digits, got {[d.digit for d in collected]}")
        await asyncio.sleep(0.05)


async def test_rtpevent_digits_travel_both_directions():
    runtime = Runtime()
    await runtime.start(RAW_CONF)
    try:
        ua = await make_ua(runtime)
        outbound, inbound = await self_call(ua)
        heard_in: list = []
        heard_out: list = []
        inbound.on_dtmf(heard_in.append)
        outbound.on_dtmf(heard_out.append)

        await outbound.send_dtmf("12#")
        await wait_digits(heard_in, 3)
        assert [d.digit for d in heard_in] == ["1", "2", "#"]
        assert all(0 <= d.duration_ms < 5000 for d in heard_in)

        await inbound.send_dtmf("4")
        await wait_digits(heard_out, 1)
        assert heard_out[0].digit == "4"

        await outbound.hangup()
    finally:
        await runtime.close()


async def test_info_mode_puts_the_digit_in_a_sip_info():
    captured: list = []

    class Collect(logging.Handler):
        def emit(self, record):
            captured.append(record.getMessage())

    sip_logger = logging.getLogger("baresip.native.sip")
    sip_logger.setLevel(logging.DEBUG)
    handler = Collect()
    sip_logger.addHandler(handler)

    runtime = Runtime()
    await runtime.start(RAW_CONF)
    try:
        await runtime.set_sip_trace(True)
        ua = await make_ua(runtime, dtmf_mode="info")
        outbound, inbound = await self_call(ua)
        heard: list = []
        inbound.on_dtmf(heard.append)

        # INFO digits go through the switch's channel queue and need its
        # bridge loop running; one sent in the first instants after the
        # answer can be eaten by the app transition (2833 digits ride the
        # media path and don't have this window). Let the bridge settle.
        await asyncio.sleep(0.5)
        await outbound.send_dtmf("7")

        # The digit must be on the wire as a SIP INFO request ...
        deadline = asyncio.get_running_loop().time() + 8
        while not any("INFO sip:" in m and "Signal=7" in m for m in captured):
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError("no SIP INFO with Signal=7 seen in the SIP trace")
            await asyncio.sleep(0.05)
        # ... and still arrive as a digit on the far leg, relayed by FS.
        await wait_digits(heard, 1)
        assert heard[0].digit == "7"

        await outbound.hangup()
    finally:
        await runtime.close()
        sip_logger.removeHandler(handler)
