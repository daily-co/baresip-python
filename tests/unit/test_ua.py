#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""UserAgent, network-free: allocation, bad input, and stale handles.

Allocating a user agent builds SIP state but touches no network, so
everything here runs offline. The register/unregister happy paths need a
registrar to answer and live in tests/integration (pytest -m bench).
"""

import pytest

native = pytest.importorskip("baresip._native")

from baresip import (
    Account,
    BaresipError,
    NoLocalAddressError,
    RegistrationError,
    StaleHandleError,
)
from baresip.errors import split_status
from baresip.runtime import Runtime
from baresip.ua import UserAgent

ACCOUNT = Account(user="alice", password="secret", domain="example.invalid")


@pytest.fixture
async def runtime():
    rt = Runtime()
    await rt.start()
    yield rt
    await rt.close()


async def test_create_returns_distinct_live_agents(runtime):
    first = await UserAgent.create(runtime, ACCOUNT)
    second = await UserAgent.create(
        runtime, Account(user="bob", password="", domain="example.invalid")
    )
    assert first.handle and second.handle
    assert first.handle != second.handle


async def test_create_accepts_a_raw_aor(runtime):
    ua = await UserAgent.create(runtime, "<sip:carol@example.invalid>;regint=0")
    assert ua.handle


async def test_create_rejects_garbage(runtime):
    with pytest.raises(BaresipError, match="allocation failed"):
        await UserAgent.create(runtime, "this is not an AOR")


async def test_stale_handle_fails_typed(runtime):
    ghost = UserAgent(runtime, 0xBAD0001)
    with pytest.raises(StaleHandleError):
        await ghost.register()


async def test_unregister_before_register_is_a_noop(runtime):
    ua = await UserAgent.create(runtime, ACCOUNT)
    await ua.unregister()  # never registered: returns without touching the network


async def test_registration_disabled_account_fails_fast(runtime):
    """reg_interval=0 dials directly; the stack would silently do nothing
    on register, so both directions must refuse up front — not wait out
    the outcome timeout on an answer that can never come."""
    ua = await UserAgent.create(
        runtime, Account(user="dana", password="", domain="example.invalid", reg_interval=0)
    )
    with pytest.raises(RegistrationError, match="registration disabled"):
        await ua.register()
    with pytest.raises(RegistrationError, match="registration disabled"):
        await ua.unregister()


async def test_registration_disabled_detected_in_a_raw_aor(runtime):
    ua = await UserAgent.create(runtime, "<sip:carol@example.invalid>;regint=0")
    with pytest.raises(RegistrationError, match="registration disabled"):
        await ua.register()


async def test_create_event_carries_no_credential(runtime):
    """ua_alloc's CREATE event natively carries the full AOR — password
    included. The shim must scrub it before it reaches a listener."""
    received = []
    runtime.subscribe(received.append)
    await UserAgent.create(runtime, ACCOUNT)
    assert received, "expected the CREATE event"
    for event in received:
        assert "secret" not in (event.text or "")


def test_split_status():
    assert split_status("401 Unauthorized") == (401, "Unauthorized")
    assert split_status("Connection refused") == (None, "Connection refused")
    assert split_status("") == (None, "")


async def test_dial_on_stale_handle_fails_typed(runtime):
    ghost = UserAgent(runtime, 0xBAD0001)
    with pytest.raises(StaleHandleError):
        await ghost.dial("sip:9196@example.invalid")


async def test_dial_unreachable_loopback_fails_typed(runtime):
    """Interface discovery skips loopback unless the configuration pins
    it, so a loopback dial under the default config can send nothing —
    the failure must name the problem, not surface as a bare EINVAL."""
    ua = await UserAgent.create(runtime, ACCOUNT)
    with pytest.raises(NoLocalAddressError, match="127.0.0.1"):
        await ua.dial("sip:9196@127.0.0.1:15060")


async def test_dial_loopback_works_when_pinned():
    """The same dial goes through once net_interface names loopback (no
    answer expected — dial() only needs the INVITE to leave)."""
    rt = Runtime()
    await rt.start(
        "net_interface 127.0.0.1\naudio_source aumem,default\naudio_player aumem,default\n"
    )
    try:
        ua = await UserAgent.create(rt, ACCOUNT)
        call = await ua.dial("sip:9196@127.0.0.1:59999")
        assert call.handle
    finally:
        await rt.close()


def test_dial_rejects_unsendable_input():
    ua = UserAgent(Runtime(), 1)  # validation happens before any command
    loop = __import__("asyncio").new_event_loop()
    try:
        for uri, headers in [
            ("", None),
            ("sip:a@b\nc", None),
            ("sip:a@b", {"Bad Name": "x"}),
            ("sip:a@b", {"X-Colon:": "x"}),
            ("sip:a@b", {"X-Ok": "multi\nline"}),
        ]:
            with pytest.raises(ValueError):
                loop.run_until_complete(ua.dial(uri, headers=headers))
    finally:
        loop.close()
