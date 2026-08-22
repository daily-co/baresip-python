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

import pytest

native = pytest.importorskip("baresip._native")

from baresip import Event, StackEvent, UnsupportedFeatureError
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
    return Call(Runtime(), INCOMING)


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
