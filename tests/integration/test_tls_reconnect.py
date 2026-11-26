#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""A request sent after the TLS connection of a call has closed.

A server keeps an idle connection only so long, and a call can be quiet
on its signalling for many minutes. The next request, a hang-up for one,
then opens a new connection to the route the call was given, and that
connection is verified like any other. Servers often name themselves in
a route by something their certificate does not carry, an address above
all.

The proxy's port 15075 routes a call by ``localtest.me``, a public name
for 127.0.0.1 that the bench certificate does not carry.

    make bench-up
    pytest -m bench tests/integration
"""

import asyncio
import socket
import subprocess
import time

import pytest

pytest.importorskip("baresip._native")

from test_proxy_routes import (  # noqa: F401  (the fixture restarts the proxy)
    PROXY_CONTAINER,
    REGISTRAR,
    closed,
    proxy,
    proxy_answers,
)
from test_telephony_gates import BENCH_CERT

from baresip import Account, Config
from baresip.runtime import Runtime
from baresip.ua import UserAgent

pytestmark = pytest.mark.bench

ROUTE_HOST = "localtest.me"


def drop_the_proxys_connections() -> None:
    """Restart the proxy: every connection it held is gone, as after a
    server's idle timeout, and it routes requests inside a call as before."""
    subprocess.run(["docker", "restart", PROXY_CONTAINER], check=True, capture_output=True)
    deadline = time.monotonic() + 30
    while not proxy_answers():
        assert time.monotonic() < deadline, "the bench proxy did not come back"
        time.sleep(0.5)


@pytest.fixture
async def runtime(monkeypatch):
    try:
        resolved = socket.gethostbyname(ROUTE_HOST)
    except OSError:
        resolved = ""
    if resolved != "127.0.0.1":
        pytest.skip(f"{ROUTE_HOST} does not resolve to 127.0.0.1 here")
    monkeypatch.setenv("SIP_CA_FILE", str(BENCH_CERT))
    rt = Runtime()
    await rt.start(
        Config(
            net_interface="127.0.0.1",
            max_concurrent_calls=None,
            extra_config_text="dns_getaddrinfo yes\n",
        )
    )
    yield rt
    await rt.close()


async def call_through(runtime, port: int):
    """A call placed over TLS through the proxy's ``port``: (caller's call, answerer's)."""
    answering = await UserAgent.create(runtime, Account(user="1004", password="", domain=REGISTRAR))
    await answering.register()
    calling = await UserAgent.create(
        runtime,
        Account(user="1003", password="", domain=REGISTRAR, reg_interval=0, transport="tls"),
    )
    incoming: asyncio.Queue = asyncio.Queue()
    answering.on_incoming(incoming.put_nowait)
    placed = await calling.dial(f"sip:1004@127.0.0.1:{port};transport=tls")
    received = await asyncio.wait_for(incoming.get(), 10)
    await received.answer()
    await placed.wait_established(timeout=10)
    await received.wait_established(timeout=10)
    return placed, received


async def test_a_hangup_arrives_over_a_new_connection_to_a_route_the_certificate_names(runtime):
    # Port 15072 routes by 127.0.0.1, which the bench certificate names.
    placed, received = await call_through(runtime, 15072)

    await asyncio.to_thread(drop_the_proxys_connections)
    gone = closed(received)
    await placed.hangup()

    await asyncio.wait_for(gone, 8)


@pytest.mark.xfail(
    strict=True,
    reason="a route that gives a host name is verified against that name: "
    "the hang-up is not sent when the certificate does not carry it",
)
async def test_a_hangup_arrives_over_a_new_connection_to_a_route_the_certificate_does_not_name(
    runtime,
):
    placed, received = await call_through(runtime, 15075)

    await asyncio.to_thread(drop_the_proxys_connections)
    gone = closed(received)
    await placed.hangup()

    await asyncio.wait_for(gone, 8)
