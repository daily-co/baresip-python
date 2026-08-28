#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""Build the vendored baresip/libre stack as static libraries.

This script drives two cmake builds — ``third_party/re`` (libre) and
``third_party/baresip`` — producing ``libre.a`` and ``libbaresip.a`` with an
explicit, pinned module list. It is the only supported way to build the native
code, for two reasons:

* **Reproducibility.** baresip's own cmake auto-detects optional modules from
  whatever happens to be installed; an explicit ``-DMODULES`` list makes the
  build identical on every machine, and a completeness guard fails the build
  if any listed module silently drops out (several module CMakeLists return
  without error when a dependency is missing).

* **License policy.** This project never publishes GPL code in its binaries.
  Developers are free to build anything locally — including ffmpeg-based
  modules via ``--extra-modules`` — but a build that would link GPL libraries
  *without being asked to* is a build-system bug, and the checks here catch it.
  The checks are decision-level (what did cmake enable, what do our link lines
  contain), never filesystem-level: whatever is installed on the machine is
  irrelevant and must stay that way.
"""

from __future__ import annotations

import argparse
import os
import platform
import re as regex
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The default module set. Every module here is pure mechanism: codecs,
#: media encryption, NAT traversal, and audio drivers that do nothing until
#: explicitly selected. Modules with load-time side effects do not belong here.
DEFAULT_MODULES: tuple[str, ...] = (
    "g711",
    "opus",
    "vp8",
    "srtp",
    "dtls_srtp",
    "ice",
    "stun",
    "turn",
    "aufile",
    "ausine",
    "auconv",
    "auresamp",
    "rtcpsummary",
)

#: Hardware speaker/mic drivers, one per platform, part of every default
#: build on that platform. Loading them opens no devices — a device is
#: only opened when a call actually uses the driver.
PLATFORM_MODULES: dict[str, tuple[str, ...]] = {
    "Darwin": ("coreaudio", "avcapture"),
    "Linux": ("alsa", "v4l2"),
}

#: Interactive/debug modules, available via --debug-modules for local
#: debugging builds only. Never part of a default or distributed build:
#: `menu` makes call-control decisions on its own, `stdio` grabs the host
#: process's stdin, and `cons` opens network command sockets.
DEBUG_MODULES: tuple[str, ...] = ("menu", "stdio", "cons")

#: baresip modules that link ffmpeg (GPL when x264 is involved).
FFMPEG_MODULES: frozenset[str] = frozenset({"avcodec", "avformat", "avfilter", "swscale"})

#: Link-command tokens that indicate GPL/ffmpeg libraries being linked.
GPL_LINK_TOKEN = regex.compile(
    r"(?:^|[\s\"'=])(?:-l(?:avcodec|avformat|avutil|avfilter|avdevice|swscale|swresample|x264)"
    r"|\S*lib(?:avcodec|avformat|avutil|avfilter|avdevice|swscale|swresample|x264)\.(?:a|so|dylib))"
)

_STATIC_EXPORT = regex.compile(r"extern const struct mod_export exports_(\w+);")
_MODULES_DETECTED = regex.compile(r"MODULES_DETECTED=(.*)")


class BuildPolicyError(SystemExit):
    """A build-policy violation. Exits non-zero with an explanation."""

    def __init__(self, message: str) -> None:
        super().__init__(f"build_native: POLICY FAILURE\n{message}")


# ---------------------------------------------------------------------------
# Pure check functions (unit-tested directly; no cmake required)
# ---------------------------------------------------------------------------


def parse_modules_from_static_c(static_c_text: str) -> set[str]:
    """Extract the enabled-module set from the cmake-generated static.c.

    With ``-DSTATIC=ON``, baresip's cmake writes one
    ``extern const struct mod_export exports_<name>;`` line per module it
    actually enabled — the authoritative, machine-readable record of what
    made it into the build.
    """
    return set(_STATIC_EXPORT.findall(static_c_text))


def parse_modules_detected(configure_output: str) -> set[str]:
    """Extract the enabled-module set from cmake's configure output.

    baresip prints ``MODULES_DETECTED=<semicolon-list>`` at configure time.
    Used as a cross-check against the static.c parse.
    """
    for line in configure_output.splitlines():
        match = _MODULES_DETECTED.search(line)
        if match:
            value = match.group(1).strip()
            return {m for m in value.split(";") if m}
    return set()


def find_gpl_link_tokens(link_text: str) -> list[str]:
    """Return GPL/ffmpeg library tokens found in link-command text."""
    return [m.strip() for m in GPL_LINK_TOKEN.findall(link_text)]


def check_gpl_modules(enabled: set[str], allowed_extra: set[str]) -> None:
    """Fail if an ffmpeg module is enabled without having been asked for.

    ``allowed_extra`` is the set the developer explicitly requested via
    ``--extra-modules``; anything there is waived (their machine, their
    build, their license terms — nothing we distribute).
    """
    unexpected = (enabled & FFMPEG_MODULES) - allowed_extra
    if unexpected:
        raise BuildPolicyError(
            f"ffmpeg module(s) enabled without being requested: {sorted(unexpected)}.\n"
            "A default build must never include ffmpeg modules. This indicates a bug in\n"
            "the module-list plumbing (or a stale build directory — try a clean build).\n"
            "To build these modules intentionally, pass --extra-modules explicitly."
        )
    waived = enabled & FFMPEG_MODULES & allowed_extra
    if waived:
        _gpl_notice(sorted(waived))


def check_gpl_link_lines(link_texts: dict[str, str], allowed_extra: set[str]) -> None:
    """Fail if our targets' link commands pull in GPL/ffmpeg libraries unrequested."""
    gpl_requested = bool(allowed_extra & FFMPEG_MODULES)
    offenders: dict[str, list[str]] = {}
    for path, text in link_texts.items():
        tokens = find_gpl_link_tokens(text)
        if tokens:
            offenders[path] = tokens
    if offenders and not gpl_requested:
        detail = "\n".join(f"  {p}: {t}" for p, t in offenders.items())
        raise BuildPolicyError(
            "GPL/ffmpeg libraries appear in link commands of a default build:\n"
            f"{detail}\n"
            "This indicates a build-system bug — installed libraries must never leak\n"
            "into a build that did not request them."
        )


