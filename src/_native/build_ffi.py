#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""cffi build recipe for the ``baresip._native`` extension.

API mode: the cdef below is compiled against the real headers and linked
statically against ``libre.a`` and ``libbaresip.a`` (by absolute path — a
plain ``-lre`` would pick up a system dylib if one exists). The static
libraries must already be built; run ``make native`` first.

Run as a script (``make ext``) to compile the extension in place for fast
C-side iteration: the shared object is copied into ``src/baresip/`` where the
editable install picks it up.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
from pathlib import Path

from cffi import FFI

REPO_ROOT = Path(__file__).resolve().parents[2]
NATIVE_PREFIX = REPO_ROOT / ".native"
BARESIP_SRC = REPO_ROOT / "third_party" / "baresip"

LIBRE_A = NATIVE_PREFIX / "lib" / "libre.a"
LIBBARESIP_A = BARESIP_SRC / "build" / "libbaresip.a"


def _openssl_lib_dir() -> Path | None:
    # Only macOS needs help locating OpenSSL: Homebrew's openssl@3 is keg-only,
    # so its lib dir is never on the default linker search path. On Linux the
    # distro's OpenSSL dev package installs into the standard system library
    # directories, so plain -lssl/-lcrypto resolves without an extra -L.
    if platform.system() == "Darwin":
        brew = shutil.which("brew")
        if brew:
            proc = subprocess.run(
                [brew, "--prefix", "openssl@3"], capture_output=True, text=True, check=False
            )
            if proc.returncode == 0:
                return Path(proc.stdout.strip()) / "lib"
    return None


ffi = FFI()

ffi.cdef("""
const char *bp_version(void);
""")

_library_dirs: list[str] = []
_libraries = ["ssl", "crypto", "z", "resolv", "m"]
# libbaresip before libre: baresip depends on re, and single-pass linkers
# resolve archives left to right.
_extra_link_args = [str(LIBBARESIP_A), str(LIBRE_A)]

if platform.system() == "Darwin":
    _extra_link_args += [
        "-framework",
        "SystemConfiguration",
        "-framework",
        "CoreFoundation",
    ]
    _ssl_dir = _openssl_lib_dir()
    if _ssl_dir:
        _library_dirs.append(str(_ssl_dir))
else:
    _libraries.append("pthread")

ffi.set_source(
    "baresip._native",
    '#include "shim.h"',
    sources=[str(Path(__file__).parent / "shim.c")],
    include_dirs=[
        str(Path(__file__).parent),
        str(NATIVE_PREFIX / "include" / "re"),
        str(BARESIP_SRC / "include"),
    ],
    library_dirs=_library_dirs,
    libraries=_libraries,
    extra_link_args=_extra_link_args,
)


def main() -> None:
    for lib in (LIBRE_A, LIBBARESIP_A):
        if not lib.exists():
            raise SystemExit(f"build_ffi: {lib} not found — run `make native` first")
    built = Path(ffi.compile(tmpdir=str(REPO_ROOT / "build" / "ffi"), verbose=True))
    dest = REPO_ROOT / "src" / "baresip" / built.name
    shutil.copy2(built, dest)
    print(f"build_ffi: OK\n  {dest}")


if __name__ == "__main__":
    main()
