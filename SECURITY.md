# Security policy

## Reporting a vulnerability

Please report suspected vulnerabilities privately through GitHub's
[private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
on this repository (**Security → Report a vulnerability**). Do not open a public issue for
anything you believe has security impact.

## Supported versions

Pre-1.0, only the latest release receives security fixes.

## Bundled libraries are our responsibility

Released wheels statically bundle libre, libbaresip, libopus, libvpx, and libg722, and
carry OpenSSL with them — Linux wheels additionally bundle ALSA's libasound. A security
advisory in any of these triggers a patch release of baresip-python with
the updated dependency, independent of the normal release cadence — the policy and pinned
versions live in [docs/UPGRADING.md](docs/UPGRADING.md). If you deploy the wheels, you
patch these libraries by upgrading baresip-python, not through your system package manager.

## Threat model notes for operators

A registered SIP user agent is a server as well as a client. Once your process holds a
socket on a publicly addressable host, it will receive traffic you did not invite:
scanners (SIPVicious and friends) probing for open relays, and unsolicited INVITEs from
strangers. Plan for that:

- **Bind deliberately.** Pin the stack to the interface you mean —
  `runtime.start(Config(net_interface=...))`, with a `sip_listen` line in
  `Config(extra_config_text=...)` for direct-mode binds — exactly as the bundled
  examples pin themselves to loopback for the bench. Do not let a process meant for one
  network listen on all of them.
- **The default posture is registrar-only.** A user agent that registers to your SIP
  server and dials out needs no exposure to anyone but that server; put it behind the same
  firewalling you would give any internal service, and let the registrar be the party that
  faces the world.
- **Screen inbound calls in code.** `on_incoming` hands your callback the caller's URI,
  the SIP Call-ID, and any allowlisted headers *before* the call is answered — the
  callback's `answer()`-or-`reject()` decision is your admission control. Reject what you
  do not recognize; an unanswered stranger costs you nothing.
- **Treat SIP traces as sensitive.** The `baresip.native.sip` trace contains call metadata
  for every party that contacts you and the digest material from authentication exchanges
  (see [docs/LOGGING.md](docs/LOGGING.md)).

Passwords are never written to logs, traces, or `repr()` output by this library. Rate and
concurrency limits (the `max_concurrent_calls` setting is reserved for this) and policy
controls for call transfer are planned; until then, admission control lives in your
`on_incoming` callback.
