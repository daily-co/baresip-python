# Changelog

All notable changes to baresip-python are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and the project versions follow
SemVer with the 0.x caveat (see the API stability policy in the README).

## [0.2.0a1] - 2026-08-27

The telephony release: hold and resume, blind and attended transfer in both
directions, trunk-style accounts, and fleet controls (call caps and draining).

### Added

- `Account.auth_user`: a separate digest-authentication username, for services whose
  credential store keys it differently from the URI user (credential-list trunks).
  Unset, the account authenticates as `user`.
- Register-less accounts: `Account(reg_interval=0)` dials without registering
  (trunk-style), and `register()` on such an account fails fast with
  `RegistrationError` instead of timing out.
- `NoLocalAddressError`: dialing a target no local interface can reach (classic
  case: a loopback target without `net_interface` pinned) now raises a typed,
  explanatory error instead of a generic EINVAL dial failure.
- Hold and resume: `call.hold()` / `call.resume()` with `call.is_on_hold`, and
  `call.remote_on_hold` tracking the far end's hold state from the `CALL_HOLD`
  / `CALL_RESUME` events.
- Blind transfer: `call.transfer(uri)` sends an in-dialog REFER and awaits the
  reported outcome — on success the transferred call closes; failures raise
  the new `TransferFailed` with the reported SIP status.
- Attended transfer: `call.attended_transfer(consult_call)` holds both legs and
  splices the two peers together with a REFER carrying Replaces; on success
  both of the application's legs end.
- `examples/07_warm_transfer.py`: the receptionist pattern — answer, consult,
  bridge the calls in Python audio, then splice and exit.
- `runtime.drain()`: stop accepting calls (inbound answered 486 on the SIP
  thread, `dial()` raises the new `DrainingError`) and resolve once the last
  live call ends — the fleet-rollout half of shutdown.
- Receiving transfers: a peer's REFER arrives as a typed `TransferRequest`
  (`call.on_transfer_request` / `call.transfer_request`), executed with
  `call.accept_transfer()` or refused with `call.reject_transfer()`; a
  `transfer_policy` on `UserAgent.create` ("manual" by default — the
  toll-fraud-safe stance — or "auto"/"reject") can decide without the
  application.
- Hardware speaker/microphone audio drivers in the default build: `coreaudio` on macOS,
  `alsa` on Linux (Linux wheels now bundle libasound), selected with
  `Config(audio_driver=...)`.
- `examples/06_softphone.py`: a real softphone — auto-detected platform audio, dial-in
  with auto-answer or dial-out via `SIP_DIAL`, DTMF send from stdin and received-DTMF
  printing.

### Changed

- `Config.max_concurrent_calls` is now enforced: beyond the limit, inbound
  INVITEs are answered 486 before any call exists. The default is 2 (one
  conversation plus a consultation leg — the warm-transfer shape); None means
  unlimited. Previously the field was ignored and the stack's compiled default
  silently capped Config-started runtimes at 4.

### Known issues

- Cycling `Runtime` start/close retains ~2 KiB per cycle inside the stack's
  global init/teardown ([#1](https://github.com/daily-co/baresip-python/issues/1)).
- `close()` while a call is still established leaks that call's native object
  graph, ~40 KiB per live call
  ([#2](https://github.com/daily-co/baresip-python/issues/2)). Hang up or
  `drain()` first to avoid it.

## [0.1.0a1] - 2026-08-24

First published pre-release, to TestPyPI and PyPI. Alpha: the API may still change
between pre-releases.

### Added

- SIP registration with automatic refresh, over UDP, TCP, or TLS (with real server
  certificate verification).
- Outbound and inbound calls, with telephony outcomes surfaced as typed exceptions
  and call lifecycle as events.
- Programmatic PCM audio per call (`call.audio.read()`/`write()`) via the built-in
  `aumem` driver, plus baresip's file/tone drivers (`aufile`, `ausine`) as pure
  configuration; audio-health counters and warnings.
- DTMF in both directions (send and receive), RFC 2833 and SIP INFO.
- Custom INVITE headers on dial; allowlisted header exposure on events; end-of-call
  statistics.
- Structured logging in the `baresip.*` hierarchy with per-call correlation and an
  optional full SIP trace; `PerCallFilter` for per-call DEBUG.
- Binary wheels for Linux (x86_64, aarch64; manylinux_2_28) and macOS (arm64,
  x86_64), CPython 3.11–3.13, statically bundling libre/libbaresip and libopus.
- Fully typed public API (`py.typed`); five runnable examples and a docker
  FreeSWITCH bench for local development.
