#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""Programmatic video: read and write a call's frames as plain bytes.

A call made or answered with ``video=True`` exposes a :class:`CallVideo`
at ``call.video``. Transmit frames come from Python; decoded receive
frames are tapped for Python — both through the ``vidmem`` driver the
default configuration selects.

Frames are packed I420 (planes tightly packed: full-resolution luma,
then two quarter-resolution chroma planes), with the geometry fixed by
:class:`~baresip.config.Config.video_size` for both directions: a
written frame must be exactly one configured-size frame, and received
frames beyond that size are dropped and counted. Timestamps are in
microseconds. Neither side blocks: a write is refused when the transmit
ring is full, and a read returns ``None`` until a new frame has
arrived. A reader that falls behind is skipped forward to the newest
frame — live video stays live.
"""

import errno
import time
from dataclasses import dataclass

from baresip._native import ffi, lib
from baresip.errors import VideoNotActive, VideoRestarted


@dataclass(frozen=True)
class VideoInfo:
    """A snapshot of one call's video streams.

    Parameters:
        tx_ready: Python may feed the call (the vidmem source is up).
        rx_ready: Decoded receive frames have started arriving.
        width: Frame width, both directions (the configured geometry).
        height: Frame height, both directions.
        fps: The transmit pacing rate in frames per second.
        tx_frames: Frames handed to the encoder so far.
        tx_skipped: Stale frames the pacer skipped past (Python wrote
            faster than the pacing rate).
        rx_frames: Decoded frames delivered for reading so far.
        rx_dropped: Received frames dropped because nothing was reading.
        rx_oversize: Received frames dropped for exceeding the
            configured geometry.
    """

    tx_ready: bool
    rx_ready: bool
    width: int
    height: int
    fps: float
    tx_frames: int
    tx_skipped: int
    rx_frames: int
    rx_dropped: int
    rx_oversize: int


@dataclass(frozen=True)
class VideoFrame:
    """One received video frame.

    Parameters:
        data: Packed I420 bytes (``width*height*3/2``).
        width: Frame width in pixels.
        height: Frame height in pixels.
        timestamp_us: The frame's timestamp, microseconds.
    """

    data: bytes
    width: int
    height: int
    timestamp_us: int


class CallVideo:
    """One call's video frames. Obtained via ``call.video``, never built.

    ``info``, ``read_frame`` and ``write_frame`` are safe from any
    thread — including threads that never touch the event loop — with at
    most one reader and one writer at a time. Operations raise
    :class:`~baresip.errors.VideoNotActive` while the call has no video
    (or after it closed), and :class:`~baresip.errors.VideoRestarted`
    once mid-call renegotiation has replaced the streams; after the
    latter, the next operation binds to the new streams automatically.
    """

    def __init__(self, call_handle: int, runtime):
        """Bind to a call's video slot. Library-internal.

        Args:
            call_handle: The native handle of the owning call.
            runtime: The owning runtime (carries the keyframe request).
        """
        self._handle = call_handle
        self._runtime = runtime
        # Same discipline as CallAudio: remember the epoch we bound to,
        # pass it with every operation, and let an -ESTALE refusal
        # (renegotiation replaced the streams) surface as VideoRestarted.
        self._bound_epoch: int | None = None
        self._frame_size = 0

    def __repr__(self) -> str:
        return f"<CallVideo call={self._handle:#x}>"

    def info(self) -> VideoInfo:
        """Probe the streams: readiness, geometry, pacing, counters.

        Raises:
            VideoNotActive: the call has no video (yet, or anymore).
        """
        info = ffi.new("struct bp_video_info *")
        if lib.bp_video_probe(self._handle, info) != 0:
            self._bound_epoch = None
            raise VideoNotActive("no video is active on this call")
        self._bound_epoch = info.epoch
        self._frame_size = info.width * info.height + 2 * ((info.width + 1) // 2) * (
            (info.height + 1) // 2
        )
        return VideoInfo(
            tx_ready=bool(info.tx_ready),
            rx_ready=bool(info.rx_ready),
            width=info.width,
            height=info.height,
            fps=info.fps_x1000 / 1000.0,
            tx_frames=info.tx_frames,
            tx_skipped=info.tx_skipped,
            rx_frames=info.rx_frames,
            rx_dropped=info.rx_dropped,
            rx_oversize=info.rx_oversize,
        )

    def write_frame(self, i420, timestamp_us: int | None = None) -> bool:
        """Queue one frame for transmission.

        Never blocks. The pacer transmits the newest queued frame at the
        configured rate; writing faster than that rate simply skips the
        stale ones (counted in :attr:`VideoInfo.tx_skipped`). Writing
        nothing transmits nothing — video's silence.

        Args:
            i420: A bytes-like object of exactly one configured-size
                packed I420 frame.
            timestamp_us: Frame timestamp in microseconds; the monotonic
                clock when omitted.

        Returns:
            True when the frame was queued; False when it was refused —
            the ring is full, or the transmit direction is momentarily
            down (a renegotiation gap). Keep pacing; delivery resumes
            by itself.

        Raises:
            ValueError: the data is empty or not one configured frame.
            VideoNotActive: the call has no video (yet, or anymore).
            VideoRestarted: renegotiation replaced the streams.
        """
        if not i420:
            raise ValueError("i420 must not be empty")
        if timestamp_us is None:
            timestamp_us = time.monotonic_ns() // 1000
        epoch = self._ensure_bound()
        rc = lib.bp_video_write(
            self._handle, epoch, ffi.from_buffer("uint8_t[]", i420), len(i420), timestamp_us
        )
        if rc == -errno.EINVAL:
            raise ValueError(
                f"a frame must be exactly one configured-size packed I420 frame "
                f"({self._frame_size} bytes), got {len(i420)}"
            )
        if rc == -errno.ENOSPC:
            return False
        self._checked(rc)
        return True

    def read_frame(self) -> VideoFrame | None:
        """Take the next received frame; ``None`` when nothing new.

        Never blocks. A reader that has fallen behind is skipped forward
        to the newest frame.

        Raises:
            VideoNotActive: the call has no video (yet, or anymore).
            VideoRestarted: renegotiation replaced the streams.
        """
        epoch = self._ensure_bound()
        buf = ffi.new("uint8_t[]", self._frame_size)
        w = ffi.new("uint32_t *")
        h = ffi.new("uint32_t *")
        ts = ffi.new("uint64_t *")
        n = self._checked(lib.bp_video_read(self._handle, epoch, buf, self._frame_size, w, h, ts))
        if n == 0:
            return None
        return VideoFrame(
            data=bytes(ffi.buffer(buf, n)), width=w[0], height=h[0], timestamp_us=ts[0]
        )

    async def request_keyframe(self) -> None:
        """Ask the far end for a keyframe (an RTCP picture update).

        Useful when a consumer joins mid-stream and needs a decodable
        starting point sooner than the next natural keyframe. Damaged
        incoming video already triggers this automatically.

        Raises:
            StaleHandleError: the call is already gone.
        """
        from baresip.errors import StaleHandleError

        ev, _ = await self._runtime.cmd(lib.BP_CMD_CALL_VIDEO_KEYFRAME, args=str(self._handle))
        if ev == lib.BP_EV_STALE_HANDLE:
            raise StaleHandleError("call no longer exists")

    def _ensure_bound(self) -> int:
        """The epoch to hand to a native read or write."""
        if self._bound_epoch is None:
            self.info()
        return self._bound_epoch

    def _checked(self, n: int) -> int:
        """Pass a native return through, unbinding on failure."""
        if n >= 0:
            return n
        self._bound_epoch = None
        if -n == errno.ESTALE:
            raise VideoRestarted("the call's video was renegotiated; buffered frames were lost")
        raise VideoNotActive("no video is active on this call")
