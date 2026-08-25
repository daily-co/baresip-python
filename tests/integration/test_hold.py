#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""Hold and resume against the FreeSWITCH bench (`make bench-up` first).

Hold travels as an in-dialog re-INVITE, and in-dialog requests go to the
Contact FreeSWITCH advertises — which stays 15060 no matter what
BENCH_SIP_PORT remaps (see bench/README.md) — so these tests run on the
default bench port.
"""

import asyncio
import os

import pytest

native = pytest.importorskip("baresip._native")

from test_telephony_gates import capture_sip_trace, fs_cli, parse_sip_records

from baresip import Account, AudioNotActive, AudioRestarted, Event
from baresip.runtime import Runtime
from baresip.ua import UserAgent

pytestmark = pytest.mark.bench

DOMAIN = f"127.0.0.1:{os.environ.get('BENCH_SIP_PORT', '15060')}"


@pytest.fixture
async def echo_call(tmp_path):
    """An established call to the echo service, with SIP tracing on."""
    runtime = Runtime()
    await runtime.start(
        f"net_interface 127.0.0.1\naudio_source ausine,440\naudio_player aufile,{tmp_path}/rx.wav\n"
    )
    await runtime.set_sip_trace(True)
    ua = await UserAgent.create(runtime, Account(user="1003", password="bench1234", domain=DOMAIN))
    await ua.register()
    call = await ua.dial(f"sip:9196@{DOMAIN}")
    await call.wait_established()
    try:
        yield call
    finally:
        await runtime.close()


async def rx_bytes(call, seconds: float) -> int:
    """Bytes the RX tap delivers over a window. The hold re-INVITEs can
    restart the audio streams; AudioRestarted just rebinds, so reads
    across a hold cycle either deliver or raise typed — never corrupt."""
    total = 0
    deadline = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < deadline:
        try:
            total += len(call.audio.read(3200))
        except (AudioNotActive, AudioRestarted):
            pass
        await asyncio.sleep(0.05)
    return total


async def wait_rx_flowing(call, timeout: float = 8.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if await rx_bytes(call, 0.2):
            return
    raise AssertionError("echo audio never arrived")


async def wait_traced(records, direction: str, prefix: str, timeout: float = 5.0) -> None:
    """The trace drains through the log reader, so records trail the SIP
    exchange slightly; poll for the one we need."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if any(d == direction and s.startswith(prefix) for d, s, *_ in parse_sip_records(records)):
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"no {direction} {prefix!r} in the SIP trace")


async def test_local_hold_pauses_and_resume_restores(echo_call):
    call = echo_call
    await wait_rx_flowing(call)

    with capture_sip_trace() as records:
        await call.hold()
        assert call.is_on_hold
        await wait_traced(records, "TX", "INVITE ")  # the re-INVITE
        await wait_traced(records, "RX", "SIP/2.0 200")
    await call.hold()  # already held: a no-op, not an error

    await rx_bytes(call, 0.5)  # drain audio that was in flight
    held = await rx_bytes(call, 1.0)
    assert held <= 3200, f"echo still flowing on hold ({held} bytes)"

    with capture_sip_trace() as records:
        await call.resume()
        assert not call.is_on_hold
        await wait_traced(records, "TX", "INVITE ")
    await call.resume()  # not held: a no-op
    await wait_rx_flowing(call)

    await call.hangup()


async def test_remote_hold_surfaces_as_events(echo_call):
    call = echo_call
    await wait_rx_flowing(call)
    loop = asyncio.get_running_loop()
    held, resumed = loop.create_future(), loop.create_future()

    def listener(event):
        if event.event is Event.CALL_HOLD and not held.done():
            held.set_result(None)
        elif event.event is Event.CALL_RESUME and not resumed.done():
            resumed.set_result(None)

    call.on(listener)
    channels = await asyncio.to_thread(fs_cli, "show channels")
    uuid = next(line.split(",")[0] for line in channels.splitlines()[1:] if "-" in line)

    await asyncio.to_thread(fs_cli, f"uuid_hold {uuid}")
    await asyncio.wait_for(held, 10)
    assert call.remote_on_hold

    await asyncio.to_thread(fs_cli, f"uuid_hold off {uuid}")
    await asyncio.wait_for(resumed, 10)
    assert not call.remote_on_hold

    await call.hangup()
