#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""Stack events: numbering, typed delivery, and the JSON encoder.

Three separate guarantees live here. The numbering cross-check pins our
Event enum to the compiled stack's, so a submodule bump that shifts the
values fails a test instead of silently relabeling every event. The
delivery tests run a canned SIP message through the real payload path —
header extraction included. And the encoder tests feed the C JSON writer
hostile bytes and require valid, byte-faithful JSON back, every time.
"""

import json
import random

import pytest

from baresip import Config, Event

native = pytest.importorskip("baresip._native")
ffi = native.ffi
lib = native.lib

from baresip.runtime import Runtime  # imports the extension, hence after the skip

# Where bevent_str's display name differs from the enum member's name.
STR_ALIASES = {
    "CALL_TRANSFER": "TRANSFER",
    "CALL_TRANSFER_FAILED": "TRANSFER_FAILED",
    "VU_TX": "VU_TX_REPORT",
    "VU_RX": "VU_RX_REPORT",
}

SIP_MESSAGE = (
    "OPTIONS sip:bob@example.com SIP/2.0\r\n"
    "Via: SIP/2.0/UDP 192.0.2.1:5060;branch=z9hG4bK776asdhds\r\n"
    'From: "Alice" <sip:alice@example.com>;tag=1928301774\r\n'
    "To: <sip:bob@example.com>\r\n"
    "Call-ID: a84b4c76e66710@pc33.example.com\r\n"
    "CSeq: 63104 OPTIONS\r\n"
    "X-Customer-Id: 42\r\n"
    'X-Weird: say "hi" ☃\r\n'
    "Y-Secret: not-allowlisted\r\n"
    "Content-Length: 0\r\n"
    "\r\n"
)


def test_event_numbering_matches_the_compiled_stack():
    """The numbers are bare enum positions upstream has inserted into
    before. Both an insertion (names shift) and an addition (count grows)
    must fail here, forcing the enum to be re-verified on every bump."""
    assert lib.bp_bevent_max() == len(Event)
    for member in Event:
        compiled = ffi.string(lib.bp_bevent_str(member.value)).decode()
        assert compiled == STR_ALIASES.get(member.name, member.name), member


async def test_typed_delivery_with_header_extraction():
    """A canned SIP message through the real payload path: the listener
    gets one typed event carrying From, To, Call-ID, and exactly the
    allowlisted headers — hostile characters escaped, secrets absent."""
    runtime = Runtime()
    await runtime.start(Config(expose_headers=("X-Customer-Id", "X-Weird", "X-Absent")))
    received = []
    runtime.subscribe(received.append)
    try:
        ev, payload = await runtime.cmd(lib.BP_CMD_TEST_EMIT, args=SIP_MESSAGE)
        assert ev == lib.BP_EV_DONE
        assert payload is None, f"the message should have decoded: {payload!r}"
    finally:
        await runtime.close()

    assert len(received) == 1
    event = received[0]
    assert event.event is Event.CUSTOM  # the id the test funnel emits with
    assert event.handle == 0 and event.ua == 0 and event.call == 0
    assert event.from_ == "sip:alice@example.com"
    assert event.to == "sip:bob@example.com"
    assert event.call_id == "a84b4c76e66710@pc33.example.com"
    assert event.headers == {"X-Customer-Id": "42", "X-Weird": 'say "hi" ☃'}
    assert not event.truncated


async def test_listeners_are_isolated():
    """One listener raising must not cost the others their event, and an
    unsubscribed listener stays silent."""
    runtime = Runtime()
    await runtime.start()
    received = []
    removed = []

    def bad(_event):
        raise RuntimeError("misbehaving listener")

    runtime.subscribe(bad)
    runtime.subscribe(received.append)
    runtime.subscribe(removed.append)
    runtime.unsubscribe(removed.append)
    try:
        await runtime.cmd(lib.BP_CMD_TEST_EMIT, args=SIP_MESSAGE)
    finally:
        await runtime.close()

    assert len(received) == 1
    assert removed == []


async def test_undecodable_input_is_reported_not_emitted():
    runtime = Runtime()
    received = []
    runtime.subscribe(received.append)
    await runtime.start()
    try:
        ev, payload = await runtime.cmd(lib.BP_CMD_TEST_EMIT, args="this is not SIP")
        assert ev == lib.BP_EV_DONE
        assert json.loads(payload) == {"error": "decode"}
    finally:
        await runtime.close()
    assert received == []


# -- the JSON encoder --------------------------------------------------------
#
# A Python re-implementation of the C escaper's *semantics*: valid UTF-8
# passes through; every other byte — controls, quotes handled by escaping,
# invalid sequences — decodes to the code point of its byte value. Two
# independent implementations agreeing on random input is the test.

VALUE_CAP = 1024


def _utf8_seq(data: bytes, i: int) -> int:
    b = data[i]
    if 0xC2 <= b <= 0xDF:
        need = 2
    elif 0xE0 <= b <= 0xEF:
        need = 3
    elif 0xF0 <= b <= 0xF4:
        need = 4
    else:
        return 0
    if i + need > len(data):
        return 0
    second = data[i + 1]
    if b == 0xE0 and not 0xA0 <= second <= 0xBF:
        return 0
    if b == 0xED and not 0x80 <= second <= 0x9F:
        return 0
    if b == 0xF0 and not 0x90 <= second <= 0xBF:
        return 0
    if b == 0xF4 and not 0x80 <= second <= 0x8F:
        return 0
    if any(not 0x80 <= data[i + k] <= 0xBF for k in range(1, need)):
        return 0
    return need


def expected_escape(data: bytes) -> tuple[str, bool]:
    out = []
    i = 0
    while i < len(data):
        if i >= VALUE_CAP:
            return "".join(out), True
        seq = _utf8_seq(data, i) if data[i] >= 0x80 else 0
        if seq:
            if i + seq > VALUE_CAP:
                return "".join(out), True
            out.append(data[i : i + seq].decode())
            i += seq
        else:
            out.append(chr(data[i]))
            i += 1
    return "".join(out), False


async def escape(runtime, data: bytes) -> tuple[str, bool]:
    ev, payload = await runtime.cmd(lib.BP_CMD_TEST_ESCAPE, args=data)
    assert ev == lib.BP_EV_DONE
    decoded = json.loads(payload)  # the guarantee under test: this never raises
    return decoded["fuzz"], decoded.get("truncated", False)


async def test_encoder_hostile_cases():
    """The named hostile shapes: quotes, backslashes, controls, invalid
    UTF-8 in its distinct failure modes (bare continuation, cut sequence,
    overlong, surrogate), valid multi-byte, and the cap — cutting at a
    code-point boundary, marked."""
    cases = [
        b'quote " backslash \\ done',
        bytes(range(1, 0x20)) + b"\x7f",
        b"\x80\xbf",  # bare continuation bytes
        b"\xc3",  # sequence cut short at end of input
        b"\xc0\xaf",  # overlong '/', invalid lead bytes
        b"\xed\xa0\x80",  # UTF-8-encoded surrogate: not valid UTF-8
        "héllo ☃ 日本語 🎉".encode(),
        b"x" * 5000,  # far past the cap: cut and marked
        b"y" * 1022 + "☃".encode(),  # multi-byte straddling the cap
    ]
    runtime = Runtime()
    await runtime.start()
    try:
        for data in cases:
            got, truncated = await escape(runtime, data)
            want, want_truncated = expected_escape(data)
            assert (got, truncated) == (want, want_truncated), data
    finally:
        await runtime.close()


async def test_encoder_fuzz():
    """Byte soup in, valid JSON out — parseable every time and
    byte-faithful per the reference implementation. Seeded, so a failure
    reproduces. NUL never appears: commands travel as C strings, and no
    SIP header value can contain one either."""
    rng = random.Random(0xBA5E51)
    runtime = Runtime()
    await runtime.start()
    try:
        for _ in range(200):
            data = bytes(rng.randrange(1, 256) for _ in range(rng.randrange(0, 600)))
            got, truncated = await escape(runtime, data)
            want, want_truncated = expected_escape(data)
            assert (got, truncated) == (want, want_truncated), data
    finally:
        await runtime.close()
