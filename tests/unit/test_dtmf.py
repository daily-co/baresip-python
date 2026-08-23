#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""DTMF: input validation and digit pairing, without a network.

Real digits over the wire live in the bench tests; these pin what
send_dtmf refuses, how the start/end reports pair into DigitEvents, and
the listener add/remove mechanics.
"""

import pytest

native = pytest.importorskip("baresip._native")

from baresip.call import Call, CallState, DigitEvent
from baresip.errors import StaleHandleError
from baresip.events import Event, StackEvent
from baresip.runtime import Runtime


@pytest.fixture
async def call():
    rt = Runtime()
    await rt.start()
    yield Call(rt, handle=0x123, ua_handle=0x1, state=CallState.ESTABLISHED)
    await rt.close()


async def test_send_dtmf_rejects_what_is_not_a_digit(call):
    for digits in ("1E", "hello", "1 2", "+1"):
        with pytest.raises(ValueError):
            await call.send_dtmf(digits)
    with pytest.raises(ValueError):
        await call.send_dtmf("1", interdigit_ms=-1)


async def test_send_dtmf_accepts_lowercase_and_fails_typed_on_a_dead_call(call):
    # 'a' upcases to a valid digit, so validation passes — and the fake
    # handle then fails typed at the stack, proving the digit reached it.
    with pytest.raises(StaleHandleError):
        await call.send_dtmf("a")


async def test_digit_reports_pair_into_events(call):
    got: list[DigitEvent] = []
    call.on_dtmf(got.append)

    call._on_stack_event(StackEvent(event=Event.CALL_DTMF_START, call=0x123, text="5"))
    call._on_stack_event(StackEvent(event=Event.CALL_DTMF_END, call=0x123))
    assert [d.digit for d in got] == ["5"]
    assert got[0].duration_ms >= 0

    # An end without a start carries no digit and is dropped.
    call._on_stack_event(StackEvent(event=Event.CALL_DTMF_END, call=0x123))
    assert len(got) == 1

    # Another call's digits are not ours.
    call._on_stack_event(StackEvent(event=Event.CALL_DTMF_START, call=0x999, text="7"))
    call._on_stack_event(StackEvent(event=Event.CALL_DTMF_END, call=0x999))
    assert len(got) == 1

    call.off_dtmf(got.append)
    call._on_stack_event(StackEvent(event=Event.CALL_DTMF_START, call=0x123, text="6"))
    call._on_stack_event(StackEvent(event=Event.CALL_DTMF_END, call=0x123))
    assert len(got) == 1
    call.off_dtmf(got.append)  # unknown callbacks are ignored
