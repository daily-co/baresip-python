#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""A real softphone: your speakers and microphone on a SIP call.

The platform's hardware audio driver is picked automatically — coreaudio
on macOS, alsa on Linux. Use a headset: there is no echo cancellation, so
open speakers feed the far end back to itself.

Register and wait for a call (the first one is answered automatically):

    SIP_USER=1001 SIP_PASS=bench1234 SIP_DOMAIN=127.0.0.1:15060 python 06_softphone.py

Or dial out instead by naming a target:

    SIP_DIAL=sip:9196@127.0.0.1:15060 python 06_softphone.py

While the call is up, type on stdin:

    digits / * / #  + Enter   send DTMF
    h (or q)        + Enter   hang up and exit

Defaults match the local FreeSWITCH bench (see bench/README.md); its
extension 9196 is an echo service — you should hear yourself back.
"""

import asyncio
import os
import platform

from baresip import Account, CallState, Config, Event, Runtime, UserAgent

DOMAIN = os.environ.get("SIP_DOMAIN", "127.0.0.1:15060")
DRIVER = "coreaudio" if platform.system() == "Darwin" else "alsa"


async def main():
    runtime = Runtime()
    conf = Config(
        # The hardware driver both ways: what the far end says plays on
        # your speakers, and your microphone is what they hear.
        audio_driver=DRIVER,
        # The bench lives on loopback, which the stack's interface
        # discovery skips unless pinned. Real SIP domains need no pin.
        net_interface="127.0.0.1" if DOMAIN.startswith(("127.", "localhost")) else None,
    )
    await runtime.start(conf)
    try:
        account = Account(
            user=os.environ.get("SIP_USER", "1001"),
            password=os.environ.get("SIP_PASS", "bench1234"),
            domain=DOMAIN,
        )
        ua = await UserAgent.create(runtime, account)
        await ua.register()

        target = os.environ.get("SIP_DIAL")
        if target:
            print(f"dialing {target} ...", flush=True)
            call = await ua.dial(target)
            await call.wait_established()
        else:
            incoming: asyncio.Queue = asyncio.Queue()
            ua.on_incoming(incoming.put_nowait)
            print(
                f"registered as {account.user}@{account.domain}; waiting for a call"
                " — set SIP_DIAL=<uri> to dial out instead",
                flush=True,
            )
            call = await incoming.get()
            print(f"call from {call.peer} — answering", flush=True)
            await call.answer()

        def on_digit(event):
            print(f"\n<< received DTMF {event.digit}", flush=True)

        def on_event(event):
            if event.event is Event.CALL_CLOSED:
                print("\ncall ended — press Enter to exit", flush=True)

        call.on_dtmf(on_digit)
        call.on(on_event)

        print(f"call is up ({DRIVER}) — digits + Enter send DTMF, h + Enter hangs up", flush=True)
        while call.state is not CallState.CLOSED:
            # input() runs in a thread so the event loop — and with it the
            # call — keeps running while we wait at the prompt.
            line = (await asyncio.to_thread(input, "> ")).strip()
            if call.state is CallState.CLOSED:
                break
            if line in ("h", "q"):
                await call.hangup()
                break
            for digit in line:
                if digit in "0123456789*#":
                    await call.send_dtmf(digit)

        await ua.unregister()
        print("bye", flush=True)
    finally:
        await runtime.close()


if __name__ == "__main__":
    asyncio.run(main())
