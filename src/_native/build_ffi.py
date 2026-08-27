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

import os
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
#define BP_CMD_CALL_SEND_DIGIT ...
#define BP_CMD_CALL_HOLD ...
#define BP_CMD_CALL_TRANSFER ...
#define BP_CMD_CALL_REPLACE_TRANSFER ...
#define BP_CMD_CALL_TRANSFER_ACCEPT ...
#define BP_CMD_CALL_TRANSFER_REJECT ...
#define BP_CMD_SET_DRAIN ...
#define BP_CMD_CALL_COUNT ...

#define BP_CMD_TEST_EMIT ...
#define BP_CMD_TEST_ESCAPE ...
#define BP_CMD_TEST_HANDLE_NEW ...
#define BP_CMD_TEST_HANDLE_DROP ...
#define BP_CMD_TEST_HANDLE_PROBE ...
#define BP_CMD_TEST_HANDLE_COUNT ...

#define BP_EV_PONG ...
#define BP_EV_DONE ...
#define BP_EV_STALE_HANDLE ...
#define BP_EV_BASE ...
#define BP_EV_AUDIO_WARNING ...

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

// The SPSC byte ring (ring.h) — a pure data structure, no loop or thread
// ties; contracts documented in the header.
typedef struct bp_ring bp_ring;

struct bp_ring_stats {
    uint64_t underruns;
    uint64_t overruns;
    uint64_t dropped;
    uint32_t high_water;
};

// Programmatic audio (the aumem driver) — semantics documented in shim.h.
struct bp_audio_info {
    uint32_t epoch;
    uint32_t tx_ready;
    uint32_t rx_ready;
    uint32_t tx_srate, tx_ch, tx_ptime;
    uint32_t rx_srate, rx_ch;
    uint32_t tx_fill, tx_capacity;
    uint32_t rx_fill, rx_capacity;
};

int bp_audio_probe(uint32_t call_handle, struct bp_audio_info *info);
int32_t bp_audio_write(uint32_t call_handle, uint32_t epoch, const uint8_t *src, uint32_t len);
int32_t bp_audio_read(uint32_t call_handle, uint32_t epoch, uint8_t *dst, uint32_t len);

struct bp_audio_stats {
    uint32_t epoch;
    uint32_t tx_fill, tx_high_water;
    uint32_t rx_fill, rx_high_water;
    uint64_t tx_silence_frames;
    uint64_t tx_starved_frames;
    uint64_t tx_rejected;
    uint64_t rx_dropped;
    uint64_t rx_discarded;
};

int bp_audio_stats_get(uint32_t call_handle, struct bp_audio_stats *out);

bp_ring *bp_ring_alloc(uint32_t capacity);
void     bp_ring_free(bp_ring *ring);
uint32_t bp_ring_write(bp_ring *ring, const uint8_t *src, uint32_t len);
uint32_t bp_ring_read(bp_ring *ring, uint8_t *dst, uint32_t len);
uint32_t bp_ring_size(const bp_ring *ring);
uint32_t bp_ring_capacity(const bp_ring *ring);
void     bp_ring_stats_get(const bp_ring *ring, struct bp_ring_stats *stats);

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
        # The coreaudio speaker/mic driver module:
        "-framework",
        "CoreAudio",
        "-framework",
        "AudioToolbox",
    ]
    for _formula in ("openssl@3", "opus"):
        _dir = _brew_lib_dir(_formula)
        if _dir:
            _library_dirs.append(str(_dir))
else:
    # -lasound is the alsa speaker/mic driver module's dependency.
    _extra_link_args += ["-lresolv", "-lpthread", "-lasound"]

# Builds with extra baresip modules need their external libraries on the
# link line (e.g. BP_EXTRA_LIBS="-lsndfile" for extra_modules="sndfile"):
# the module code sits in libbaresip.a either way, and its symbols must
# resolve when the extension links.
_extra_libs = os.environ.get("BP_EXTRA_LIBS")
if _extra_libs:
    _extra_link_args += _extra_libs.split()

# BP_SANITIZE=address,undefined instruments the shim sources (the static
# libraries stay uninstrumented — heap tracking still covers them, since
# every mem_alloc reaches the intercepted malloc). Recovery is disabled so
# the first finding fails the run instead of scrolling past a green suite.
_extra_compile_args: list[str] = []
_sanitize = os.environ.get("BP_SANITIZE")
if _sanitize:
    _extra_compile_args += [
        f"-fsanitize={_sanitize}",
        "-fno-sanitize-recover=all",
        "-fno-omit-frame-pointer",
        "-g",
    ]
    _extra_link_args += [f"-fsanitize={_sanitize}"]

ffi.set_source(
    "baresip._native",
    # The entire C surface visible to Python is shim.h — never a raw
    # libre/libbaresip header. Keep it that way.
    '#include "shim.h"',
    sources=[
        str(Path(__file__).parent / "shim.c"),
        str(Path(__file__).parent / "ring.c"),
        str(Path(__file__).parent / "aumem.c"),
    ],
    include_dirs=[
        str(Path(__file__).parent),
        str(NATIVE_PREFIX / "include" / "re"),
        str(BARESIP_SRC / "include"),
    ],
    library_dirs=_library_dirs,
    extra_compile_args=_extra_compile_args,
    extra_link_args=_extra_link_args,
    # cffi would otherwise switch the module to the limited API and name it
    # _native.abi3.so; wheels here are built per interpreter, and the
    # extension's name should match the wheel's cp-tag.
    py_limited_api=False,
)


def main() -> None:
    for lib in (LIBRE_A, LIBBARESIP_A):
        if not lib.exists():
            raise SystemExit(f"build_ffi: {lib} not found — run `make native` first")
    # Each sanitize flavor gets its own build dir: the compiler cache only
    # watches sources, so sharing one dir would silently mix flag sets.
    flavor = f"ffi-san-{_sanitize.replace(',', '-')}" if _sanitize else "ffi"
    tmpdir = REPO_ROOT / "build" / flavor
    built = Path(ffi.compile(tmpdir=str(tmpdir), verbose=True))
    dest = REPO_ROOT / "src" / "baresip" / built.name
    # Fresh inode, never an in-place overwrite: macOS caches a vnode's
    # code signature, and rewriting an extension the kernel has already
    # executed gets the next process that imports it killed (SIGKILL).
    dest.unlink(missing_ok=True)
    shutil.copy2(built, dest)
    print(f"build_ffi: OK\n  {dest}")


if __name__ == "__main__":
    main()
