# Changelog

All notable changes to baresip-python are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and the project versions follow
SemVer with the 0.x caveat (see the API stability policy in the README).

## [0.4.1a1] - 2026-09-05

A point release for embedders: `Config` can now express a pinned network
interface, so configurations that previously required raw text can use a
`Config` object — and with it the features `Runtime.start()` applies only on
that path (`expose_headers`, `instance_id`, `sip_trace`, the log level).

### Added

- `Config.net_interface`: the network interface name or local address the
  stack binds and routes from (e.g. `"127.0.0.1"`, `"eth0"`). None keeps
  normal interface discovery, which deliberately skips loopback — so
  loopback-only setups pin it. Previously this was only expressible as a raw
  `net_interface` configuration line, which forced the raw-text start path
  and silently dropped every command-applied `Config` feature.

## [0.4.0a1] - 2026-09-04

Mid-call video, wideband audio, and operational hardening: video can now start
and stop inside a running call, G.722 joins the bundled codecs, and interrupted
playback, vanished peers, and typo'd configuration all fail fast instead of
silently.

### Added

- Mid-call video add and remove: `call.set_video_direction("sendrecv" |
  "sendonly" | "recvonly" | "inactive")` renegotiates the call's video with a
  re-INVITE, with `add_video()` / `remove_video()` shorthands. `dial(...,
  video=...)` now also takes a direction string — `video="inactive"` negotiates
  the video stream without activating it, so video can be brought up later
  (a call dialed with `video=False` still has no video stream and cannot add
  one). The accepting side needs nothing beyond `answer(video=True)`: an
  incoming mid-call video offer is auto-accepted and frames simply start
  flowing. `examples/09_midcall_video.py` runs the whole story as a
  two-terminal demo; the mid-call section in `docs/VIDEO.md` has the details,
  including the "retry shortly" timing right after establishment.
- G.722 wideband audio (16 kHz) in the default build, via the bundled
  permissively-licensed [libg722](https://github.com/sippy/libg722). Select it
  with the full spec — `Account(audio_codecs=("g722/16000/1",))` — since a bare
  codec name implies 8 kHz. The bench FreeSWITCH now negotiates it too.
- `CallAudio.flush_tx()`: discard PCM that is written but not yet transmitted —
  the transmit thread drains the buffer on its next tick, so at most one frame
  (~20 ms) can still reach the wire. Built for interruptions: stop writing,
  flush, resume with the new audio. Discarded bytes are counted in the new
  `AudioStats.tx_flushed_bytes`.
- `Config.rtp_timeout`: seconds without received RTP after which a call is
  declared dead and closed (reason `"rtp stream error"`) — catches peers that
  vanish without a BYE. Direction-aware (held and send-only streams are not
  checked); 0, the default, disables detection.
- `Config.instance_id`: a caller-supplied UUID carried on registration Contacts
  as `+sip.instance="<urn:uuid:...>"` (RFC 5626/3840), with the `gruu`
  extension advertised — a registrar that supports it replaces a restarted
  instance's old binding instead of stacking a stale one.
- Startup validation of the configured audio drivers: a typo like
  `Config(audio_driver="aumen")` now fails `start()` loudly, naming the field
  and the registered drivers, instead of surfacing when the first call fails to
  allocate audio.
- cp314 wheels: Python 3.14 joins the build matrix.

### Known issues

- The two 0.2.0a1 leaks remain open: ~2 KiB retained per runtime start/close
  cycle ([#1](https://github.com/daily-co/baresip-python/issues/1)), and
  `close()` during a live call leaking that call's native object graph
  ([#2](https://github.com/daily-co/baresip-python/issues/2)) — hang up or
  `drain()` first.

## [0.3.0a1] - 2026-08-28

The video release: VP8 calls with programmatic frames, camera capture on both
platforms, and a video-call example that doubles as a softphone.

### Added

- VP8 video calls: `dial(..., video=True)` and `answer(video=True)` negotiate a
  video stream (libvpx is now bundled), and `call.video` exchanges it as frames —
  `write_frame()` queues packed I420 for the paced encoder, `read_frame()` takes
  decoded frames, `info()` reports geometry/readiness/counters, and
  `request_keyframe()` asks the far end for a picture update. Frame geometry,
  pacing, and bitrate come from the new `Config.video_size` / `video_fps` /
  `video_bitrate`. A peer that declines video (an audio-only switch) leaves a
  working audio call, with `call.video` raising the new `VideoNotActive`;
  mid-call renegotiation surfaces as the new `VideoRestarted`, after which the
  next operation rebinds.
- Camera capture drivers in the default build: `avcapture` on macOS, `v4l2` on
  Linux, selected with `Config(video_source=...)` — transmitted video then comes
  from the camera instead of `write_frame()`, while `read_frame()` still taps
  received video.
- `examples/08_video_call.py`: a video call between two instances (direct mode,
  no switch needed) or against a video-capable service — camera or synthetic
  source, live preview in a Tk window (color with numpy installed, grayscale
  without), full-color Y4M recording for headless machines; `AUDIO_DRIVER=coreaudio`
  (or `alsa`) turns it into a full video softphone with real microphone and
  speakers.

### Known issues

- The two 0.2.0a1 leaks remain open: ~2 KiB retained per runtime start/close
  cycle ([#1](https://github.com/daily-co/baresip-python/issues/1)), and
  `close()` during a live call leaking that call's native object graph
  ([#2](https://github.com/daily-co/baresip-python/issues/2)) — hang up or
  `drain()` first.

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
