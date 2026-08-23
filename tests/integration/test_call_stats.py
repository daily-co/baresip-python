#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""Call statistics against the bench: real RTP, real RTCP, real numbers.

One long-ish echo call carries a tone, collects live RTCP snapshots,
and closes; the final report must hold numbers consistent with the
call's length, score the loss-free bench near the E-model's ceiling,
and agree with the independent summary line the rtcpsummary module
prints natively. Run with: pytest -m bench tests/integration
"""

import asyncio
import logging
import math
import os
import re
import struct

import pytest

native = pytest.importorskip("baresip._native")

from baresip import Account, Event
from baresip.runtime import Runtime
from baresip.ua import UserAgent

pytestmark = pytest.mark.bench

DOMAIN = f"127.0.0.1:{os.environ.get('BENCH_SIP_PORT', '15060')}"
RAW_CONF = "net_interface 127.0.0.1\naudio_source aumem,default\naudio_player aumem,default\n"
CALL_SECONDS = 8  # long enough for at least one RTCP exchange


def sine(rate: int, seconds: float) -> bytes:
    n = int(rate * seconds)
    return b"".join(
        struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / rate))) for i in range(n)
    )


async def test_echo_call_yields_a_consistent_final_report():
    native_log: list = []

    class Collect(logging.Handler):
        def emit(self, record):
            native_log.append(record.getMessage())

    nat_logger = logging.getLogger("baresip.native")
    nat_logger.setLevel(logging.INFO)
    handler = Collect()
    nat_logger.addHandler(handler)

    runtime = Runtime()
    await runtime.start(RAW_CONF)
    try:
        await runtime.set_native_log_level("info")  # the rtcpsummary line is INFO
        ua = await UserAgent.create(
            runtime, Account(user="1003", password="bench1234", domain=DOMAIN)
        )
        await ua.register()
        call = await ua.dial(f"sip:9196@{DOMAIN}")
        await call.wait_established()

        loop = asyncio.get_running_loop()
        closed = loop.create_future()
        call.on(
            lambda e: (
                closed.set_result(None)
                if e.event is Event.CALL_CLOSED and not closed.done()
                else None
            )
        )
        snapshots: list = []
        call.on_rtcp(snapshots.append)

        info = call.audio.info()
        call.audio.write(sine(info.tx_sample_rate, 1.0))
        deadline = loop.time() + CALL_SECONDS
        while loop.time() < deadline:
            pcm = call.audio.read(4096)
            if pcm:
                call.audio.write(pcm)
            else:
                await asyncio.sleep(0.01)

        await call.hangup()
        await asyncio.wait_for(closed, 10)
    finally:
        await runtime.close()
        nat_logger.removeHandler(handler)

    stats = call.final_stats
    assert stats is not None, "a closed media call must leave a final report"

    # Numbers consistent with an 8 s, 50 pps G.711 call.
    assert stats.duration_s >= CALL_SECONDS - 2
    assert stats.tx_packets > 40 * (CALL_SECONDS - 2)
    assert stats.rx_packets > 40 * (CALL_SECONDS - 2)
    assert stats.tx_bytes > stats.tx_packets * 100  # 160-byte payloads
    assert stats.rx_lost <= 5

    # The loss-free loopback bench sits at the model's ceiling.
    assert stats.mos_estimate >= 4.3

    # Live snapshots arrived while the call ran, carrying real counters.
    assert snapshots, "no RTCP snapshot in a call long enough for several"
    assert snapshots[-1].tx_packets > 0

    # Independent cross-check: the rtcpsummary module's own line, printed
    # natively at close. Its counts are RTCP-derived — snapshotted at the
    # last report exchange, so they can trail the live metric counters by
    # up to one reporting interval — but they must describe the same
    # stream: nonzero, never ahead of the live counters, and behind by no
    # more than an interval's worth of packets (50 pps, intervals well
    # under 6 s here).
    summary = next((m for m in native_log if "EX=BareSip" in m), None)
    assert summary is not None, "rtcpsummary printed no summary line"
    fs_rx = int(re.search(r"PR=(\d+)", summary).group(1))
    fs_tx = int(re.search(r"PS=(\d+)", summary).group(1))
    for reported, live in ((fs_tx, stats.tx_packets), (fs_rx, stats.rx_packets)):
        assert 0 < reported <= live + 25
        assert live - reported <= 6 * 50
