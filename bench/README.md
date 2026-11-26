# FreeSWITCH bench

A disposable, localhost-only FreeSWITCH instance that integration tests (and humans) can
register against and call. It is the standing SIP peer for everything the unit suite cannot
cover: registration, real calls, RTP. Beside it runs a SIP proxy (Kamailio), for what a switch
cannot show: how an endpoint behaves once a proxy has put itself on a call's route (see
[The proxy](#the-proxy)).

## Security posture — read this first

- **The credentials are dummy and committed on purpose.** Accounts `1001`-`1004` share the
  password `bench1234`. They protect nothing; they exist so the auth code paths get exercised.
- **Everything binds to `127.0.0.1` only** (see `docker-compose.yml`) and the SIP port is a
  non-default one, so the bench cannot collide with — or be mistaken for — a real SIP agent.
  Do not edit the port mappings to expose it; if you need it reachable from another machine,
  you want a real FreeSWITCH deployment, not this.

## Usage

```sh
make bench-up      # pull (first time) + start, waits until healthy
make bench-logs    # follow the FreeSWITCH console
make bench-down    # stop and remove
```

The first `bench-up` pulls the pinned image. On Apple Silicon it runs under Docker Desktop's
emulation (the image is amd64-only); that is fine at bench scale.

| What | Value |
|---|---|
| SIP | `127.0.0.1:15060` (UDP/TCP) — override the host port with `BENCH_SIP_PORT` |
| SIP over TLS | `127.0.0.1:15061` — override the host port with `BENCH_TLS_PORT` |
| RTP | `127.0.0.1:16384–16403` (UDP) |
| Accounts | `1001` / `1002` (humans), `1003` / `1004` (automated tests), password `bench1234` |
| `9196` | echo test — you hear yourself |
| `9664` | playback test — a generated tone |
| `9486`, `9603` | reject with 486 / 603 before answering (failure-path tests) |
| `1001`-`1004` | bridges to that registered user |

Tests read `BENCH_SIP_PORT` (default `15060`) rather than hardcoding the port. Overriding it
only remaps the host side, while FreeSWITCH keeps advertising port `15060` in its Contact —
registration and calls still work, but in-dialog requests sent to the Contact go astray. Only
override it to escape a port collision, and prefer the default. The same applies to
`BENCH_TLS_PORT` and the TLS port.

TLS uses a self-signed certificate for `127.0.0.1` that `make bench-up` generates into
`bench/certs/` (gitignored — regenerate freely, it protects nothing). Clients verify the
server against `bench/certs/cert.pem`; FreeSWITCH reads the combined `wss.pem`.

## Manual smoke test

With any SIP client (e.g. Linphone): register as user `1002`, password `bench1234`, domain
`127.0.0.1:15060`, transport UDP — then dial `9196` and talk. **Disable ICE and STUN in the
client**: ICE connectivity checks cannot reliably traverse Docker's port forwarding (the bench
filters candidates so some ICE clients work anyway, but plain RTP is the supported path). If a
call is rejected immediately, the client is still offering ICE — some clients keep ICE as a
per-account setting that overrides the global one; change it and re-register. Hearing yourself back proves
registration, both call directions of signaling, and RTP both ways through Docker's port
forwarding. `9664` should play a steady beeping tone.

## The proxy

FreeSWITCH ends each call leg itself and never adds a Record-Route, so nothing sent inside a
call through it is ever routed. The proxy does the opposite: it registers endpoints, relays
calls between them, and Record-Routes every one. From then on the ACK, a hold, a transfer
request and the hang-up all go to the proxy's route address, not to the peer.

Register at `127.0.0.1:15070` (no password is asked), and place the call to the port that
gives the route form you want:

| Call enters on | Record-Route the call gets (top entry first) |
|---|---|
| `15070` (UDP/TCP) | `<sip:127.0.0.1:15070;lr>` |
| `15071` (TLS) | `<sip:127.0.0.1:15070;lr>`, `<sip:127.0.0.1:15071;transport=tls;lr>` |
| `15072` (TLS) | `<sips:127.0.0.1:15072;lr>` |
| `15073` (TLS) | `<sips:localhost:15073;lr>` |
| `15074` (TLS) | `<sip:127.0.0.1:15070;lr>`, `<sips:127.0.0.1:15074;lr>` |
| `15075` (TLS) | `<sips:localtest.me:15075;lr>`: a public name for 127.0.0.1 that the bench certificate does not carry, as a server's route often names an address its certificate does not |

The answering side takes the top entry as its next hop, the caller the bottom one. The `sips:`
forms are what Flexisip (sip.linphone.org) sends. The proxy reaches an endpoint the way it
registered, relays only between registered endpoints (not to FreeSWITCH), and shares
FreeSWITCH's network namespace, which is why its ports are published on that service. Its TLS
certificate is the bench's own (`bench/certs/cert.pem`). `make bench-proxy-logs` follows its
log. Configuration: `conf/kamailio.cfg`.

Because the proxy lives in FreeSWITCH's network namespace, restarting the FreeSWITCH container
on its own leaves the proxy running without a network, and Docker still reports it healthy.
Restart it too (`docker restart baresip-bench-kamailio`); the proxy tests do so themselves when
they find it unreachable.

## Driving the switch from tests

`mod_event_socket` listens on the *container's* loopback only (never published). Tests reach it
through Docker instead of the network:

```sh
docker exec baresip-bench-freeswitch fs_cli -x "status"
docker exec baresip-bench-freeswitch fs_cli -x "originate user/1002 &echo()"
```

## Configuration

The entire switch is configured by `conf/freeswitch.xml` — a deliberately minimal single file
rather than the vanilla configuration tree, so the whole peer the tests depend on is reviewable
at a glance. The RTP port range in that file and the published range in `docker-compose.yml`
must stay in lockstep.
