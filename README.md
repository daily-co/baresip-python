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

## Quick start

Until the first release lands on PyPI, install from a source checkout. You need
[uv](https://docs.astral.sh/uv/), a C compiler, cmake, and the OpenSSL and opus development
headers (`apt install cmake libssl-dev libopus-dev` on Debian/Ubuntu,
`brew install cmake openssl@3 opus` on macOS):

```
git clone --recurse-submodules https://github.com/daily-co/baresip-python.git
cd baresip-python
uv sync --group dev
make native ext
```

Everything below runs against the bundled FreeSWITCH bench — a docker compose setup with
test users (`1001`/`1002`, password `bench1234`) and an echo service at extension `9196`
(see [bench/README.md](bench/README.md)):

```
make bench-up
```

The examples are the tutorial, written to be read in order. Each is self-contained,
heavily commented, and configured entirely through `SIP_USER`/`SIP_PASS`/`SIP_DOMAIN`
environment variables whose defaults match the bench.

**[01 — register](examples/01_register.py).** The core lifecycle: one `Runtime` per process
(it owns the SIP thread), a `UserAgent` bound to an `Account`, `register()`, a clean
shutdown. Re-registration is automatic while it runs.

```
uv run python examples/01_register.py
```

**[02 — dial](examples/02_dial.py).** An outbound call to the echo service and a call that
fails: `dial()`, `wait_established()`, custom INVITE headers, and telephony outcomes as
typed exceptions (`CallBusy`, `CallDeclined`, ...) instead of error codes.

```
uv run python examples/02_dial.py
```

**[03 — programmatic audio](examples/03_echo_aumem.py).** The `aumem` driver, the shape a
voice agent needs: `call.audio.read()` returns the far end's PCM, `call.audio.write()`
queues yours — plain bytes, no sound card, no files.

```
uv run python examples/03_echo_aumem.py
```

**[04 — answer + WAV bot](examples/04_answer_wav_bot.py).** A bot with zero custom audio
code: answer an incoming call, play a greeting from a WAV file, record the caller — audio
drivers as pure configuration. Have the bench call the bot from a second terminal:

```
uv run python examples/04_answer_wav_bot.py
docker exec baresip-bench-freeswitch fs_cli -x "originate user/1001 &echo()"
```

**[05 — DTMF IVR](examples/05_dtmf_ivr.py).** DTMF in both directions: run one instance as
a mini-IVR (press 1 for a tone, 2 to hang up), a second as the caller that presses the keys.

```
uv run python examples/05_dtmf_ivr.py
SIP_USER=1002 SIP_PASS=bench1234 uv run python examples/05_dtmf_ivr.py sip:1001@127.0.0.1:15060
```

## Audio and device selection

Audio drivers are chosen when the runtime starts — `Config(audio_source=...,
audio_player=...)`, one driver per direction — and stay put for the runtime's lifetime;
there is no mid-call driver switching. This is deliberate for v0.1: audio a program decides
on at runtime is what the `aumem` driver is for (it is just bytes your code reads and
writes), and richer switching APIs are planned for a later release. What a "device" means
belongs to each driver: a WAV path for `aufile`, a tone frequency for `ausine`, nothing at
all for `aumem`.

## Custom headers

Outbound INVITEs take custom headers directly: `ua.dial(uri, headers={...})` (example 02).
On the receiving side, `Config(expose_headers=[...])` allowlists header names whose values
then ride along on call events.

One deliberate absence: the `100 Trying` that answers an incoming INVITE cannot carry
custom headers. The stack sends it automatically, before the application ever sees the
call — and it is a hop-by-hop response, so a header on it would die at the first proxy
anyway. Headers on the final response (answer/reject) are planned for a later release.

## Logging

The library logs into the standard `baresip.*` logger hierarchy and never touches handlers
or output itself. Records carry correlation fields in `extra` — most importantly
`sip_call_id`, the spine that ties Python-side events, native-stack lines, and SIP traces
to one call. The native stack's own logs surface under `baresip.native`, and a full SIP
message trace is available under `baresip.native.sip`. Per-call DEBUG on a busy server is a
filter problem, not a level problem — see `PerCallFilter` in
[docs/LOGGING.md](docs/LOGGING.md), along with the loguru bridge for applications (pipecat
among them) that log through loguru.

## Platform support

Linux (x86_64, aarch64) and macOS (Apple Silicon and Intel), CPython 3.11–3.13. On Windows,
[WSL2](https://learn.microsoft.com/windows/wsl/) is the supported path for now — the Linux
build runs there as-is; native Windows support is a design goal the code keeps its door
open for, but it is not built or tested today.

Call transfer (hold/resume, blind and attended REFER) is not in v0.1; it is the headline of
the next release.

## Building with GPL codecs (H.264)

The published wheels never contain GPL code. H.264 via ffmpeg's `avcodec` is available as a
**source-build opt-in** — your machine, your build, your license terms — documented in
[docs/GPL-CODECS.md](docs/GPL-CODECS.md), including the x264 encode-vs-decode nuance that
decides whether GPL applies at all. Support level, honestly: the `avcodec` build is
compile-verified weekly in CI (built and unit-tested, never distributed); its runtime
behavior is community-supported.

## Security

See [SECURITY.md](SECURITY.md) — how to report a vulnerability, which versions receive
fixes, and a short threat-model note for operators: a registered user agent is a server as
well as a client, and a publicly addressable one will receive unsolicited INVITEs and
scanner traffic.

## API stability policy

The public API is exactly what `baresip.__all__` exports and these docs describe; everything
else is internal. Versioning is SemVer with the 0.x caveat: minor bumps before 1.0 may break,
and every break is called out in the CHANGELOG. Deprecated names keep working for one minor
version with a `DeprecationWarning` before removal. The package is fully typed (`py.typed`).

## License

BSD 2-Clause. Copyright (c) 2026, Daily. Bundled third-party components (libre, libbaresip)
are BSD-3-Clause; their notices ship with every distribution. Built wheels additionally
bundle [OpenSSL](https://openssl-library.org/) and [libopus](https://opus-codec.org/) under
their respective licenses — see [docs/UPGRADING.md](docs/UPGRADING.md) for how security
releases in those propagate here.
