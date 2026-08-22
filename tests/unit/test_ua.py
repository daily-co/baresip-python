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

from baresip import Account, BaresipError, StaleHandleError
from baresip.runtime import Runtime
from baresip.ua import UserAgent, _parse_status

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


async def test_create_event_carries_no_credential(runtime):
    """ua_alloc's CREATE event natively carries the full AOR — password
    included. The shim must scrub it before it reaches a listener."""
    received = []
    runtime.subscribe(received.append)
    await UserAgent.create(runtime, ACCOUNT)
    assert received, "expected the CREATE event"
    for event in received:
        assert "secret" not in (event.text or "")


def test_parse_status():
    assert _parse_status("401 Unauthorized") == (401, "Unauthorized")
    assert _parse_status("Connection refused") == (None, "Connection refused")
    assert _parse_status("") == (None, "")
