# Vendored dependencies and the upgrade policy

baresip-python statically bundles the [baresip](https://github.com/baresip/baresip) SIP stack
and its [libre](https://github.com/baresip/re) library as git submodules under `third_party/`.
They are never forked: any fix we need goes upstream as a pull request, and we pick it up by
bumping the pin.

## Currently pinned versions

| Submodule | Version | Commit |
|---|---|---|
| `third_party/re` | v4.13.0 | `b827bcaf55a18be141ca7bcee1e22872a3a29656` |
| `third_party/baresip` | v4.13.0 | `8a4f18c1c1b5331daa026236dcdd108deff601e8` |

baresip and libre are released in lockstep; always bump both to the same version.

## Bump policy

1. **Release tags only.** Submodules are pinned to upstream release tags, never to `main` or
   arbitrary commits.
2. **Bump both together**, to matching versions, in a dedicated pull request that contains
   nothing else.
3. **The full test suite must pass before merging a bump** — unit and integration. The
   integration suite is not part of the per-push CI: run the `bench` workflow on the bump's
   branch, or `make bench-up` and `pytest -m bench tests/integration` locally. Pay
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
