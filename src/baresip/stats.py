#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""Per-call media statistics: live RTCP snapshots and the final report.

The stack attaches a statistics object to every ``CALL_RTCP`` event (one
per RTCP report the far end sends, typically every few seconds) and to
the final ``CALL_CLOSED`` event. :class:`CallStats` is the typed view:
obtain it live via :meth:`Call.on_rtcp <baresip.call.Call.on_rtcp>` or
after the fact from :attr:`Call.final_stats
<baresip.call.Call.final_stats>`.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class CallStats:
    """One snapshot of a call's media health.

    RTCP-derived fields (losses, jitter, round-trip time) are zero until
    the first RTCP exchange and reflect the most recent one — in a final
    report they can trail the call's end by up to one reporting interval.
    A call with no media at all reports zeros throughout.

    Parameters:
        duration_s: Seconds since the call was answered.
        setup_s: Seconds the call took to establish.
        tx_packets: RTP packets sent.
        tx_bytes: RTP payload bytes sent.
        tx_errors: Transmit errors.
        tx_avg_bitrate_bps: Average transmit bitrate.
        rx_packets: RTP packets received.
        rx_bytes: RTP payload bytes received.
        rx_errors: Receive errors.
        rx_avg_bitrate_bps: Average receive bitrate.
        tx_lost: Packets we sent that the far end reported lost.
        rx_lost: Packets the far end sent that never reached us.
        tx_jitter_ms: Inter-arrival jitter the far end measured.
        rx_jitter_ms: Inter-arrival jitter we measured.
        rtt_ms: Round-trip time from the RTCP exchange.
        jbuf_late: Frames that arrived too late for the jitter buffer.
        jbuf_lost: Frames the jitter buffer declared lost.
        jbuf_overflow: Jitter buffer overflows.
        jbuf_delay_ms: Current jitter buffer delay.
        jbuf_jitter_ms: Current jitter delay estimate.
        audio_tx_silence_frames: See :class:`~baresip.audio.AudioStats`.
        audio_tx_starved_frames: See :class:`~baresip.audio.AudioStats`.
        audio_rx_dropped_bytes: See :class:`~baresip.audio.AudioStats`.
        audio_rx_discarded_bytes: See :class:`~baresip.audio.AudioStats`.
    """

    duration_s: int = 0
    setup_s: int = 0
    tx_packets: int = 0
    tx_bytes: int = 0
    tx_errors: int = 0
    tx_avg_bitrate_bps: int = 0
    rx_packets: int = 0
    rx_bytes: int = 0
    rx_errors: int = 0
    rx_avg_bitrate_bps: int = 0
    tx_lost: int = 0
    rx_lost: int = 0
    tx_jitter_ms: float = 0.0
    rx_jitter_ms: float = 0.0
    rtt_ms: float = 0.0
    jbuf_late: int = 0
    jbuf_lost: int = 0
    jbuf_overflow: int = 0
    jbuf_delay_ms: int = 0
    jbuf_jitter_ms: int = 0
    audio_tx_silence_frames: int = 0
    audio_tx_starved_frames: int = 0
    audio_rx_dropped_bytes: int = 0
    audio_rx_discarded_bytes: int = 0

    @classmethod
    def _from_payload(cls, stats: dict) -> "CallStats":
        """Build from the event payload's stats object. Library-internal."""
        tx = stats.get("tx", {})
        rx = stats.get("rx", {})
        rtcp = stats.get("rtcp", {})
        jbuf = stats.get("jbuf", {})
        audio = stats.get("audio", {})
        return cls(
            duration_s=stats.get("duration", 0),
            setup_s=stats.get("setup", 0),
            tx_packets=tx.get("packets", 0),
            tx_bytes=tx.get("bytes", 0),
            tx_errors=tx.get("errors", 0),
            tx_avg_bitrate_bps=tx.get("avg_bitrate", 0),
            rx_packets=rx.get("packets", 0),
            rx_bytes=rx.get("bytes", 0),
            rx_errors=rx.get("errors", 0),
            rx_avg_bitrate_bps=rx.get("avg_bitrate", 0),
            tx_lost=rtcp.get("tx_lost", 0),
            rx_lost=rtcp.get("rx_lost", 0),
            tx_jitter_ms=rtcp.get("tx_jitter_us", 0) / 1000,
            rx_jitter_ms=rtcp.get("rx_jitter_us", 0) / 1000,
            rtt_ms=rtcp.get("rtt_us", 0) / 1000,
            jbuf_late=jbuf.get("late", 0),
            jbuf_lost=jbuf.get("lost", 0),
            jbuf_overflow=jbuf.get("overflow", 0),
            jbuf_delay_ms=jbuf.get("delay_ms", 0),
            jbuf_jitter_ms=jbuf.get("jitter_ms", 0),
            audio_tx_silence_frames=audio.get("tx_silence_frames", 0),
            audio_tx_starved_frames=audio.get("tx_starved_frames", 0),
            audio_rx_dropped_bytes=audio.get("rx_dropped", 0),
            audio_rx_discarded_bytes=audio.get("rx_discarded", 0),
        )

    @property
    def mos_estimate(self) -> float:
        """An *estimate* of the call's MOS, in [1.0, 5.0].

        A simplified ITU-T G.107 E-model computed from receive loss,
        receive jitter, and round-trip time, calibrated for narrowband
        codecs — treat it as indicative for wideband (opus). A loss-free
        low-latency call scores about 4.4, the model's ceiling.
        """
        loss_pct = 100.0 * self.rx_lost / max(1, self.rx_packets + self.rx_lost)
        effective = self.rtt_ms / 2 + 2 * self.rx_jitter_ms + 10.0
        r = 93.2 - (effective / 40 if effective < 160 else (effective - 120) / 10)
        r -= 2.5 * loss_pct
        r = max(0.0, min(100.0, r))
        mos = 1 + 0.035 * r + 7e-6 * r * (r - 60) * (100 - r)
        return max(1.0, min(5.0, round(mos, 2)))
