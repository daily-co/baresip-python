#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""Media encryption: the account setting, and real calls between two agents.

The calls run directly agent to agent over loopback (pinned
``sip_listen``, no switch), so the caller can offer every form a peer
might send and the answering account's ``media_encryption`` decides what
happens. Against a real switch the same behaviour is covered by the
bench tests.
"""

import asyncio
import contextlib
import itertools
import logging

import pytest

native = pytest.importorskip("baresip._native")

from baresip import Account, CallFailed, Config, Runtime, UserAgent, VideoNotActive

# A closed runtime's SIP socket lingers in-process; sequential tests
# each take a fresh port.
_PORTS = itertools.count(5285)

# What a caller can offer, as the stack mode that produces it.
PLAIN = None
KEYS_ON_PLAIN_LINE = "srtp"  # would like SRTP, accepts none
SRTP = "srtp-mand"
SRTP_FEEDBACK = "srtp-mandf"  # the RTCP-feedback profile

W, H = 128, 96


def account(user: str, domain: str = "example.com", **fields) -> Account:
    return Account(user=user, password="", domain=domain, reg_interval=0, **fields)


def test_the_default_is_no_encryption():
    assert account("bot").media_encryption == "none"
    assert "mediaenc" not in account("bot").aor()


def test_srtp_selects_mandatory_srtp():
    assert ";mediaenc=srtp-mand" in account("bot", media_encryption="srtp").aor()


def test_an_unknown_value_is_refused():
    with pytest.raises(ValueError, match="media_encryption"):
        account("bot", media_encryption="zrtp")


def test_a_raw_mode_in_extra_params_takes_the_fields_place():
    raw = account("bot", media_encryption="srtp", extra_params=("mediaenc=dtls_srtp",))

    assert raw.aor().count("mediaenc=") == 1
    assert ";mediaenc=dtls_srtp" in raw.aor()


def test_the_setting_shows_in_the_repr():
    assert "media_encryption='srtp'" in repr(account("bot", media_encryption="srtp"))


@pytest.fixture
async def loopback():
    """One runtime listening on loopback, and its address."""
    own = f"127.0.0.1:{next(_PORTS)}"
    runtime = Runtime()
    await runtime.start(Config(net_interface="127.0.0.1", extra_config_text=f"sip_listen {own}\n"))
    yield runtime, own
    await runtime.close()


@pytest.fixture
async def roomy_loopback():
    """A loopback runtime with video, and no cap on concurrent calls."""
    own = f"127.0.0.1:{next(_PORTS)}"
    runtime = Runtime()
    await runtime.start(
        Config(
            net_interface="127.0.0.1",
            max_concurrent_calls=None,
            video_size=(W, H),
            video_fps=15.0,
            video_bitrate=256_000,
            extra_config_text=f"sip_listen {own}\n",
        )
    )
    yield runtime, own
    await runtime.close()


async def call_bot(loopback, offer: str | None, bot_setting: str = "none", bot_raw: str = ""):
    """Place a call offering ``offer`` to a bot with ``bot_setting``.

    ``bot_raw`` gives the bot a stack mode in its raw account parameters
    instead. Returns (caller's call, bot's call, the caller's wait); the
    bot's call is None when the call was refused before the bot's
    application saw it.
    """
    runtime, own = loopback
    raw = (f"mediaenc={offer}",) if offer else ()
    caller = await UserAgent.create(runtime, account("caller", own, extra_params=raw))
    bot_fields = {"extra_params": (f"mediaenc={bot_raw}",)} if bot_raw else {}
    bot = await UserAgent.create(
        runtime, account("bot", own, media_encryption=bot_setting, **bot_fields)
    )
    incoming: asyncio.Queue = asyncio.Queue()
    bot.on_incoming(incoming.put_nowait)

    placed = await caller.dial(f"sip:bot@{own}")
    arrived = asyncio.create_task(incoming.get())
    failed = asyncio.create_task(placed.wait_established(timeout=5))
    await asyncio.wait({arrived, failed}, return_when=asyncio.FIRST_COMPLETED)
    if not arrived.done():
        arrived.cancel()
        return placed, None, failed
    received = arrived.result()
    await received.answer()
    await failed
    await received.wait_established(timeout=5)
    return placed, received, failed


@pytest.mark.parametrize(
    ("offer", "bot_setting", "encrypted"),
    [
        (PLAIN, "none", False),
        (KEYS_ON_PLAIN_LINE, "none", True),
        (SRTP, "none", True),
        (SRTP_FEEDBACK, "none", True),
        (KEYS_ON_PLAIN_LINE, "srtp", True),
        (SRTP, "srtp", True),
        (SRTP_FEEDBACK, "srtp", True),
    ],
)
async def test_a_received_call_gets_the_encryption_its_offer_allows(
    loopback, offer, bot_setting, encrypted
):
    placed, received, _ = await call_bot(loopback, offer, bot_setting)

    # Audio must actually arrive: an answer the caller cannot decrypt
    # would establish and then carry nothing.
    await asyncio.sleep(0.6)
    assert received.media_encrypted is encrypted
    assert placed.media_encrypted is encrypted
    await placed.hangup()
    await asyncio.sleep(0.2)
    assert received.final_stats.rx_packets > 0
    assert placed.final_stats.rx_packets > 0


async def test_requiring_srtp_refuses_an_unencrypted_call_before_the_application(loopback):
    _placed, received, failed = await call_bot(loopback, PLAIN, "srtp")

    assert received is None
    with pytest.raises(CallFailed) as refusal:
        await failed
    assert refusal.value.status == 488


async def test_overlapping_calls_each_get_their_own_encryption(roomy_loopback):
    runtime, own = roomy_loopback
    plain = await UserAgent.create(runtime, account("plain", own))
    secure = await UserAgent.create(runtime, account("secure", own, media_encryption="srtp"))
    bot = await UserAgent.create(runtime, account("bot", own))
    incoming: asyncio.Queue = asyncio.Queue()
    bot.on_incoming(incoming.put_nowait)

    # Back to back, neither answered yet: the bot's account is switched
    # for the second call while the first is still ringing.
    placed = {"plain": await plain.dial(f"sip:bot@{own}")}
    placed["secure"] = await secure.dial(f"sip:bot@{own}")
    received = {}
    for _ in placed:
        call = await asyncio.wait_for(incoming.get(), 5)
        received["secure" if "secure@" in call.peer else "plain"] = call
    assert set(received) == {"plain", "secure"}
    for call in received.values():
        await call.answer()
    for call in (*placed.values(), *received.values()):
        await call.wait_established(timeout=5)
    await asyncio.sleep(0.6)

    assert not placed["plain"].media_encrypted and not received["plain"].media_encrypted
    assert placed["secure"].media_encrypted and received["secure"].media_encrypted
    for call in placed.values():
        await call.hangup()
    await asyncio.sleep(0.2)
    assert all(call.final_stats.rx_packets > 0 for call in received.values())


async def test_a_video_call_is_encrypted_and_its_frames_arrive(roomy_loopback):
    runtime, own = roomy_loopback
    caller = await UserAgent.create(runtime, account("caller", own, media_encryption="srtp"))
    bot = await UserAgent.create(runtime, account("bot", own))
    incoming: asyncio.Queue = asyncio.Queue()
    bot.on_incoming(incoming.put_nowait)

    placed = await caller.dial(f"sip:bot@{own}", video=True)
    received = await asyncio.wait_for(incoming.get(), 5)
    await received.answer(video=True)
    await placed.wait_established(timeout=5)
    await received.wait_established(timeout=5)

    bright = bytes([200]) * (W * H) + b"\x80" * (2 * (W // 2) * (H // 2))
    frame = None
    deadline = asyncio.get_running_loop().time() + 5
    while frame is None:
        assert asyncio.get_running_loop().time() < deadline, "no video frame arrived"
        with contextlib.suppress(VideoNotActive):  # the streams are still coming up
            placed.video.write_frame(bright)
            frame = received.video.read_frame()
        await asyncio.sleep(0.03)

    assert placed.media_encrypted and received.media_encrypted
    assert (frame.width, frame.height) == (W, H)
    assert frame.data[0] > 150  # VP8 is lossy; a bright frame stays bright


async def test_a_raw_mode_answers_only_the_offer_it_matches(loopback):
    placed, received, _ = await call_bot(loopback, SRTP_FEEDBACK, bot_raw=SRTP_FEEDBACK)

    await asyncio.sleep(0.3)
    assert received is not None
    assert placed.media_encrypted and received.media_encrypted


async def test_a_raw_mode_is_not_switched_to_fit_another_offer(loopback):
    _placed, received, failed = await call_bot(loopback, SRTP, bot_raw=SRTP_FEEDBACK)

    assert received is None
    with pytest.raises(CallFailed) as refusal:
        await failed
    assert refusal.value.status == 488


async def test_a_call_placed_with_srtp_is_encrypted(loopback):
    runtime, own = loopback
    caller = await UserAgent.create(runtime, account("caller", own, media_encryption="srtp"))
    bot = await UserAgent.create(runtime, account("bot", own))
    incoming: asyncio.Queue = asyncio.Queue()
    bot.on_incoming(incoming.put_nowait)

    placed = await caller.dial(f"sip:bot@{own}")
    assert placed.media_encrypted is False  # nothing is negotiated yet
    received = await asyncio.wait_for(incoming.get(), 5)
    await received.answer()
    await placed.wait_established(timeout=5)
    await received.wait_established(timeout=5)
    await asyncio.sleep(0.3)

    assert placed.media_encrypted and received.media_encrypted


async def test_srtp_without_tls_signalling_warns_once_per_agent(loopback, caplog):
    runtime, own = loopback
    caplog.set_level(logging.WARNING, logger="baresip.ua")

    await UserAgent.create(runtime, account("plain", own))
    await UserAgent.create(runtime, account("bot", own, media_encryption="srtp"))

    warnings = [r.getMessage() for r in caplog.records if "SRTP keys" in r.getMessage()]
    assert len(warnings) == 1
    assert "transport='tls'" in warnings[0]
