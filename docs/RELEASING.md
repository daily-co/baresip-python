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

One-time prerequisites, per registry (pypi.org and test.pypi.org are separate accounts):
a pending Trusted Publisher registered for project `baresip-python` with owner `daily-co`,
repository `baresip-python`, workflow `wheels.yml`, environment `release`. The pending
publisher becomes the real one when the first upload claims the project name.

## Cutting a release

1. Set `__version__` in `src/baresip/__init__.py`, date the version's section in
   `CHANGELOG.md`, and land those on `main` with CI green.
2. Tag that commit and push the tag:

   ```
   git tag v0.1.0a1
   git push origin v0.1.0a1
   ```

3. The tag push runs the `wheels` workflow; when the build matrix is green, the publish
   job waits for approval on the `release` environment. Approving it uploads to TestPyPI
   first, then to PyPI.
4. Verify from a clean environment — the extra index is required because dependencies do
   not live on TestPyPI:

   ```
   uv venv /tmp/relcheck && VIRTUAL_ENV=/tmp/relcheck uv pip install \
       --index-url https://test.pypi.org/simple/ \
       --extra-index-url https://pypi.org/simple/ baresip-python
   /tmp/relcheck/bin/python -c "import baresip; print(baresip.__version__)"
   ```

   and confirm the PyPI project page shows the release.

Pre-releases (`aN`, `bN`, `rcN`) are invisible to a default `pip install baresip-python`
until a final version exists; installing one takes `--pre` or an exact version pin.
