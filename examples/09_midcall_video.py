#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""Starting video in the middle of an audio call — and accepting it.

The call begins audio-only, with the video stream negotiated but
inactive: ``dial(uri, video="inactive")``. Mid-call, the caller brings
video up with ``call.add_video()`` (a re-INVITE), streams a moving-bars
pattern for a while, then takes video down again with
``call.remove_video()`` — the audio call continues untouched.

The accepting side is the point of the demo: it does *nothing* beyond
having answered with ``answer(video=True)``. An incoming mid-call video
offer is auto-accepted by the stack, frames simply start arriving, and
a writer that was failing quietly with ``VideoNotActive`` starts
succeeding. No callbacks, no renegotiation code.

Two instances make a self-contained demo (a switch in the middle would
need video support of its own — the bundled FreeSWITCH bench has none):

    SIP_LISTEN=127.0.0.1:5070 python 09_midcall_video.py
    SIP_LISTEN=127.0.0.1:5072 SIP_DIAL=sip:alice@127.0.0.1:5070 python 09_midcall_video.py

Watch the second terminal: video frames flow only between "adding
video" and "removing video". Against a registrar, use SIP_USER /
SIP_PASS / SIP_DOMAIN instead of SIP_LISTEN, as in example 08.

Environment:

    SIP_LISTEN   direct mode: bind here, skip registration, take calls directly
    SIP_DIAL     place the call (and drive the add/remove); otherwise answer
    ADD_AFTER    seconds of audio-only before video comes up (default 3)
    VIDEO_FOR    seconds of video before it goes down again (default 8)
    VIDEO_SIZE   "WxH" (default 320x240)

One timing detail worth copying into real applications: right after
establishment the ACK can still be in flight, and a re-INVITE cannot be
sent until it lands. ``set_video_direction`` (and the ``add_video`` /
``remove_video`` shorthands) then raise with "retry shortly" —
``change_video`` below shows the retry loop.
"""

import asyncio
import logging
import os

from baresip import (
    Account,
    BaresipError,
    Config,
    Runtime,
    UserAgent,
    VideoNotActive,
    VideoRestarted,
)

DOMAIN = os.environ.get("SIP_DOMAIN", "127.0.0.1:15060")
USER = os.environ.get("SIP_USER", "alice")
PASSWORD = os.environ.get("SIP_PASS", "")
DIAL = os.environ.get("SIP_DIAL")
LISTEN = os.environ.get("SIP_LISTEN")
ADD_AFTER = float(os.environ.get("ADD_AFTER", "3"))
VIDEO_FOR = float(os.environ.get("VIDEO_FOR", "8"))
try:
    W, H = (int(v) for v in os.environ.get("VIDEO_SIZE", "320x240").split("x"))
    if W < 2 or H < 2:
        raise ValueError
except ValueError:
    raise SystemExit(
        f"VIDEO_SIZE must be WxH, e.g. 320x240, got {os.environ.get('VIDEO_SIZE')!r}"
    ) from None


async def change_video(call, direction: str, timeout: float = 5.0):
    """The retry idiom for mid-call direction changes.

    A "retry shortly" failure means another session refresh (usually the
    establishment ACK) is still in flight; the staged direction is kept,
    so calling again simply sends the re-INVITE once the window clears.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        try:
            await call.set_video_direction(direction)
            return
        except BaresipError as e:
            if "retry" not in str(e) or asyncio.get_running_loop().time() > deadline:
                raise
            await asyncio.sleep(0.1)


async def stream_bars(call):
    """Feed moving bars whenever video is up; fail quietly when it is not.

    The same loop serves both roles: before video starts (and after it
    is removed) every write raises VideoNotActive, which is the normal
    idle state here, not an error.
    """
    luma_row = (bytes(range(0, 256, 8)) * (W // 32 + 1))[:W]
    chroma = b"\x80" * (2 * (W // 2) * (H // 2))
    shift = 0
    while True:
        row = luma_row[shift:] + luma_row[:shift]
        try:
            call.video.write_frame(row * H + chroma)
        except (VideoNotActive, VideoRestarted):
            pass
        shift = (shift + 4) % W
        await asyncio.sleep(1 / 15)


async def report_frames(call):
    """Print video up/down transitions, and a frame count while up."""
    up = False
    received = 0
    tick = asyncio.get_running_loop().time() + 1
    while True:
        try:
            frame = call.video.read_frame()
        except (VideoNotActive, VideoRestarted):
            frame = None
            if up:
                print("video went down")
                up = False
        if frame is not None:
            received += 1
            if not up:
                print("video came up")
                up = True
        now = asyncio.get_running_loop().time()
        if now >= tick:
            if up:
                print(f"  {received} frames received this second")
            received = 0
            tick = now + 1
        await asyncio.sleep(1 / 30)


async def main():
    logging.basicConfig(level=logging.WARNING, format="%(name)s: %(message)s")
    conf = Config(video_size=(W, H), video_fps=15.0).render()
    domain = DOMAIN
    if LISTEN:
        domain = LISTEN
        conf = f"sip_listen {LISTEN}\nnet_interface 127.0.0.1\n" + conf

    runtime = Runtime()
    await runtime.start(conf)
    try:
        ua = await UserAgent.create(
            runtime,
            Account(
                user=USER,
                password=PASSWORD,
                domain=domain,
                reg_interval=0 if LISTEN else 600,
            ),
        )
        if not LISTEN:
            await ua.register()

        if DIAL:
            # Caller: audio-only on the wire, but the video stream is
            # negotiated (inactive) so it can be activated later. A call
            # dialed with video=False could never add video.
            call = await ua.dial(DIAL, video="inactive")
            await call.wait_established()
            print(f"connected to {DIAL} — audio only")
        else:
            where = f"direct on {LISTEN}" if LISTEN else f"registered as {USER}@{domain}"
            print(f"{where}; waiting for a call ...")
            incoming: asyncio.Queue = asyncio.Queue()
            ua.on_incoming(incoming.put_nowait)
            call = await incoming.get()
            # video=True is the whole acceptance story: it gives the
            # call a video stream, and a mid-call video offer is then
            # auto-accepted with no further code.
            await call.answer(video=True)
            print(f"answered {call.peer} — audio only until the peer adds video")

        tasks = [
            asyncio.create_task(stream_bars(call)),
            asyncio.create_task(report_frames(call)),
        ]
        try:
            if DIAL:
                await asyncio.sleep(ADD_AFTER)
                print("adding video ...")
                await change_video(call, "sendrecv")
                await asyncio.sleep(VIDEO_FOR)
                print("removing video — audio continues ...")
                await change_video(call, "inactive")
                await asyncio.sleep(3)
                print("done; hanging up")
                await call.hangup()
            else:
                # Callee: just stay on the call until the peer hangs up.
                while call.state.value != "closed":
                    await asyncio.sleep(0.2)
                print("peer hung up")
        finally:
            for task in tasks:
                task.cancel()
    finally:
        await runtime.close()


if __name__ == "__main__":
    asyncio.run(main())
