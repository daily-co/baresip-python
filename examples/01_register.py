#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""Register with a SIP server, stay registered until Ctrl-C, leave cleanly.

Configuration comes from the environment; the defaults match the local
FreeSWITCH bench (see bench/README.md — start it with `make bench-up`):

    SIP_USER=1001 SIP_PASS=bench1234 SIP_DOMAIN=127.0.0.1:15060 python 01_register.py
"""

import asyncio
import os
import signal

from baresip import Account, Runtime, UserAgent


async def main():
    # One Runtime per process: it owns the SIP thread everything runs on.
    runtime = Runtime()
    await runtime.start()
    try:
        account = Account(
            user=os.environ.get("SIP_USER", "1001"),
            password=os.environ.get("SIP_PASS", "bench1234"),
            domain=os.environ.get("SIP_DOMAIN", "127.0.0.1:15060"),
        )
        ua = await UserAgent.create(runtime, account)

        # Sends REGISTER and returns once the server confirms; a rejection
        # (wrong password, unreachable server) raises RegistrationError.
        await ua.register()
        print(f"registered as {account.user}@{account.domain} — Ctrl-C to exit", flush=True)

        # The clean-shutdown idiom: Ctrl-C sets an event and nothing gets
        # cancelled, so the teardown below runs normally.
        stop = asyncio.Event()
        asyncio.get_running_loop().add_signal_handler(signal.SIGINT, stop.set)
        await stop.wait()  # park here; re-registration is automatic

        await ua.unregister()  # tell the server we are going away
        print("unregistered", flush=True)
    finally:
        await runtime.close()  # stops the SIP thread; safe after any failure


if __name__ == "__main__":
    asyncio.run(main())
