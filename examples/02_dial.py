#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""Dial out: call the bench's echo test, talk for ten seconds, hang up —
then show what a failed call looks like.

Audio here is a generated tone (ausine) echoed back by the far end — no
code touches PCM. For the programmatic path, where your code reads and
writes the call's audio, see 03_echo_aumem.py.

Defaults match the local FreeSWITCH bench (see bench/README.md):

    SIP_USER=1001 SIP_PASS=bench1234 SIP_DOMAIN=127.0.0.1:15060 python 02_dial.py
"""

import asyncio
import os

from baresip import Account, CallBusy, CallFailed, Config, Runtime, UserAgent

DOMAIN = os.environ.get("SIP_DOMAIN", "127.0.0.1:15060")


async def main():
    runtime = Runtime()
    # Tone out, recording in — and pinned to loopback, where the bench lives.
    await runtime.start(
        Config(
            net_interface="127.0.0.1",
            audio_source="ausine,440",
            audio_player="aufile,/tmp/dial_rx.wav",
        )
    )
    try:
        account = Account(
            user=os.environ.get("SIP_USER", "1001"),
            password=os.environ.get("SIP_PASS", "bench1234"),
            domain=DOMAIN,
        )
        ua = await UserAgent.create(runtime, account)
        await ua.register()

        # A good call: the echo service answers and reflects our tone.
        # Extra INVITE headers travel with the dial — P-Asserted-Identity
        # is the classic one, asserting who is calling toward a trunk that
        # trusts you (Twilio, carrier SBCs). Any authentication challenge
        # on the INVITE is answered with the account's credentials
        # automatically, like registration.
        call = await ua.dial(
            f"sip:9196@{DOMAIN}",
            headers={"P-Asserted-Identity": f"<sip:{account.user}@{DOMAIN}>"},
        )
        await call.wait_established()  # raises CallBusy/CallDeclined/... on failure
        print(f"talking to {call.peer} — holding the call for 10 s", flush=True)
        await asyncio.sleep(10)
        await call.hangup()
        print("hung up", flush=True)

        # A failed call: this extension always answers busy. Ordinary
        # telephony outcomes arrive as typed exceptions, not error codes.
        try:
            failed = await ua.dial(f"sip:9486@{DOMAIN}")
            await failed.wait_established()
        except CallBusy:
            print("9486 is busy — as expected", flush=True)
        except CallFailed as exc:
            print(f"call failed otherwise: {exc}", flush=True)

        await ua.unregister()
    finally:
        await runtime.close()


if __name__ == "__main__":
    asyncio.run(main())
