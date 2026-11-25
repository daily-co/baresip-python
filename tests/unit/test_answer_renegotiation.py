#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""An answered call is not renegotiated on the stack's own initiative.

The stack sets an incoming call up expecting video, then compares that
expectation with the call as answered shortly after it is established,
and sends a re-INVITE when they differ. A call answered without video
must not trip that comparison.
"""

import asyncio
import itertools
import logging

import pytest

native = pytest.importorskip("baresip._native")

from baresip import Account, Config, Runtime, UserAgent, VideoNotActive

_PORTS = itertools.count(15750)
# The stack's comparison runs up to 300 ms after a call is established.
SETTLE = 0.6


class Sent(logging.Handler):
    """The SIP messages the stack sent, as its trace logged them."""

    def __init__(self):
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record):
        message = record.getMessage()
        if message.startswith("TX "):
            self.messages.append(message.split("\n", 1)[-1])

    def starting(self, start: str) -> list[str]:
        return [m for m in self.messages if m.startswith(start)]


@pytest.fixture
async def pair():
    """``connect(offer_video, answer_video)`` places and answers a call: (caller's, answerer's, trace)."""
    own = f"127.0.0.1:{next(_PORTS)}"
    sent = Sent()
    sip_logger = logging.getLogger("baresip.native.sip")
    level = sip_logger.level
    sip_logger.addHandler(sent)
    sip_logger.setLevel(logging.DEBUG)
    runtime = Runtime()
    await runtime.start(
        Config(net_interface="127.0.0.1", sip_trace=True, extra_config_text=f"sip_listen {own}\n")
    )
    agents = {}
    for user in ("alice", "bob"):
        agents[user] = await UserAgent.create(
            runtime, Account(user=user, password="", domain=own, reg_interval=0)
        )
    incoming: asyncio.Queue = asyncio.Queue()
    agents["bob"].on_incoming(incoming.put_nowait)

    async def connect(offer_video: bool, answer_video: bool):
        placed = await agents["alice"].dial(f"sip:bob@{own}", video=offer_video)
        received = await asyncio.wait_for(incoming.get(), 5)
        await received.answer(video=answer_video)
        await placed.wait_established(timeout=5)
        await received.wait_established(timeout=5)
        await asyncio.sleep(SETTLE)
        return placed, received, sent

    yield connect
    await runtime.close()
    sip_logger.removeHandler(sent)
    sip_logger.setLevel(level)


@pytest.mark.parametrize(
    ("offer_video", "answer_video"),
    [(False, False), (True, False), (True, True), (False, True)],
    ids=["audio call", "video declined", "video accepted", "video wanted, none offered"],
)
async def test_an_answered_call_is_not_renegotiated(pair, offer_video, answer_video):
    _placed, _received, sent = await pair(offer_video, answer_video)

    assert len(sent.starting("INVITE ")) == 1


async def test_declined_video_is_refused_in_the_answer(pair):
    _placed, received, sent = await pair(offer_video=True, answer_video=False)

    (answer,) = sent.starting("SIP/2.0 200")
    assert "\r\nm=video 0 " in answer
    with pytest.raises(VideoNotActive):
        received.video.info()


async def test_a_hold_right_after_the_answer_is_sent(pair):
    placed, received, sent = await pair(offer_video=False, answer_video=False)

    await received.hold()

    assert len(sent.starting("INVITE ")) == 2
    await placed.hangup()
