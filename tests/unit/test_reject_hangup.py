#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""How a call is refused or ended: the status, the reason and the headers.

Real calls between two agents on loopback. What the far end is told is
read two ways: from the failure the caller's own call raises, and from
the SIP messages the stack logs as received.
"""

import asyncio
import itertools
import logging

import pytest

native = pytest.importorskip("baresip._native")

from baresip import (
    Account,
    CallBusy,
    CallDeclined,
    CallFailed,
    CallState,
    Config,
    Event,
    Runtime,
    UserAgent,
)

lib = native.lib

# A closed runtime's SIP socket lingers in-process; sequential tests
# each take a fresh port.
_PORTS = itertools.count(5485)


class Received(logging.Handler):
    """The SIP messages the stack received, as its trace logged them.

    The trace is delivered by the log thread, apart from call events, so
    a message can be logged after the call event it caused.
    """

    def __init__(self):
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record):
        message = record.getMessage()
        if message.startswith("RX "):
            self.messages.append(message.split("\n", 1)[-1])

    async def first(self, start: str, timeout: float = 3.0) -> str:
        """The first received message whose start line begins with ``start``.

        Waits for it to be logged; empty when none is within ``timeout``.
        """
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            found = next((m for m in self.messages if m.startswith(start)), "")
            if found or asyncio.get_running_loop().time() >= deadline:
                return found
            await asyncio.sleep(0.01)


@pytest.fixture
async def pair():
    """A caller and a bot on loopback, an incoming-call queue, and the trace."""
    own = f"127.0.0.1:{next(_PORTS)}"
    trace = Received()
    sip_logger = logging.getLogger("baresip.native.sip")
    level = sip_logger.level
    sip_logger.addHandler(trace)
    sip_logger.setLevel(logging.DEBUG)
    runtime = Runtime()
    await runtime.start(
        Config(net_interface="127.0.0.1", sip_trace=True, extra_config_text=f"sip_listen {own}\n")
    )
    caller = await UserAgent.create(
        runtime, Account(user="caller", password="", domain=own, reg_interval=0)
    )
    bot = await UserAgent.create(
        runtime, Account(user="bot", password="", domain=own, reg_interval=0)
    )
    incoming: asyncio.Queue = asyncio.Queue()
    bot.on_incoming(incoming.put_nowait)

    async def ring():
        placed = await caller.dial(f"sip:bot@{own}")
        return placed, await asyncio.wait_for(incoming.get(), 5)

    yield ring, trace
    await runtime.close()
    sip_logger.removeHandler(trace)
    sip_logger.setLevel(level)


async def connect(ring):
    placed, received = await ring()
    await received.answer()
    await placed.wait_established(timeout=5)
    await received.wait_established(timeout=5)
    return placed, received


async def until_closed(call, timeout: float = 3.0) -> None:
    done = asyncio.get_running_loop().create_future()

    def listener(event):
        if event.event is Event.CALL_CLOSED and not done.done():
            done.set_result(None)

    call.on(listener)
    if call.state is not CallState.CLOSED:
        await asyncio.wait_for(done, timeout)


async def test_reject_answers_busy_by_default(pair):
    ring, _trace = pair
    placed, received = await ring()

    await received.reject()

    with pytest.raises(CallBusy) as refusal:
        await placed.wait_established(timeout=5)
    assert (refusal.value.status, refusal.value.reason) == (486, "Busy Here")


@pytest.mark.parametrize(
    ("status", "phrase"),
    [
        (480, "Temporarily Unavailable"),
        (503, "Service Unavailable"),
        (603, "Decline"),
        (404, "Not Found"),
        (499, "Request Failure"),  # no phrase of its own: the class's
    ],
)
async def test_reject_sends_the_status_with_its_standard_phrase(pair, status, phrase):
    ring, _trace = pair
    placed, received = await ring()

    await received.reject(status=status)

    with pytest.raises(CallFailed) as refusal:
        await placed.wait_established(timeout=5)
    assert (refusal.value.status, refusal.value.reason) == (status, phrase)
    assert isinstance(refusal.value, CallDeclined) is (status == 603)


async def test_reject_sends_the_callers_own_reason(pair):
    ring, _trace = pair
    placed, received = await ring()

    await received.reject(status=480, reason="Agent on a break")

    with pytest.raises(CallFailed) as refusal:
        await placed.wait_established(timeout=5)
    assert (refusal.value.status, refusal.value.reason) == (480, "Agent on a break")


@pytest.mark.parametrize(
    "reason",
    [
        "100% busy",
        "Busy 100%d now",
        "Agent busy %s%s%s%s%s%s%s%s",
        "%n%n%n%n",
        "%H %r %m %w %v %b %j",
        "%",
    ],
)
async def test_a_reason_is_never_read_as_a_format(pair, reason):
    """The reason is the application's text: it reaches the wire, and the
    rejecting side's own close event, exactly as written."""
    ring, _trace = pair
    placed, received = await ring()
    closed: list[str] = []
    received.on(
        lambda event: closed.append(event.text) if event.event is Event.CALL_CLOSED else None
    )

    await received.reject(status=480, reason=reason)

    with pytest.raises(CallFailed) as refusal:
        await placed.wait_established(timeout=5)
    assert (refusal.value.status, refusal.value.reason) == (480, reason)
    await until_closed(received)
    assert closed == [reason]


