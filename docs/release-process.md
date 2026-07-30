# KiroCrew Release Process

Deeper detail: [`release-process-design.md`](release-process-design.md).

## The model

- **`main` is always the latest (non-stable) code.** Nightly builds off `main` —
  once a night, or several times a day on demand.
- **Feature releases are cut as a release branch off `main`**, on 0.1
  increments: `0.1.0` → `0.2.0` → `0.3.0`.
- **Bug fixes go on the release branch**, not `main`. Each produces a new
  insider RC (release candidate): `0.2.0.rc1`, `0.2.0.rc2`, …
- **Stable is the last RC we judge stable enough** — promoted by tagging it, not
  rebuilt. So `0.2.0.rc5` becomes stable `0.2.0`.
- **Hot patches bump the patch digit** (`0.2.0` → `0.2.1`), cut from the release
  branch. Small ones go straight to stable; larger ones go through an RC on
  insider first.
- **After each stable cut**, bump the version on `main` by 0.1 and merge the
  branch's fixes back. Releasing stable `0.2.0` moves `main` to `0.3.0`.

## Channels

| Channel | Built from | Who |
|---|---|---|
| nightly | `main` | us and contributors |
| insider | release branch, RC tags | power users testing ahead |
| stable | the promoted insider | everyone (client default) |

Nightly installs side by side as its own app; insider and stable are two update
lanes of one production app, switchable in Settings.

## Cutting a release

1. Branch off `main` (`0.2.0`).
2. Tag RCs on the branch as fixes land: `v0.2.0-rc.1`, `-rc.2`, … → insider.
3. Tag the good RC's commit with a bare `v0.2.0` → stable.
4. Bump `main` to `0.3.0`, merge the fixes back.
5. Hot patch: fix on the branch, tag `v0.2.1`.

## How builds are triggered

- **Nightly** runs on a schedule every night, and can be kicked off on demand
  any time.
- **Insider and stable** are triggered by pushing a version tag — an RC tag
  publishes to insider, a plain version tag publishes to stable.

The release branch, RC numbering, promote decision, and back-merge are all
human process; the pipeline only reacts to the tag.

Each build ships a signed and notarized macOS app, a Linux AppImage, a CLI
installer, a pip wheel, and a Docker image. The update feed for a channel is
repointed last, after the artifacts are verified downloadable, and clients only
install with the user's consent. Windows builds but is not yet signed or
published.

There is no rollback: we roll forward by cutting a new version.
