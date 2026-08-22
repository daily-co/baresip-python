# FreeSWITCH bench

A disposable, localhost-only FreeSWITCH instance that integration tests (and humans) can
register against and call. It is the standing SIP peer for everything the unit suite cannot
cover: registration, real calls, RTP.

## Security posture — read this first

- **The credentials are dummy and committed on purpose.** Accounts `1001` and `1002` share the
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
| RTP | `127.0.0.1:16384–16393` (UDP) |
| Accounts | `1001` / `1002`, password `bench1234` |
| `9196` | echo test — you hear yourself |
| `9664` | playback test — a generated tone |
| `1001`, `1002` | bridges to that registered user |

Tests read `BENCH_SIP_PORT` (default `15060`) rather than hardcoding the port. Overriding it
only remaps the host side, while FreeSWITCH keeps advertising port `15060` in its Contact —
registration and calls still work, but in-dialog requests sent to the Contact go astray. Only
override it to escape a port collision, and prefer the default.

## Manual smoke test

With any SIP client (e.g. Linphone): register as user `1002`, password `bench1234`, domain
`127.0.0.1:15060`, transport UDP — then dial `9196` and talk. **Disable ICE and STUN in the
client**: ICE connectivity checks cannot reliably traverse Docker's port forwarding (the bench
filters candidates so some ICE clients work anyway, but plain RTP is the supported path). If a
call is rejected immediately, the client is still offering ICE — some clients keep ICE as a
per-account setting that overrides the global one; change it and re-register. Hearing yourself back proves
registration, both call directions of signaling, and RTP both ways through Docker's port
forwarding. `9664` should play a steady beeping tone.

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
