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

from baresip import Account, CallBusy, CallDeclined, Event
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
