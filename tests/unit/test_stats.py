#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""Call statistics: payload parsing, the MOS model, and the Call plumbing.

Real numbers from real calls live in the bench tests; these pin the
payload-to-dataclass mapping, the E-model's shape at its edges, and how
final_stats and on_rtcp behave — without a network.
"""

import logging

import pytest

native = pytest.importorskip("baresip._native")

from baresip.call import Call, CallState
from baresip.events import Event, StackEvent
from baresip.runtime import Runtime
from baresip.stats import CallStats

PAYLOAD = {
    "duration": 12,
    "setup": 1,
    "tx": {"packets": 600, "bytes": 96000, "errors": 0, "avg_bitrate": 64000},
    "rx": {"packets": 590, "bytes": 94400, "errors": 0, "avg_bitrate": 64000},
    "rtcp": {
        "tx_lost": 1,
        "rx_lost": 10,
        "tx_jitter_us": 2000,
        "rx_jitter_us": 3500,
        "rtt_us": 40000,
    },
    "jbuf": {"late": 2, "lost": 1, "overflow": 0, "delay_ms": 40, "jitter_ms": 12},
    "audio": {
        "tx_silence_frames": 50,
        "tx_starved_frames": 0,
        "rx_dropped": 0,
        "rx_discarded": 320,
    },
}


def test_payload_maps_to_typed_fields():
    stats = CallStats._from_payload(PAYLOAD)
    assert stats.duration_s == 12 and stats.setup_s == 1
    assert stats.tx_packets == 600 and stats.rx_bytes == 94400
    assert stats.tx_lost == 1 and stats.rx_lost == 10
    assert stats.rx_jitter_ms == 3.5 and stats.rtt_ms == 40.0  # us on the wire
    assert stats.jbuf_delay_ms == 40
    assert stats.audio_rx_discarded_bytes == 320


def test_missing_payload_pieces_default_to_zero():
    stats = CallStats._from_payload({})
    assert stats.tx_packets == 0 and stats.rtt_ms == 0.0
    assert stats.mos_estimate >= 4.3  # zeros read as a perfect, empty call


def test_mos_model_shape():
    perfect = CallStats(rx_packets=1000)
    assert 4.3 <= perfect.mos_estimate <= 4.5  # the narrowband ceiling

    lossy = CallStats(rx_packets=900, rx_lost=100)  # 10% loss
    assert lossy.mos_estimate <= 3.5

    awful = CallStats(rx_packets=100, rx_lost=900, rtt_ms=2000, rx_jitter_ms=500)
    assert awful.mos_estimate == 1.0  # clamped at the floor


@pytest.fixture
async def runtime():
    rt = Runtime()
    await rt.start()
    yield rt
    await rt.close()


async def test_closed_event_populates_final_stats_and_logs_once(runtime, caplog):
    call = Call(runtime, handle=0x123, ua_handle=0x1, state=CallState.ESTABLISHED)
    assert call.final_stats is None

    with caplog.at_level(logging.INFO, logger="baresip.call"):
        call._on_stack_event(
            StackEvent(event=Event.CALL_CLOSED, call=0x123, text="ok", stats=PAYLOAD)
        )

    assert call.final_stats is not None
    assert call.final_stats.tx_packets == 600
    records = [r for r in caplog.records if "call finished" in r.getMessage()]
    assert len(records) == 1
    assert records[0].mos_estimate == call.final_stats.mos_estimate
    assert records[0].rx_packets == 590  # every field rides as an extra


async def test_closed_without_stats_stays_none(runtime):
    call = Call(runtime, handle=0x123, ua_handle=0x1, state=CallState.ESTABLISHED)
    call._on_stack_event(StackEvent(event=Event.CALL_CLOSED, call=0x123))
    assert call.final_stats is None


async def test_rtcp_snapshots_are_typed_filtered_and_removable(runtime):
    call = Call(runtime, handle=0x123, ua_handle=0x1, state=CallState.ESTABLISHED)
    got: list = []
    call.on_rtcp(got.append)

    call._on_stack_event(StackEvent(event=Event.CALL_RTCP, call=0x123, stats=PAYLOAD))
    call._on_stack_event(StackEvent(event=Event.CALL_RTCP, call=0x123))  # no stats: dropped
    call._on_stack_event(StackEvent(event=Event.CALL_RTCP, call=0x999, stats=PAYLOAD))

    assert len(got) == 1 and isinstance(got[0], CallStats)
    assert got[0].rx_packets == 590

    call.off_rtcp(got.append)
    call._on_stack_event(StackEvent(event=Event.CALL_RTCP, call=0x123, stats=PAYLOAD))
    assert len(got) == 1
    call.off_rtcp(got.append)  # unknown callbacks are ignored
