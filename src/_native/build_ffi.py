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


def _brew_lib_dir(formula: str) -> Path | None:
    # Only macOS needs help locating these: Homebrew's openssl@3 is keg-only, so
    # its lib dir is never on the default linker search path, and opus lives
    # under the Homebrew prefix rather than a system one. On Linux the distro
    # dev packages install into the standard system library directories, so
    # plain -lssl/-lopus resolves without an extra -L.
    if platform.system() == "Darwin":
        brew = shutil.which("brew")
        if brew:
            proc = subprocess.run(
                [brew, "--prefix", formula], capture_output=True, text=True, check=False
            )
            if proc.returncode == 0:
                return Path(proc.stdout.strip()) / "lib"
    return None


ffi = FFI()

ffi.cdef("""
#define BP_CMD_PING ...
#define BP_CMD_STOP ...
#define BP_CMD_SET_LOG_LEVEL ...
#define BP_CMD_SET_SIP_TRACE ...
#define BP_CMD_SET_EXPOSE_HEADERS ...
#define BP_CMD_UA_ALLOC ...
#define BP_CMD_UA_REGISTER ...
#define BP_CMD_UA_UNREGISTER ...
#define BP_CMD_CALL_ANSWER ...
#define BP_CMD_CALL_REJECT ...
#define BP_CMD_CALL_HANGUP ...
#define BP_CMD_UA_CONNECT ...

#define BP_CMD_TEST_EMIT ...
#define BP_CMD_TEST_ESCAPE ...
#define BP_CMD_TEST_HANDLE_NEW ...
#define BP_CMD_TEST_HANDLE_DROP ...
#define BP_CMD_TEST_HANDLE_PROBE ...

#define BP_EV_PONG ...
#define BP_EV_DONE ...
#define BP_EV_STALE_HANDLE ...
#define BP_EV_BASE ...

int bp_bevent_max(void);
const char *bp_bevent_str(int ev);

#define BP_LOG_DEBUG ...
#define BP_LOG_INFO ...
#define BP_LOG_WARN ...
#define BP_LOG_ERROR ...

#define BP_LOG_CH_MAIN ...
#define BP_LOG_CH_SIP ...

struct bp_log_rec {
    uint32_t level;
    uint32_t channel;
    uint32_t dropped;
    uint32_t len;
    char msg[...];
};

const char *bp_version(void);

int  bp_init(void);
void bp_close(void);

int  bp_loop_init(const char *conf_dir, const char *config_text, int log_level);
int  bp_loop_run(void);
int  bp_loop_done(void);

int  bp_cmd(int cmd, uint32_t handle, const char *json_args);

void bp_log_start(void);
void bp_log_stop(void);
int  bp_log_read(struct bp_log_rec *rec);

// "Python+C" (not plain "Python") so the callback gets external linkage:
// shim.c is a separate translation unit and must be able to call it.
extern "Python+C" void bp_event_h(int ev, uint32_t handle, const char *json);
""")

_library_dirs: list[str] = []
# libbaresip before libre: baresip depends on re, and single-pass linkers
# resolve archives left to right. Every -l comes after both archives, in
# extra_link_args rather than libraries=: distutils puts libraries= BEFORE
# extra_link_args on the link line, and GNU ld's --as-needed (the Ubuntu
# default) drops a shared library listed before the archive members that
# need it — the symptom is an extension that links clean and then fails
# at import with an undefined OpenSSL/opus symbol.
_extra_link_args = [
    str(LIBBARESIP_A),
    str(LIBRE_A),
    "-lopus",
    "-lssl",
    "-lcrypto",
    "-lz",
    "-lm",
]

if platform.system() == "Darwin":
    _extra_link_args += [
        "-lresolv",
        "-framework",
        "SystemConfiguration",
        "-framework",
        "CoreFoundation",
    ]
    for _formula in ("openssl@3", "opus"):
        _dir = _brew_lib_dir(_formula)
        if _dir:
            _library_dirs.append(str(_dir))
else:
    _extra_link_args += ["-lresolv", "-lpthread"]

ffi.set_source(
    "baresip._native",
    # The entire C surface visible to Python is shim.h — never a raw
    # libre/libbaresip header. Keep it that way.
    '#include "shim.h"',
    sources=[str(Path(__file__).parent / "shim.c")],
    include_dirs=[
        str(Path(__file__).parent),
        str(NATIVE_PREFIX / "include" / "re"),
        str(BARESIP_SRC / "include"),
    ],
    library_dirs=_library_dirs,
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
