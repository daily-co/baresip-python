#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""DTMF both ways: a mini-IVR that reacts to keys, and a caller that presses them.

Run without arguments to be the IVR: answer, then press 1 to hear a tone,
press 2 to hang up. Run with a SIP URI to be the caller instead: dial it
and press 1, then 2 — dialing the IVR from a second terminal drives one
with the other.

"Play a file" mid-call simply means writing its PCM through
``call.audio.write()``. Audio drivers are chosen in configuration when
the runtime starts and stay put — there is no mid-call driver
switching. Audio a program decides on at runtime is what the aumem
driver is for: it is just bytes the program writes.

Defaults match the local FreeSWITCH bench (see bench/README.md):

    SIP_USER=1001 SIP_PASS=bench1234 SIP_DOMAIN=127.0.0.1:15060 python 05_dtmf_ivr.py
    SIP_USER=1002 SIP_PASS=bench1234 python 05_dtmf_ivr.py sip:1001@127.0.0.1:15060
"""

import asyncio
import math
import os
import struct
import sys

from baresip import Account, CallState, Event, Runtime, UserAgent

DOMAIN = os.environ.get("SIP_DOMAIN", "127.0.0.1:15060")
RAW_CONF = "net_interface 127.0.0.1\naudio_source aumem,default\naudio_player aumem,default\n"


def tone(rate: int, seconds: float = 1.0, freq: int = 660) -> bytes:
    n = int(rate * seconds)
    return b"".join(
        struct.pack("<h", int(8000 * math.sin(2 * math.pi * freq * i / rate))) for i in range(n)
    )


async def serve(ua) -> None:
    """The IVR side: react to the caller's keys until they hang up."""
    incoming: asyncio.Queue = asyncio.Queue()
    ua.on_incoming(incoming.put_nowait)
    print("IVR ready — waiting for a call; press 1 for a tone, 2 to hang up", flush=True)

    call = await incoming.get()
    done = asyncio.get_running_loop().create_future()

    def on_digit(event):
        print(f"caller pressed {event.digit} ({event.duration_ms} ms)", flush=True)
        if event.digit == "1":
            info = call.audio.info()
            call.audio.write(tone(info.tx_sample_rate))
        elif event.digit == "2" and not done.done():
            done.set_result(None)

    def on_event(event):
        if event.event is Event.CALL_CLOSED and not done.done():
            done.set_result(None)

    call.on_dtmf(on_digit)
    call.on(on_event)
    await call.answer()
    await done
    if call.state is not CallState.CLOSED:
        await call.hangup()
    print("call finished", flush=True)


async def press(ua, uri: str) -> None:
    """The caller side: dial the IVR and press its keys."""
    call = await ua.dial(uri)
    await call.wait_established()
    print(f"connected to {uri}; pressing 1, then 2", flush=True)
    await call.send_dtmf("1")
    await asyncio.sleep(2)  # long enough to hear the tone come back
    await call.send_dtmf("2")  # asks the IVR to hang up
    await asyncio.sleep(2)


async def main():
    runtime = Runtime()
    await runtime.start(RAW_CONF)
    try:
        account = Account(
            user=os.environ.get("SIP_USER", "1001"),
            password=os.environ.get("SIP_PASS", "bench1234"),
            domain=DOMAIN,
        )
        ua = await UserAgent.create(runtime, account)
        await ua.register()
        if len(sys.argv) > 1:
            await press(ua, sys.argv[1])
        else:
            await serve(ua)
        await ua.unregister()
    finally:
        await runtime.close()


if __name__ == "__main__":
    asyncio.run(main())
