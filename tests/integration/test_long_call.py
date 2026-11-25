#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""Calls that outlast a session refresh.

FreeSWITCH asks for a session timer of 120 s and refreshes the session
with a re-INVITE about every 60 s. A call has to answer those and keep
carrying audio; a stack that mishandles them is dropped by the switch a
minute or two in, which no short test would show.

Over TLS: the signalling connection stays open, so the switch's
requests reach the agent however long the call has been quiet.

    make bench-up
    pytest -m bench tests/integration
"""

import asyncio
import contextlib
import uuid

import pytest

pytest.importorskip("baresip._native")

from test_telephony_gates import BENCH_CERT, TLS_DOMAIN, capture_sip_trace, fs_cli

from baresip import Account, AudioRestarted, CallState, Config
from baresip.runtime import Runtime
from baresip.ua import UserAgent

pytestmark = pytest.mark.bench

# Long enough for two refreshes.
DURATION = 130.0


def received_bytes(call) -> int:
    with contextlib.suppress(AudioRestarted):  # a refresh restarts the stream
        return len(call.audio.read(1 << 22))
    return 0


async def test_calls_in_both_directions_outlast_two_session_refreshes(monkeypatch):
    monkeypatch.setenv("SIP_CA_FILE", str(BENCH_CERT))
    runtime = Runtime()
    await runtime.start(
        Config(
            net_interface="127.0.0.1",
            max_concurrent_calls=None,
            audio_source="ausine,440",
            sip_trace=True,
        )
    )
    try:
        agents = {}
        for user in ("1003", "1004"):
            agents[user] = await UserAgent.create(
                runtime,
                Account(user=user, password="bench1234", domain=TLS_DOMAIN, transport="tls"),
            )
            await agents[user].register()
        incoming: asyncio.Queue = asyncio.Queue()
        agents["1003"].on_incoming(incoming.put_nowait)

        with capture_sip_trace() as records:
            await asyncio.to_thread(
                fs_cli, f"bgapi originate {{origination_uuid={uuid.uuid4()}}}user/1003 &echo()"
            )
            received = await asyncio.wait_for(incoming.get(), 10)
            await received.answer()
            placed = await agents["1004"].dial(f"sip:9196@{TLS_DOMAIN};transport=tls")
            await received.wait_established(timeout=10)
            await placed.wait_established(timeout=10)

            loop = asyncio.get_running_loop()
            end = loop.time() + DURATION
            late = {"received": 0, "placed": 0}
            while loop.time() < end:
                await asyncio.sleep(0.5)
                counts = {"received": received_bytes(received), "placed": received_bytes(placed)}
                if end - loop.time() < 10:
                    for name, count in counts.items():
                        late[name] += count

            assert received.state is CallState.ESTABLISHED
            assert placed.state is CallState.ESTABLISHED
            # Audio still arrives in the last ten seconds of each call.
            assert late["received"] > 0 and late["placed"] > 0
            await placed.hangup()
            await received.hangup()

        starts = [
            (lines[0][:2], lines[1])
            for lines in (record.splitlines() for record in records)
            if len(lines) > 1
        ]
        refreshes = [line for way, line in starts if way == "RX" and line.startswith("INVITE ")]
        # One INVITE set the received call up; the rest are refreshes, two per call.
        assert len(refreshes) >= 1 + 4
    finally:
        await asyncio.to_thread(fs_cli, "hupall")
        await runtime.close()
