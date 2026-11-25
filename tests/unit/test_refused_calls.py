#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""A call the stack refuses on its own is reported in the log.

The stack answers some INVITEs itself, before a call is ever reported to
the application: an offer nothing in the account matches, or a call past
the concurrency limit. The application sees nothing of such a call, so a
warning says who called and why it was refused.
"""

import asyncio
import itertools
import logging

import pytest

native = pytest.importorskip("baresip._native")

from baresip import Account, CallFailed, Config, Runtime, UserAgent

_PORTS = itertools.count(15850)
REFUSED = "refused before the application saw it"


async def logged(caplog, text: str) -> str:
    """The first native log message containing ``text``, once the log thread delivers it."""
    for _ in range(300):
        for record in caplog.records:
            if text in record.getMessage():
                return record.getMessage()
        await asyncio.sleep(0.01)
    return ""


async def refused_call(caplog, config: Config, callee: Account, own: str) -> tuple[int, str]:
    """Call ``callee`` and return the status the caller got and the warning logged."""
    runtime = Runtime()
    await runtime.start(config)
    try:
        caller = await UserAgent.create(
            runtime, Account(user="caller", password="", domain=own, reg_interval=0)
        )
        bot = await UserAgent.create(runtime, callee)
        seen: list = []
        bot.on_incoming(seen.append)
        with caplog.at_level(logging.WARNING, logger="baresip.native"):
            placed = await caller.dial(f"sip:bot@{own}")
            with pytest.raises(CallFailed) as refusal:
                await placed.wait_established(timeout=5)
            warning = await logged(caplog, REFUSED)
        assert not seen, "the application was told about the refused call"
        return refusal.value.status, warning
    finally:
        await runtime.close()


async def test_an_offer_the_account_cannot_accept_is_reported(caplog):
    own = f"127.0.0.1:{next(_PORTS)}"
    status, warning = await refused_call(
        caplog,
        Config(net_interface="127.0.0.1", extra_config_text=f"sip_listen {own}\n"),
        Account(user="bot", password="", domain=own, reg_interval=0, media_encryption="srtp"),
        own,
    )

    assert status == 488
    assert f"call from sip:caller@{own}" in warning
    assert "m=audio " in warning and " RTP/AVP " in warning
    assert "acceptable to this account" in warning


async def test_a_call_past_the_limit_is_reported(caplog):
    own = f"127.0.0.1:{next(_PORTS)}"
    # The caller's own call is the one the limit allows.
    status, warning = await refused_call(
        caplog,
        Config(
            net_interface="127.0.0.1",
            max_concurrent_calls=1,
            extra_config_text=f"sip_listen {own}\n",
        ),
        Account(user="bot", password="", domain=own, reg_interval=0),
        own,
    )

    assert status == 486
    assert f"call from sip:caller@{own}" in warning
    assert "486 Max Calls" in warning and "Config.max_concurrent_calls" in warning


async def test_an_accepted_call_logs_no_refusal(caplog):
    own = f"127.0.0.1:{next(_PORTS)}"
    runtime = Runtime()
    await runtime.start(Config(net_interface="127.0.0.1", extra_config_text=f"sip_listen {own}\n"))
    try:
        agents = [
            await UserAgent.create(
                runtime, Account(user=user, password="", domain=own, reg_interval=0)
            )
            for user in ("caller", "bot")
        ]
        incoming: asyncio.Queue = asyncio.Queue()
        agents[1].on_incoming(incoming.put_nowait)
        with caplog.at_level(logging.WARNING, logger="baresip.native"):
            placed = await agents[0].dial(f"sip:bot@{own}")
            received = await asyncio.wait_for(incoming.get(), 5)
            await received.answer()
            await placed.wait_established(timeout=5)
            await placed.hangup()
            await asyncio.sleep(0.3)
        assert not [r for r in caplog.records if REFUSED in r.getMessage()]
    finally:
        await runtime.close()