async def test_a_reason_with_headers_is_never_read_as_a_format(pair):
    ring, trace = pair
    placed, received = await ring()
    closed: list[str] = []
    received.on(
        lambda event: closed.append(event.text) if event.event is Event.CALL_CLOSED else None
    )

    await received.reject(status=480, reason="50%s off %n", headers={"X-Note": "100% %s"})

    with pytest.raises(CallFailed) as refusal:
        await placed.wait_established(timeout=5)
    assert refusal.value.reason == "50%s off %n"
    assert "\r\nX-Note: 100% %s\r\n" in await trace.first("SIP/2.0 480")
    await until_closed(received)
    assert closed == ["50%s off %n"]


async def test_reject_headers_arrive_as_written(pair):
    ring, trace = pair
    placed, received = await ring()

    # Text that a printf-style format would act on must arrive untouched.
    await received.reject(status=503, headers={"Retry-After": "30", "X-Note": "100% %s %n %H"})

    with pytest.raises(CallFailed):
        await placed.wait_established(timeout=5)
    response = await trace.first("SIP/2.0 503")
    assert "\r\nRetry-After: 30\r\n" in response
    assert "\r\nX-Note: 100% %s %n %H\r\n" in response
    assert response.count("Content-Length:") == 1


async def test_hangup_headers_ride_the_bye(pair):
    ring, trace = pair
    placed, received = await connect(ring)

    await received.hangup(headers={"Reason": 'Q.850;cause=16;text="resolved"', "X-Queue": "sales"})

    await until_closed(placed)
    bye = await trace.first("BYE ")
    assert '\r\nReason: Q.850;cause=16;text="resolved"\r\n' in bye
    assert "\r\nX-Queue: sales\r\n" in bye


async def test_hangup_without_headers_sends_a_bare_bye(pair):
    ring, trace = pair
    placed, received = await connect(ring)

    await received.hangup()

    await until_closed(placed)
    bye = await trace.first("BYE ")
    assert bye
    assert "X-" not in bye and "Reason:" not in bye


async def test_hangup_headers_ride_the_rejection_of_an_unanswered_call(pair):
    ring, trace = pair
    placed, received = await ring()

    await received.hangup(headers={"X-Disposition": "no-agent"})

    with pytest.raises(CallBusy):
        await placed.wait_established(timeout=5)
    assert "\r\nX-Disposition: no-agent\r\n" in await trace.first("SIP/2.0 486")


async def test_hanging_up_an_unanswered_outgoing_call_cancels_it(pair):
    ring, trace = pair
    placed, received = await ring()

    await placed.hangup(headers={"X-Why": "changed my mind"})

    await until_closed(received)
    cancel = await trace.first("CANCEL ")
    assert cancel
    assert "X-Why" not in cancel  # a cancellation carries no headers


async def test_reject_on_an_answered_call_ends_it(pair):
    ring, trace = pair
    placed, received = await connect(ring)

    await received.reject(status=603)

    await until_closed(placed)
    assert await trace.first("BYE ")


@pytest.mark.parametrize("status", [0, 180, 200, 299, 700])
async def test_reject_refuses_a_status_that_is_not_a_refusal(pair, status):
    ring, _trace = pair
    placed, received = await ring()

    with pytest.raises(ValueError, match="3xx-6xx"):
        await received.reject(status=status)

    assert received.state is not CallState.CLOSED
    await placed.hangup()


@pytest.mark.parametrize(
    "arguments",
    [
        {"reason": "two\r\nlines"},
        {"reason": ""},
        {"headers": {"X-Bad Name": "value"}},
        {"headers": {"X-Bad": "two\nlines"}},
        {"headers": {"": "value"}},
        {"headers": {"X-Long": "x" * lib.BP_CLOSE_HEADERS_MAX}},
    ],
)
async def test_bad_arguments_are_refused_and_leave_the_call_alone(pair, arguments):
    ring, _trace = pair
    placed, received = await ring()

    with pytest.raises(ValueError):
        await received.reject(status=486, **arguments)

    assert received.state is not CallState.CLOSED
    await placed.hangup()
