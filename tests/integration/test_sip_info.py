#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""SIP INFO received inside a call, against the FreeSWITCH bench.

FreeSWITCH calls the registered user and, on command, sends an INFO
into the live call, the way a PBX passes an application event to an
endpoint. The reply the bot sends is read from its own SIP trace. Run
with:

    pytest -m bench tests/integration
"""

import asyncio
import logging
import os
import re
import subprocess
import uuid

import pytest

pytest.importorskip("baresip._native")

from baresip import Account, Config, InfoEvent
from baresip.runtime import Runtime
from baresip.ua import UserAgent

pytestmark = pytest.mark.bench

DOMAIN = f"127.0.0.1:{os.environ.get('BENCH_SIP_PORT', '15060')}"
CONTAINER = "baresip-bench-freeswitch"
# FreeSWITCH sends INFO only to an endpoint that advertised support for
# its own extension, unless the channel says to send it regardless.
ORIGINATE = (
    "bgapi originate {{origination_uuid={uuid},fs_send_unsupported_info=true}}user/1003 &echo()"
)
INFO_REPLY = re.compile(r"^SIP/2\.0 (\d{3}) [^\r\n]*[\s\S]*?^CSeq: *\d+ INFO", re.MULTILINE)


async def fs_cli(command: str) -> str:
    result = await asyncio.to_thread(
        subprocess.run,
        ["docker", "exec", CONTAINER, "fs_cli", "-x", command],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.stdout


class SipTrace(logging.Handler):
    """The SIP messages the stack sent, as its trace logged them."""

    def __init__(self):
        super().__init__()
        self.sent: list[str] = []

    def emit(self, record):
        message = record.getMessage()
        if message.startswith("TX "):
            self.sent.append(message.split("\n", 1)[-1])

    def info_replies(self) -> list[int]:
        return [int(m.group(1)) for text in self.sent if (m := INFO_REPLY.match(text))]


@pytest.fixture
async def bench_call():
    """A live call from FreeSWITCH, its FreeSWITCH id, and our SIP trace."""
    trace = SipTrace()
    sip_logger = logging.getLogger("baresip.native.sip")
    level = sip_logger.level
    sip_logger.addHandler(trace)
    sip_logger.setLevel(logging.DEBUG)
    runtime = Runtime()
    await runtime.start(Config(net_interface="127.0.0.1", sip_trace=True))
    try:
        account = Account(user="1003", password="bench1234", domain=DOMAIN)
        ua = await UserAgent.create(runtime, account)
        await ua.register()
        incoming: asyncio.Queue = asyncio.Queue()
        ua.on_incoming(incoming.put_nowait)
        channel = str(uuid.uuid4())
        await fs_cli(ORIGINATE.format(uuid=channel))
        call = await asyncio.wait_for(incoming.get(), 10)
        await call.answer()
        await call.wait_established()
        yield call, channel, trace
        await fs_cli(f"uuid_kill {channel}")
    finally:
        await runtime.close()
        sip_logger.removeHandler(trace)
        sip_logger.setLevel(level)


async def until(condition, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        assert asyncio.get_running_loop().time() < deadline, "condition not met in time"
        await asyncio.sleep(0.02)


async def test_an_info_reaches_the_listener_and_is_answered_ok(bench_call):
    call, channel, trace = bench_call
    received: list[InfoEvent] = []
    call.on_info(received.append)

    result = await fs_cli(f"uuid_send_info {channel} application x-pbx-event recording=on")
    assert "+OK" in result

    await until(lambda: received and trace.info_replies())
    assert received == [InfoEvent("application/x-pbx-event", b"recording=on")]
    assert trace.info_replies() == [200]


async def test_an_info_nobody_listens_for_is_still_answered_ok(bench_call):
    _call, channel, trace = bench_call

    await fs_cli(f"uuid_send_info {channel} application x-pbx-event queue=sales")

    await until(trace.info_replies)
    assert trace.info_replies() == [200]


async def test_every_info_is_delivered_in_order_until_the_listener_is_removed(bench_call):
    call, channel, trace = bench_call
    received: list[InfoEvent] = []
    call.on_info(received.append)

    for body in ("first", "second"):
        await fs_cli(f"uuid_send_info {channel} application x-pbx-event {body}")
    # With no type given FreeSWITCH labels the body as its own data type.
    await fs_cli(f"uuid_send_info {channel} third")
    await until(lambda: len(received) == 3)
    call.off_info(received.append)
    await fs_cli(f"uuid_send_info {channel} application x-pbx-event fourth")
    await until(lambda: len(trace.info_replies()) == 4)

    assert [(e.content_type, e.body) for e in received] == [
        ("application/x-pbx-event", b"first"),
        ("application/x-pbx-event", b"second"),
        ("freeswitch/data", b"third"),
    ]
    assert trace.info_replies() == [200, 200, 200, 200]


async def test_a_body_longer_than_one_event_value_arrives_whole(bench_call):
    call, channel, _trace = bench_call
    received: list[InfoEvent] = []
    call.on_info(received.append)
    body = "0123456789" * 90  # as much as one FreeSWITCH command carries

    await fs_cli(f"uuid_send_info {channel} application x-pbx-event {body}")

    await until(lambda: received)
    assert received[0].body == body.encode()
