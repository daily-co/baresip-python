#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""Programmatic PCM: your code reads and writes the call's audio.

The aumem driver (the default) gives each call a pair of buffers:
read() returns what the far end is saying, write() queues what to say —
plain bytes of 16-bit PCM, no sound card, no files. This is the shape a
voice agent needs: the stack does SIP, RTP, and codecs; your code just
handles samples.

Defaults match the local FreeSWITCH bench (see bench/README.md):

    SIP_USER=1001 SIP_PASS=bench1234 SIP_DOMAIN=127.0.0.1:15060 python 03_echo_aumem.py
"""

import asyncio
import math
import os
import struct

from baresip import Account, AudioNotActive, Runtime, UserAgent

DOMAIN = os.environ.get("SIP_DOMAIN", "127.0.0.1:15060")


async def main():
    runtime = Runtime()
    # aumem both ways; pinned to loopback, where the bench lives.
    await runtime.start(
        "net_interface 127.0.0.1\naudio_source aumem,default\naudio_player aumem,default\n"
    )
    try:
        account = Account(
            user=os.environ.get("SIP_USER", "1001"),
            password=os.environ.get("SIP_PASS", "bench1234"),
            domain=DOMAIN,
        )
        ua = await UserAgent.create(runtime, account)
        await ua.register()

        call = await ua.dial(f"sip:9196@{DOMAIN}")  # the bench's echo service
        await call.wait_established()

        info = call.audio.info()
        print(f"audio up: {info.tx_sample_rate} Hz, {info.tx_channels} ch", flush=True)

        # Say something: one second of a 440 Hz tone, generated as PCM.
        rate = info.tx_sample_rate
        tone = b"".join(
            struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / rate)))
            for i in range(rate)
        )
        call.audio.write(tone)

        # Then echo: whatever the far end sends goes straight back. 9196
        # is itself an echo service, so the tone keeps bouncing between
        # us — your speakers are not involved, only your code is.
        echoed = 0
        deadline = asyncio.get_running_loop().time() + 10
        while asyncio.get_running_loop().time() < deadline:
            try:
                pcm = call.audio.read(4096)
            except AudioNotActive:
                break  # the far end hung up under us
            if pcm:
                echoed += call.audio.write(pcm)
            else:
                await asyncio.sleep(0.01)
        print(f"echoed {echoed} bytes of the far end's audio back at it", flush=True)

        await call.hangup()
        await ua.unregister()
    finally:
        await runtime.close()


if __name__ == "__main__":
    asyncio.run(main())
