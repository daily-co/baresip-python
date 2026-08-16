#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""Audit built artifacts for GPL/ffmpeg linkage.

Inspects the dynamic-library dependencies of built binaries (``otool -L`` on
macOS, ``ldd`` on Linux) and fails if any ffmpeg/x264 library appears. On a
developer machine this is an advisory diagnostic — it gates nothing locally.
The same audit runs in release CI, where it is the enforcement gate: no
artifact that links GPL code is ever published.

Usage: check_linkage.py [--allow-gpl] [paths...]
Without paths, audits the default build outputs if present.
"""

from __future__ import annotations

import argparse
import platform
import re as regex
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

GPL_LIB = regex.compile(
    r"lib(?:avcodec|avformat|avutil|avfilter|avdevice|swscale|swresample|x264)\."
)


def default_artifacts() -> list[Path]:
    candidates: list[Path] = []
    bs_build = REPO_ROOT / "third_party" / "baresip" / "build"
    for name in ("baresip", "test/selftest"):
        path = bs_build / name
        if path.exists():
            candidates.append(path)
    candidates.extend((REPO_ROOT / "src" / "baresip").glob("*.so"))
    candidates.extend((REPO_ROOT / "src" / "baresip").glob("*.dylib"))
    return candidates


def linked_libraries(binary: Path) -> list[str]:
    if platform.system() == "Darwin":
        cmd = ["otool", "-L", str(binary)]
    else:
        cmd = ["ldd", str(binary)]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise SystemExit(f"check_linkage: {' '.join(cmd)} failed: {proc.stderr.strip()}")
    return proc.stdout.splitlines()


def audit(paths: list[Path], allow_gpl: bool) -> int:
    if not paths:
        print("check_linkage: no artifacts found to audit (build first)", file=sys.stderr)
        return 0
    failures: dict[Path, list[str]] = {}
    for binary in paths:
        offending = [line.strip() for line in linked_libraries(binary) if GPL_LIB.search(line)]
        status = "GPL LINKAGE" if offending else "clean"
        print(f"  {binary}: {status}")
        if offending:
            for line in offending:
                print(f"    {line}")
            failures[binary] = offending
    if failures and not allow_gpl:
        print(
            "check_linkage: FAIL — GPL/ffmpeg libraries are linked. These artifacts must\n"
            "never be published. (If this is an intentional local build of ffmpeg-based\n"
            "modules, re-run with --allow-gpl.)",
            file=sys.stderr,
        )
        return 1
    print("check_linkage: OK")
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path)
    parser.add_argument(
        "--allow-gpl", action="store_true", help="report but do not fail on GPL linkage"
    )
    args = parser.parse_args(argv)
    paths = args.paths or default_artifacts()
    raise SystemExit(audit(paths, args.allow_gpl))


if __name__ == "__main__":
    main()