def check_completeness(actual: set[str], expected: set[str]) -> None:
    """Fail unless the built module set matches the expected set exactly.

    Several baresip module CMakeLists silently skip themselves when a
    dependency is missing. A build that quietly shrinks would ship missing
    features; a build that quietly grows would ship unaudited code. Both
    directions are errors.
    """
    missing = expected - actual
    surplus = actual - expected
    if missing or surplus:
        parts = []
        if missing:
            parts.append(
                f"modules that were requested but did not build: {sorted(missing)}\n"
                "  (usually a missing system dependency — the module's CMakeLists\n"
                "   returned without error instead of failing)"
            )
        if surplus:
            parts.append(f"modules that built but were not requested: {sorted(surplus)}")
        raise BuildPolicyError("module set mismatch:\n" + "\n".join(parts))


def _gpl_notice(modules: list[str]) -> None:
    """Informational only — a notice is not friction."""
    print(
        "=" * 78 + f"\nNOTICE: building ffmpeg-based module(s) {modules} at your request.\n"
        "The resulting local binaries link GPL-licensed code (x264-enabled ffmpeg\n"
        "builds are GPL): fine to build, run, and test on your own systems; do not\n"
        "redistribute them unless you are prepared to meet GPL obligations.\n" + "=" * 78,
        file=sys.stderr,
    )


# ---------------------------------------------------------------------------
# Build orchestration
# ---------------------------------------------------------------------------


def _run(cmd: list[str], cwd: Path | None = None) -> str:
    print(f"+ {' '.join(cmd)}", file=sys.stderr)
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)
    sys.stderr.write(proc.stderr)
    if proc.returncode != 0:
        sys.stdout.write(proc.stdout)
        raise SystemExit(f"build_native: command failed ({proc.returncode}): {' '.join(cmd)}")
    return proc.stdout + proc.stderr


def _openssl_root() -> str | None:
    if platform.system() == "Darwin":
        brew = shutil.which("brew")
        if brew:
            proc = subprocess.run(
                [brew, "--prefix", "openssl@3"], capture_output=True, text=True, check=False
            )
            if proc.returncode == 0:
                return proc.stdout.strip()
    return None


def _collect_link_texts(build_dir: Path) -> dict[str, str]:
    """Gather every link command cmake generated for this build tree."""
    texts: dict[str, str] = {}
    for link_txt in build_dir.rglob("link.txt"):
        texts[str(link_txt)] = link_txt.read_text(errors="replace")
    ninja = build_dir / "build.ninja"
    if ninja.exists():
        texts[str(ninja)] = ninja.read_text(errors="replace")
    return texts


