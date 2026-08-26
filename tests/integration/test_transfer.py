#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""Blind transfer against the FreeSWITCH bench (`make bench-up` first).

Two agents (the test-reserved accounts 1003 and 1004) bridge through the
switch, then one leg REFERs. Like hold, REFER is in-dialog and goes to
the Contact FreeSWITCH advertises, so these tests run on the default
bench port (see bench/README.md).

Two bench realities pin the scenarios' shape. FreeSWITCH executes a
transfer by re-entering the *other* leg's existing channel into the
dialplan (the echo app then runs on that channel — no new channel
appears), and it reports sipfrag success as soon as the transfer
executes, whatever the dialplan does next — so a failing NOTIFY needs a
transferee that can actually fail its replacement call, which this bench
cannot produce. The failure covered here is the other real one: a REFER
on a non-bridged call, which FreeSWITCH refuses with 403.
"""

import asyncio
import os

import pytest

native = pytest.importorskip("baresip._native")

from test_telephony_gates import fs_cli

from baresip import Account, TransferFailed
from baresip.call import CallState
from baresip.runtime import Runtime
from baresip.ua import UserAgent

pytestmark = pytest.mark.bench

DOMAIN = f"127.0.0.1:{os.environ.get('BENCH_SIP_PORT', '15060')}"


@pytest.fixture
async def runtime(tmp_path):
    rt = Runtime()
    await rt.start(
        f"net_interface 127.0.0.1\naudio_source ausine,440\naudio_player aufile,{tmp_path}/rx.wav\n"
    )
    try:
        yield rt
    finally:
        await rt.close()


async def test_blind_transfer_repoints_the_far_end_and_closes_our_leg(runtime):
    ua_a = await UserAgent.create(
        runtime, Account(user="1003", password="bench1234", domain=DOMAIN)
    )
    ua_b = await UserAgent.create(
        runtime, Account(user="1004", password="bench1234", domain=DOMAIN)
    )
    await ua_a.register()
    await ua_b.register()

    incoming: asyncio.Queue = asyncio.Queue()
    ua_b.on_incoming(incoming.put_nowait)
    a_call = await ua_a.dial(f"sip:1004@{DOMAIN}")
    b_call = await asyncio.wait_for(incoming.get(), 10)
    await b_call.answer()
    await a_call.wait_established()
    await b_call.wait_established()

    await a_call.transfer(f"sip:9196@{DOMAIN}")

    # Success IS the closing of our leg; the switch reported the
    # transfer executed and the stack ended the call.
    assert a_call.state is CallState.CLOSED

    # B's channel gets re-entered into the dialplan at 9196: poll until
    # the switch shows it running the echo application.
    deadline = asyncio.get_running_loop().time() + 5
    channels = ""
    while asyncio.get_running_loop().time() < deadline:
        channels = await asyncio.to_thread(fs_cli, "show channels")
        if ",echo," in channels:
            break
        await asyncio.sleep(0.2)
    assert ",echo," in channels, f"far leg never reached the echo app:\n{channels}"
    assert b_call.state is CallState.ESTABLISHED
    await b_call.hangup()


async def test_transfer_refused_fails_typed_and_the_call_survives(runtime):
    """FreeSWITCH answers the REFER itself with 403 on a non-bridged
    call — the REFER-transaction failure path, distinct from a failing
    NOTIFY."""
    ua = await UserAgent.create(runtime, Account(user="1003", password="bench1234", domain=DOMAIN))
    await ua.register()
    call = await ua.dial(f"sip:9196@{DOMAIN}")
    await call.wait_established()

    with pytest.raises(TransferFailed) as excinfo:
        await call.transfer(f"sip:9664@{DOMAIN}")
    assert excinfo.value.status == 403

    # A failed transfer leaves the call intact.
    assert call.state is CallState.ESTABLISHED
    await call.hangup()
