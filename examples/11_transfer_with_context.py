#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""Transfer a caller to a human agent, with what the bot learned.

A bot that hands a caller over passes context with the transfer, so the
agent does not ask again. ``call.transfer(uri, headers={...})`` attaches
the headers to the address the far end is told to call; the far end sets
them on the call it places, and the agent's side reads them as ordinary
headers. ``User-to-User`` is the standard header for this and the one
contact-center platforms read, usually as hex data ending in
``;encoding=hex``.

This example plays all three parts in one process, on loopback, with no
server:

- the **bot** answers a caller, then transfers them with context;
- the **caller's phone** is asked to call somewhere else. It sees the
  context in ``TransferRequest.headers`` and accepts, which places the
  new call with those headers on it;
- the **agent desk** receives that call and prints the context. It lists
  the header names in ``Config(expose_headers=...)``: a received header
  is only exposed when asked for.

    python 11_transfer_with_context.py

Against a real platform only the bot's part is yours; whether the context
arrives is up to the party being transferred. See docs/TRANSFER.md.
"""

import asyncio

from baresip import Account, Config, Runtime, UserAgent

ADDRESS = "127.0.0.1:5790"
CASE = {"caller": "Dana", "reason": "refund", "order": "A-1042"}


def to_uui(text: str) -> str:
    """Text as a User-to-User value: hex data, marked as such."""
    return f"{text.encode().hex()};encoding=hex"


def from_uui(value: str) -> str:
    return bytes.fromhex(value.partition(";")[0]).decode()


async def main():
    runtime = Runtime()
    await runtime.start(
        Config(
            net_interface="127.0.0.1",
            # All three parties live in this one process, so their calls
            # add up past the default limit.
            max_concurrent_calls=None,
            expose_headers=("User-to-User", "X-Order"),
            extra_config_text=f"sip_listen {ADDRESS}\n",
        )
    )
    try:
        agents = {}
        for user in ("bot", "caller", "desk"):
            agents[user] = await UserAgent.create(
                runtime, Account(user=user, password="", domain=ADDRESS, reg_interval=0)
            )
        to_bot: asyncio.Queue = asyncio.Queue()
        to_desk: asyncio.Queue = asyncio.Queue()
        agents["bot"].on_incoming(to_bot.put_nowait)
        agents["desk"].on_incoming(to_desk.put_nowait)

        # The caller rings the bot, and the bot answers.
        callers_call = await agents["caller"].dial(f"sip:bot@{ADDRESS}")
        bots_call = await to_bot.get()
        await bots_call.answer()
        await callers_call.wait_established()
        await bots_call.wait_established()
        print("bot: talking to the caller", flush=True)

        # The caller's phone: do what a transfer request asks, keeping
        # the context it carries.
        async def follow(request):
            print(f"caller's phone: asked to call {request.target}", flush=True)
            await callers_call.accept_transfer()

        callers_call.on_transfer_request(lambda request: asyncio.ensure_future(follow(request)))

        # The bot hands the caller to the desk, with the case so far.
        summary = ", ".join(f"{key}={value}" for key, value in CASE.items())
        transfer = asyncio.ensure_future(
            bots_call.transfer(
                f"sip:desk@{ADDRESS}",
                headers={"User-to-User": to_uui(summary), "X-Order": CASE["order"]},
            )
        )

        # The desk gets a call that already says who is calling and why.
        desks_call = await to_desk.get()
        print(f"desk: call from the caller, order {desks_call.headers['X-Order']}", flush=True)
        print(f"desk: context: {from_uui(desks_call.headers['User-to-User'])}", flush=True)
        await desks_call.answer()

        await transfer
        print(f"bot: transferred, my call is {bots_call.state.value}", flush=True)
        await desks_call.hangup()
    finally:
        await runtime.close()


if __name__ == "__main__":
    asyncio.run(main())
