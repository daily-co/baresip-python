# Changelog

All notable changes to baresip-python are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and the project versions follow
SemVer with the 0.x caveat (see the API stability policy in the README).

## [0.7.0] - 2026-11-27

### Upgrading from 0.6.1

No call, account or configuration code has to change. What behaves differently
on the wire, most likely to be noticed first:

1. **TLS servers are now verified.** An account or dial target that uses TLS
   fails where the server's certificate does not chain to a trusted CA or does
   not name the host connected to.
   - A server with a private or self-signed certificate: set `SIP_CA_FILE` to
     its CA certificate.
   - A server reached by IP address over TLS: its certificate has to carry
     that address, or the server has to be reached by the name it does carry.
   - A minimal container with no CA certificates: install `ca-certificates`
     or `certifi`. Without any, TLS is switched off at start and a TLS account
     raises `NoCaCertificatesError`.
2. **Calls that were refused are now answered.** A received call that offers
   SRTP, or an unencrypted call in the `RTP/AVPF` profile, used to be answered
   `488` before the application saw it. It now reaches `on_incoming`.
3. **A transfer carries context.** A call placed for a peer's transfer request
   now carries the `User-to-User` and `X-` headers attached to the transfer
   target, and `Call.transfer` sends its target in angle brackets.
4. **A SIP INFO request is answered `200 OK`** where it was answered `488`.
5. **A received call is no longer renegotiated right after it is answered.**
   The far end saw a re-INVITE 50 to 150 ms after every answer, which
   restarted the audio; it now sees none.
6. **Audio differs slightly with the bundled stack's upgrade** (v4.10.0 to
   v4.13.0): Opus conceals lost packets, and audio after a pause is no longer
   heard late. See "Changed" below.

What this release does not yet do is listed under "Known issues" below.

### Added

- `Account(media_encryption="none" | "srtp")`: `"srtp"` encrypts every call
  with SRTP (SDES keys) and refuses a received call that offers none, or
  whose keys cannot be used for its audio (an unsupported cipher suite, a
  key of the wrong length).
  `Call.media_encrypted` reports whether a call's audio is encrypted.
- `Call.hangup(headers=None)`: custom headers on the BYE that ends an answered
  call, such as `User-to-User` or the `X-` headers a contact-center platform
  reads to route the caller after the bot hangs up. On a received call not
  yet answered they go on the rejection. With no arguments it behaves as
  before.
- `Call.reject(status=486, reason=None, headers=None)`: a refusal carries the
  status the application chooses (300-699), its standard reason phrase or
  one supplied, and custom headers. With no arguments it behaves as before.
- `sips:` addresses: `dial("sips:...")` places the call over TLS, and a call
  whose route or far-end contact is a `sips:` address completes. Until now no
  request to such an address was sent, which broke calls through
  sip.linphone.org and to OpenAI's SIP endpoint. See "Fixed".
- `Call.transfer(uri, headers=None)`: context for the party a transfer calls,
  such as `User-to-User`, attached to the transfer target. A peer's transfer
  request exposes the context of its target as `TransferRequest.headers`, and
  `Call.accept_transfer(headers=None)` sets it on the call it places, with
  the headers given added to it.
- `Call.on_info` / `Call.off_info` and `InfoEvent`: a SIP INFO request
  received on a call is delivered with its content type and raw body. DTMF
  sent as INFO keeps arriving through `on_dtmf`.
- `NoCaCertificatesError`: raised for a TLS account or dial target when the
  runtime found no CA certificates to verify servers with.

### Changed

- **A received call that offers SRTP is accepted and encrypted** on an account
  with no encryption configured. Until now it was answered `488 Not
  Acceptable Here` before the application saw it. When the caller offers both
  an SRTP and an unencrypted audio section, the SRTP one is taken.

- **A transfer target is sent in angle brackets, and an accepted transfer
  carries the target's headers.** `Call.transfer` encloses a `sip:` or
  `sips:` target in angle brackets, which an address with parameters or
  attached headers needs to be read correctly. A call placed for a peer's
  transfer request, by `accept_transfer` or by the `"auto"` transfer
  policy, now carries the `User-to-User` and `X-` headers attached to the
  target; until now every attached header was discarded. The target's other
  headers are still left out.

