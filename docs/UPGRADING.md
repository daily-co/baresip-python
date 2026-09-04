# Vendored dependencies and the upgrade policy

baresip-python statically bundles the [baresip](https://github.com/baresip/baresip) SIP stack
and its [libre](https://github.com/baresip/re) library as git submodules under `third_party/`.
They are never forked: any fix we need goes upstream as a pull request, and we pick it up by
bumping the pin.

## Currently pinned versions

| Submodule | Version | Commit |
|---|---|---|
| `third_party/re` | v4.10.0 | `57beb068cd7944c829102710fd416749c3e694d9` |
| `third_party/baresip` | v4.10.0 | `a1542aedf7c8dfba095214ef3e83fcd4d9fb4884` |

baresip and libre are released in lockstep; always bump both to the same version.

## Bump policy

1. **Release tags only.** Submodules are pinned to upstream release tags, never to `main` or
   arbitrary commits.
2. **Bump both together**, to matching versions, in a dedicated pull request that contains
   nothing else.
3. **The full test suite must pass before merging a bump** — unit and integration. Pay
   particular attention to any test that guards against upstream enum or API changes: those
   exist precisely to catch silent breakage a version bump can introduce.
4. Read the upstream changelogs for both projects before bumping, with special attention to
   changes in the public API (`baresip.h`), the event enum, module behavior, and build-system
   requirements.

## Security updates for bundled libraries

Released wheels bundle OpenSSL, libopus, libvpx, and libg722 (built into the binary,
repaired in by the wheel tooling), and Linux wheels also bundle ALSA's libasound for the
hardware audio driver. A security release in any bundled library is our responsibility to
ship:

- Watch the security advisories for OpenSSL, libopus, libvpx, libg722, alsa-lib, libre,
  and baresip.
- A relevant advisory triggers a patch release of baresip-python with the updated dependency,
  independent of the normal release cadence.
