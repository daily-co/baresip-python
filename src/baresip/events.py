#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""Typed stack events.

:class:`Event` mirrors the native stack's event numbering, member for
member. The numbers are bare enum positions upstream has inserted into
before, so a unit test cross-checks every member against the compiled
stack — a version bump that shifts them fails the suite instead of
silently relabeling every event.

One stance worth knowing: :attr:`Event.CALL_TRANSFER` (a peer asking us to
transfer) is delivered like any other event, but no API acts on it yet —
the stack auto-acknowledges the request and nothing executes the transfer
behind the application's back. The event is informational until transfer
execution ships.
"""

from dataclasses import dataclass, field
from enum import IntEnum


class Event(IntEnum):
    """Everything the stack can report, by name."""

    REGISTERING = 0
    REGISTER_OK = 1
    REGISTER_FAIL = 2
    UNREGISTERING = 3
    FALLBACK_OK = 4
    FALLBACK_FAIL = 5
    MWI_NOTIFY = 6
    CREATE = 7
    SHUTDOWN = 8
    EXIT = 9
    CALL_INCOMING = 10
    CALL_OUTGOING = 11
    CALL_RINGING = 12
    CALL_PROGRESS = 13
    CALL_ANSWERED = 14
    CALL_ESTABLISHED = 15
    CALL_CLOSED = 16
    CALL_TRANSFER = 17
    CALL_REDIRECT = 18
    CALL_TRANSFER_FAILED = 19
    CALL_DTMF_START = 20
    CALL_DTMF_END = 21
    CALL_RTPESTAB = 22
    CALL_RTCP = 23
    CALL_MENC = 24
    VU_TX = 25
    VU_RX = 26
    AUDIO_ERROR = 27
    CALL_LOCAL_SDP = 28
    CALL_REMOTE_SDP = 29
    CALL_HOLD = 30
    CALL_RESUME = 31
    REFER = 32
    MODULE = 33
    END_OF_FILE = 34
    CUSTOM = 35
    SIPSESS_CONN = 36
    SIPSESS_FAILED = 37


@dataclass(frozen=True)
class StackEvent:
    """One event as delivered to listeners.

    Every string field originates on the network. The native encoder
    escapes them byte by byte and caps each value, so they are always
    present-able — but treat their content as untrusted.

    Parameters:
        event: What happened.
        handle: The most specific object concerned — the call when there
            is one, else the user agent, else 0.
        ua: Handle of the user agent concerned, 0 for none.
        call: Handle of the call concerned, 0 for none.
        text: The stack's free-form detail line, when it attached one
            (a DTMF digit, an SDP direction, an error description).
        peer: The remote party's URI, on call events.
        call_id: The SIP Call-ID — the correlation key across our logs,
            the peer's logs, and any capture in between.
        from_: The From header's address, when the event carries a
            SIP message.
        to: The To header's address, when the event carries a SIP message.
        headers: The allowlisted headers found on the message
            (:class:`~baresip.config.Config` ``expose_headers``).
        truncated: True when any value was cut to fit its size cap.
    """

    event: Event
    handle: int = 0
    ua: int = 0
    call: int = 0
    text: str | None = None
    peer: str | None = None
    call_id: str | None = None
    from_: str | None = None
    to: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    truncated: bool = False