- **An unencrypted call offered in the RTCP-feedback profile is accepted.**
  An offer of `RTP/AVPF` was answered `488 Not Acceptable Here`; the Linphone
  app offers it first and only then falls back, so its unencrypted calls took
  seconds longer to connect. It is now answered in `RTP/AVPF`. An account with
  `media_encryption="srtp"` refuses it, as it does any unencrypted offer.

- **Received video larger than `Config.video_size` is reported.** Such frames
  are dropped, as before; the first one on a call now logs a warning naming
  both sizes.

- **`hold()` and `resume()` wait for a renegotiation that is still being
  answered.** Both return when their re-INVITE is sent, so a `resume()`
  straight after a `hold()` could arrive while the hold was still awaiting its
  answer, and raised "a session refresh is already in flight; retry shortly".
  They now wait up to five seconds for it and then send.

- **A call refused before the application sees it is reported.** The stack
  answers some INVITEs itself: an offer nothing in the account matches (488),
  a call past `Config.max_concurrent_calls` (486), an unsupported extension.
  Each now logs a warning naming the caller and the reason, and for a refused
  offer its media sections.

- **A SIP INFO request is answered `200 OK`.** Until now an INFO with a body
  other than DTMF relay was answered `488 Not Acceptable Here`. A body over
  8 KiB is still refused with `488`.

- **TLS servers are verified.** A TLS connection now requires the server's
  certificate to chain to a trusted CA and to name the host connected to;
  until now no certificate was checked. The CA certificates are found at
  start: `SSL_CERT_FILE`, `SSL_CERT_DIR`, the platform's locations as
  Python's `ssl` module reports them, then `certifi` if installed. A server
  with a private or self-signed certificate needs `SIP_CA_FILE` pointing at
  its CA, which the SIP stack then trusts in addition. With no CA certificates found, TLS is disabled and UDP and TCP are
  unaffected. A rejected certificate fails registration with the reason in
  the error. `sip_cafile`, `sip_capath` or `sip_verify_server` in
  `extra_config_text` override all of this.

  A proxy on a call's route often gives itself as a bare IP address. A
  request inside the call that needs a new connection to it (after the first
  one closed, or to another node of the service) accepts a certificate valid
  for that address or for the host name the call was configured with: the
  name dialled or the outbound proxy, or, for a call a registered agent
  receives, the registrar's name. Such a connection sends that name as the
  TLS server name (SNI), where a connection to an address sent none. Not covered, so that such a request is not
  sent once the connection has closed: a call dialled by IP address whose
  server's certificate lacks the address, a call received on a connection the
  far end opened, and a route giving a host name the certificate lacks.

- The bundled baresip and libre move from v4.10.0 to v4.13.0. No API of this
  package changes. What a call can show differently:
  - Opus conceals lost packets (PLC) instead of playing a gap.
  - The transmit RTP timestamp advances while there is nothing to send, so
    audio after a pause is not heard late by the far end.
  - Padding on received RTP packets is stripped before decoding.
  - The adaptive jitter buffer adds three times the measured jitter as
    margin, up from two; the fixed buffer is unchanged.
  - The stack no longer reads `audio_buffer_mode` or `audio_silence`; a
    line setting either in `extra_config_text` has no effect.

### Fixed

- A placed call that the far end refused straight away could report "not
  established within N s" in place of the refusal's status. The refusal
  could be delivered before `dial()` had built the call, and was dropped.
  The same held for the call an accepted transfer places.

- **An answered call is no longer renegotiated a moment after it is
  established.** A call answered without video was sent a re-INVITE, with the
  same media as the answer, 50 to 150 ms after the answer. It restarted the
  audio streams, and a hold sent in that moment was refused as colliding
  with it.

