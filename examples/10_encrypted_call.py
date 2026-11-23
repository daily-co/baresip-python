#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""Encrypted calls: one account setting, and a call that says what it got.

``Account(media_encryption=...)`` decides whether a call's audio and video
are encrypted with SRTP:

- ``"none"`` (the default) places unencrypted calls and, for a call it
  receives, follows the caller: SRTP when the caller offers it,
  unencrypted otherwise.
- ``"srtp"`` encrypts every call, and refuses a call that offers no SRTP
  before the application ever sees it.

``call.media_encrypted`` reports what a call ended up with. The SRTP keys
travel in the call setup, so this example registers over TLS; the bench's
certificate is self-signed, hence ``SIP_CA_FILE``.

Run without arguments to answer calls, with a SIP URI to place one.
Against the local FreeSWITCH bench (see bench/README.md):

    # Answer whatever arrives, encrypted or not:
    SIP_CA_FILE=bench/certs/cert.pem python 10_encrypted_call.py
    docker exec baresip-bench-freeswitch fs_cli -x \\
        "originate {rtp_secure_media=mandatory}user/1001 &echo()"   # SRTP
    docker exec baresip-bench-freeswitch fs_cli -x "originate user/1001 &echo()"

    # Accept only encrypted calls: the second originate above now fails.
    SIP_ENCRYPTION=srtp SIP_CA_FILE=bench/certs/cert.pem python 10_encrypted_call.py

    # Place an encrypted call to the bench's echo service:
    SIP_ENCRYPTION=srtp SIP_USER=1002 SIP_CA_FILE=bench/certs/cert.pem \\
        python 10_encrypted_call.py "sip:9196@127.0.0.1:15061;transport=tls"
"""

import asyncio
import os
import sys

from baresip import Account, CallFailed, CallState, Config, Event, Runtime, UserAgent

DOMAIN = os.environ.get("SIP_DOMAIN", "127.0.0.1:15061")
ENCRYPTION = os.environ.get("SIP_ENCRYPTION", "none")
# aumem audio both ways (the default driver); pinned to loopback, where
# the bench lives.
CONF = Config(net_interface="127.0.0.1")


def describe(call) -> str:
    return "encrypted with SRTP" if call.media_encrypted else "NOT encrypted"


async def answer_calls(ua) -> None:
    """Answer each call and report its encryption, until interrupted."""
    incoming: asyncio.Queue = asyncio.Queue()
    ua.on_incoming(incoming.put_nowait)
    print(f"ready (media_encryption={ENCRYPTION!r}) — waiting for calls", flush=True)

    while True:
        call = await incoming.get()
        closed = asyncio.get_running_loop().create_future()

        def on_event(event, closed=closed):
            if event.event is Event.CALL_CLOSED and not closed.done():
                closed.set_result(None)

        call.on(on_event)
        await call.answer()
        await call.wait_established()
        print(f"call from {call.peer}: {describe(call)}", flush=True)
        await closed
        print("call finished", flush=True)


async def place_call(ua, uri: str) -> None:
    """Dial ``uri``, report the call's encryption, and hang up."""
    call = await ua.dial(uri)
    try:
        await call.wait_established()
    except CallFailed as failure:
        # With "srtp", a far end that cannot do SRTP refuses the call
        # (488 Not Acceptable Here) instead of taking it unencrypted.
        print(f"call to {uri} failed: {failure}", flush=True)
        return
    print(f"connected to {uri}: {describe(call)}", flush=True)
    await asyncio.sleep(3)
    if call.state is not CallState.CLOSED:
        await call.hangup()


async def main():
    runtime = Runtime()
    await runtime.start(CONF)
    try:
        account = Account(
            user=os.environ.get("SIP_USER", "1001"),
            password=os.environ.get("SIP_PASS", "bench1234"),
            domain=DOMAIN,
            transport="tls",
            media_encryption=ENCRYPTION,
        )
        ua = await UserAgent.create(runtime, account)
        await ua.register()
        if len(sys.argv) > 1:
            await place_call(ua, sys.argv[1])
        else:
            await answer_calls(ua)
        await ua.unregister()
    finally:
        await runtime.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
