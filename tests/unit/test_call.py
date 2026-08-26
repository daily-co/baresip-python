#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""Call, network-free: the snapshot, the state machine, and eager errors.

The state machine is pure Python driven by events, so it is tested here by
feeding events directly. Real signaling — answer to ESTABLISHED, RTP,
hangup races — needs a peer and lives in tests/integration (-m bench).
"""

import asyncio

import pytest

native = pytest.importorskip("baresip._native")

from baresip import BaresipError, CallFailed, Event, StackEvent, UnsupportedFeatureError
from baresip.call import Call, CallState
from baresip.runtime import Runtime

INCOMING = StackEvent(
    event=Event.CALL_INCOMING,
    ua=7,
    call=9,
    peer="sip:9999@example.invalid",
    call_id="abc@bench",
    headers={"X-Customer-Id": "42"},
)


def incoming_call() -> Call:
    # An unstarted Runtime supports subscribe/unsubscribe, which is all
    # the state machine needs.
    return Call._from_incoming(Runtime(), INCOMING)


def event_for(call: Call, kind: Event) -> StackEvent:
    return StackEvent(event=kind, ua=7, call=call.handle)


def test_snapshot_from_the_incoming_event():
    call = incoming_call()
    assert call.state is CallState.INCOMING
    assert call.handle == 9
    assert call.peer == "sip:9999@example.invalid"
    assert call.call_id == "abc@bench"
    assert call.headers == {"X-Customer-Id": "42"}


async def test_answer_rejects_headers_eagerly():
    call = incoming_call()
    with pytest.raises(UnsupportedFeatureError):
        await call.answer(headers={"X-Reply": "1"})
    assert call.state is CallState.INCOMING  # nothing was sent


async def test_hold_requires_an_established_call():
    call = incoming_call()
    with pytest.raises(BaresipError, match="requires an established call"):
        await call.hold()


async def test_hold_and_resume_no_change_paths_send_nothing():
    # The unstarted Runtime raises on any command, so returning cleanly
    # proves the no-change paths never touch the stack.
    call = incoming_call()
    call._on_stack_event(event_for(call, Event.CALL_ESTABLISHED))
    await call.resume()  # not held: nothing to do
    call._on_hold = True
    await call.hold()  # already held: nothing to do


def test_remote_hold_tracked_by_events():
    call = incoming_call()
    call._on_stack_event(event_for(call, Event.CALL_ESTABLISHED))
    assert not call.remote_on_hold
    call._on_stack_event(event_for(call, Event.CALL_HOLD))
    assert call.remote_on_hold
    call._on_stack_event(event_for(call, Event.CALL_RESUME))
    assert not call.remote_on_hold


async def test_transfer_requires_an_established_call():
    call = incoming_call()
    with pytest.raises(BaresipError, match="requires an established call"):
        await call.transfer("sip:9196@example.invalid")


async def test_transfer_guards_input_and_serializes():
    call = incoming_call()
    call._on_stack_event(event_for(call, Event.CALL_ESTABLISHED))
    for uri in ("", "sip:a@b\nc", "sip:a b"):
        with pytest.raises(ValueError):
            await call.transfer(uri)
    # The stack tracks one REFER subscription per call, so a second
    # transfer while one is pending must refuse instead of silently
    # replacing the first.
    call._transfer_pending = True
    with pytest.raises(BaresipError, match="already in progress"):
        await call.transfer("sip:9196@example.invalid")


def test_transfer_success_close_text_is_not_an_error_status():
    exc = CallFailed.from_close_reason("Call transfered")
    assert exc.status is None
    assert "transferred" in str(exc)


async def test_attended_transfer_guards():
    call = incoming_call()
    other = Call(Runtime(), handle=10, ua_handle=7, state=CallState.INCOMING)
    with pytest.raises(ValueError, match="itself"):
        await call.attended_transfer(call)
    with pytest.raises(BaresipError, match="both calls established"):
        await call.attended_transfer(other)  # neither is established
    call._on_stack_event(event_for(call, Event.CALL_ESTABLISHED))
    with pytest.raises(BaresipError, match="both calls established"):
        await call.attended_transfer(other)  # the consult leg is not
    other._on_stack_event(StackEvent(event=Event.CALL_ESTABLISHED, ua=7, call=10))
    call._transfer_pending = True
    with pytest.raises(BaresipError, match="already in progress"):
        await call.attended_transfer(other)


def test_state_advances_only_by_matching_events():
    call = incoming_call()
    foreign = StackEvent(event=Event.CALL_ESTABLISHED, ua=7, call=999)
    call._on_stack_event(foreign)
    assert call.state is CallState.INCOMING, "another call's event must not move state"
    call._on_stack_event(event_for(call, Event.CALL_ESTABLISHED))
    assert call.state is CallState.ESTABLISHED
    call._on_stack_event(event_for(call, Event.CALL_CLOSED))
    assert call.state is CallState.CLOSED


def test_call_listeners_are_isolated():
    call = incoming_call()
    received = []

    def bad(_event):
        raise RuntimeError("misbehaving listener")

    call.on(bad)
    call.on(received.append)
    call._on_stack_event(event_for(call, Event.CALL_ESTABLISHED))
    assert len(received) == 1
    call.off(received.append)
    call._on_stack_event(event_for(call, Event.CALL_CLOSED))
    assert len(received) == 1


# -- outbound: failure mapping and wait_established --------------------------


def test_close_reason_mapping():
    from baresip import CallBusy, CallDeclined, CallFailed

    busy = CallFailed.from_close_reason("486 Busy Here")
    assert isinstance(busy, CallBusy) and busy.status == 486 and busy.reason == "Busy Here"
    declined = CallFailed.from_close_reason("603 Decline")
    assert isinstance(declined, CallDeclined) and declined.status == 603
    other = CallFailed.from_close_reason("404 Not Found")
    assert type(other) is CallFailed and other.status == 404
    transport = CallFailed.from_close_reason("Connection reset by peer")
    assert type(transport) is CallFailed and transport.status is None


def outgoing_call() -> Call:
    return Call(Runtime(), handle=11, ua_handle=7, state=CallState.OUTGOING, peer="sip:x@y")


async def test_wait_established_returns_when_established():
    call = outgoing_call()
    call._on_stack_event(StackEvent(event=Event.CALL_ESTABLISHED, ua=7, call=11))
    await call.wait_established(timeout=0.1)  # returns immediately


async def test_wait_established_raises_mapped_error_after_close():
    from baresip import CallBusy

    call = outgoing_call()
    call._on_stack_event(StackEvent(event=Event.CALL_CLOSED, ua=7, call=11, text="486 Busy Here"))
    with pytest.raises(CallBusy):
        await call.wait_established(timeout=0.1)


async def test_wait_established_raises_while_waiting():
    from baresip import CallDeclined

    call = outgoing_call()

    async def close_soon():
        await asyncio.sleep(0.01)
        call._on_stack_event(StackEvent(event=Event.CALL_CLOSED, ua=7, call=11, text="603 Decline"))

    asyncio.get_running_loop().create_task(close_soon())
    with pytest.raises(CallDeclined):
        await call.wait_established(timeout=1)


async def test_wait_established_timeout_maps_to_calltimeout():
    from baresip import CallTimeout

    call = outgoing_call()  # nothing will ever answer; hangup attempt is best-effort
    with pytest.raises(CallTimeout):
        await call.wait_established(timeout=0.05)
