#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""Install-side checks for a built wheel.

Runs in the clean per-wheel test environment cibuildwheel creates: imports
the installed package (never the source tree), audits the installed
extension's linkage — the GPL release gate: a wheel that links ffmpeg/x264
must never be published — drives one full Runtime start/close cycle, and
checks that the bundled libre was built with the carried patches.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent


def main() -> None:
    import baresip

    pkg_dir = Path(baresip.__file__).resolve().parent
    if SCRIPTS_DIR.parent in pkg_dir.parents:
        raise SystemExit("wheel_smoke: imported baresip from the source tree, not the wheel")

    extensions = sorted(pkg_dir.glob("_native*.so"))
    if not extensions:
        raise SystemExit(f"wheel_smoke: no compiled extension found in {pkg_dir}")
    subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "check_linkage.py"), *map(str, extensions)],
        check=True,
    )

    async def cycle() -> None:
        runtime = baresip.Runtime()
        await runtime.start()
        try:
            await carries_patches(runtime)
        finally:
            await runtime.close()

    async def carries_patches(runtime) -> None:
        """Fail if the wheel's libre lacks the carried sips: patch.

        Unpatched, the stack refuses a sips: target outright ("Function
        not implemented"). Patched, the dial is accepted and fails later
        on its own, nothing listening at the address.
        """
        account = baresip.Account(user="smoke", password="", domain="smoke.invalid", reg_interval=0)
        ua = await baresip.UserAgent.create(runtime, account)
        try:
            # TEST-NET-1: routable in form, answered by nobody.
            call = await ua.dial("sips:smoke@192.0.2.1:9")
        except (baresip.NoCaCertificatesError, baresip.NoLocalAddressError) as error:
            print(f"wheel_smoke: the sips: check was skipped ({type(error).__name__})")
            return
        except baresip.BaresipError as error:
            if "not implemented" in str(error).lower():
                raise SystemExit(
                    "wheel_smoke: the bundled libre refuses a sips: target; "
                    "it was built without third_party/patches"
                ) from None
            return
        await call.hangup()

    asyncio.run(cycle())
    carries_the_patch_texts()
    print("wheel_smoke: OK")


# Text only carried patches put into libre: the second one's warnings for
# a hang-up that could not be sent or was not delivered, and the third
# one's notice for a TLS next hop that may prove its call's host name.
PATCH_TEXTS = (
    b"BYE could not be sent",
    b"BYE was not delivered",
    b"may prove its address or",
)


def carries_the_patch_texts() -> None:
    """Fail if the wheel's libre lacks the second or third carried patch.

    Nothing short of a call in the state each one handles would log
    these, so the built extension is searched for their text instead.
    """
    import baresip._native as native

    with open(native.__file__, "rb") as extension:
        built = extension.read()
    missing = [text.decode() for text in PATCH_TEXTS if text not in built]
    if missing:
        raise SystemExit(
            f"wheel_smoke: the bundled libre lacks {missing}; "
            "it was built without third_party/patches"
        )


if __name__ == "__main__":
    main()
