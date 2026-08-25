#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""Outbound calls against the FreeSWITCH bench (`make bench-up` first).

Dials the bench's service extensions: 9196 echoes, 9486 answers busy,
9603 declines. Run with: pytest -m bench tests/integration
"""

import asyncio
import os
import subprocess

import pytest

native = pytest.importorskip("baresip._native")

from test_telephony_gates import capture_sip_trace, wait_closed

from baresip import Account, CallBusy, CallDeclined, Event, RegistrationError
from baresip.call import CallState
from baresip.runtime import Runtime
from baresip.ua import UserAgent

pytestmark = pytest.mark.bench

DOMAIN = f"127.0.0.1:{os.environ.get('BENCH_SIP_PORT', '15060')}"
CONTAINER = "baresip-bench-freeswitch"


@pytest.fixture
async def bench_ua(tmp_path):
    runtime = Runtime()
    await runtime.start(
        f"net_interface 127.0.0.1\naudio_source ausine,440\naudio_player aufile,{tmp_path}/rx.wav\n"
    )
    ua = await UserAgent.create(runtime, Account(user="1003", password="bench1234", domain=DOMAIN))
    await ua.register()
    try:
        yield ua
    finally:
        await runtime.close()


async def test_dial_echo_established_and_hangup(bench_ua):
    call = await bench_ua.dial(f"sip:9196@{DOMAIN}", headers={"X-Customer-Id": "77"})
    assert call.state is CallState.OUTGOING

    rtp = asyncio.get_running_loop().create_future()

    def listener(event):
        if event.event is Event.CALL_RTPESTAB and not rtp.done():
            rtp.set_result(event)

    call.on(listener)
    await call.wait_established()
    assert call.state is CallState.ESTABLISHED
    assert call.call_id, "the Call-ID arrives with the call's events"
    await asyncio.wait_for(rtp, 10)  # echo is reflecting our tone

    # The header went out on the INVITE: FreeSWITCH keeps received
    # X-headers as sip_h_* variables on its leg of the live call.
    def fs_cli(command):
        return subprocess.run(
            ["docker", "exec", CONTAINER, "fs_cli", "-x", command],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        ).stdout

    channels = await asyncio.to_thread(fs_cli, "show channels")
    uuid = next(line.split(",")[0] for line in channels.splitlines()[1:] if "-" in line)
    dump = await asyncio.to_thread(fs_cli, f"uuid_dump {uuid}")
    assert "sip_h_X-Customer-Id: 77" in dump, "expected the custom header on the bench's leg"

    closed = asyncio.get_running_loop().create_future()
    call.on(
        lambda e: (
            closed.set_result(None) if e.event is Event.CALL_CLOSED and not closed.done() else None
        )
    )
    await call.hangup()
    await asyncio.wait_for(closed, 10)
    assert call.state is CallState.CLOSED


async def test_dial_busy(bench_ua):
    call = await bench_ua.dial(f"sip:9486@{DOMAIN}")
    with pytest.raises(CallBusy) as excinfo:
        await call.wait_established()
    assert excinfo.value.status == 486
    assert call.state is CallState.CLOSED


async def test_dial_declined(bench_ua):
    call = await bench_ua.dial(f"sip:9603@{DOMAIN}")
    with pytest.raises(CallDeclined) as excinfo:
        await call.wait_established()
    assert excinfo.value.status == 603
    assert call.state is CallState.CLOSED


async def test_dial_without_registration(tmp_path):
    """A reg_interval=0 account dials trunk-style: register() refuses up
    front, no REGISTER ever goes on the wire, and the INVITE is
    digest-challenged and answered with the account's credentials."""
    runtime = Runtime()
    await runtime.start(
        f"net_interface 127.0.0.1\naudio_source ausine,440\naudio_player aufile,{tmp_path}/rx.wav\n"
    )
    try:
        await runtime.set_sip_trace(True)
        ua = await UserAgent.create(
            runtime, Account(user="1003", password="bench1234", domain=DOMAIN, reg_interval=0)
        )
        with pytest.raises(RegistrationError, match="registration disabled"):
            await ua.register()
        with capture_sip_trace() as records:
            call = await ua.dial(f"sip:9196@{DOMAIN}")
            await call.wait_established()
            assert call.state is CallState.ESTABLISHED
            await call.hangup()
            await wait_closed(call)
        starts = [
            rec.splitlines()[1].strip()
            for rec in records
            if len(rec.splitlines()) >= 2 and rec.splitlines()[0][:2] in ("TX", "RX")
        ]
        assert starts, "expected traced SIP messages"
        assert not any(s.startswith("REGISTER ") for s in starts)
        assert any(s.startswith(("SIP/2.0 401", "SIP/2.0 407")) for s in starts), (
            "expected the INVITE to be digest-challenged"
        )
    finally:
        await runtime.close()
