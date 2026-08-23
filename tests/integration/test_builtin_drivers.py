#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""Built-in audio drivers against the bench: a call served with zero
custom audio code.

The aufile source plays a generated greeting at the caller and the
aufile player records the caller to disk. The bench leg is echo(), so
the recording should carry the greeting back — proving both drivers,
the END_OF_FILE event, and that ``call.audio`` reads still work under a
player that is not aumem. Run with: pytest -m bench tests/integration
"""

import array
import asyncio
import math
import os
import shutil
import struct
import subprocess
import tempfile
import wave

import pytest

native = pytest.importorskip("baresip._native")

from baresip import Account, Event
from baresip.call import CallState
from baresip.runtime import Runtime
from baresip.ua import UserAgent

pytestmark = pytest.mark.bench

DOMAIN = f"127.0.0.1:{os.environ.get('BENCH_SIP_PORT', '15060')}"
CONTAINER = "baresip-bench-freeswitch"
ORIGINATE = "bgapi originate user/1003 &echo()"

TONE_RMS_FLOOR = 1000


def write_greeting(path: str, seconds: float = 1.0, rate: int = 8000) -> None:
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        for i in range(int(rate * seconds)):
            w.writeframes(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / rate))))


def peak_window_rms(pcm: bytes, rate: int, window_ms: int = 100) -> float:
    samples = array.array("h", pcm[: len(pcm) - len(pcm) % 2])
    win = max(1, rate * window_ms // 1000)
    peak = 0.0
    for start in range(0, max(1, len(samples) - win), win):
        chunk = samples[start : start + win]
        if chunk:
            peak = max(peak, math.sqrt(sum(s * s for s in chunk) / len(chunk)))
    return peak


async def fs_cli(command: str) -> str:
    result = await asyncio.to_thread(
        subprocess.run,
        ["docker", "exec", CONTAINER, "fs_cli", "-x", command],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.stdout


async def test_wav_bot_records_the_caller(bench_paths):
    recording, incoming = bench_paths
    await fs_cli(ORIGINATE)
    call = await asyncio.wait_for(incoming.get(), 10)

    loop = asyncio.get_running_loop()
    eof = loop.create_future()
    closed = loop.create_future()

    def listener(event):
        if event.event is Event.END_OF_FILE and not eof.done():
            eof.set_result(None)
        elif event.event is Event.CALL_CLOSED and not closed.done():
            closed.set_result(None)

    call.on(listener)
    await call.answer()

    # The 1 s greeting runs out and the stack must say so — while the
    # call stays up and keeps recording.
    await asyncio.wait_for(eof, 10)
    assert call.state is CallState.ESTABLISHED

    # The receive tap works under any player: the echoed greeting is
    # also readable as PCM alongside the recording.
    tapped = bytearray()
    deadline = loop.time() + 1.5
    while loop.time() < deadline:
        pcm = call.audio.read(4096)
        if pcm:
            tapped += pcm
        else:
            await asyncio.sleep(0.01)
    assert tapped, "call.audio.read() must deliver under the aufile player"

    # And the aumem write path is inert under a foreign source: nothing
    # is accepted, nothing raises.
    assert call.audio.write(b"\x00" * 320) == 0

    await call.hangup()
    await asyncio.wait_for(closed, 10)

    # The recording is a real, playable WAV of the call's length, and
    # the echoed greeting tone is in it.
    with wave.open(recording, "rb") as w:
        rate = w.getframerate()
        frames = w.readframes(w.getnframes())
    assert w.getnchannels() == 1
    assert len(frames) >= rate  # at least a second landed on disk
    assert peak_window_rms(frames, rate) > TONE_RMS_FLOOR


@pytest.fixture
async def bench_paths():
    # Not pytest's tmp_path: the stack keeps audio devices in a fixed
    # 128-byte buffer, and pytest's deeply nested tmp directories push
    # the WAV paths past it — the silently truncated path then names
    # nothing and the source never starts. Config validation catches
    # this for applications; raw configuration text does not.
    tmp = tempfile.mkdtemp(prefix="bp-")
    greeting = f"{tmp}/greeting.wav"
    recording = f"{tmp}/recording.wav"
    write_greeting(greeting)

    runtime = Runtime()
    await runtime.start(
        f"net_interface 127.0.0.1\naudio_source aufile,{greeting}\naudio_player aufile,{recording}\n"
    )
    ua = await UserAgent.create(runtime, Account(user="1003", password="bench1234", domain=DOMAIN))
    await ua.register()
    incoming: asyncio.Queue = asyncio.Queue()
    ua.on_incoming(incoming.put_nowait)
    try:
        yield recording, incoming
    finally:
        await fs_cli("hupall NORMAL_CLEARING")
        await runtime.close()
        shutil.rmtree(tmp, ignore_errors=True)
