#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""Install-side checks for a built wheel.

Runs in the clean per-wheel test environment cibuildwheel creates: imports
the installed package (never the source tree), audits the installed
extension's linkage — the GPL release gate: a wheel that links ffmpeg/x264
must never be published — and drives one full Runtime start/close cycle.
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
        await runtime.close()

    asyncio.run(cycle())
    print("wheel_smoke: OK")


if __name__ == "__main__":
    main()
