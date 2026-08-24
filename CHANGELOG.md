# Changelog

All notable changes to baresip-python are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and the project versions follow
SemVer with the 0.x caveat (see the API stability policy in the README).

## [Unreleased]

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
