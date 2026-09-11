#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""Warm transfer: consult, bridge the calls yourself, then splice them.

The receptionist pattern, in its two halves:

1. **Bridge-and-listen** — pure audio plumbing, no SIP: the bot pumps
   PCM between its two calls with ``call.audio``, so both parties talk
   through it while it stays in the path (a real bot could record,
   listen, or speak here).
2. **Splice-and-exit** — after ``BRIDGE_SECONDS`` the bot connects the
   parties directly with an attended transfer (a REFER carrying a
   Replaces header) and drops out; both of its legs end.

The bot registers, answers the first caller, and consults
``TRANSFER_TO``. The caller must reach the bot *bridged through the
switch* (dialing its extension) — a switch refuses to transfer a leg
that is not part of a bridge. With the local FreeSWITCH bench (see
bench/README.md), run the bot and then call it with the softphone
example from a second terminal:

    SIP_USER=1001 SIP_PASS=bench1234 SIP_DOMAIN=127.0.0.1:15060 \\
    TRANSFER_TO=sip:9196@127.0.0.1:15060 python 07_warm_transfer.py

    SIP_USER=1002 SIP_DIAL=sip:1001@127.0.0.1:15060 python 06_softphone.py
"""

import asyncio
import os

from baresip import Account, AudioNotActive, AudioRestarted, Config, Runtime, UserAgent

DOMAIN = os.environ.get("SIP_DOMAIN", "127.0.0.1:15060")
TRANSFER_TO = os.environ.get("TRANSFER_TO", f"sip:9196@{DOMAIN}")
BRIDGE_SECONDS = float(os.environ.get("BRIDGE_SECONDS", "8"))
# aumem audio both ways (the default driver); pinned to loopback, where
# the bench lives.
CONF = Config(net_interface="127.0.0.1")


async def pump(src, dst) -> None:
    # One direction of the bridge: what src's peer says, dst's peer
    # hears. 20 ms at a time; the rings absorb pacing jitter. Holds and
    # renegotiations swap the underlying streams mid-flight — the typed
    # audio errors just mean "try again", and the pump carries on until
    # it is cancelled.
    while True:
        try:
            pcm = src.audio.read(3840)
            if pcm:
                dst.audio.write(pcm)
        except (AudioNotActive, AudioRestarted):
            pass
        await asyncio.sleep(0.02)


async def main():
    runtime = Runtime()
    await runtime.start(CONF)
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
        print(f"registered as {account.user}@{account.domain}; waiting for a caller", flush=True)
        caller = await incoming.get()
        print(f"call from {caller.peer} — answering", flush=True)
        await caller.answer()
        await caller.wait_established()

        print(f"consulting {TRANSFER_TO} ...", flush=True)
        consult = await ua.dial(TRANSFER_TO)
        await consult.wait_established()

        # Strategy 1: bridge-and-listen. The bot is the wire between
        # its two calls for a while.
        print(f"bridging the calls for {BRIDGE_SECONDS:g} s", flush=True)
        bridge = [
            asyncio.create_task(pump(caller, consult)),
            asyncio.create_task(pump(consult, caller)),
        ]
        await asyncio.sleep(BRIDGE_SECONDS)
        for task in bridge:
            task.cancel()

        # Strategy 2: splice-and-exit. On success both of the bot's
        # legs end — the parties now talk directly.
        print("splicing the parties together ...", flush=True)
        await caller.attended_transfer(consult)
        print("transferred — the bot is out of the call. bye", flush=True)
    finally:
        await runtime.close()


if __name__ == "__main__":
    asyncio.run(main())
