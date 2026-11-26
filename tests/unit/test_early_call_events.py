#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""A placed call sees the events that arrive before its object exists.

``dial()`` learns the call's handle from the stack's answer to its
command, and the far end may refuse the call before the event loop gets
to that answer: a busy loop, a nearby server. The refusal must still
reach the call.
"""

import asyncio
import itertools
import time

import pytest

native = pytest.importorskip("baresip._native")

from baresip import Account, CallFailed, CallState, Config, Runtime, UserAgent

_PORTS = itertools.count(15880)


@pytest.fixture
async def refusing():
    """A runtime, a caller, and the address of an agent that refuses every call."""
    own = f"127.0.0.1:{next(_PORTS)}"
    runtime = Runtime()
    await runtime.start(Config(net_interface="127.0.0.1", extra_config_text=f"sip_listen {own}\n"))
    caller = await UserAgent.create(
        runtime, Account(user="caller", password="", domain=own, reg_interval=0)
    )
    # An account that requires SRTP: the stack refuses the caller's
    # unencrypted offer itself, with a 488, as fast as a refusal can be.
    await UserAgent.create(
        runtime,
        Account(user="bot", password="", domain=own, reg_interval=0, media_encryption="srtp"),
    )
    yield caller, f"sip:bot@{own}"
    await runtime.close()


async def test_a_refusal_that_arrives_with_the_dial_answer_reaches_the_call(refusing):
    caller, target = refusing

    dialling = asyncio.ensure_future(caller.dial(target))
    await asyncio.sleep(0)  # the command is on its way to the stack
    # The loop is busy while the stack dials and is refused.
    time.sleep(0.5)  # noqa: ASYNC251
    placed = await dialling

    with pytest.raises(CallFailed) as refusal:
        await placed.wait_established(timeout=2)
    assert refusal.value.status == 488
    assert placed.state is CallState.CLOSED