def build(
    prefix: Path,
    *,
    local_re: Path | None = None,
    local_baresip: Path | None = None,
    extra_modules: str = "",
    debug_modules: bool = False,
    jobs: int | None = None,
) -> None:
    jobs = jobs or os.cpu_count() or 4
    prefix = prefix.resolve()
    re_src = (local_re or REPO_ROOT / "third_party" / "re").resolve()
    baresip_src = (local_baresip or REPO_ROOT / "third_party" / "baresip").resolve()

    extras = {m for m in extra_modules.replace(",", ";").split(";") if m}
    modules: list[str] = (
        list(DEFAULT_MODULES) + list(PLATFORM_MODULES.get(platform.system(), ())) + sorted(extras)
    )
    if debug_modules:
        modules += list(DEBUG_MODULES)
    expected = set(modules)

    # The libdir is pinned because GNUInstallDirs picks lib64 on RHEL-family
    # systems (the manylinux wheel containers among them), and everything
    # downstream expects the static library at <prefix>/lib/libre.a.
    common = [
        "-DCMAKE_BUILD_TYPE=Release",
        "-DCMAKE_POSITION_INDEPENDENT_CODE=ON",
        "-DCMAKE_INSTALL_LIBDIR=lib",
    ]
    openssl = _openssl_root()
    if openssl:
        common.append(f"-DOPENSSL_ROOT_DIR={openssl}")

    # --- libre ---
    re_build = re_src / "build"
    _run(["cmake", "-B", str(re_build), f"-DCMAKE_INSTALL_PREFIX={prefix}", *common], cwd=re_src)
    _run(["cmake", "--build", str(re_build), "-j", str(jobs)], cwd=re_src)
    _run(["cmake", "--install", str(re_build)], cwd=re_src)

    # --- baresip (static, explicit module list) ---
    bs_build = baresip_src / "build"
    configure_out = _run(
        [
            "cmake",
            "-B",
            str(bs_build),
            "-DSTATIC=ON",
            f"-DMODULES={';'.join(modules)}",
            f"-Dre_DIR={prefix}/lib/cmake/re",
            f"-DCMAKE_PREFIX_PATH={prefix}",
            *common,
        ],
        cwd=baresip_src,
    )
    _run(["cmake", "--build", str(bs_build), "-j", str(jobs)], cwd=baresip_src)

    # --- policy + completeness checks (decision-level: what cmake decided,
    #     what our link lines say — never what the machine has installed) ---
    static_c = bs_build / "src" / "static.c"
    enabled = parse_modules_from_static_c(static_c.read_text())
    detected = parse_modules_detected(configure_out)
    if detected and detected != enabled:
        raise BuildPolicyError(
            f"configure output and generated static.c disagree on the module set:\n"
            f"  MODULES_DETECTED: {sorted(detected)}\n  static.c: {sorted(enabled)}"
        )
    check_gpl_modules(enabled, extras)
    check_gpl_link_lines(_collect_link_texts(bs_build), extras)
    check_completeness(enabled, expected)

    # --- artifacts ---
    libre_a = prefix / "lib" / "libre.a"
    libbaresip_a = bs_build / "libbaresip.a"
    for artifact in (libre_a, libbaresip_a):
        if not artifact.exists():
            raise SystemExit(f"build_native: expected artifact missing: {artifact}")

    print(
        f"build_native: OK\n  {libre_a}\n  {libbaresip_a}\n  modules: {';'.join(sorted(enabled))}"
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", type=Path, default=REPO_ROOT / ".native")
    parser.add_argument(
        "--local-re", type=Path, help="use a local libre checkout instead of the submodule"
    )
    parser.add_argument(
        "--local-baresip", type=Path, help="use a local baresip checkout instead of the submodule"
    )
    parser.add_argument(
        "--extra-modules",
        default=os.environ.get("BARESIP_EXTRA_MODULES", ""),
        help="additional baresip modules, semicolon-separated (built at your own license terms)",
    )
    parser.add_argument(
        "--debug-modules",
        action="store_true",
        help=f"also build {'/'.join(DEBUG_MODULES)} for interactive debugging",
    )
    parser.add_argument("--jobs", type=int, default=None)
    args = parser.parse_args(argv)
    build(
        args.prefix,
        local_re=args.local_re,
        local_baresip=args.local_baresip,
        extra_modules=args.extra_modules,
        debug_modules=args.debug_modules,
        jobs=args.jobs,
    )


if __name__ == "__main__":
    main()
