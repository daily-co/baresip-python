# Release engineering

## Building the wheels

The `wheels` workflow builds the distributable wheel set with cibuildwheel:
manylinux_2_28 x86_64 and aarch64, macOS arm64 and x86_64, for CPython 3.11 through 3.13.
It runs on demand (`workflow_dispatch`) and on `v*` tag pushes. The static configuration —
interpreter selection, container images, the before-all native build, and the per-wheel
test command — lives in `[tool.cibuildwheel]` in `pyproject.toml`.

Three gates run inside every wheel build:

- **Module completeness.** `scripts/build_native.py` fails the build if the compiled module
  set differs from the requested set in either direction. A dependency missing in the build
  container must fail the build, never silently shrink the wheel.
- **GPL linkage audit.** `scripts/wheel_smoke.py` runs `scripts/check_linkage.py` against the
  extension installed from the wheel. No published artifact may link ffmpeg/x264; this is the
  enforcement point, not an advisory check.
- **Runtime smoke.** The same script imports the installed package in a clean environment and
  drives a full `Runtime` start/close cycle.

The wheels bundle OpenSSL (grafted in by auditwheel/delocate) and libopus (compiled in
statically on Linux, grafted on macOS). Security releases in either are our responsibility to
re-ship — see `docs/UPGRADING.md`.

macOS wheels currently require macOS 15: the bundled dylibs come from Homebrew bottles, which
target the build runner's OS. Lowering that floor means building OpenSSL and opus from source
in the macOS before-all step instead of using brew.

## Publishing model

Releases reach PyPI exclusively through the `wheels` workflow, via Trusted Publishing
(OIDC) from the protected `release` environment on `v*` tag pushes. No PyPI API tokens
exist anywhere. Because the GPL linkage audit runs inside that same workflow, there is
structurally no path to PyPI that bypasses it.

CI never combines `pull_request_target` with a checkout of the PR head — that combination
hands untrusted code the repository's write token.
