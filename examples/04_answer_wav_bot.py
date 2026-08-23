#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""A bot with zero custom audio code: greet the caller, record the caller.

Audio drivers are pure configuration. Here the transmit side plays a WAV
file (the aufile source) and the receive side records to one (the aufile
player) — no code touches PCM, and ``Config(audio_source=...,
audio_player=...)`` is all it takes. When the greeting runs out the
stack says so with an END_OF_FILE call event and the call carries on.

Defaults match the local FreeSWITCH bench (see bench/README.md):

    SIP_USER=1001 SIP_PASS=bench1234 SIP_DOMAIN=127.0.0.1:15060 python 04_answer_wav_bot.py

Then, from another terminal, call the bot back through the bench:

    docker exec baresip-bench-freeswitch fs_cli -x "originate user/1001 &echo()"
"""

import asyncio
import math
import os
import struct
import wave

from baresip import Account, Event, Runtime, UserAgent

DOMAIN = os.environ.get("SIP_DOMAIN", "127.0.0.1:15060")
GREETING, RECORDING = "greeting.wav", "recording.wav"


def write_greeting(path: str, seconds: float = 2.0, rate: int = 8000) -> None:
    """A two-note greeting tone, so there is something to play."""
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        for i in range(int(rate * seconds)):
            freq = 440 if i < rate * seconds / 2 else 660
            w.writeframes(struct.pack("<h", int(8000 * math.sin(2 * math.pi * freq * i / rate))))


async def main():
    write_greeting(GREETING)
    runtime = Runtime()
    # Equivalent to Config(audio_source=f"aufile,{GREETING}",
    # audio_player=f"aufile,{RECORDING}") — raw text only because the
    # bench also needs the stack pinned to loopback.
    await runtime.start(
        f"net_interface 127.0.0.1\naudio_source aufile,{GREETING}\naudio_player aufile,{RECORDING}\n"
    )
    try:
        account = Account(
            user=os.environ.get("SIP_USER", "1001"),
            password=os.environ.get("SIP_PASS", "bench1234"),
            domain=DOMAIN,
        )
        ua = await UserAgent.create(runtime, account)
        await ua.register()

        incoming: asyncio.Queue = asyncio.Queue()
        ua.on_incoming(incoming.put_nowait)
        print(f"registered; waiting for a call to {account.user}@{DOMAIN} ...", flush=True)

        call = await incoming.get()
        print(f"call from {call.peer} — answering", flush=True)

        closed = asyncio.get_running_loop().create_future()

        def listener(event):
            if event.event is Event.END_OF_FILE:
                print("greeting finished — still recording the caller", flush=True)
            elif event.event is Event.CALL_CLOSED and not closed.done():
                closed.set_result(None)

        call.on(listener)
        await call.answer()
        try:
            await asyncio.wait_for(closed, 30)  # serve until the caller hangs up
            print("caller hung up", flush=True)
        except TimeoutError:
            await call.hangup()
            print("time is up, hanging up", flush=True)

        await ua.unregister()
    finally:
        await runtime.close()

    with wave.open(RECORDING, "rb") as w:
        print(f"recorded {w.getnframes() / w.getframerate():.1f} s into {RECORDING}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
