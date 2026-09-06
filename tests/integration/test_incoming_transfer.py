#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""Receiving transfer requests (`make bench-up` first).

The waiting-transferor flow needs a peer that sends a REFER and then
waits for the outcome — which FreeSWITCH will not do (its
``uuid_deflect`` abandons the dialog right after the REFER). Our own
stack does it properly, so these tests run a **direct UA→UA call inside
one runtime**: the configuration pins ``sip_listen`` and agent A dials
agent B at our own port, no switch in the signaling path. A then uses
``call.transfer()`` (the commit-29 transferor) against B's receive-side
API — each test exercises both halves at once. The bench still serves
as the transfer target (the echo service) and as the REFER-and-abandon
transferor in the deflect test.
"""

import asyncio
import itertools
import os

import pytest

native = pytest.importorskip("baresip._native")

from test_telephony_gates import fs_cli

from baresip import Account, BaresipError, Config, StaleHandleError, TransferFailed
from baresip.call import CallState
from baresip.runtime import Runtime
from baresip.ua import UserAgent

pytestmark = pytest.mark.bench

DOMAIN = f"127.0.0.1:{os.environ.get('BENCH_SIP_PORT', '15060')}"

# One listen port per runtime: the previous test's socket can outlive
# its runtime's close within the same process.
_PORTS = itertools.count(5070)


@pytest.fixture
async def runtime(tmp_path):
    own = f"127.0.0.1:{next(_PORTS)}"
    rt = Runtime()
    await rt.start(
        Config(
            net_interface="127.0.0.1",
            max_concurrent_calls=None,
            audio_source="ausine,440",
            audio_player=f"aufile,{tmp_path}/rx.wav",
            extra_config_text=f"sip_listen {own}\n",
        )
    )
    try:
        yield rt, own
    finally:
        await rt.close()


async def direct_pair(runtime, own, *, policy: str = "manual"):
    """A direct A->B call: returns (a_call, b_call, ua_b).

    B carries the bench credentials (its accept-dial toward the echo
    service must pass the switch's auth); A registers nowhere.
    """
    ua_b = await UserAgent.create(
        runtime,
        Account(user="1003", password="bench1234", domain=DOMAIN),
        transfer_policy=policy,
    )
    await ua_b.register()
    ua_a = await UserAgent.create(
        runtime, Account(user="1004", password="bench1234", domain=DOMAIN, reg_interval=0)
    )

    incoming: asyncio.Queue = asyncio.Queue()
    ua_b.on_incoming(incoming.put_nowait)

    async def answerer():
        call = await incoming.get()
        await call.answer()
        return call

    task = asyncio.create_task(answerer())
    a_call = await ua_a.dial(f"sip:1003@{own}")
    await a_call.wait_established()
    b_call = await asyncio.wait_for(task, 10)
    return a_call, b_call, ua_b


async def test_manual_accept_executes_the_transfer(runtime):
    rt, own = runtime
    a_call, b_call, _ = await direct_pair(rt, own)

    requests = []
    b_call.on_transfer_request(requests.append)
    xfer = asyncio.create_task(a_call.transfer(f"sip:9196@{DOMAIN}", timeout=15))

    deadline = asyncio.get_running_loop().time() + 10
    while not requests and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.05)
    assert requests, "the transfer request never arrived"
    request = requests[0]
    assert request.target == f"sip:9196@{DOMAIN}"
    assert request.method == "INVITE"
    assert b_call.transfer_request is request

    new_call = await b_call.accept_transfer()
    await new_call.wait_established()
    assert b_call.transfer_request is None

    # The xcall linkage reports success to the transferor and retires
    # both original legs; the replacement call carries on.
    await xfer  # A's transfer() returns success
    assert a_call.state is CallState.CLOSED
    deadline = asyncio.get_running_loop().time() + 5
    while b_call.state is not CallState.CLOSED and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.1)
    assert b_call.state is CallState.CLOSED
    assert new_call.state is CallState.ESTABLISHED
    await new_call.hangup()


async def test_manual_reject_fails_the_transferor_and_keeps_the_call(runtime):
    rt, own = runtime
    a_call, b_call, _ = await direct_pair(rt, own)

    requests = []
    b_call.on_transfer_request(requests.append)
    xfer = asyncio.create_task(a_call.transfer(f"sip:9196@{DOMAIN}", timeout=15))
    deadline = asyncio.get_running_loop().time() + 10
    while not requests and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.05)
    assert requests

    await b_call.reject_transfer(486)
    with pytest.raises(TransferFailed) as excinfo:
        await xfer
    assert excinfo.value.status == 486

    # A failed request costs nothing: both legs carry on.
    assert a_call.state is CallState.ESTABLISHED
    assert b_call.state is CallState.ESTABLISHED
    with pytest.raises(BaresipError, match="no transfer request"):
        await b_call.reject_transfer()  # already answered
    await a_call.hangup()


async def test_auto_policy_executes_without_the_application(runtime):
    rt, own = runtime
    a_call, _b_call, ua_b = await direct_pair(rt, own, policy="auto")

    new_calls = []
    ua_b.on_transfer_call(new_calls.append)
    await a_call.transfer(f"sip:9196@{DOMAIN}", timeout=15)  # success = policy accepted

    assert a_call.state is CallState.CLOSED
    deadline = asyncio.get_running_loop().time() + 10
    while asyncio.get_running_loop().time() < deadline:
        if new_calls and new_calls[0].state is CallState.ESTABLISHED:
            break
        await asyncio.sleep(0.1)
    assert new_calls, "the policy-dialed call was never delivered"
    assert new_calls[0].state is CallState.ESTABLISHED
    await new_calls[0].hangup()


async def test_reject_policy_refuses_without_the_application(runtime):
    rt, own = runtime
    a_call, b_call, _ = await direct_pair(rt, own, policy="reject")

    with pytest.raises(TransferFailed) as excinfo:
        await a_call.transfer(f"sip:9196@{DOMAIN}", timeout=15)
    assert excinfo.value.status == 603
    assert a_call.state is CallState.ESTABLISHED
    assert b_call.state is CallState.ESTABLISHED
    await a_call.hangup()


async def test_deflecting_transferor_abandons_the_dialog(runtime):
    """FreeSWITCH's uuid_deflect sends the REFER and quits the call, so
    the request outlives its dialog: accepting through the dead call is
    a typed stale error, and honoring the request means dialing its
    target yourself."""
    rt, _own = runtime
    ua = await UserAgent.create(rt, Account(user="1003", password="bench1234", domain=DOMAIN))
    await ua.register()
    incoming: asyncio.Queue = asyncio.Queue()
    ua.on_incoming(incoming.put_nowait)

    async def answerer():
        call = await incoming.get()
        await call.answer()
        return call

    task = asyncio.create_task(answerer())
    out = await asyncio.to_thread(fs_cli, "originate user/1003 &park")
    uuid = out.strip().split()[-1]
    call = await asyncio.wait_for(task, 10)

    requests = []
    call.on_transfer_request(requests.append)
    await asyncio.to_thread(fs_cli, f"uuid_deflect {uuid} sip:9196@{DOMAIN}")

    deadline = asyncio.get_running_loop().time() + 10
    while asyncio.get_running_loop().time() < deadline:
        if requests and call.state is CallState.CLOSED:
            break
        await asyncio.sleep(0.05)
    assert requests, "the deflect REFER never arrived"
    assert call.state is CallState.CLOSED

    with pytest.raises(StaleHandleError):
        await call.accept_transfer()

    # The documented pattern for an abandoned request: dial it yourself.
    replacement = await ua.dial(requests[0].target)
    await replacement.wait_established()
    await replacement.hangup()
