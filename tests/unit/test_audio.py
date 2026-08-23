#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""Programmatic audio: the Python face, without live media.

Real PCM flow needs a real call — that lives in the bench tests. These
pin the error surface around it: unknown calls, a closed runtime, input
validation, and the object plumbing on Call.
"""

import pytest

native = pytest.importorskip("baresip._native")

from baresip.audio import CallAudio
from baresip.call import Call, CallState
from baresip.errors import AudioNotActive
from baresip.runtime import Runtime


@pytest.fixture
async def runtime():
    rt = Runtime()
    await rt.start()
    yield rt
    await rt.close()


async def test_unknown_call_has_no_audio(runtime):
    audio = CallAudio(0x0BADF00D)
    with pytest.raises(AudioNotActive):
        audio.info()
    with pytest.raises(AudioNotActive):
        audio.read(320)
    with pytest.raises(AudioNotActive):
        audio.write(b"\x00" * 320)


async def test_audio_is_not_active_after_runtime_close():
    rt = Runtime()
    await rt.start()
    await rt.close()
    # The native gate is shut: a fail-fast error, never a crash.
    with pytest.raises(AudioNotActive):
        CallAudio(0x123).info()


def test_read_size_is_validated():
    with pytest.raises(ValueError):
        CallAudio(0x123).read(0)
    with pytest.raises(ValueError):
        CallAudio(0x123).read(-4)


async def test_call_audio_is_one_object_and_empty_write_is_free(runtime):
    call = Call(runtime, handle=0x123, ua_handle=0x1, state=CallState.OUTGOING)
    assert call.audio is call.audio
    # b"" short-circuits before touching the native layer at all.
    assert call.audio.write(b"") == 0
