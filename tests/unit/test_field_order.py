#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""Fields added to a public class go after the ones it already had.

``Account`` and ``StackEvent`` take their fields by position as well as
by name, so a field inserted among the others shifts every positional
argument after it.
"""

import dataclasses

from baresip import Account, Event, StackEvent

# The fields each class had in 0.6.1, in order.
ACCOUNT_0_6_1 = (
    "user",
    "domain",
    "password",
    "registrar",
    "reg_interval",
    "transport",
    "audio_codecs",
    "dtmf_mode",
    "auth_user",
    "extra_params",
)
STACK_EVENT_0_6_1 = (
    "event",
    "handle",
    "ua",
    "call",
    "text",
    "peer",
    "call_id",
    "from_",
    "to",
    "headers",
    "stats",
    "truncated",
)


def names(cls) -> tuple[str, ...]:
    return tuple(f.name for f in dataclasses.fields(cls))


def test_account_keeps_its_earlier_fields_first():
    assert names(Account)[: len(ACCOUNT_0_6_1)] == ACCOUNT_0_6_1


def test_stack_event_keeps_its_earlier_fields_first():
    assert names(StackEvent)[: len(STACK_EVENT_0_6_1)] == STACK_EVENT_0_6_1


def test_an_account_built_by_position_means_what_it_did():
    account = Account(
        "bot", "pbx.example", "secret", None, 300, "tls", ("pcmu",), "info", "alice", ("a=b",)
    )

    assert account.auth_user == "alice"
    assert account.extra_params == ("a=b",)
    assert account.media_encryption == "none"


def test_a_stack_event_built_by_position_means_what_it_did():
    event = StackEvent(Event.CALL_CLOSED, 1, 2, 3, "text", "peer", "id", "from", "to", {}, {}, True)

    assert event.truncated is True
    assert event.body == b""
