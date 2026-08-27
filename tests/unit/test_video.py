#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""The vidmem frame machinery, without live media.

Real frame flow needs a real video call — that lives in the integration
tests. These drive the native frame rings deterministically through the
test-only slot commands: whole-frame semantics, drop-oldest catch-up,
the epoch contract, and the error surface around unknown calls and a
closed runtime.
"""

import json

import pytest

native = pytest.importorskip("baresip._native")

from baresip._native import ffi, lib

from baresip.errors import VideoNotActive
from baresip.runtime import Runtime
from baresip.video import CallVideo, VideoFrame

W, H, FPS = 64, 48, 15
FRAME = W * H + 2 * (W // 2) * (H // 2)  # packed I420 bytes
HANDLE = 7777


@pytest.fixture
async def runtime():
    rt = Runtime()
    await rt.start()
    yield rt
    await rt.close()


@pytest.fixture
async def slot(runtime):
    _, payload = await runtime.cmd(lib.BP_CMD_TEST_VIDEO_SLOT, args=f"{HANDLE} {W} {H} {FPS}")
    assert json.loads(payload)["handle"] == HANDLE
    yield runtime
    await runtime.cmd(lib.BP_CMD_TEST_VIDEO_SLOT, args=f"{HANDLE} 0 0 0")


def probe(handle=HANDLE):
    info = ffi.new("struct bp_video_info *")
    rc = lib.bp_video_probe(handle, info)
    return rc, info


def write(data, *, epoch=0, ts=0, handle=HANDLE):
    return lib.bp_video_write(handle, epoch, ffi.from_buffer("uint8_t[]", data), len(data), ts)


def read(max_len=FRAME, *, epoch=0, handle=HANDLE):
    buf = ffi.new("uint8_t[]", max_len)
    w = ffi.new("uint32_t *")
    h = ffi.new("uint32_t *")
    ts = ffi.new("uint64_t *")
    n = lib.bp_video_read(handle, epoch, buf, max_len, w, h, ts)
    return n, bytes(ffi.buffer(buf, n)) if n > 0 else b"", w[0], h[0], ts[0]


async def test_unknown_call_has_no_video(runtime):
    import errno

    rc, _ = probe(0x0BADF00D)
    assert rc == errno.ENOENT
    assert write(b"\x00" * FRAME, handle=0x0BADF00D) == -errno.ENOENT
    n, *_ = read(handle=0x0BADF00D)
    assert n == -errno.ENOENT


async def test_video_is_gone_after_runtime_close():
    import errno

    rt = Runtime()
    await rt.start()
    await rt.cmd(lib.BP_CMD_TEST_VIDEO_SLOT, args=f"{HANDLE} {W} {H} {FPS}")
    await rt.close()
    rc, _ = probe()
    assert rc == errno.ENOENT


async def test_probe_reports_geometry_and_readiness(slot):
    rc, info = probe()
    assert rc == 0
    assert (info.width, info.height) == (W, H)
    assert info.fps_x1000 == FPS * 1000
    assert info.tx_ready and info.rx_ready
    assert info.epoch == 0


async def test_frame_roundtrip_through_the_loop(slot):
    frame = bytes((i * 7) % 256 for i in range(FRAME))
    assert write(frame, ts=123_456) == 0

    _, payload = await slot.cmd(lib.BP_CMD_TEST_VIDEO_LOOP, args=str(HANDLE))
    assert json.loads(payload)["moved"] == 1

    n, data, w, h, ts = read()
    assert n == FRAME
    assert data == frame
    assert (w, h, ts) == (W, H, 123_456)
    # Nothing further queued.
    n, *_ = read()
    assert n == 0


async def test_write_length_must_be_one_configured_frame(slot):
    import errno

    assert write(b"\x00" * (FRAME - 1)) == -errno.EINVAL
    assert write(b"\x00" * (FRAME + 1)) == -errno.EINVAL


async def test_ring_full_refuses_and_counts(slot):
    import errno

    for i in range(4):  # ring capacity in frames
        assert write(bytes([i]) * FRAME) == 0
    assert write(b"\xff" * FRAME) == -errno.ENOSPC


async def test_reader_behind_skips_to_newest(slot):
    for i in range(4):
        assert write(bytes([i]) * FRAME, ts=i) == 0
    _, payload = await slot.cmd(lib.BP_CMD_TEST_VIDEO_LOOP, args=str(HANDLE))
    assert json.loads(payload)["moved"] == 4

    # Four frames behind is past the clamp: the read lands on the newest
    # frame and the backlog is gone.
    n, data, _, _, ts = read()
    assert n == FRAME
    assert data == bytes([3]) * FRAME
    assert ts == 3
    n, *_ = read()
    assert n == 0


async def test_small_buffer_is_msgsize_and_frame_stays(slot):
    import errno

    assert write(b"\x2a" * FRAME) == 0
    await slot.cmd(lib.BP_CMD_TEST_VIDEO_LOOP, args=str(HANDLE))

    n, *_ = read(max_len=16)
    assert n == -errno.EMSGSIZE
    # The frame was left queued; a proper read still gets it.
    n, data, *_ = read()
    assert n == FRAME
    assert data == b"\x2a" * FRAME


async def test_stale_epoch_is_typed(slot):
    import errno

    assert write(b"\x00" * FRAME, epoch=1) == -errno.ESTALE
    n, *_ = read(epoch=1)
    assert n == -errno.ESTALE


async def test_slot_drop_removes_the_slot(slot):
    import errno

    await slot.cmd(lib.BP_CMD_TEST_VIDEO_SLOT, args=f"{HANDLE} 0 0 0")
    rc, _ = probe()
    assert rc == errno.ENOENT


# -- the CallVideo wrapper over the same machinery ---------------------


async def test_wrapper_not_active_on_unknown_call(runtime):
    video = CallVideo(0x0BADF00D, runtime)
    with pytest.raises(VideoNotActive):
        video.info()
    with pytest.raises(VideoNotActive):
        video.read_frame()
    with pytest.raises(VideoNotActive):
        video.write_frame(b"\x00" * FRAME)


async def test_wrapper_not_active_after_runtime_close():
    rt = Runtime()
    await rt.start()
    await rt.close()
    with pytest.raises(VideoNotActive):
        CallVideo(0x123, rt).info()


async def test_wrapper_roundtrip_and_info(slot):
    video = CallVideo(HANDLE, slot)
    info = video.info()
    assert (info.width, info.height, info.fps) == (W, H, float(FPS))
    assert info.tx_ready and info.rx_ready

    frame = bytes((i * 3) % 256 for i in range(FRAME))
    assert video.write_frame(frame, timestamp_us=42) is True
    await slot.cmd(lib.BP_CMD_TEST_VIDEO_LOOP, args=str(HANDLE))

    got = video.read_frame()
    assert isinstance(got, VideoFrame)
    assert got.data == frame
    assert (got.width, got.height, got.timestamp_us) == (W, H, 42)
    assert video.read_frame() is None


async def test_wrapper_write_validates_size_and_reports_full(slot):
    video = CallVideo(HANDLE, slot)
    with pytest.raises(ValueError):
        video.write_frame(b"")
    with pytest.raises(ValueError):
        video.write_frame(b"\x00" * (FRAME - 2))
    for _ in range(4):
        assert video.write_frame(b"\x00" * FRAME) is True
    assert video.write_frame(b"\x00" * FRAME) is False
