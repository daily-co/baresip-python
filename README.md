# baresip-python

Python bindings for the [baresip](https://github.com/baresip/baresip) SIP stack — a complete,
embeddable SIP user agent for Python applications: registration, inbound and outbound calls,
programmatic PCM audio access, and DTMF, with an asyncio-native API.

> **Status: pre-alpha, under active development.** Nothing here is ready for use yet.
> The first usable release will be `v0.1.0a1` on TestPyPI.

## What this is

- `pip install baresip-python`, `import baresip` — no compiler, no system SIP stack needed
  (wheels statically bundle [libre](https://github.com/baresip/re) and libbaresip).
- A **generic** binding: nothing framework-specific inside. Audio can go to/from WAV files
  (baresip's built-in drivers) or to/from your application as raw PCM (the `aumem` driver) —
  which is how frameworks like [pipecat](https://github.com/pipecat-ai/pipecat) consume it.
- BSD-2-Clause, on top of baresip/libre (BSD-3). No GPL code is ever published in our binaries.

## API stability policy

The public API is exactly what `baresip.__all__` exports and these docs describe; everything
else is internal. Versioning is SemVer with the 0.x caveat: minor bumps before 1.0 may break,
and every break is called out in the CHANGELOG. Deprecated names keep working for one minor
version with a `DeprecationWarning` before removal. The package is fully typed (`py.typed`).

## License

BSD 2-Clause. Copyright (c) 2026, Daily. Bundled third-party components (libre, libbaresip)
are BSD-3-Clause; their notices ship with every distribution.
