#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""SIP INFO requests as the application sees them.

How the native event decodes and how a call hands it to ``on_info``
listeners. The request and its reply on the wire are covered by the
bench tests.
"""

import base64
import json

import pytest

native = pytest.importorskip("baresip._native")

from baresip import Event, InfoEvent
from baresip.call import Call, CallState
from baresip.events import SHIM_EVENT_BASE, StackEvent
from baresip.runtime import Runtime

lib = native.lib


@pytest.fixture
async def runtime():
    rt = Runtime()
    await rt.start()
    yield rt
    await rt.close()


def test_the_event_number_matches_the_shim():
    assert Event.CALL_INFO.value == lib.BP_EV_CALL_INFO
    assert Event.CALL_INFO.value >= SHIM_EVENT_BASE


@pytest.mark.parametrize(
    "body", [b"recording=on", bytes(range(256)), b""], ids=["text", "binary", "empty"]
)
async def test_the_native_payload_decodes_to_type_and_raw_body(runtime, body):
    seen: list[StackEvent] = []
    runtime.subscribe(seen.append)
    payload = {
        "call": 0x123,
        "text": "application/x-pbx-event",
        "body": base64.b64encode(body).decode(),
    }

    runtime._deliver_stack_event(lib.BP_EV_BASE + lib.BP_EV_CALL_INFO, 0x123, json.dumps(payload))

    assert seen[-1].event is Event.CALL_INFO
    assert seen[-1].call == 0x123
    assert seen[-1].text == "application/x-pbx-event"
    assert seen[-1].body == body


async def test_listeners_get_typed_events_for_their_own_call_until_removed(runtime):
    call = Call(runtime, handle=0x123, ua_handle=0x1, state=CallState.ESTABLISHED)
    got: list = []
    call.on_info(got.append)

    def info(handle, content_type, body):
        call._on_stack_event(
            StackEvent(event=Event.CALL_INFO, call=handle, text=content_type, body=body)
        )

    info(0x123, "application/x-pbx-event", b"queue=sales")
    info(0x123, None, b"")  # an INFO with neither a type nor a body
    info(0x999, "application/x-pbx-event", b"someone else's call")
    call._on_stack_event(StackEvent(event=Event.CALL_RINGING, call=0x123))

    assert got == [InfoEvent("application/x-pbx-event", b"queue=sales"), InfoEvent("", b"")]

    call.off_info(got.append)
    info(0x123, "application/x-pbx-event", b"after removal")
    assert len(got) == 2
    call.off_info(got.append)  # unknown callbacks are ignored


async def test_a_listener_that_raises_does_not_stop_the_others(runtime):
    call = Call(runtime, handle=0x123, ua_handle=0x1, state=CallState.ESTABLISHED)
    got: list = []

    def broken(_event):
        raise RuntimeError("listener bug")

    call.on_info(broken)
    call.on_info(got.append)
    call._on_stack_event(StackEvent(event=Event.CALL_INFO, call=0x123, text="a/b", body=b"x"))

    assert got == [InfoEvent("a/b", b"x")]
