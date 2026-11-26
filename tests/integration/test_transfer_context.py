#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""Call context on a transfer, on the bench.

Through the proxy, our own agents play all three parts, so the context
is followed from the transferor to the party the transfer calls. Through
FreeSWITCH only the request that leaves the transferor is checked: as
the party being transferred, FreeSWITCH places the new call without the
headers attached to the target.

    make bench-up
    pytest -m bench tests/integration
"""

import asyncio
import os

import pytest

pytest.importorskip("baresip._native")

from test_proxy_routes import REGISTRAR, proxy  # noqa: F401  (the fixture restarts the proxy)
from test_telephony_gates import bridged, capture_sip_trace

from baresip import Account, CallState, Config
from baresip.runtime import Runtime
from baresip.ua import UserAgent

pytestmark = pytest.mark.bench

DOMAIN = f"127.0.0.1:{os.environ.get('BENCH_SIP_PORT', '15060')}"
CONTEXT = {"User-to-User": "56a390f3d2b7310023a2;encoding=hex", "X-Case": "12 34"}


@pytest.fixture
async def runtime():
    rt = Runtime()
    await rt.start(
        Config(
            net_interface="127.0.0.1",
            max_concurrent_calls=None,
            expose_headers=("User-to-User", "X-Case"),
            sip_trace=True,
        )
    )
    yield rt
    await rt.close()


async def agent(runtime, user: str, domain: str, password: str, **fields) -> UserAgent:
    ua = await UserAgent.create(
        runtime, Account(user=user, password=password, domain=domain, **fields)
    )
    if fields.get("reg_interval") != 0:
        await ua.register()
    return ua


async def call_between(caller: UserAgent, callee: UserAgent, target: str):
    incoming: asyncio.Queue = asyncio.Queue()
    callee.on_incoming(incoming.put_nowait)
    placed = await caller.dial(target)
    received = await asyncio.wait_for(incoming.get(), 10)
    await received.answer()
    await placed.wait_established(timeout=10)
    await received.wait_established(timeout=10)
    return placed, received


async def wait_for(items: list):
    while not items:
        await asyncio.sleep(0.05)
    return items[0]


async def test_context_reaches_the_new_party_through_the_proxy(runtime):
    transferor = await agent(runtime, "1003", REGISTRAR, "", reg_interval=0)
    transferred = await agent(runtime, "1004", REGISTRAR, "")
    new_party = await agent(runtime, "1005", REGISTRAR, "")
    arriving: asyncio.Queue = asyncio.Queue()
    new_party.on_incoming(arriving.put_nowait)
    placed, received = await call_between(transferor, transferred, f"sip:1004@{REGISTRAR}")
    requests: list = []
    received.on_transfer_request(requests.append)

    transfer = asyncio.create_task(
        placed.transfer(f"sip:1005@{REGISTRAR}", headers=CONTEXT, timeout=15)
    )
    request = await asyncio.wait_for(wait_for(requests), 10)
    assert request.headers == CONTEXT

    await received.accept_transfer()
    new_call = await asyncio.wait_for(arriving.get(), 10)
    assert new_call.headers == CONTEXT
    await new_call.answer()
    await transfer
    assert placed.state is CallState.CLOSED
    await new_call.hangup()


async def test_the_request_to_freeswitch_carries_the_context_in_its_target(runtime):
    transferor = await agent(runtime, "1003", DOMAIN, "bench1234")
    transferred = await agent(runtime, "1004", DOMAIN, "bench1234")
    placed, received = await call_between(transferor, transferred, f"sip:1004@{DOMAIN}")

    await bridged()
    with capture_sip_trace() as records:
        await placed.transfer(f"sip:9196@{DOMAIN}", headers=CONTEXT)

    assert placed.state is CallState.CLOSED
    refer = next(
        r for r in records if r.startswith("TX") and r.splitlines()[1].startswith("REFER ")
    )
    refer_to = next(line for line in refer.splitlines() if line.lower().startswith("refer-to:"))
    assert refer_to == (
        f"Refer-To: <sip:9196@{DOMAIN}"
        "?User-to-User=56a390f3d2b7310023a2%3Bencoding%3Dhex&X-Case=12%2034>"
    )
    if received.state is not CallState.CLOSED:
        await received.hangup()
