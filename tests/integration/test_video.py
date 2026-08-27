#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""Video negotiation against the FreeSWITCH bench.

The bench's FreeSWITCH build carries no VP8 codec module, which makes
it the mixed-capability peer every real deployment eventually meets:
we offer video, the switch declines it, and the call must proceed as
a plain audio call with the video surface reporting exactly that.
(The full VP8 media path is proven by the direct-call tests in
tests/unit/test_video_call.py — no switch transcodes for us there.)
Run with: pytest -m bench tests/integration
"""

import asyncio
import os

import pytest

native = pytest.importorskip("baresip._native")
from baresip._native import lib
from test_telephony_gates import capture_sip_trace

from baresip import Account, Config, Runtime, UserAgent, VideoNotActive

pytestmark = pytest.mark.bench

DOMAIN = f"127.0.0.1:{os.environ.get('BENCH_SIP_PORT', '15060')}"


async def test_video_offer_degrades_gracefully():
    runtime = Runtime()
    # Raw configuration (the loopback bench needs net_interface pinned),
    # so the SIP trace is enabled by command below rather than by Config.
    await runtime.start(
        "net_interface 127.0.0.1\n" + Config(video_size=(320, 240), video_fps=15.0).render()
    )
    await runtime.cmd(lib.BP_CMD_SET_SIP_TRACE, args="1")
    try:
        ua = await UserAgent.create(
            runtime, Account(user="1003", password="bench1234", domain=DOMAIN)
        )
        await ua.register()

        with capture_sip_trace() as records:
            call = await ua.dial(f"sip:9196@{DOMAIN}", video=True)
            await call.wait_established()

        # The offer really carried video: our own trace shows the INVITE
        # with an m=video line and VP8 in it.
        invites = [r for r in records if "INVITE sip:9196" in r and "m=video" in r]
        assert invites, "the INVITE never offered video"
        assert any("VP8" in r for r in invites)

        # The switch declined video; the call is up as audio-only and
        # the video surface says so, typed.
        with pytest.raises(VideoNotActive):
            call.video.info()

        # Audio is unaffected: the echo still carries our PCM back.
        deadline = asyncio.get_running_loop().time() + 5
        got = b""
        call.audio.write(b"\x01\x02" * 800)
        while asyncio.get_running_loop().time() < deadline and len(got) < 320:
            got += call.audio.read(4096)
            await asyncio.sleep(0.02)
        assert len(got) >= 320, "audio did not flow on the degraded call"

        await call.hangup()
    finally:
        await runtime.close()
