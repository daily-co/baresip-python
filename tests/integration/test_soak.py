#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""Long-running soak: ten minutes of echo with the buffers watched.

Deliberately outside the ordinary bench run (`pytest -m soak` to run
it): a pacing bug shows up as drift, and drift needs minutes, not
seconds — a transmit clock built on relative sleeps loses about a
millisecond a minute, and the buffer depths are where that accumulates.
"""

import asyncio
import math
import os
import struct

import pytest

native = pytest.importorskip("baresip._native")

from baresip import Account
from baresip.call import CallState
from baresip.runtime import Runtime
from baresip.ua import UserAgent

pytestmark = [pytest.mark.bench, pytest.mark.soak]

DOMAIN = f"127.0.0.1:{os.environ.get('BENCH_SIP_PORT', '15060')}"
RAW_CONF = "net_interface 127.0.0.1\naudio_source aumem,default\naudio_player aumem,default\n"

SOAK_SECONDS = 600


async def test_ten_minute_echo_depth_stays_bounded():
    runtime = Runtime()
    await runtime.start(RAW_CONF)
    try:
        ua = await UserAgent.create(
            runtime, Account(user="1003", password="bench1234", domain=DOMAIN)
        )
        await ua.register()
        call = await ua.dial(f"sip:9196@{DOMAIN}")
        await call.wait_established()

        # Prime the loop with a second of tone, then echo whatever comes
        # back for the full duration, sampling the buffer depths 1/s.
        deadline = asyncio.get_running_loop().time() + 5
        info = None
        while asyncio.get_running_loop().time() < deadline:
            info = call.audio.info()
            if info.tx_ready and info.rx_ready:
                break
            await asyncio.sleep(0.05)
        assert info and info.tx_ready and info.rx_ready
        rate = info.tx_sample_rate
        call.audio.write(
            b"".join(
                struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / rate)))
                for i in range(rate)
            )
        )

        depths = []
        loop = asyncio.get_running_loop()
        start = loop.time()
        next_sample = start + 1
        while loop.time() - start < SOAK_SECONDS:
            pcm = call.audio.read(4096)
            if pcm:
                call.audio.write(pcm)
            else:
                await asyncio.sleep(0.01)
            if loop.time() >= next_sample:
                next_sample += 1
                info = call.audio.info()
                depths.append((info.tx_buffered, info.rx_buffered))

        assert call.state is CallState.ESTABLISHED, "the call must survive the soak"
        assert len(depths) >= SOAK_SECONDS * 9 // 10

        # Bounded means bounded the whole way: with the reader keeping
        # up, neither buffer should ever approach half its capacity —
        # a drifting transmit clock would show up here as a climb.
        max_tx = max(d[0] for d in depths)
        max_rx = max(d[1] for d in depths)
        assert max_tx < info.tx_capacity // 2, f"tx depth climbed to {max_tx}"
        assert max_rx < info.rx_capacity // 2, f"rx depth climbed to {max_rx}"

        await call.hangup()
        await ua.unregister()
    finally:
        await runtime.close()