- Calls through a proxy or to a peer that uses `sips:` addresses now work.
  A request whose next hop was a `sips:` URI, from a Record-Route (Flexisip,
  sip.linphone.org) or from the peer's Contact (OpenAI's SIP endpoint), was
  never sent: a call placed that way did not complete, and a hang-up, hold or
  transfer did not reach the far end. Such a request now goes over TLS, and
  `dial("sips:...")` works. The fix is a patch carried on the bundled libre
  (docs/UPGRADING.md, "Carried patches").
- A hang-up that cannot be sent, or is sent and not delivered, is logged as
  a warning on `baresip.native`. It used to fail without a trace, leaving the
  far end in a call this side had left.

### Known issues

Calls and transports:

- A call over UDP behind a NAT that drops idle bindings can lose its
  signalling after about a minute without SIP traffic: the far end's hang-up
  then never arrives. `Config.rtp_timeout` is 0 by default, so such a call
  stays open until the application ends it. As in 0.6.1.
- Inside a call over TLS, a request that needs a new connection is not sent
  when the route gives a host name the server's certificate lacks, when the
  call was dialled by IP address and the certificate lacks that address, or
  when the call arrived on a connection the far end opened.
- A TLS connection verified for one call is reused by another call going to
  the same address and port, without that call's own check.
- An offer for DTLS-SRTP is answered as unencrypted, and refused by an account
  that requires SRTP. A default account ignores SRTP keys offered on the
  `RTP/AVPF` profile and answers unencrypted.
- Received video larger than `Config.video_size` is dropped, with a warning.

Headers and callbacks:

- With `rtp_stats yes` in `extra_config_text`, the stack puts its own
  `X-RTP-Stat` header on a BYE in place of the headers given to `hangup()`.
- `reject()` and `hangup()` accept a `Content-Length` header, which conflicts
  with the one the message already carries.
- A NUL character in a header attached to a peer's transfer target cuts the
  headers of the call placed short, the application's own included.
- Registering the same callback twice with `on_info`, `on_rtcp` or
  `on_audio_warning` leaves one copy that the matching `off_` call does not
  remove.

TLS trust:

- A malformed `sip_cafile`, `sip_capath` or `sip_verify_server` line in
  `extra_config_text` (no value, or a longer name that begins the same way)
  still switches off CA discovery, leaving TLS unverified.
- A directory named by `SSL_CERT_DIR` counts as CA certificates when it holds
  any file at all, and is then used in place of a real bundle.
- After a `start()` that was refused, a later `start()` on the same runtime
  keeps TLS marked unavailable even when CA certificates are now found.
- When two TLS accounts fail within five seconds, the second error can name
  the first one's certificate problem.
- The Linux wheels bundle OpenSSL 1.1.1, which its project no longer
  maintains.

## [0.6.1] - 2026-11-20

### Fixed

- Dialing a host name from an account that does not register
  (`reg_interval=0`) with media NAT enabled (`medianat=stun`, `ice` or
  `turn` in `extra_params`) failed with "Address family not supported by
  protocol family" before any INVITE was sent. With no registration and no
  numeric target the stack had no address family for the call, and the
  media-NAT server lookup needs one. The dial now uses the family the host
  has an address for, as a registered account's would.

## [0.6.0] - 2026-10-29

The first final release. The package is what 0.5.2a3 shipped, unchanged: no
code differs between the two, and the public API is the `__all__` of 0.5.2a3.
A plain `pip install baresip-python` now installs it; the pre-releases before
it needed `--pre`.

### Changed

- Development status classifier is Production/Stable. Versioning stays SemVer
  with the 0.x caveat: a minor version may break the API, and every break is
  called out here.

## [0.5.2a3] - 2026-10-01

A logging fix: the stack's log and SIP threads carry no context of their own,
so what crossed from them into Python arrived without the request-scoped
values a host sets around a session. The context active at `Runtime.start()`
now travels with them.

### Fixed

- Native log records, event callbacks, and the notices the stack's own
  threads log (the unexpected-exit critical among them) now run in the
  context that was active when `Runtime.start()` was called. A loguru
  `contextualize` field or a correlation id set there appears on them, and
  on whatever an event handler logs in turn.

