# Vendored dependencies and the upgrade policy

baresip-python statically bundles the [baresip](https://github.com/baresip/baresip) SIP stack
and its [libre](https://github.com/baresip/re) library as git submodules under `third_party/`.
They are never forked, and the submodule checkouts are never edited: a fix we need goes
upstream as a pull request, and we pick it up by bumping the pin. The one exception is a fix
upstream has declined or not yet released, which is carried as a patch file (see
[Carried patches](#carried-patches)).

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
   nothing else, apart from refreshing or dropping the carried patches the bump affects.
3. **The full test suite must pass before merging a bump** — unit and integration. The
   integration suite is not part of the per-push CI: run the `bench` workflow on the bump's
   branch, or `make bench-up` and `pytest -m bench tests/integration` locally. Pay
   particular attention to any test that guards against upstream enum or API changes: those
   exist precisely to catch silent breakage a version bump can introduce.
4. Read the upstream changelogs for both projects before bumping, with special attention to
   changes in the public API (`baresip.h`), the event enum, module behavior, and build-system
   requirements.

## Carried patches

The bundled libre is not identical to upstream's release: the build applies the patches under
`third_party/patches/<library>/`, in name order, to a copy of the library staged under the build
prefix (`scripts/build_native.py`). The submodule checkout stays as upstream released it. A
patch that no longer applies fails the build, and `tests/unit/test_carried_patches.py` applies
every patch to the pinned commit.

| Patch | What it does | Why it is carried | Goes when |
|---|---|---|---|
| `re/0001-sip-sips-next-hop-over-tls.patch` | Sends a request whose next hop is a `sips:` URI over TLS; upstream refuses it with `ENOSYS`. | Flexisip (sip.linphone.org) Record-Routes with `sips:` and OpenAI's SIP endpoint answers with a `sips:` Contact, so without it no ACK, BYE or re-INVITE goes out on those paths. Upstream declined the change ([re#1647](https://github.com/baresip/re/pull/1647), [re#1659](https://github.com/baresip/re/issues/1659)). | Upstream accepts `sips:` next hops. |
| `re/0002-sipsess-warn-on-unsent-bye.patch` | Logs a warning when a session's BYE cannot be sent or is not delivered. | The error was dropped in three places, so a hang-up that never reached the far end left no trace. Offered upstream. | A release includes it. |

A patch is a standing cost: every bump has to check that it still applies and is still
needed. Add one only for a fix upstream has declined or not yet released, name its origin in
the patch's own header, and add a row here. When bumping, run the unit suite first: a patch
upstream has since made unnecessary or that no longer applies shows up there.

## Security updates for bundled libraries

Released wheels bundle OpenSSL, libopus, libvpx, and libg722 (built into the binary,
repaired in by the wheel tooling), and Linux wheels also bundle ALSA's libasound for the
hardware audio driver. A security release in any bundled library is our responsibility to
ship:

- Watch the security advisories for OpenSSL, libopus, libvpx, libg722, alsa-lib, libre,
  and baresip.
- A relevant advisory triggers a patch release of baresip-python with the updated dependency,
  independent of the normal release cadence.
