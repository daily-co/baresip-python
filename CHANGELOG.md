# Changelog

All notable changes to baresip-python are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and the project versions follow
SemVer with the 0.x caveat (see the API stability policy in the README).

## [Unreleased]

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

- Hardware speaker/microphone audio drivers in the default build: `coreaudio` on macOS,
  `alsa` on Linux (Linux wheels now bundle libasound), selected with
  `Config(audio_driver=...)`.
- `examples/06_softphone.py`: a real softphone — auto-detected platform audio, dial-in
  with auto-answer or dial-out via `SIP_DIAL`, DTMF send from stdin and received-DTMF
  printing.

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
