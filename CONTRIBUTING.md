# Contributing

## Development setup

You need [uv](https://docs.astral.sh/uv/), a C compiler, cmake, and the OpenSSL,
opus, and libvpx development headers (see the README's quick start for the
platform package lists):

```
git clone --recurse-submodules https://github.com/daily-co/baresip-python.git
cd baresip-python
uv sync --group dev
make native ext        # build the vendored C stack, then the extension
uv run pytest tests/unit
```

Integration tests run against the bundled FreeSWITCH bench:

```
make bench-up
uv run pytest -m bench tests/integration
```

## Gates

Every change must pass before it lands:

- `uv run pytest tests/unit`
- `uv run ruff check src scripts tests examples` and
  `uv run ruff format --check src scripts tests examples`
- C changes: `uv run clang-format -i src/_native/*.c src/_native/*.h`
  (or `make format`)
- The sanitizer lanes (`make ext-san test-san`, `make ext-tsan test-tsan`)
  guard memory and threading; run them for changes to the native layer.

## Ground rules

- **The vendored libre/libbaresip stay unmodified.** A fix that belongs in the
  stack goes upstream (https://github.com/baresip/baresip), never as a local
  patch; the binding picks it up through a submodule bump.
- **No GPL code in published binaries.** The build enforces this
  (`scripts/build_native.py` audits modules and link lines); don't fight it.
- Public API carries Google-style docstrings, and breaking changes are called
  out in CHANGELOG.md — see the API stability policy in the README.

## Security

Please do not report vulnerabilities in public issues — see
[SECURITY.md](SECURITY.md).
