#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""Inbound calls against the FreeSWITCH bench (`make bench-up` first).

The bench originates calls to account 1003 (reserved for tests) via
fs_cli inside the container. Audio: ausine generates the outgoing tone and
aufile records the incoming side — the gates check signaling states and
RTP flowing, not audio content.

Run with: pytest -m bench tests/integration
"""

import asyncio
import os
import subprocess

import pytest

native = pytest.importorskip("baresip._native")
lib = native.lib

from baresip import Account, Event, StaleHandleError
from baresip.call import Call, CallState
from baresip.runtime import Runtime
from baresip.ua import UserAgent

pytestmark = pytest.mark.bench

DOMAIN = f"127.0.0.1:{os.environ.get('BENCH_SIP_PORT', '15060')}"
CONTAINER = "baresip-bench-freeswitch"
ORIGINATE = (
    "bgapi originate {origination_caller_id_number=9999,sip_h_X-Customer-Id=42}user/1003 &echo()"
)


async def fs_cli(command: str) -> str:
    result = await asyncio.to_thread(
        subprocess.run,
        ["docker", "exec", CONTAINER, "fs_cli", "-x", command],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.stdout


def watch(call: Call, kind: Event) -> asyncio.Future:
    """A future resolved by the call's next event of the given kind."""
    future = asyncio.get_running_loop().create_future()

    def listener(event):
        if event.event is kind and not future.done():
            future.set_result(event)

    call.on(listener)
    return future


@pytest.fixture
async def bench_ua(tmp_path):
    runtime = Runtime()
    # net_interface pins the stack to loopback, where the bench lives —
    # otherwise the LAN address wins and calls need a route baresip
    # refuses to build toward a loopback registrar.
    await runtime.start(
        "net_interface 127.0.0.1\n"
        # ausine's "device" is the tone frequency; without one it inherits
        # the platform default device string and refuses to start.
        f"audio_source ausine,440\naudio_player aufile,{tmp_path}/rx.wav\n"
    )
    # The raw-text config path skips Config's post-handshake commands, so
    # set the header allowlist directly — before the UA exists, since the
    # capture filter is installed at allocation.
    await runtime.cmd(lib.BP_CMD_SET_EXPOSE_HEADERS, args="X-Customer-Id")
    ua = await UserAgent.create(runtime, Account(user="1003", password="bench1234", domain=DOMAIN))
    await ua.register()
    incoming: asyncio.Queue = asyncio.Queue()
    ua.on_incoming(incoming.put_nowait)
    try:
        yield ua, incoming
    finally:
        await fs_cli("hupall NORMAL_CLEARING")  # drop any leftover bench legs
        await runtime.close()


async def test_answer_established_and_remote_hangup(bench_ua):
    _ua, incoming = bench_ua
    await fs_cli(ORIGINATE)

    call = await asyncio.wait_for(incoming.get(), 10)
    assert call.state is CallState.INCOMING
    assert "9999" in (call.peer or "")
    assert call.headers == {"X-Customer-Id": "42"}, "allowlisted INVITE header must surface"

    established = watch(call, Event.CALL_ESTABLISHED)
    rtp = watch(call, Event.CALL_RTPESTAB)
    closed = watch(call, Event.CALL_CLOSED)
    await call.answer()
    await asyncio.wait_for(established, 10)
    assert call.state is CallState.ESTABLISHED
    await asyncio.wait_for(rtp, 10)  # RTP flowing both ways through the bench

    await fs_cli("hupall NORMAL_CLEARING")  # the far end hangs up
    await asyncio.wait_for(closed, 10)
    assert call.state is CallState.CLOSED

    # A late answer on the closed call degrades to a typed error.
    with pytest.raises(StaleHandleError):
        await call.answer()


async def test_local_hangup(bench_ua):
    _ua, incoming = bench_ua
    await fs_cli(ORIGINATE)
    call = await asyncio.wait_for(incoming.get(), 10)
    established = watch(call, Event.CALL_ESTABLISHED)
    closed = watch(call, Event.CALL_CLOSED)
    await call.answer()
    await asyncio.wait_for(established, 10)
    await call.hangup()
    await asyncio.wait_for(closed, 10)
    assert call.state is CallState.CLOSED


async def test_reject_is_busy(bench_ua):
    _ua, incoming = bench_ua
    await fs_cli(ORIGINATE)
    call = await asyncio.wait_for(incoming.get(), 10)
    closed = watch(call, Event.CALL_CLOSED)
    await call.reject()
    await asyncio.wait_for(closed, 10)
    assert call.state is CallState.CLOSED
    # The 486 must reach the far end: the bench leg ends USER_BUSY.
    for _ in range(20):
        log = await fs_cli("show calls count")
        if "0 total" in log:
            break
        await asyncio.sleep(0.25)
