#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""Programmatic PCM: read and write a call's audio as plain bytes.

Every :class:`~baresip.call.Call` exposes a :class:`CallAudio` at
``call.audio``. Receive audio — the decoded PCM the far end is sending —
is always tapped and readable, whatever audio driver the configuration
selected. Transmit audio comes from Python only when the configuration
selects ``audio_source aumem`` (the default ``Config``); under any other
source, :meth:`CallAudio.write` accepts nothing.

PCM is signed 16-bit interleaved, native byte order. Rates and channel
counts are whatever the call negotiated — :meth:`CallAudio.info` reports
them per direction. The buffers never block: a read returns what is
there, a write queues what fits. A reader that falls more than half a
buffer behind is skipped forward — oldest audio dropped — so live audio
stays live.
"""

import errno
from dataclasses import dataclass

from baresip._native import ffi, lib
from baresip.errors import AudioNotActive, AudioRestarted


@dataclass(frozen=True)
class AudioInfo:
    """A snapshot of one call's audio streams.

    Parameters:
        tx_ready: Python may feed the call (the aumem source is up).
        rx_ready: Decoded receive audio is being tapped.
        tx_sample_rate: Transmit rate in Hz (0 until ``tx_ready``).
        tx_channels: Transmit channel count.
        tx_ptime_ms: The transmit packet time — a natural write pace.
        rx_sample_rate: Receive rate in Hz (0 until ``rx_ready``).
        rx_channels: Receive channel count.
        tx_buffered: Bytes written but not yet transmitted.
        rx_buffered: Bytes received but not yet read.
        tx_capacity: Transmit buffer size in bytes.
        rx_capacity: Receive buffer size in bytes.
    """

    tx_ready: bool
    rx_ready: bool
    tx_sample_rate: int
    tx_channels: int
    tx_ptime_ms: int
    rx_sample_rate: int
    rx_channels: int
    tx_buffered: int
    rx_buffered: int
    tx_capacity: int
    rx_capacity: int


class CallAudio:
    """One call's PCM streams. Obtained via ``call.audio``, never built.

    Safe to use from any thread — including threads that never touch
    the event loop — with at most one reader and one writer at a time.
    Operations raise :class:`~baresip.errors.AudioNotActive` while the
    call has no media (or after it closed), and
    :class:`~baresip.errors.AudioRestarted` once mid-call renegotiation
    has replaced the streams; after the latter, the next operation binds
    to the new streams automatically.
    """

    def __init__(self, call_handle: int):
        """Bind to a call's audio slot. Library-internal.

        Args:
            call_handle: The native handle of the owning call.
        """
        self._handle = call_handle
        # The native slot stamps its streams with an epoch that advances
        # whenever they are torn down and replaced (mid-call
        # renegotiation). We remember the epoch we bound to and pass it
        # with every read and write; the native side refuses a mismatch
        # with ESTALE, which _checked() turns into AudioRestarted. None
        # means unbound — the next operation probes the slot and binds
        # to whatever streams are current.
        self._bound_epoch: int | None = None

    def __repr__(self) -> str:
        return f"<CallAudio call={self._handle:#x}>"

    def info(self) -> AudioInfo:
        """Probe the streams: readiness, formats, and buffer depths.

        Raises:
            AudioNotActive: the call has no audio (yet, or anymore).
        """
        info = ffi.new("struct bp_audio_info *")
        if lib.bp_audio_probe(self._handle, info) != 0:
            self._bound_epoch = None
            raise AudioNotActive("no audio is active on this call")
        self._bound_epoch = info.epoch
        return AudioInfo(
            tx_ready=bool(info.tx_ready),
            rx_ready=bool(info.rx_ready),
            tx_sample_rate=info.tx_srate,
            tx_channels=info.tx_ch,
            tx_ptime_ms=info.tx_ptime,
            rx_sample_rate=info.rx_srate,
            rx_channels=info.rx_ch,
            tx_buffered=info.tx_fill,
            rx_buffered=info.rx_fill,
            tx_capacity=info.tx_capacity,
            rx_capacity=info.rx_capacity,
        )

    def write(self, pcm) -> int:
        """Queue PCM for transmission; returns the bytes accepted.

        Never blocks: what does not fit in the buffer is not taken, and
        the transmit clock sends silence whenever the buffer runs dry —
        feed it steadily rather than far ahead.

        Args:
            pcm: A bytes-like object of 16-bit interleaved samples.

        Raises:
            AudioNotActive: the call has no audio (yet, or anymore).
            AudioRestarted: renegotiation replaced the streams.
        """
        if not pcm:
            return 0
        epoch = self._ensure_bound()
        n = lib.bp_audio_write(self._handle, epoch, ffi.from_buffer("uint8_t[]", pcm), len(pcm))
        return self._checked(n)

    def read(self, max_bytes: int) -> bytes:
        """Take up to ``max_bytes`` of received PCM; b"" when none yet.

        Never blocks. An empty result simply means nothing has arrived
        since the last read — sleep a frame and try again.

        Args:
            max_bytes: Upper bound on the returned length.

        Raises:
            AudioNotActive: the call has no audio (yet, or anymore).
            AudioRestarted: renegotiation replaced the streams.
        """
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        epoch = self._ensure_bound()
        buf = ffi.new("uint8_t[]", max_bytes)
        n = self._checked(lib.bp_audio_read(self._handle, epoch, buf, max_bytes))
        return bytes(ffi.buffer(buf, n))

    def _ensure_bound(self) -> int:
        """The epoch to hand to a native read or write.

        Probes first when unbound — a fresh object, or the previous
        operation failed — so every operation speaks to the current
        streams, or raises AudioNotActive from the probe itself.
        """
        if self._bound_epoch is None:
            self.info()
        return self._bound_epoch

    def _checked(self, n: int) -> int:
        """Pass a native byte count through, unbinding on failure.

        Any failure drops the binding so the next operation re-probes.
        ESTALE — the slot's epoch moved past ours because renegotiation
        replaced the streams — becomes AudioRestarted; anything else
        means no slot serves this call and becomes AudioNotActive.
        """
        if n >= 0:
            return n
        self._bound_epoch = None
        if -n == errno.ESTALE:
            raise AudioRestarted("the call's audio was renegotiated; buffered data was lost")
        raise AudioNotActive("no audio is active on this call")
