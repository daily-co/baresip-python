# baresip-python

Python bindings for the [baresip](https://github.com/baresip/baresip) SIP stack — a complete,
embeddable SIP user agent for Python applications: registration, inbound and outbound calls,
programmatic PCM audio and VP8 video access, and DTMF, with an asyncio-native API.

> Current release: `v0.7.0` on PyPI. Versioning is SemVer with the 0.x caveat: a minor
> version may break the API, and every break is called out in the CHANGELOG.

## What this is

- `pip install baresip-python`, `import baresip` — no compiler, no system SIP stack needed
  (wheels statically bundle [libre](https://github.com/baresip/re) and libbaresip).
- A **generic** binding: nothing framework-specific inside. Audio can go to/from WAV files
  (baresip's built-in drivers) or to/from your application as raw PCM (the `aumem` driver) —
  which is how frameworks like [pipecat](https://github.com/pipecat-ai/pipecat) consume it.
- BSD-2-Clause, on top of baresip/libre (BSD-3). No GPL code is ever published in our binaries.

## Quick start

Releases install from PyPI (`pip install baresip-python`). For development, build from a
source checkout: you need
[uv](https://docs.astral.sh/uv/), a C compiler, cmake, and the OpenSSL, opus, and libvpx
development headers (`apt install cmake libssl-dev libopus-dev libvpx-dev libv4l-dev
libasound2-dev` on
Debian/Ubuntu, `brew install cmake openssl@3 opus libvpx` on macOS):

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

**[06 — softphone](examples/06_softphone.py).** Real speakers-and-microphone audio: the
platform's hardware driver is picked automatically (`coreaudio` on macOS, `alsa` on Linux).
Register and auto-answer the first call, or set `SIP_DIAL` to dial out; type digits + Enter
to send DTMF, received digits are printed, `h` hangs up. Dialing the bench's echo service
lets you hear yourself — use a headset, there is no echo cancellation.

```
SIP_DIAL=sip:9196@127.0.0.1:15060 uv run python examples/06_softphone.py
```

**[07 — warm transfer](examples/07_warm_transfer.py).** The receptionist pattern: answer a
caller, consult a second destination, bridge the two calls in Python audio while staying in
the path, then splice the parties together with an attended transfer and drop out. Run the
bot, then call it with the softphone example from a second terminal (the caller must reach
the bot bridged through the switch — see the example's docstring):

```
SIP_USER=1001 SIP_PASS=bench1234 uv run python examples/07_warm_transfer.py
SIP_USER=1002 SIP_DIAL=sip:1001@127.0.0.1:15060 uv run python examples/06_softphone.py
```

**[08 — video call](examples/08_video_call.py).** Your camera on a VP8 call, the far end's
video in a window. Two instances call each other directly — no switch in the middle (the
bench declines video) — each showing the other's camera; macOS asks for camera permission
on first run. Add `AUDIO_DRIVER=coreaudio` (macOS) or `alsa` (Linux) for a full video
softphone with real microphone and speakers:

```
SIP_LISTEN=127.0.0.1:5070 uv run python examples/08_video_call.py
SIP_LISTEN=127.0.0.1:5072 SIP_DIAL=sip:alice@127.0.0.1:5070 uv run python examples/08_video_call.py
```

**[10 — encrypted calls](examples/10_encrypted_call.py).** SRTP with one account setting,
and a call that reports what it got. Answer calls and follow each caller's offer, or set
`SIP_ENCRYPTION=srtp` to accept and place only encrypted calls; it registers over TLS, so
the SRTP keys stay private:

```
SIP_CA_FILE=bench/certs/cert.pem uv run python examples/10_encrypted_call.py
docker exec baresip-bench-freeswitch fs_cli -x "originate {rtp_secure_media=mandatory}user/1001 &echo()"
```

**[11 — transfer with context](examples/11_transfer_with_context.py).** A bot hands a caller
to a human agent and passes what it learned as a `User-to-User` header, so the agent does not
ask again. It plays the bot, the caller's phone and the agent desk in one process, with no
server:

```
uv run python examples/11_transfer_with_context.py
```

## Audio and device selection

Audio drivers are chosen when the runtime starts — `Config(audio_source=...,
audio_player=...)`, one driver per direction — and stay put for the runtime's lifetime;
there is no mid-call driver switching. This is deliberate: audio a program decides
on at runtime is what the `aumem` driver is for (it is just bytes your code reads and
writes), and richer switching APIs are planned for a later release. What a "device" means
belongs to each driver: a WAV path for `aufile`, a tone frequency for `ausine`, the sound
card for the hardware drivers (`coreaudio` on macOS, `alsa` on Linux), nothing at all for
`aumem`.

## Custom headers

Outbound INVITEs take custom headers directly: `ua.dial(uri, headers={...})` (example 02).
On the receiving side, `Config(expose_headers=[...])` allowlists header names whose values
then ride along on call events.

The messages that refuse or end a call take them too, and a refusal takes its status:

```python
await call.reject(status=503, headers={"Retry-After": "30"})        # try another instance
await call.reject(status=480, reason="Agent on a break")
await call.hangup(headers={"Reason": 'Q.850;cause=16;text="resolved"', "X-Queue": "sales"})
```

A switch or contact-center platform routes on that status: 486, the default, says this
endpoint is busy; 503 that the call should go elsewhere. `hangup(headers=...)` puts the
headers on the BYE of an answered call, or on the rejection of an incoming one not yet
answered; an outgoing call that nobody answered is cancelled, and a cancellation carries none.

Two deliberate absences. The `100 Trying` that answers an incoming INVITE cannot carry
custom headers: the stack sends it automatically, before the application ever sees the
call — and it is a hop-by-hop response, so a header on it would die at the first proxy
anyway. Nor can the answer (`200 OK`): the stack has no way to add them yet.

## Call context on a transfer

A bot that hands a caller to a human agent or a queue can pass what it learned, so the
caller is not asked again. The context travels with the transfer, attached to the address
the far end is told to call:

```python
await call.transfer(
    "sip:support@pbx.example.com",
    headers={"User-to-User": "56a390f3d2b7310023a2;encoding=hex", "X-Case": "1234"},
)
```

The far end sets those headers on the call it places, and the party it calls reads them as
ordinary headers. `User-to-User` is the standard one; contact-center platforms such as
Genesys and Twilio read it, usually as hex data ending in `;encoding=hex`. The library does
the escaping the address form needs.

Whether the context arrives is the far end's choice. Our own agents carry it, which the
test suite checks through a proxy. FreeSWITCH, as the party being transferred, places the
new call without it unless its dialplan forwards it; other switches and carriers vary.

To receive context, list the header names in `Config(expose_headers=["User-to-User", ...])`;
they then appear in the incoming call's `headers`, like any exposed header.

When a peer asks one of our calls to transfer, `TransferRequest.headers` holds the context
attached to its target, and `call.accept_transfer()` sets it on the call it places.
`accept_transfer(headers={...})` adds headers to it; a name the request also has takes the
value given, and a value of `None` leaves that header out.
Headers that define the call itself (From, To, Call-ID, Via, Route, Contact and the like)
are never taken from a transfer target. Example 11 shows all three parties, and
[docs/TRANSFER.md](docs/TRANSFER.md) the model behind them.

## SIP INFO

A PBX or contact-center platform can send a SIP INFO inside a call to pass an application
event: a recording signal, a queue or agent event. `call.on_info(callback)` delivers each
one as an `InfoEvent` with the request's content type and its body, as raw bytes:

```python
call.on_info(lambda info: print(info.content_type, info.body))
```

Every INFO is answered `200 OK`, whether or not the application listens, so the sender never
gets an error for an event the application ignores. DTMF sent as INFO is not reported
here; it arrives through `call.on_dtmf` like any other digit. A body over 8 KiB is refused
with `488`. Sending INFO is not available yet.

## Transfers and hold

Hold/resume (`call.hold()`, with the far end's hold state in `call.remote_on_hold`), blind
transfer (`call.transfer(uri)`), attended transfer (`call.attended_transfer(consult)`), and
the receiving side — a peer's REFER as a typed `TransferRequest`, governed by a
`transfer_policy` that defaults to manual because a transfer is the far end instructing
your agent to place a call. The semantics have real surprises (a successful transfer
*closes* your call; that is the protocol, not the binding) — read
[docs/TRANSFER.md](docs/TRANSFER.md) before building on them. Example 07 is the runnable
version.

## Video

Calls made or answered with ``video=True`` negotiate VP8, and `call.video` exchanges it as
packed I420 frames — `write_frame()` feeds the paced encoder, `read_frame()` takes decoded
frames, and camera capture (`avcapture` on macOS, `v4l2` on Linux) is pure configuration
via `Config(video_source=...)`. The library deliberately ships no display: rendering
belongs to the application on its main thread, and [docs/VIDEO.md](docs/VIDEO.md) explains
the frames contract, the readiness and renegotiation semantics, and why that display
stance is the only one that works.

## SIP trunks

Trunk-style connections — no registration, and a digest username that differs from the URI
user — are `Account(reg_interval=0)` and `Account(auth_user=...)`. What each means, which
provider products want which shape (Twilio's SIP Domains vs Elastic SIP Trunking split as
the worked example), and the fail-fast behaviors around them are in
[docs/TRUNKS.md](docs/TRUNKS.md).

## Fleet operations

Two controls for running agents at scale. `Config(max_concurrent_calls=...)` caps the
runtime's calls natively — beyond the limit, inbound INVITEs are refused with `486` before
any call object exists. The default is 2 (one conversation plus a consultation leg, the
warm-transfer shape); `None` removes the cap. `await runtime.drain()` is the rollout half
of shutdown: new inbound calls are refused on the SIP thread, `dial()` raises
`DrainingError`, and the call resolves once the last live call ends — drain, then
`close()`, and the fleet can retire the instance without dropping anyone mid-sentence.

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

Linux (x86_64, aarch64) and macOS (Apple Silicon and Intel), CPython 3.11–3.14. On Windows,
[WSL2](https://learn.microsoft.com/windows/wsl/) is the supported path for now — the Linux
build runs there as-is; native Windows support is a design goal the code keeps its door
open for, but it is not built or tested today.

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

### TLS

Every TLS connection verifies the server: its certificate must chain to a trusted CA and
name the host connected to. The CA certificates are found when the runtime starts, first
hit wins:

1. `SSL_CERT_FILE`, then `SSL_CERT_DIR`;
2. the file, then the directory, Python's `ssl` module reports for the platform;
3. the `certifi` package, when installed.

For a server with a private or self-signed certificate, point `SIP_CA_FILE` at its CA
certificate (a PEM file). It is trusted in addition to the certificates found above, and by
the SIP stack alone: public servers keep verifying, and nothing else in the process comes
to trust the private CA. A `SIP_CA_FILE` that does not name a file is an error at start.
The runtime logs what it verifies against. When no CA certificates are found, TLS is
disabled (UDP and TCP are unaffected) and a TLS account or target raises
`NoCaCertificatesError`. A rejected certificate fails the registration with the reason
(`self-signed certificate`, `hostname mismatch`, ...) in the error.

There is no switch for verification among the `Config` fields. A caller that sets
`sip_cafile`, `sip_capath` or `sip_verify_server` in `Config(extra_config_text=...)`
takes TLS trust into their own hands, and nothing above is applied.

### Media encryption

Call audio and video can be encrypted with SRTP, the form carriers, hosted PBXs and
contact-center platforms require. One account field decides it:

| `Account(media_encryption=...)` | Calls placed | Calls received |
|---|---|---|
| `"none"` (default) | unencrypted | follow the caller: unencrypted, or SRTP when the caller offers it |
| `"srtp"` | SRTP, required | SRTP only; an unencrypted call is refused with `488` |

So a default account answers a caller that requires SRTP with no configuration, and
`"srtp"` guarantees that nothing is ever sent in the clear. `call.media_encrypted` reports
what a call ended up with.

The SRTP keys are exchanged in the call setup (SDES), so they are only as private as the
signalling: use `transport="tls"` with `"srtp"`. The runtime warns when it is not. DTLS-SRTP
and ZRTP have no typed setting; a `mediaenc=` entry in `extra_params` selects the stack's
mode directly and takes the place of this field.
Example 10 is the runnable version.

## API stability policy

The public API is exactly what `baresip.__all__` exports and these docs describe; everything
else is internal. Versioning is SemVer with the 0.x caveat: minor bumps before 1.0 may break,
and every break is called out in the CHANGELOG. Deprecated names keep working for one minor
version with a `DeprecationWarning` before removal. The package is fully typed (`py.typed`).

## License

BSD 2-Clause. Copyright (c) 2026, Daily. Bundled third-party components (libre, libbaresip)
are BSD-3-Clause; their notices ship with every distribution. Built wheels additionally
bundle [OpenSSL](https://openssl-library.org/), [libopus](https://opus-codec.org/),
[libvpx](https://www.webmproject.org/), and
[libg722](https://github.com/sippy/libg722) — and, on Linux, ALSA's
[libasound](https://www.alsa-project.org/) — under their respective licenses; see
[docs/UPGRADING.md](docs/UPGRADING.md) for how security releases in those propagate here.
