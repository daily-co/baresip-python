#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""What a switch and a routed peer see when a call is refused or ended.

FreeSWITCH names the cause it derived from a refusal, which is what its
own routing acts on. Through the bench proxy, on the route form
Flexisip uses, the status, the reason and the headers arrive at the
other agent. Run with:

    pytest -m bench tests/integration
"""

import asyncio
import logging
import os
import pathlib
import subprocess

import pytest

pytest.importorskip("baresip._native")

from baresip import Account, CallFailed, Config
from baresip.runtime import Runtime
from baresip.ua import UserAgent

pytestmark = pytest.mark.bench

DOMAIN = f"127.0.0.1:{os.environ.get('BENCH_SIP_PORT', '15060')}"
CONTAINER = "baresip-bench-freeswitch"
BENCH_CERT = pathlib.Path(__file__).resolve().parents[2] / "bench" / "certs" / "cert.pem"
PROXY = "127.0.0.1:15070"
SIPS_PORT = 15072  # the proxy port whose calls get a sips: Record-Route


async def fs_cli(command: str) -> str:
    result = await asyncio.to_thread(
        subprocess.run,
        ["docker", "exec", CONTAINER, "fs_cli", "-x", command],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.stdout.strip()


class Received(logging.Handler):
    """The SIP messages the stack received, as its trace logged them."""

    def __init__(self):
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record):
        message = record.getMessage()
        if message.startswith("RX "):
            self.messages.append(message.split("\n", 1)[-1])

    def first(self, start: str) -> str:
        return next((m for m in self.messages if m.startswith(start)), "")


@pytest.fixture
async def traced(monkeypatch):
    """A runtime with its SIP trace captured: (runtime, received messages)."""
    monkeypatch.setenv("SIP_CA_FILE", str(BENCH_CERT))
    trace = Received()
    sip_logger = logging.getLogger("baresip.native.sip")
    level = sip_logger.level
    sip_logger.addHandler(trace)
    sip_logger.setLevel(logging.DEBUG)
    runtime = Runtime()
    await runtime.start(
        Config(net_interface="127.0.0.1", sip_trace=True, max_concurrent_calls=None)
    )
    yield runtime, trace
    await runtime.close()
    sip_logger.removeHandler(trace)
    sip_logger.setLevel(level)


@pytest.mark.parametrize(
    ("status", "cause"),
    [
        (486, "USER_BUSY"),
        (480, "NO_USER_RESPONSE"),
        (503, "NORMAL_TEMPORARY_FAILURE"),
        (603, "CALL_REJECTED"),
        (404, "UNALLOCATED_NUMBER"),
    ],
)
async def test_the_switch_derives_its_cause_from_the_status(traced, status, cause):
    runtime, _trace = traced
    ua = await UserAgent.create(runtime, Account(user="1003", password="bench1234", domain=DOMAIN))
    await ua.register()
    incoming: asyncio.Queue = asyncio.Queue()
    ua.on_incoming(incoming.put_nowait)

    originate = asyncio.create_task(fs_cli("originate user/1003 &echo()"))
    call = await asyncio.wait_for(incoming.get(), 10)
    await call.reject(status=status)

    assert await originate == f"-ERR {cause}"


async def through_the_proxy(runtime):
    """Ring an agent registered at the proxy, on its sips: route form."""
    callee = await UserAgent.create(runtime, Account(user="1004", password="", domain=PROXY))
    await callee.register()
    caller = await UserAgent.create(
        runtime,
        Account(user="1003", password="", domain=PROXY, reg_interval=0, transport="tls"),
    )
    incoming: asyncio.Queue = asyncio.Queue()
    callee.on_incoming(incoming.put_nowait)
    placed = await caller.dial(f"sip:1004@127.0.0.1:{SIPS_PORT};transport=tls")
    return placed, await asyncio.wait_for(incoming.get(), 10)


async def test_a_refusal_arrives_through_the_proxy(traced):
    runtime, trace = traced
    placed, received = await through_the_proxy(runtime)

    await received.reject(status=480, reason="No agent free", headers={"Retry-After": "30"})

    with pytest.raises(CallFailed) as refusal:
        await placed.wait_established(timeout=10)
    assert (refusal.value.status, refusal.value.reason) == (480, "No agent free")
    assert "\r\nRetry-After: 30\r\n" in trace.first("SIP/2.0 480")


async def test_a_proxy_does_not_pass_503_on(traced):
    # RFC 3261 16.7 tells a proxy not to forward a 503, which speaks for
    # the server that sent it and not for the destination; Kamailio turns
    # it into a 500. A refusal that must cross a proxy as itself needs
    # another status.
    runtime, _trace = traced
    placed, received = await through_the_proxy(runtime)

    await received.reject(status=503)

    with pytest.raises(CallFailed) as refusal:
        await placed.wait_established(timeout=10)
    assert refusal.value.status == 500


async def test_hangup_headers_arrive_through_the_proxy(traced):
    runtime, trace = traced
    placed, received = await through_the_proxy(runtime)
    await received.answer()
    await placed.wait_established(timeout=10)
    await received.wait_established(timeout=10)

    await received.hangup(headers={"X-Disposition": "resolved"})

    for _ in range(60):
        if trace.first("BYE "):
            break
        await asyncio.sleep(0.05)
    assert "\r\nX-Disposition: resolved\r\n" in trace.first("BYE ")
