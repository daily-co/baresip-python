#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""Video end to end: a real VP8 call between two agents in one runtime.

Direct UA→UA over loopback (pinned ``sip_listen``, no switch, no
network beyond 127.0.0.1), so the full path runs for real: Python
frames → pacing → VP8 encode → RTP → decode → filter tap → Python
frames. Runs everywhere the unit suite runs.
"""

import asyncio
import itertools

import pytest

native = pytest.importorskip("baresip._native")

from baresip import (
    Account,
    BaresipError,
    CallState,
    Config,
    Runtime,
    UserAgent,
    VideoNotActive,
    VideoRestarted,
)

# A closed runtime's SIP socket lingers in-process; sequential tests
# each take a fresh port.
_PORTS = itertools.count(5085)

W, H = 128, 96
FRAME = W * H + 2 * (W // 2) * (H // 2)


def luma_frame(value: int) -> bytes:
    """A packed I420 frame with uniform luma; survives VP8 grossly."""
    return bytes([value]) * (W * H) + b"\x80" * (2 * (W // 2) * (H // 2))


@pytest.fixture
async def pair():
    own = f"127.0.0.1:{next(_PORTS)}"
    runtime = Runtime()
    await runtime.start(
        f"net_interface 127.0.0.1\nsip_listen {own}\n"
        + Config(video_size=(W, H), video_fps=15.0, video_bitrate=256_000).render()
    )
    ua_a = await UserAgent.create(
        runtime, Account(user="alice", password="", domain=own, reg_interval=0)
    )
    ua_b = await UserAgent.create(
        runtime, Account(user="bob", password="", domain=own, reg_interval=0)
    )
    incoming: asyncio.Queue = asyncio.Queue()
    ua_b.on_incoming(incoming.put_nowait)

    async def connect(video: bool | str = True):
        a_call = await ua_a.dial(f"sip:bob@{own}", video=video)
        b_call = await asyncio.wait_for(incoming.get(), 5)
        # answer() takes a bool; a directional dial still needs the
        # answerer's video enabled for later renegotiation.
        await b_call.answer(video=bool(video))
        await a_call.wait_established()
        await b_call.wait_established()
        return a_call, b_call

    try:
        yield connect
    finally:
        await runtime.close()


async def wait_video_up(call, timeout: float = 5.0):
    # tx_ready flips when the transmit source binds (at establishment);
    # rx_ready follows only once the first decoded frame arrives, so it
    # cannot be a precondition for starting to send.
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        try:
            info = call.video.info()
            if info.tx_ready:
                return info
        except VideoNotActive:
            pass
        assert asyncio.get_running_loop().time() < deadline, "video never came up"
        await asyncio.sleep(0.05)


async def pump_until_frame(src_call, dst_call, luma, timeout: float = 5.0):
    """Write `luma` frames at the source until the sink decodes one."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        src_call.video.write_frame(luma_frame(luma))
        frame = dst_call.video.read_frame()
        if frame is not None:
            return frame
        assert asyncio.get_running_loop().time() < deadline, "no frame arrived"
        await asyncio.sleep(0.03)


async def test_vp8_frames_flow_both_ways(pair):
    a_call, b_call = await pair()
    info = await wait_video_up(a_call)
    assert (info.width, info.height) == (W, H)
    await wait_video_up(b_call)

    got = await pump_until_frame(a_call, b_call, 200)
    assert (got.width, got.height) == (W, H)
    assert len(got.data) == FRAME
    # VP8 is lossy; a uniform bright frame stays bright.
    assert got.data[0] > 150

    got = await pump_until_frame(b_call, a_call, 30)
    assert (got.width, got.height) == (W, H)
    assert got.data[0] < 100

    await a_call.hangup()


async def test_video_survives_hold_resume(pair):
    # The renegotiation gauntlet: hold restarts the source from config,
    # and the pinned device name must keep the call's handle attached.
    a_call, b_call = await pair()
    await wait_video_up(a_call)
    await wait_video_up(b_call)
    await pump_until_frame(a_call, b_call, 128)

    await b_call.hold()
    await asyncio.sleep(0.3)
    await b_call.resume()

    recovered = None
    deadline = asyncio.get_running_loop().time() + 10
    while True:
        try:
            recovered = await pump_until_frame(a_call, b_call, 220, timeout=2.0)
            break
        except (AssertionError, VideoRestarted):
            # VideoRestarted is the epoch contract doing its job: the
            # renegotiation replaced the streams, and the next operation
            # rebinds. Keep pumping until frames flow again.
            assert asyncio.get_running_loop().time() < deadline, (
                "video never recovered after hold/resume"
            )
    assert recovered is not None and recovered.data[0] > 150
    await a_call.hangup()


async def set_dir_retrying(call, direction, timeout: float = 3.0):
    """Right after answer the ACK can still be in flight; retry briefly."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        try:
            await call.set_video_direction(direction)
            return
        except BaresipError as e:
            if "retry" not in str(e) or asyncio.get_running_loop().time() > deadline:
                raise
            await asyncio.sleep(0.1)


async def test_mid_call_video_add_and_remove(pair):
    # The video stream is negotiated inactive at setup: audio-only on
    # the wire, but the m-line is there to activate mid-call.
    a_call, b_call = await pair(video="inactive")
    with pytest.raises(VideoNotActive):
        a_call.video.info()

    await set_dir_retrying(a_call, "sendrecv")
    info = await wait_video_up(a_call)
    assert (info.width, info.height) == (W, H)
    await wait_video_up(b_call)
    got = await pump_until_frame(a_call, b_call, 200)
    assert got.data[0] > 150

    await set_dir_retrying(a_call, "inactive")
    # The re-INVITE tears the streams down; a bound CallVideo surfaces
    # the replacement and then the absence.
    deadline = asyncio.get_running_loop().time() + 5
    while True:
        try:
            a_call.video.write_frame(luma_frame(128))
        except (VideoNotActive, VideoRestarted):
            break
        assert asyncio.get_running_loop().time() < deadline, "video never came down"
        await asyncio.sleep(0.05)
    assert a_call.state is CallState.ESTABLISHED

    await a_call.hangup()


async def test_add_video_needs_a_negotiated_stream(pair):
    # video=False dials with no video m-line at all; there is nothing a
    # re-INVITE could activate.
    a_call, _b_call = await pair(video=False)
    with pytest.raises(BaresipError, match="no video stream"):
        await a_call.add_video()
    with pytest.raises(ValueError):
        await a_call.set_video_direction("both-ways")
    await a_call.hangup()


async def test_keyframe_request_is_harmless(pair):
    a_call, b_call = await pair()
    await wait_video_up(a_call)
    await a_call.video.request_keyframe()
    await b_call.video.request_keyframe()
    await a_call.hangup()


async def test_audio_only_call_has_no_video(pair):
    # Video stays inert without video=True — the audio-only default is
    # unchanged by the vidmem machinery existing.
    a_call, b_call = await pair(video=False)
    with pytest.raises(VideoNotActive):
        a_call.video.info()
    with pytest.raises(VideoNotActive):
        b_call.video.read_frame()
    await a_call.hangup()
    deadline = asyncio.get_running_loop().time() + 5
    while a_call.state is not CallState.CLOSED or b_call.state is not CallState.CLOSED:
        assert asyncio.get_running_loop().time() < deadline
        await asyncio.sleep(0.01)