## [0.5.2a2] - 2026-09-29

Jitter-buffer statistics are now real. Every wheel so far compiled them out —
baresip keeps them behind `#ifndef RELEASE`, and libre's build defines
`RELEASE` for any non-Debug configuration — so `CallStats.jbuf_*` read zero
on every call, whatever the buffer was doing. The native build now keeps
them in, the report says when they are absent instead of zeroing them, and
two more counters ride along.

### Added

- `CallStats.jbuf_out_of_sequence`: frames that arrived out of sequence
  order — the reordering a jitter buffer exists to absorb.
- `CallStats.jbuf_skew_ms`: the sender-versus-receiver clock drift the
  jitter buffer currently measures; it re-anchors playout (dropping one
  frame) when this exceeds 20 ms across a 10 s window.

### Changed

- `CallStats.jbuf_*` fields are typed `int | None` and read `None` when the
  stack reports no jitter-buffer statistics; previously an absent report
  read as `0`, indistinguishable from a measured zero. Code doing arithmetic
  on these fields must handle `None`.

### Fixed

- `jbuf_delay_ms`, `jbuf_late`, `jbuf_lost`, `jbuf_overflow` and
  `jbuf_jitter_ms` always read `0`: the counters were compiled out of the
  native library. The build now defines `JBUF_STAT` explicitly.
  `jbuf_delay_ms` reports the playout offset in force — the configured
  minimum in fixed mode (100 with the stack default), the adapted value in
  adaptive mode.

## [0.5.2a1] - 2026-09-19

A point release: SIP accounts gain an escape hatch for parameters the typed
fields don't model — media-NAT traversal (ICE/STUN) in particular — and the
wheel build's module-completeness cross-check no longer trips over colorized
cmake output.

### Added

- `Account.extra_params`: extra account parameters appended verbatim to the
  address-of-record, each a `"key=value"` string — the per-account escape hatch
  matching `Config.extra_config_text` at the global scope, for directives the
  typed fields don't model. Media-NAT traversal is the motivating case
  (`("medianat=ice", "stunserver=stun:host:port")`). Only control characters
  are rejected, and it is omitted from `repr()` since it may carry a credential.

### Fixed

- Module-completeness detection strips ANSI color sequences from cmake's
  configure output before parsing `MODULES_DETECTED`, so a colorized build
  environment (`CLICOLOR_FORCE`, newer cmake) can no longer contaminate the last
  parsed module name and trip the build's module-set cross-check.
- The remaining examples (`02`–`07`) start the runtime with a `Config` object,
  completing the Config-only `Runtime.start()` migration from 0.5.0a1.

## [0.5.1a1] - 2026-09-07

A point release: the live-call close leak is fixed, and the package metadata now
carries the project links and author shown on the PyPI page.

### Fixed

- `close()` during a live call no longer leaks the call's native object graph
  ([#2](https://github.com/daily-co/baresip-python/issues/2)). The stopped
  event loop could never run a live call's asynchronous SIP teardown, so the
  terminating session/transaction/dialog objects kept their mutual references
  and the whole graph became unreachable-but-unfreed (~40 KB per live call).
  The shim's unwind now runs the stack's forced shutdown — terminating calls,
  closing sessions, and aborting pending transactions — before the final
  teardown, verified leak-clean under LeakSanitizer.

## [0.5.0a1] - 2026-09-06

### Added

- `Config.extra_config_text`: raw configuration lines appended verbatim after
  the rendered fields — the escape hatch to stack directives the typed fields
  don't model (`sip_listen`, module tuning), now travelling *inside* the
  `Config` object so the command-applied settings (`expose_headers`,
  `instance_id`, `sip_trace`, the log level) are never lost with it.

### Changed

- **BREAKING**: `Runtime.start()` no longer accepts raw configuration text —
  it takes a `Config` (or None for the stack's defaults) and raises
  `TypeError` otherwise. A raw-text start physically cannot carry the
  command-applied settings above and dropped them silently; anything the
  typed fields don't model goes in `Config(extra_config_text=...)`.

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
