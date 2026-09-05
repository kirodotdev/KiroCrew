# Release Reference

The single reference for how a Kiro Crew release is cut, what CI does at each
step, where the artifacts land, and how to verify one.

Ownership boundary: [CONTRIBUTING.md](../../CONTRIBUTING.md) → "Releasing New
Versions" owns the **human** process (cutting a release branch, numbering RCs,
promoting, back-merging, bumping the in-code version) and the exact git
commands. This file documents what the **pipeline** does once a tag exists.
macOS signing mechanics and notary-credential rotation live in
[signing-runbook.md](signing-runbook.md); desktop packaging lives in
[desktop-app.md](desktop-app.md); the PR-time quality gates live in
[../ci/ci-and-reviews.md](../ci/ci-and-reviews.md).

## The three channels

| Channel | Trigger | Version shape |
|---------|---------|---------------|
| `nightly` | `nightly.yml`: cron `0 6 * * *` (06:00 UTC) plus manual dispatch, from `main` HEAD | `<base>-nightly.<YYYYMMDD>t<HHMMSS>` |
| `insider` | `release.yml`: push of a prerelease tag (`v0.2.0-rc.1`) | `<x.y.z>-rc.N` |
| `stable` | `release.yml`: push of a bare semver tag (`v0.2.0`) on a recorded candidate's commit; the run verifies and promotes that candidate's exact bytes, never rebuilding | Release identity `<x.y.z>`; artifacts retain the selected candidate's embedded `<x.y.z>rcN` version |

The channel name is a literal path segment everywhere (`cli/insider/...`,
`feed/insider/...`, the `:insider` image tag), so there is no name-to-prefix
mapping to get wrong. `test_publish_feed_contract.py` pins the channel set
against `publish-cli.yml`, and `cli.sh` rejects anything outside it before
touching the CDN.

Channel is derived from the tag, and nothing else. `release.yml`'s `version` job
treats **any** prerelease label as insider and maps it to a PEP 440 `rcN` wheel
using the label's trailing number. Cutting the branch, numbering the RCs,
deciding to promote, and merging back are human steps the pipeline knows nothing
about, which is why there is no cut/promote/rollback workflow (see "Deliberately
not built").

## Stable release: a fresh build at the bare version, never a byte republish

A stable release must ship bytes whose own embedded version is a bare `X.Y.Z`,
with no prerelease suffix anywhere in the artifact, its filename, or the feed.
That is what rules out republishing the candidate's bytes: they were stamped
from the prerelease tag, and nothing downstream can re-stamp them without
invalidating the recorded digests and the macOS signatures. So a bare tag
rebuilds from the commit the candidate cleared, rather than reusing what the
candidate produced. The mechanism:

- **A successful prerelease run clears the candidate commit.** After every
  publish lane succeeds, `record-promotion` assembles the exact wheel/sdist,
  AppImage, notarized zip/DMG, and the attested OCI manifest digest into a
  `stable-promotion-<x.y.z>` GitHub artifact (90-day retention) whose manifest
  (`scripts/release_promotion.py create`) carries per-file SHA-256/SHA-512/size
  plus the source SHA, tag, run id, and versions. Its primary role is evidence
  that this commit shipped clean on insider; the bytes it carries are only
  consumed by the byte-reuse escape hatch below.
- **A bare `vX.Y.Z` tag rebuilds that commit at the bare version.** `stable-gate`
  confirms a **successful** same-commit prerelease run exists, so stable still
  only ships code that soaked. Then the ordinary build path runs on the stable
  channel — `build-wheel.yml`, `build-desktop.yml`, `build-windows.yml`,
  `sign-and-notarize.yml`, the OCI builder — receiving `X.Y.Z` exactly the way an
  insider build receives `X.Y.Z-rc.N`. What a stable release gives up is byte
  identity with the binary insiders ran; what it never gives up is that the
  commit soaked.
- **Byte-for-byte reuse remains available, and names its own cost.** Setting
  `vars.STABLE_PROMOTE_BYTES` to exactly the base version being released takes
  the promotion path instead: `resolve-promotion` verifies the recorded bundle
  and the publish lanes move stable pointers to those exact bytes. Use it when
  stable must run the identical binary that was validated. The cost is the whole
  reason it is not the default — those bytes advertise the candidate's `rcN`
  stamp in the wheel name, the feed, `pip show`, and `kirocrew --version`. The
  variable is scoped to one version so it cannot be left switched on, and the
  gate prints a warning naming the consequence.
- **Everything fails closed.** No successful same-commit prerelease run, an
  undocumented version, or a release branch still declaring the RC spelling all
  abort the release before any lane publishes. On the byte-reuse path a missing,
  expired, ambiguous, or digest-mismatched record aborts it too.
- **The bare version is stamped in at build time, not patched afterwards** (see
  "Version stamping"). There is no metadata-rewrite step to trust. `stable-gate`
  additionally compares the tag against all three declaration files, because a
  rebuild would otherwise paper over a release branch that never landed its
  drop-RC-suffix PR: the artifact would be right while every source install and
  every later RC on that branch still read the stale spelling. The 0.4.0
  promotion was nearly tagged in exactly that state, because only the tag name
  was checked.
- **A stable wheel installs without `--pre`**, because its own version carries no
  prerelease suffix. A wheel published through the byte-reuse hatch does need it.
- **The display fold still exists, and stable is no longer its main job.** A
  rebuilt stable has nothing to fold — its stamp is already bare. The fold
  remains load-bearing for insider and nightly, where the prerelease number is
  meaningful information, and for any install shipped before this policy that is
  still running promoted RC-stamped bytes. It is a CONTRACT over every surface,
  not a fix at two spots — the 0.4.0 promotion shipped with only the
  running-version fold wired, and users then reported the raw stamp on four other
  surfaces one by one (the About panel's available-update line, the version chip,
  the Settings footer,
  and the proactive update popup), each needing its own hotfix.

  The contract:

  - *Fold rule*: `_display_version()` in
    `src/kiro_crew/dashboard/handlers/updates.py` folds a version to its bare
    `X.Y.Z` on the **stable** channel only (insider/nightly keep the full
    stamp, where the prerelease number is meaningful information). The SPA
    mirror for desktop-reported versions that never cross the gateway is
    `website/src/utils/displayVersion.ts` — keep it the ONLY TypeScript
    spelling.
  - *Every RAW version field is functional and must never be folded in place*:
    `__version__` and the status frame's `version` (the SPA compares it across
    pushes to force a reload over a gateway upgrade), `latest_version` /
    `update_latest_version` (the arm target `verify_shadow_venv` compares
    byte-for-byte, and the update popup's per-version snooze/skip keys),
    Electron's `info.version` (`versionLooksPrerelease` derives the update lane
    from the `-` suffix), and every `_is_newer` / floor comparison. Folding one
    of these in place broke the entire stable in-app apply path once (caught in
    review); folding the snooze key would make dismissing `0.4.0` swallow the
    next release's rc candidate.
  - *Display therefore always reads a folded SIBLING field, never the raw one*:
    `version_display` and `update_latest_version_display` on the status frame,
    `latest_version_display` on `/api/update/check` and the channel-switch
    response, `current_version` on the check response. **Any NEW surface that
    prints a version to the user must consume one of these (with a raw
    fallback for older gateways) or add its own sibling — never render a raw
    field directly.** A UI review that sees `status?.version`,
    `update_latest_version`, or `info?.version` in display JSX should treat it
    as a bug.

  `test/test_stable_version_display.py`, the `version_display` /
  `update_latest_version_display` tests in
  `test/test_dashboard_status_snapshot.py`, and the AboutPanel /
  UpdateFoundModal / SettingsPage frontend tests are the regression gates.
  **A display change still wants to land before the RC is cut**, so the surface
  it fixes is exercised during the soak rather than first appearing in the
  release. It is no longer unrecoverable if it does not: stable is rebuilt from
  the commit, so a fold landed on the release branch before the bare tag does
  reach stable. Under the byte-reuse hatch the original constraint holds in
  full — those bytes are the RC's, so a fold added after the RC was cut can
  never reach that release.
- **Hot patches follow the same rule**: at least one recorded RC before the
  bare patch tag. A hot patch is the NEXT three-segment version (`0.4.0` →
  `0.4.1`): the release workflow's Derive-Version step rejects a four-segment
  base like `0.4.0.1` outright, and re-using the shipped version is impossible —
  its published keys are immutable.
- **A hotfix RC never moves the insider channel backward.** The channel feeds
  are mutable last-writer-wins pointers, and the insider line is usually AHEAD
  of the line being hotfixed — when `v0.4.1-insider.1` published while insider
  served `0.5.0-insider.1`, the feed rolled back and every insider client was
  offered a downgrade (electron-updater accepts one whenever the installed
  version carries a prerelease suffix). Every publish workflow now runs
  `scripts/check_feed_advance.py` before its pointer writes: the versioned
  assets and the GitHub release always publish (that is what the stable gate
  and promotion consume), but the feed, the legacy mac feed, and the `latest/`
  aliases are rewritten only when this run's version is the newest the channel
  has seen — judged against the live feed (via the public CDN; the publish
  role is Put-only on `feed/*`) AND the repo's tags, which close the CDN's
  `max-age` staleness window. Equal versions advance, so re-running a
  half-finished publish stays idempotent. A stable release is unaffected either
  way: whether it rebuilds or republishes the candidate's bytes, its version is
  newer than everything the stable feed has served.
  `test/test_check_feed_advance.py` pins the verdicts and the wiring.

### Runbook: promoting an RC to stable

The constraint that shapes the whole timeline: **promotion is byte-for-byte, so
anything a stable user will see must already be in the RC that gets promoted** —
there is no build step at stable-tag time to add it.

1. **Before the RC is cut — bake the fix in.**
   - *Version drop PR*: merge the PR that changes `__version__` from
     `X.Y.Z-rc.N` to the bare `X.Y.Z` in all three version files — step 1 of
     the three-step sequence in "Version numbering policy". **This PR is what
     makes the next RC a promotion candidate**; the 0.4.0 promotion was nearly
     tagged before it existed because this step lived only in the policy
     section, not here. The checklist in step 2 below verifies it landed.
   - *CHANGELOG*: the release branch already carries `## [X.Y.Z] - <date>` (no
     `[Unreleased]`, enforced by the changelog gate). Confirm at cut time.
   - *Version display*: the base-version fold above must be merged to `main`
     and cherry-picked to `release/X.Y` **before the RC is cut**, or stable will
     show the RC stamp.
2. **Cut the RC — verify content, not PR status.** On the target commit confirm:
   `github-release` has an `if:`; `CHANGELOG.md` line 5 is `## [X.Y.Z]` with zero
   non-bare-release `##` headings; no `### Contributors` (the GitHub Release
   page renders its own); no em or en dash anywhere in the new section;
   `__version__ = "X.Y.Z"` **in all three version files** (`src/kiro_crew/__init__.py`,
   `pyproject.toml`, `website/electron/package.json` — the 0.4.0 promotion was
   nearly tagged on a commit still declaring `0.4.0-rc.9` because only the tag
   name was checked); the bytecode/pycache test fix is present; the display-fold
   contract above is fully present (run the regression gates:
   `test_stable_version_display.py` + the `version_display` tests — a stamped
   stable build must show `X.Y.Z` on the version chip, the Settings footer, the
   available-update line, AND the update popup); no existing bare
   `vX.Y.Z` tag. Then tag `vX.Y.Z-insider.N`.
3. **Soak.** Ship the RC on insider and let real users run it. **Do not push any
   change to the release branch between soak and release** — stable is rebuilt
   from this commit, so a commit that lands after the soak ships code nobody ran.
4. **Release (bare tag).** Confirm the `vX.Y.Z-insider.N` run at the target
   commit is SUCCESS — `stable-gate` requires it, so a candidate whose run was
   cancelled or failed cannot be released. Push a bare `vX.Y.Z` tag on that
   commit. The build lanes RUN: stable is rebuilt from this commit with `X.Y.Z`
   stamped in, so the shipped wheel is `kirocrew-X.Y.Z-py3-none-any.whl` and
   `kirocrew --version` prints `X.Y.Z`. Expect the full build time, not a
   pointer move. `Create GitHub Release` runs (the `if:` fix) and renders
   GitHub's own contributor block — **do not hand-write a contributors list in
   the body** (that duplicated the native block on v0.3.0). Verify: stable feed
   carries the bare `X.Y.Z`, the wheel filename has no `rc`, About shows
   `X.Y.Z`, CHANGELOG shows no draft heading.

   To ship the candidate's exact bytes instead — the only mode where stable runs
   the identical binary that was validated — set `vars.STABLE_PROMOTE_BYTES` to
   exactly `X.Y.Z` before pushing the tag, and unset it afterwards. That release
   will advertise the candidate's `rcN` version everywhere its bytes are read,
   and the gate warns about it in the run log.

## Workflows in the release path

Every one of these exists on `main`. Two trigger workflows call the same set of
reusable ones, which hold all of the build, sign, and publish logic so the
recipe exists exactly once; the triggers carry only their trigger, their
concurrency group, and their version derivation.

| Workflow | Kind | Role |
|---|---|---|
| `nightly.yml` | trigger (schedule + dispatch) | Derives the date stamp, then calls everything below. `concurrency: nightly-build` with `cancel-in-progress: true`. |
| `release.yml` | trigger (`push` on `v*` tags) | Derives version + channel + wheel version from the tag. A prerelease tag builds, publishes to insider, and records the immutable promotion bundle; a bare tag verifies that same-commit bundle and promotes the exact files/OCI digest to stable without building. Then creates the GitHub Release. `concurrency: release-publish` with `cancel-in-progress: false` (queued). |
| `dependency-vulnerability.yml` | reusable gate | `scripts/check_npm_audit.py`. On a release every build job needs it; on a nightly every **publish** job needs it and no build job does, so a slow registry delays publication rather than failing the build. |
| `build-wheel.yml` | reusable build | Stamps the PEP 440 version into `pyproject.toml` and `__init__.py`, stamps the distribution channel, builds the frontend and stages it into the package, then `python -m build`. Uploads artifact `cli-wheel` (wheel + sdist). Credential-free. |
| `build-desktop.yml` | reusable build | Matrix `macos-15` (universal macOS app) and `ubuntu-22.04` / `ubuntu-22.04-arm` (AppImage + deb + rpm) via `packaging/build-desktop.sh`, then a `smoke-linux-packages` job that installs the deb and rpm in Ubuntu 24.04 and Amazon Linux 2023 containers. Deliberately credential-free (`contents: read` only, pinned by `test_workflow_permissions.py`), so it builds **unsigned** and hands the `.app` downstream. |
| `build-windows.yml` | reusable build | `windows-latest`, an NSIS `Setup.exe`. Separate from `build-desktop.yml` because Authenticode signing has to happen *inside* the build (the installer compresses its own already-signed executable), so this job holds an AWS Signer identity and `build-desktop.yml` can stay credential-free. Callers pass `soft_fail: true`, so a Windows failure cannot skip the mac/Linux lanes. |
| `publish-cli.yml` | reusable publish | Wheel + `SHA256SUMS` + KMS-signed `cli-manifest.json` to `cli/<channel>/<version>/`, the same signed manifest to `feed/<channel>/latest-cli.json`, and a PEP 503 index under `feed/<channel>/simple/`. |
| `publish-linux.yml` | reusable publish | One Linux artifact to `desktop/<channel>/<version>/`, its channel file under `<feed prefix>/latest-linux[-arm64].yml`, then the `latest/` alias. Invoked ONCE PER (ARCH, FORMAT) PAIR — `arch: x64\|arm64` × `format: appimage\|deb\|rpm`, six callers — each with its own keys and feed, so no two ever share one. |
| `sign-and-notarize.yml` | reusable publish | Three chained jobs (`sign`, `notarize`, `publish`) covering the whole macOS trust chain and the mac feed write. |
| `publish-docker.yml` | reusable publish | Multi-arch (`linux/amd64,linux/arm64`) image built from the same wheel, pushed to `ghcr.io/<owner>/kirocrew`. |
| `publish-installer.yml` | independent publish | Publishes `cli.sh` to the distribution bucket root. Triggered by a push to `main` touching `cli.sh` (path-filtered), plus manual dispatch. **Not** part of a channel release. |

Release-adjacent, deliberately outside the release path:

| Workflow | Role |
|---|---|
| `ota-test.yml` | End-to-end macOS auto-update proof: builds two real app versions signed with one throwaway self-signed identity in a temp keychain, serves a local feed, drives consent over the Chrome DevTools Protocol, and asserts the on-disk bundle version flips. Nightly at `40 8 * * *` plus dispatch. Proves the **swap mechanism**, not Gatekeeper acceptance. Needs no secrets. |
| `docker-smoke.yml` | PR gate on the container contract (amd64, load-to-daemon, no push). |
| `pages.yml` | Deploys the marketing site in `site/` to GitHub Pages on `main`, path-scoped to `site/**`. |
| `ship-report.yml` | Twice-daily merged-PR summary to Slack. Not a release step. |

## Where artifacts land

### Two S3 buckets, two trust domains

The **signing bucket** is private working space and never public:

```
pre-signed/<channel>/<version>/     unsigned uploads from the sign job
signed/<channel>/<version>/         CDSigner output (CI cannot write here)
notarized/<channel>/<version>/      stapled, Gatekeeper-verified archive
```

The **distribution bucket** is private with BLOCK_ALL and served only through
CloudFront with Origin Access Control. Two advertised hostnames alias the same
distribution: `updates.crew.kiro.dev` for pointers and
`download.crew.kiro.dev` for artifact bytes. Splitting the URL classes across
hostnames means future protective policy on the byte surface can never touch the
availability-critical feed path.

```
cli/<channel>/<version>/kirocrew-<version>-py3-none-any.whl   immutable
cli/<channel>/<version>/SHA256SUMS                            immutable
cli/<channel>/<version>/cli-manifest.json                     immutable
desktop/<channel>/<version>/KiroCrew.zip                      immutable
desktop/<channel>/<version>/KiroCrew.dmg                      immutable
desktop/<channel>/<version>/KiroCrew-x86_64.AppImage          immutable
desktop/<channel>/<version>/KiroCrew-aarch64.AppImage         immutable
desktop/<channel>/<version>/KiroCrew-x86_64.deb               immutable
desktop/<channel>/<version>/KiroCrew-aarch64.deb              immutable
desktop/<channel>/<version>/KiroCrew-x86_64.rpm               immutable
desktop/<channel>/<version>/KiroCrew-aarch64.rpm              immutable
desktop/<channel>/latest/KiroCrew.dmg                         pointer, max-age=300
desktop/<channel>/latest/KiroCrew-x86_64.AppImage             pointer, max-age=300
desktop/<channel>/latest/KiroCrew-aarch64.AppImage            pointer, max-age=300
desktop/<channel>/latest/KiroCrew-<arch>.deb                  pointer, max-age=300
desktop/<channel>/latest/KiroCrew-<arch>.rpm                  pointer, max-age=300
feed/<channel>/latest-mac.yml                                 pointer, max-age=300
feed/<channel>/latest-mac.json                                pointer, max-age=300 (legacy bridge)
feed/<channel>/latest-linux.yml                               pointer, max-age=300  (x64)
feed/<channel>/latest-linux-arm64.yml                         pointer, max-age=300  (arm64)
feed/<channel>/latest-cli.json                                pointer, no-cache
feed/<channel>/simple/  +  feed/<channel>/simple/kirocrew/    pointer, no-cache
cli.sh                                                        pointer, no-cache (only root object)
```

Every public URL is exactly one of two classes, and the class decides the cache
policy:

- **Immutable versioned keys** are written once with `--if-none-match '*'` and
  cached `public, max-age=31536000, immutable`. Republishing one with different
  bytes leaves stale copies on some edges while a no-cache pointer is already
  fresh, so clients hit checksum mismatches. Every lane therefore treats a 412
  (`PreconditionFailed`) as a retry: it fetches the published object through the
  CDN, compares sha256, continues when identical, and **fails** when the bytes
  differ. The version string is burned at that point; cut a new version.
- **Mutable channel pointers** are plain overwrites with a short cache. Flipping
  a feed is the go-live action.

The `feed/*` CloudFront behavior is `CACHING_DISABLED`, so the edge never caches
a feed. That is not sufficient on its own: a cache policy governs CloudFront's
own storage and does **not** emit a `Cache-Control` response header, and nothing
else in the distribution injects one. A feed served with no freshness metadata
gets *heuristically* cached by clients (roughly 10% of the object's age, so a
day-old feed earns itself hours of "fresh"). Both mac feed writes therefore
assert the served header through the public CDN with `curl -I` immediately after
the write, and fail the job if `max-age` is missing. The check goes through the
CDN rather than `s3api head-object` because the publish role is Put-only on
`feed/*`; a read-back would `AccessDenied` and abort *after* the feed was already
published, failing on permissions instead of on the condition it guards.
`max-age=300` bounds pointer staleness at five minutes; the CLI feed uses
`no-cache` instead because it is polled far less often, so revalidating always
costs nothing.

### GitHub Container Registry

`ghcr.io/<owner>/kirocrew`, resolved from `github.repository_owner` so forks
publish into their own namespace. Tag discipline mirrors the CDN keys: the
**version** tag is immutable (a re-run that finds it present skips the build,
verifies the existing digest already carries this repo's provenance via
`gh attestation verify --signer-workflow`, and does **not** move the alias), and
the **channel** alias (`nightly` / `insider` / `stable`, plus `latest` for
stable) moves only after the version tag and its attestation exist. GHCR needs
no AWS credentials: the push authenticates with the workflow's own
`GITHUB_TOKEN`, so this lane also works on forks.

The GHCR package is public, so `docker pull ghcr.io/kirodotdev/kirocrew:stable`
works with no login. That is not automatic: GHCR creates every package private
and inherits only *access permissions* from the linked repository, never
visibility — a public repo does not imply a pullable image, and the flip is
one-way (a public package cannot be made private again). Both canonical callers
pass `require_public_access: true`, which arms the logged-out-pull gate proving
anonymous consumers can resolve the image; a visibility regression fails the
lane instead of shipping an unpullable tag. The input itself still defaults to
`false` and the step is scoped to `kirodotdev`, so forks keep private packages
and authenticate with a token carrying `read:packages`.

### GitHub Releases

`release.yml`'s `github-release` job attaches the wheel, the sdist, the
AppImage, and the two gated macOS artifacts, renamed
`KiroCrew-<version>-universal-mac.zip` and `KiroCrew-<version>-universal.dmg`.
It accepts macOS bytes **only** from the exact name-bound artifact the notarize
job attached after the Gatekeeper gate, and re-validates them structurally
before publishing (ZIP CRC plus exactly one top-level `.app`; DMG `koly` UDIF
trailer). The unsigned electron-builder zip and DMG are inter-job handoffs and
never become release assets. Windows `Setup.exe` is not attached. The release is
marked `prerelease` when the channel is insider, and notes are generated.

`github-release` is the one job that needs `contents: write`, and it is the only
job that has it: the signing jobs hold AWS credentials but never
`contents: write`. `test_workflow_permissions.py` pins that split.

### There is no PyPI publish

Nothing in the repository publishes to PyPI, and `pip install kirocrew` from
PyPI is not a supported path. `publish-cli.yml` builds a **private static PEP 503
index** per channel under `feed/<channel>/simple/` and installs go through it:

```bash
pip install --pre kirocrew --extra-index-url https://updates.crew.kiro.dev/feed/insider/simple/
```

`--extra-index-url` (not `--index-url`) is deliberate: the channel index carries
only `kirocrew`, so cutting off PyPI would fail on dependency resolution. pip
verifies the `#sha256=` fragment on each link, giving the same fail-closed
integrity as the feed. Because CloudFront with OAC does not resolve directory
indexes, the workflow uploads both `.../simple/kirocrew/index.html` and the
literal trailing-slash key `.../simple/kirocrew/` that pip requests, using
`s3api put-object` (an `s3 cp` to a trailing-slash destination silently writes a
different key). The project page is merged with the live one so prior versions
stay installable, and a non-200/non-404 fetch aborts the step rather than
truncating the version history.

## The macOS trust chain

`sign-and-notarize.yml`, three jobs, called with `write_feed: true` by both
triggers. Nothing about it is caller-specific: the trigger files carry only
version derivation and `uses:` calls.

1. **sign** (ubuntu). Flattens the build artifacts, attests SLSA provenance for
   the wheel, sdist, and every Linux artifact (not the mac zip or DMG, whose bytes are not
   final yet), uploads everything to `pre-signed/`, extracts the `.app` from the
   `*-mac.zip`, and submits it to CDSigner with a manifest generated at sign
   time from the actual bundle contents by
   `packaging/signing/generate-manifest.py`. `packaging/signing/sign.sh` polls
   every 30s with a 15-minute ceiling. `awscurl` is installed **before** AWS
   credentials are configured, so a drifted release of it can never observe the
   signing credentials.
2. **notarize** (macos-15). `notarytool submit --wait`, `stapler staple`, then a
   fail-closed `spctl --assess` that must report `Notarized Developer ID`. On an
   `Invalid` verdict the itemized Apple log is printed. The branded
   electron-builder DMG is then converted to a writable layout template; its
   unsigned app is removed and replaced with the stapled app before the image
   is shrunk and recompressed. This preserves the Finder background and icon
   positions while ensuring no unsigned app survives. The resulting DMG is
   signed by a second CDSigner task with a `type: dmg` manifest, notarized,
   stapled, and held to the same `spctl` gate. The DMG signature is load-bearing
   twice over: an `hdiutil` DMG carries an adhoc signature that the Apple notary
   accepts but Gatekeeper treats as "no usable signature" ("app is damaged" on
   drag-out), and an unsigned DMG cannot be stapled at all (`stapler` Error 73),
   so first-install verification would need network. The stapled DMG is attested
   after stapling, because stapling changes the shipping bytes. The job ends by
   attaching the gated artifact, which is the sole input of everything
   downstream. The Apple credential is fetched from AWS Secrets Manager at
   runtime, masked, scoped to single steps in this job, and never written to
   `GITHUB_ENV`, a file, or a log.
3. **publish** (ubuntu). Copies the gated zip and DMG to the distribution
   bucket, writes `latest-mac.yml`, writes the legacy `latest-mac.json` bridge,
   then the human `latest/KiroCrew.dmg` alias. Separate from notarize so a
   transient S3 failure retries as a two-minute ubuntu job instead of repeating
   two Apple submissions, and so the expensive macOS runner never burns minutes
   on uploads. Its `if:` starts with `success()`, which is required: a custom
   job-level `if` replaces the implicit success check, and without it the job
   would run after a failed or skipped notarize.

Linux publishing is deliberately not in this workflow: the AppImage takes no
part in the macOS trust chain, so `publish-linux.yml` is its own lane. Linux has
no Gatekeeper equivalent and the AppImage is not code-signed by design; it ships
with its own in-lane SLSA provenance, attested before anything reaches S3, so a
CDN-served AppImage always carries verifiable provenance even when the macOS
workflow fails or is cancelled.

### Go-live ordering

Within every lane the order is fixed: versioned immutable bytes first, then the
feed, then the convenience `latest/` alias. A feed written before its artifacts
would hand clients a 403; an alias written before the feed would point ahead of
the go-live switch. Before writing a feed, the mac and Linux lanes re-fetch the
just-published object **through the public CDN** and compare its sha512 against
the digest the feed is about to advertise, failing closed on a mismatch. That is
what makes the tolerated 412 safe: a same-version re-run whose artifact differs
byte-for-byte would otherwise leave the old object published while the feed
described the new one, and every client would refuse to install.

Ordering **across** runs is protected only by the trigger workflows'
`concurrency` groups (nightly cancels an in-flight older run, release queues),
not by a version comparison at write time, which would itself be a
read-then-write race.

## Version stamping

The in-code `__version__` in `src/kiro_crew/__init__.py` is the source of truth
for non-tag builds. A tagged release overrides all three manifests at build
time. See CONTRIBUTING.md → "Bumping the in-code version" for the three files
and why the base must stay a bare `X.Y.Z`.

| Channel | Desktop / semver stamp | CLI wheel (PEP 440) |
|---------|------------------------|---------------------|
| nightly | `0.2.0-nightly.20260708t061155` | `0.2.0.dev20260708061155` |
| insider | `0.2.0-rc.1` | `0.2.0rc1` |
| stable | retains the promoted candidate's stamp (`0.2.0-rc.N`) | retains `0.2.0rcN` |

A bare stable tag does not stamp or build: it verifies the selected candidate's
recorded manifest and reuses its embedded version and byte digests unchanged,
because re-stamping would change (and invalidate) the tested, signed bytes. The
bare `X.Y.Z` names the git tag, the GitHub Release, and the stable channel
paths — the release identity — while the artifacts keep the candidate's
embedded prerelease version.

Two stamps exist because the consumers disagree: Squirrel and electron-builder
need semver, the wheel needs PEP 440. `nightly.yml` reads the clock **once** and
slices it, because three separate `date -u` calls can straddle UTC midnight and
pair an old-day date with a new-day time, which would move the version backward.

The nightly semver shape is not cosmetic. Date and time are **one alphanumeric
identifier separated by `t`** (`<YYYYMMDD>t<HHMMSS>`), never a bare 14-digit run
and never two dot-separated numeric identifiers. Two independent constraints
force it, both proven live on `windows-latest`:

- Squirrel.Windows derives each release entry's version from the nupkg
  *filename* and Int32-parses digit runs when sorting, so a run above
  2147483647 makes `Update.com --releasify` die with an overflow. The bound is
  magnitude, not digit count. Dot-splitting does not help: electron-builder
  concatenates the identifiers back into the filename. A letter between the
  digit runs is what survives that concatenation.
- SemVer forbids leading zeros in a purely numeric prerelease identifier, and
  the 06:00 cron yields `HHMMSS=060000`. Inside an alphanumeric identifier the
  leading zero is legal.

Ordering still works: the identifier is fixed-width and semver compares
alphanumeric identifiers lexically, which for a zero-padded `YYYYMMDDtHHMMSS` is
chronological. The `-nightly.` prefix is load-bearing (`auto-update.js`
`channelForVersion`, the instance guard's `identityFamily`, and
`packaging/build-desktop.sh`'s `*-nightly.*` glob all match on it).
`test/test_nightly_version_contract.py` pins every property above.

Seconds precision exists so no published key is ever overwritten: a date-only
stamp let two nightlies on one UTC date collide on the same
`signed/`, `notarized/`, and `cli/` keys.

**One collision trap:** any two prerelease tags sharing a base and a trailing
number collapse onto the same PEP 440 wheel version, because `release.yml` maps
by trailing number alone. `v0.2.0-rc.1` and `v0.2.0-insider.1` both map to
`0.2.0rc1`, and the second publish fails as a republish of an immutable key.
Stick to one convention (`-rc.N`) per base version.

### Version numbering policy

`__version__` in `src/kiro_crew/__init__.py` is the branch's DECLARED identity.
A tagged build overrides all three manifests from the tag (the table above), so
the in-code value is what a non-tag build reports and what the promote sequence
manipulates — the final byte stamp is decided by the tag, not this value.

- **On an insider release branch, `__version__` carries the RC suffix, and the
  tag matches.** The branch reads as what it is: `__version__ = "X.Y.Z-rc.N"`,
  tags `vX.Y.Z-insider.N`. Do not leave a release branch declaring a bare
  `X.Y.Z` while it is still cutting RCs. All three version files
  (`src/kiro_crew/__init__.py`, `pyproject.toml`,
  `website/electron/package.json`) use the **same dual-valid spelling**
  `X.Y.Z-rc.N` — valid SemVer and valid (non-canonical) PEP 440. The canonical
  PEP 440 form (`0.4.0rc4`) is forbidden in `__init__.py`:
  `packaging/build-desktop.sh` feeds `__version__` verbatim to
  electron-builder, which requires SemVer.
- **Promoting an insider line to stable is a three-step sequence:**
  1. **Drop the RC in a PR** — change `__version__` from `X.Y.ZrcN` to the bare
     `X.Y.Z`. This is the release commit; it also sets the base the stable
     display folds to (`_display_version`, see "Client auto-update").
  2. **Cut one more RC tag** (`vX.Y.Z-insider.<N+1>`) on that commit and let it
     soak. This bare-`__version__` commit is the promotion candidate.
  3. **Tag the bare `vX.Y.Z`** on the same commit to promote — promotion
     republishes the soaked candidate's exact bytes (see "Stable promotion").
- **`main` (nightly) is always one MINOR ahead of the active insider line.**
  While `release/0.4` stabilizes on insider at `0.4.x`, `main`'s `__version__`
  is already `0.5.0`. The release branch owns the version being shipped; `main`
  owns the next one. This keeps every nightly strictly newer than any RC of the
  shipping line, so a nightly user is never offered what looks like a downgrade
  to an RC.

**Why the display still folds even after step 1.** The build stamps the version
FROM THE TAG, and the desktop's embedded version MUST equal the feed version or
the auto-updater's compare gate breaks (see "Client auto-update"). So the
promotion candidate's *bytes* still carry the RC/insider stamp (`0.4.0rcN` /
`0.4.0-insider.N`) even though the branch declares a bare `__version__`. The
bare declaration sets the source-of-truth and the fold's base; `_display_version`
is what actually shows a stable user `0.4.0`. The two are complementary, not
alternatives.

## CLI channel and the signed manifest

The wheel is a first-class channel target, not a byproduct: a Linux or EC2 host
tracks nightly, insider, or stable and installs from the same feed shape the
desktop uses. `publish-cli.yml` depends only on the built wheel and its own
KMS key, never on Apple or CDSigner, so a macOS signing failure cannot block a
CLI release. The same independence holds for `publish-linux.yml` (needs only
`build-desktop`) and `publish-docker.yml` (needs only the wheel).

`SHA256SUMS` sits beside the wheel for legacy tooling, but it is only a
corruption check. Authenticity comes from a canonical JSON artifact manifest
signed with a non-exportable RSA KMS key:

```json
{
  "algorithm": "RSASSA_PKCS1_V1_5_SHA_256",
  "channel": "insider",
  "key_id": "sha256:<SubjectPublicKeyInfo DER digest>",
  "pub_date": "2026-07-18T06:15:00Z",
  "python_requires": ">=3.10",
  "schema": "kirocrew-cli-artifact-manifest-v1",
  "sha256": "<wheel digest>",
  "signature": "<base64 RSA signature over canonical JSON without this field>",
  "version": "0.2.0",
  "wheel_url": "https://download.crew.kiro.dev/cli/insider/0.2.0/kirocrew-0.2.0-py3-none-any.whl"
}
```

The signature covers sorted compact UTF-8 JSON of every field except
`signature`. The four signature fields are additive to the six legacy ones, so
older installers stay parse-compatible while a strict installer authenticates
the same object.

`cli.sh` embeds the public key and its expected `key_id` (the SHA-256 of the
PEM's DER encoding). Before any network I/O it requires OpenSSL, refuses an
unconfigured pin, materializes the key, and checks that fingerprint. It then
reconstructs the canonical bytes and verifies the signature, applies bounded
sizes plus duplicate-key and exact-field-set rejection, and validates the
authenticated channel, version, digest and canonical wheel URL against what was
requested, so even a valid signer cannot redirect the installer to another
origin. Only then does it fetch wheel bytes, and it re-checks them against the
signed digest. Missing key, missing signature, malformed or duplicate fields,
wrong key id, bad signature, redirect metadata, and digest mismatch all refuse
installation. There is no unsigned fallback.

Publication is configured as one unit: `publish-cli.yml` fails **before any
upload** when exactly one of `secrets.AWS_SIGNING_ROLE_ARN` and
`vars.CLI_MANIFEST_SIGNING_KEY_ARN` is set, rather than publishing an unsigned
manifest, and skips entirely when neither is (fork or feature branch). It also
refuses when `CLI_DIST_BUCKET` or `CLI_CDN_BASE` is unset, because the origin
bucket is private and a manifest must never advertise a raw S3 URL.
`pub_date` is derived from the source commit's timestamp rather than wall clock,
so a retried job produces a byte-identical manifest and the immutable write
still succeeds.

Key provisioning, the `kms:GetPublicKey` + `kms:Sign` grant, and the rotation
procedure (dual-trust, never an in-place swap, because schema v1 pins exactly
one key) are in [../../packaging/signing/README.md](../../packaging/signing/README.md).

`publish-installer.yml` mechanically enforces the rollout order rather than
trusting it. It publishes only from `main` (checked explicitly, because
`workflow_dispatch` lets a maintainer pick any ref and `environment: prod` does
not constrain that), checks out `main`'s tip rather than the event SHA (one live
copy, whose only correct content is latest reviewed `main`), fails loudly on
missing configuration instead of skipping green, refuses to publish an installer
that still pins `CLI_MANIFEST_KEY_ID="UNCONFIGURED"` or whose pinned id does not
match its embedded key, and refuses unless **every live channel feed** verifies
against that pinned key using the same `cli-manifest.py verify` checks the
installer runs. A channel serving no feed at all is skipped with a warning,
since publishing is not a regression for it.

### Breaking releases: the forced-update floor

A release that older clients must not keep running against (a feed-schema
break, a protocol break, a data migration without back-compat) declares a
**minimum supported version** in [`packaging/MIN_VERSION`](../../packaging/MIN_VERSION):
one bare release version on its own line (comments and blank lines are
ignored; more than one value line fails the publish). Every CLI feed manifest
published from a commit carrying that value embeds it as the optional signed
`min_version` field.

What each consumer does with it:

- **Running gateways** compare the floor against their own version on the
  normal feed check — after verifying the manifest's signature against the
  same pinned key the installer uses (`platform/feed_trust.py`), because the
  floor coerces the UI and a tampered feed must not be able to hold every
  dashboard hostage. Versions are folded per channel first (a promoted
  stable build's `0.3.0rc13` stamp IS the `0.3.0` release). Below the floor,
  `update_required` turns true on the status frame and
  `GET /api/update/check`, and the dashboard's proactive update modal drops
  its snooze/skip/Escape affordances — the prompt stays up until the install
  is updated. The gateway itself keeps running, and every verification or
  parse failure degrades to the ordinary dismissible prompt: the floor fails
  toward freedom, never toward coercion.
- **The installer** verifies the field's format and otherwise ignores it — it
  always installs the signed version, which is exactly how a floored install
  gets satisfied.
- **The enterprise governance pin** (`updates.min_version` in
  `security_policy.json`) is independent and OR'd with the feed floor;
  either alone makes the update mandatory.

Rules for setting the floor:

- Set it to the first version old clients can safely land on — usually the
  breaking release itself.
- Never set it in the same release that introduces floor support: clients
  only learn to read the field after updating once, so a floor only moves
  clients that already run a floor-aware build.
- Clear or lower it only to roll back a mistake; installs above the floor are
  never affected. `cli-manifest.py` refuses a floor above the manifest's own
  version and any non-bare-release value at publish time.

### Installing and switching channels

```bash
# install, or move to another channel
curl -fsSL https://download.crew.kiro.dev/cli.sh | sh -s -- --channel {nightly|insider|stable}
```

The installer resolves the channel feed, verifies it as described above,
installs with `pipx` when available (otherwise a managed venv beside the data
home), and records the channel in `~/.kiro/crew/channel`. Default channel is
`stable`; `KIROCREW_CHANNEL` overrides it, and `--version` pins an exact wheel
through the immutable `cli/<channel>/<version>/cli-manifest.json` instead of the
mutable feed. This download path is separate from the source install
(`install.sh`, a git clone plus `pip install -e .`), which is what `kirocrew
update` refreshes: that command needs a git checkout at
`KIROCREW_PROJECT_DIR` and runs `git fetch` plus `git reset --hard` (checking a
governance source pin on the remote URL first, so the fleet, not the human at the
terminal, decides which remote a host may take code from), then rebuilds the
frontend, reinstalls with `pip install -e .`, and re-runs
`setup --agent-only`. The dashboard's `POST /api/update` performs the equivalent
and restarts the gateway; neither path consumes the channel feed.

## Client auto-update

The desktop updater is `electron-updater` in `website/electron/auto-update.js`.
It runs in packaged macOS, Linux and Windows builds: `SUPPORTED_PLATFORMS` is
exactly `{darwin, linux, win32}`.
On macOS electron-updater's `MacUpdater` downloads the archive itself and serves
it to Electron's built-in `autoUpdater` (Squirrel.Mac) over a loopback proxy, so
the atomic bundle swap is unchanged and `NSURLCache` is no longer in the feed
path. On Linux the install shape decides: an AppImage is replaced in place (so
the directory holding it must be writable, which `bundle-location.js`'s
`canUpdateLinuxInstall` gates on), while a deb or rpm is handed to
`dpkg`/`rpm` behind an elevation prompt by electron-updater's `DebUpdater` /
`RpmUpdater`. `resolveLinuxInstall()` picks the shape from three positive
signals — `resources/package-type`, `$APPIMAGE`, and an `/opt` install path —
and a package whose FORMAT cannot be named is refused rather than pointed at
another format's feed. On Windows `NsisUpdater` reads
`latest.yml` and runs the NSIS installer, verifying the download's Authenticode
signature **fail-closed** against the `publisherName` pinned in
`website/electron/package.json`. That verification is why `publish-windows.yml`
refuses to publish an installer whose signature or signer does not check out: a
bad publish would not degrade updates, it would fail every client's update at
once.

Two Windows details do not generalise from the other platforms:

- **Windows has exactly one channel file, whatever the arch.**
  `Provider.getChannelFilePrefix()` appends an arch suffix for linux alone and
  returns `""` for win32, so `NsisUpdater` always requests `latest.yml`. A second
  Windows arch is a second entry inside that one file, never a second feed, and
  it also has to contend with `Provider.findFile()` disambiguating entries by
  matching `process.arch` against the URL path.
- **Windows updates are visible but non-interactive.** `quitAndInstall` passes
  `isSilent=false` and `isForceRunAfter=true`. The assisted installer
  (`nsis.oneClick: false`) uses update-only hooks in `installer.nsh` to skip the
  Welcome, install-mode, and Finish decisions, leaving only the native
  extraction page and its real progress visible. At completion it runs the
  locked electron-builder `StartApp` contract and exits successfully. The same
  hooks call `SetSilent normal` for `/S --updated`, so a client released before
  this behavior change also gets visible progress on its first upgrade into it.
  The downloaded installer owns this UI contract: a downgrade or channel
  switch-back to an installer that predates these hooks shows that release's
  legacy assisted wizard instead, so operators and users must retain the
  install scope detected by that wizard.

`SUPPORTED_PLATFORMS` is necessary but not sufficient: a channel can lack a
desktop publish lane entirely, which is what `KNOWN_CHANNELS` and
`channelHasLane()` record. There is deliberately no Windows-specific channel set:
every channel in `KNOWN_CHANNELS` publishes Windows, so a separate set would be a
declaration claiming a restriction that does not exist. A channel with no lane
reports `disabled: "channel"` rather than arming an updater that can only 404.
`test_the_updater_offers_exactly_the_channels_that_publish_windows` in
`test_windows_signing_contract.py` pins `KNOWN_CHANNELS` to the callers that
actually invoke the lane, in both directions, so the two cannot drift -- and if a
channel ever loses its Windows lane, that test fails until the restriction is
reintroduced.

The client resolves `{feedBase}/{channel}/` as a **directory** (the trailing
slash matters: without it `new URL("latest-mac.yml", base)` replaces the last
segment and resolves the wrong channel) and the library appends the platform
filename. The feed base defaults to `https://updates.crew.kiro.dev/feed` and is
overridable through `KIROCREW_UPDATE_FEED`, which enforces HTTPS except on
loopback so the local harness works. The yml lives on the pointer host while
`files[].url` entries are absolute byte-host URLs; electron-updater's
`newUrlFromBase` ignores the base for absolute URLs, which is what preserves the
split. First check runs 30s after launch, then every 4 hours.

Feed shape (electron-updater channel metadata, exactly what electron-builder
generates). `sha512` is **base64 of the raw digest**, never hex, because
electron-updater string-compares it and a hex value fails every download:

```yaml
version: 0.1.0-nightly.20260721t061155
files:
  - url: https://download.crew.kiro.dev/desktop/nightly/0.1.0-nightly.20260721t061155/KiroCrew.zip
    sha512: '<base64>'
    size: 123456789
  - url: https://download.crew.kiro.dev/desktop/nightly/0.1.0-nightly.20260721t061155/KiroCrew.dmg
    sha512: '<base64>'
    size: 234567890
path: https://download.crew.kiro.dev/desktop/nightly/0.1.0-nightly.20260721t061155/KiroCrew.zip
sha512: '<base64>'
releaseDate: '2026-07-21T06:22:13Z'
```

The zip is the update payload (the updater's `findFile` skips dmg/pkg); the DMG
entry is listed for tooling parity and stays the human first-install download,
which is why it also gets its own `desktop/<channel>/latest/KiroCrew.dmg`
permalink. Downloads are verified fail-closed against the feed's `sha512` before
install, and on macOS Squirrel.Mac additionally validates the swapped bundle's
code signature, which is precisely why the feed may only ever point at signed
artifacts.

`feed/<channel>/latest-mac.json` is a transition bridge for installs fielded
before the electron-updater migration, which poll that flat JSON and know
nothing about the yml. It advertises the same version and the same bytes, so an
old install updates once and never reads it again. Deleting it would strand
those installs permanently with a manual DMG re-download as the only escape.
`test_publish_feed_contract.py` pins it so it cannot be dropped silently; it is
safe to remove once no pre-migration installs remain.

Four updater policy flags each differ from the library default on purpose:
`autoDownload=false` (the library must never fetch from inside
`checkForUpdates`; whether a discovered update downloads without a click is a
separate preference read per discovery, and keeping the flag false is what
routes the automatic and the consented download through one guarded function),
`autoInstallOnAppQuit=false` (the default would swap the bundle on quit without
stopping the embedded Python gateway), `allowDowngrade=true` (the gate is
difference-based, so a feed pointed at an older version is offered, which is
what makes a channel switch-back work), and `allowPrerelease=true` (every
nightly and insider stamp is a semver prerelease and would otherwise be
invisible to its own channel). The library still refuses an equal version before
the `allowDowngrade` branch, which is what prevents a self-reinstall loop.

**Desktop updates download automatically by default, and install on the next
quit.** The `autoDownloadUpdates` preference (electron-store, default `true`,
opt out in Settings → About) decides whether the `update-available` handler
calls `startDownload()` itself. The INSTALL is not made automatic by this: the
existing `update-downloaded` handler arms a `before-quit` install that stops the
gateway first, so a downloaded update lands on the user's own next quit rather
than interrupting a live session. `autoInstallOnAppQuit` stays false on every
platform — on macOS that flag stages eagerly, which arms ShipIt to swap the
bundle on ANY exit (including exits that skip the gateway teardown) and cannot
be un-armed, so it would also defeat release retraction.

Turning the preference off keeps bytes already fetched but **disarms the
install-on-quit for a stage that was downloaded automatically**, so the update a
user just declined does not land on their next quit; a stage they explicitly
downloaded stays armed, because the preference is not what put it there. The
stage itself is never discarded, so an explicit Install still applies it with
nothing to re-download.

**Which channel a build follows is a default plus an opt-in, not a property of
the bytes.** `channelForVersion()` classifies the version stamp and `nightly`
stays pinned by it, but for the two production lanes `resolveChannel()` honours
the persisted Settings → About preference and defaults to **stable** when none is
set. It cannot read the lane out of the stamp, and a rebuilt stable does not
change that. For every install shipped while stable was PROMOTED, the stable and
insider downloads of a release were the same notarized file carrying the same
`-insider.N` stamp, so a stamp-derived channel would send all of those installs
to the insider feed. Those binaries are still in the field, and `resolveChannel()`
has to keep answering correctly for them. A stable build produced by a rebuild
does carry its own bare stamp, but reading the lane from it would only be safe
once no promoted install remains — which is not a condition this code can check.
The consequences to know:

- **Insider is an explicit opt-in.** Any install with no recorded preference
  follows stable — including one installed from the insider DMG, and including an
  insider install that predates this rule. The two downloads are identical files,
  so nothing in them can record which page one came from, and nothing already on
  disk distinguishes an insider install from a stable one that has been offered
  the promoted build. Insider is reached by the switcher, once, per install.
- **There is no way to seed that preference retroactively.** A migration would
  have to read the channel from the version stamp, and the first build carrying
  any such migration is itself promotion-stamped, so it would write `insider` for
  every stable install — the defect this rule exists to remove, made permanent.
  A future transition could use a persisted last-run version; this one cannot.
- **The "you are running prerelease bytes" note still keys on the stamp**
  (`stampedChannel`), not on the followed channel, because that statement is
  about the bytes and stays literally true on a promoted stable install.

The specific to Kiro Crew part is install ordering: the app supervises a bundled
Python gateway child, so before `quitAndInstall` the client stops it gracefully
(`POST /api/shutdown`, then SIGTERM, then SIGKILL) and disarms the liveness
watchdog that would otherwise resurrect it mid-swap. Choosing "Later" defers to
natural quit through a `before-quit` hook in the same stop-gateway-first order.

## Windows

`build-windows.yml` builds and **Authenticode-signs** the NSIS `Setup.exe`
through AWS Signer during the build (signing profile `KiroCrewWindowsExe`),
whenever `AWS_WINDOWS_SIGNING_ROLE_ARN` is present and the caller passed
`use_prod_environment: true`. Signing happens inside the build because the NSIS
installer compresses its own already-signed executables.

`publish-windows.yml` then publishes that installer on **every desktop channel --
nightly, insider and stable**, following the same contract as `publish-linux.yml`:
an immutable versioned key, then the feed, then the mutable `latest/` alias.
Nightly and insider publish a fresh signed build; stable republishes the verified
promotion bundle's installer (see the stable note below).

    desktop/<channel>/<version>/KiroCrew-Setup.exe            immutable
    desktop/<channel>/<version>/KiroCrew-Setup.exe.blockmap   immutable
    desktop/<channel>/latest/KiroCrew-Setup.exe               pointer, max-age=300
    feed/<channel>/latest.yml                                 pointer, max-age=300

Three things about this lane are deliberate rather than incidental:

- **It verifies before it publishes.** `scripts/verify_windows_installer.py`
  refuses an installer whose certificate table is empty, whose SIGNER
  certificate is not the pinned publisher, or which carries no RFC3161
  timestamp. It matches the signer alone because that is what the client checks,
  so a build whose leaf is wrong but whose issuer happens to carry our name
  cannot pass here and then be refused by every client. `build-windows.yml`
  skips signing cleanly when its secret is absent, so "a working but unsigned
  installer" is a state that actually occurs, and `NsisUpdater` verifies
  fail-closed. Publishing one would break every client's update simultaneously.
  The guard checks signature metadata, not the Authenticode digest: byte
  identity from the build artifact to the CDN is already established by the
  write-once versioned key and the feed step's read-back comparison.
- **There is deliberately no architecture check**, and the analogy to
  `publish-linux.yml`'s ELF-machine check does not transfer. An AppImage IS its
  payload, so its ELF header describes what the user runs. An NSIS installer is
  a stub that unpacks a payload, and NSIS ships only a 32-bit stub: the signed
  x64 nightly reports COFF machine `0x014c` with a PE32 optional header. So the
  header says nothing about the packaged architecture, and asserting `0x8664`
  rejects every genuine installer. Architecture is bound by artifact identity
  instead: the lane accepts `x64` alone and consumes the artifact the x64 build
  job uploaded, by name.
- **It does not trust `build-windows`'s job result.** That caller runs with
  `soft_fail`, so its result is `success` even when the build failed and uploaded
  nothing. The lane probes for the artifact and skips cleanly when it is absent,
  which keeps a Windows-only failure from blocking the mac and Linux lanes. The
  probe checks the listing's own exit status separately from the match, so an API
  error is never laundered into "nothing to publish". It retries the listing
  before giving up, because failing closed here also fails the run, and
  `release_promotion.py` then refuses to promote that commit at all -- a single
  API blip should not cost stable promotion of the mac, Linux and CLI artifacts
  the same run already published, while a sustained failure still must. Asking the Actions API what this run uploaded needs `actions: read`,
  which the reusable workflow and both caller jobs grant; without it the probe
  403s and, because it fails closed, the lane aborts rather than publishing.
- **Stable publishes by promotion, and the Windows role is optional.** Stable
  does not rebuild: it republishes the bundle `scripts/release_promotion.py`
  verified byte for byte. Windows contributes two roles to that bundle,
  `windows_installer` and `windows_blockmap`, and both are **optional** rather
  than required. Optional is the whole point: a required role would make a stable
  release depend on a successful Windows build, which is the coupling `soft_fail`
  exists to prevent, so a candidate recorded from a run whose Windows build failed
  simply carries no installer and the other platforms still ship. The two travel
  as a pair, because an installer promoted without its blockmap still updates and
  merely turns every client's update into a full download instead of a
  differential one -- a silent degradation, which is exactly the kind that has to
  be made impossible rather than documented.

  In promote mode the lane verifies the whole bundle before it looks at the
  installer, and re-verifies the attestation this same workflow produced at
  insider time instead of minting a second one (a fresh attestation over
  republished bytes would testify only that stable's own run held the file, which
  is equally true of tampered bytes).

  `record-promotion` therefore **waits on** `build-windows` without **requiring**
  it. Waiting is mandatory: assembling before the installer artifact exists would
  silently record a Windows-less candidate from a build that actually succeeded.
  Requiring success is forbidden: it recreates the coupling, and `soft_fail`
  forces that job's result to `success` even on failure, so the check would assert
  nothing.

The `KiroCrew-Setup.exe` basename is a public contract: it is what
`manualDownloadUrl()` hands a user whose in-app update failed, which is why it
carries neither the version nor electron-builder's spaces. The blockmap must
travel with the installer or electron-updater silently falls back to a full
download for every update.

Linux arm64 is no longer open: `build-desktop.yml` builds it on `ubuntu-22.04-arm`
and `release.yml`/`nightly.yml` each call `publish-linux.yml` twice, once per arch.
The arches are separate JOBS rather than a matrix so a failure on one cannot
cancel or skip the other. A new platform lane
needs: a matrix entry with a stable `{os}-{arch}` id; two artifact roles (a
first-install installer and an update archive the platform updater consumes,
both from the standard desktop packaging path); artifacts carrying the stamped
version; staging only to `pre-signed/` and only through the publish role;
the platform's native signing verified fail-closed before any artifact becomes
client-visible; a `feed/<channel>/latest-<platform>.yml` in the
electron-updater shape with absolute byte-host URLs; a client updater that
honors the gateway stop, the "Later" deferral, and platform-native signature
validation of the download; the platform added to `SUPPORTED_PLATFORMS`; a
working roll-forward path (a lane whose updater cannot pick up a newer version
has no recovery story); and channel-appropriate retention so nightlies do not
accumulate unbounded.

## Identity and trust boundaries

CI holds no static cloud credentials. Every AWS interaction is short-lived OIDC.

The publish role's OIDC trust accepts exactly two subjects: `ref:refs/heads/main`
and `environment:prod`. Release runs are tag-triggered (`ref:refs/tags/v*`),
which is **not** trusted, so every publishing job declares `environment: prod`,
which switches the caller's subject. This is not optional plumbing: it is why
`publish-cli.yml`, `publish-linux.yml`, `publish-installer.yml`, and all three
jobs in `sign-and-notarize.yml` name the environment, and why `build-windows.yml`
takes `use_prod_environment` as an input rather than deriving it (inside a called
reusable workflow the `github` context reports the *caller's* trigger and never
`workflow_call`, so an `event_name` test would leave the environment unset on
exactly the paths that need it).

CI cannot write `signed/*`. Only the CDSigner service principal's role can,
which is what makes "signed artifacts originate from the signer" structural
rather than procedural.

`publish-docker.yml` takes no `secrets: inherit`. It authenticates with
`GITHUB_TOKEN` alone, and inheriting would expose every signing and CDN secret
to a lane documented as needing none. `packages: write` is scoped to that one
job.

Full account, role, endpoint, and credential-rotation detail is in
[signing-runbook.md](signing-runbook.md).

## Verifying a release

Each lane self-verifies through the public CDN before it reports success, which
is the check that matters: "uploaded" is not "live and correct".

- Immutable keys: sha256 compare on a 412, so a re-run can never diverge from
  what is published.
- Feeds: sha512 of the CDN-served artifact must equal the digest the feed is
  about to advertise, and the served `Cache-Control` must carry `max-age`.
- `cli.sh`: sha256 of the CDN-served script must equal the published bytes, with
  retries for edge revalidation, and the header must be `no-cache`. The script is
  also `sh -n` parsed and must reject an unknown channel before reaching the CDN.
- Docker: a version tag that already exists must carry provenance attested by
  this repository's `publish-docker` workflow, verified with
  `--signer-workflow`, before the run treats it as a valid prior publish.

Manual spot-check of a channel after a release:

```bash
CH=stable
BYTES=https://download.crew.kiro.dev
PTR=https://updates.crew.kiro.dev

curl -fsSI "$BYTES/desktop/$CH/latest/KiroCrew.dmg" | head -1
curl -fsSI "$BYTES/desktop/$CH/latest/KiroCrew-x86_64.AppImage" | head -1
curl -fsSI "$BYTES/desktop/$CH/latest/KiroCrew-aarch64.AppImage" | head -1
curl -fsS  "$PTR/feed/$CH/latest-mac.yml"
curl -fsS  "$PTR/feed/$CH/latest-linux.yml"
curl -fsS  "$PTR/feed/$CH/latest-linux-arm64.yml"
curl -fsS  "$PTR/feed/$CH/latest-cli.json" > /tmp/feed.json
curl -fsS  "$PTR/feed/$CH/simple/kirocrew/" | head -5

# authenticate the CLI feed with the same checks cli.sh runs
python3 packaging/signing/cli-manifest.py verify \
  --manifest /tmp/feed.json \
  --public-key packaging/signing/cli-manifest-public.pem \
  --expected-channel "$CH" \
  --artifact-base "$BYTES"
```

For the desktop swap itself, `ota-test.yml` is the end-to-end proof; run it on
demand after a change to the updater. It validates the swap mechanism, not
Gatekeeper acceptance, since it signs with a throwaway identity.

## Recovery: roll forward

**There is no rollback.** The recovery path for a bad release is to cut a new
version from the release branch and let the channel feed advance to it. Published
CDN keys are immutable and are never overwritten, so there is nothing to revert
in place, and every lane refuses a same-version republish with different bytes.

The client capability to *accept* an older version exists (`allowDowngrade=true`,
so a feed repointed backward would be offered), but repointing is not the
operational answer: it fights the immutable-key discipline and the concurrency
groups that exist to stop a channel rolling backward.

Practical consequences when something goes wrong mid-release:

- A failed publish step re-runs safely. Immutable writes are idempotent on
  identical bytes and abort on different bytes; the mac `publish` job is a
  cheap ubuntu retry that does not repeat Apple submissions.
- A re-run of an **older** release never moves a channel forward or backward: the
  Docker lane refuses to move the alias, and the S3 lanes refuse a divergent
  republish.
- A Docker run that died between its version-tag push and its attestation leaves
  an unattested digest that later runs will refuse. Delete that version tag in
  the GHCR package settings and re-run to rebuild and attest cleanly.
- A stable Docker run that died between its `stable` and `latest` writes is
  repaired automatically, but only when `stable` already resolves to this run's
  digest, so the repair can only converge `latest` toward `stable`.

## Changelog

Every release lands a `## [X.Y.Z] - YYYY-MM-DD` section in `CHANGELOG.md`
through a normal PR, alongside any version bump. The section format (ordering,
tone, the three-sentence budget per subsection) is specified once in
[AGENTS.md](../../AGENTS.md) → "Release Changelog". The dashboard reads the
changelog from `KIROCREW_PROJECT_DIR/CHANGELOG.md` for source installs and from
the bundled copy inside the package for wheel installs.

**`main` holds the canonical copy.** A release branch necessarily carries its
own `CHANGELOG.md`, so two copies exist while a release is in flight, and that
divergence is what the 0.3.0 incident grew out of: the release branch's copy was
rewritten in isolation and lost three shipped sections that `main` still had.
The rules that keep the two from drifting:

- **Write the section on the release branch first** (it is what ships), then port
  it to `main` **verbatim** — a port adds a section and removes nothing.
- **`main` is the recovery source.** If a release branch's changelog is damaged,
  restore from `main` rather than reconstructing by hand; `main` is never rewound
  by a release cut, so its copy is the one that still has the full history.
- **Write it from the commit range, under the release's final heading.** There is no
  `## [Unreleased]` section to accumulate into and no in-progress prerelease heading
  to rename later — `scripts/check_changelog_history.py` refuses both, at head, with
  or without a base ref. So the section is composed once, from
  `git log --oneline <last-tag>..HEAD`, and every commit in that range is accounted
  for rather than sampled. 0.4.0 is the cautionary case: its per-PR accumulation
  reached 721 lines while describing about 11% of the 453 commits it covered, and it
  named none of the eighteen breaking changes it shipped.
- **Editing the in-flight section after it is written needs the documented human
  override**, because the immutability rule cannot tell a not-yet-shipped section
  from a shipped one without a tag lookup, and a shallow CI checkout has no tags.
  This is the intended trade: a fix cherry-picked into a later RC that deserves a
  changelog line is rare, and the alternative — an exemption keyed on position in
  the file — once made the most recently shipped section the only editable one.

## Deliberately not built

These names appear in older design material and in code comments that point
here. None of them exists, and the omissions are decisions, not gaps.

| Not built | Why |
|---|---|
| `beta-cut.yml`, `beta-hotfix.yml`, `promote-stable.yml` | Cutting a branch, numbering RCs, and promoting are human steps. The pipeline reacts only to a pushed tag. |
| `rollback.yml`, `blocked-versions.json` | There is no rollback. Recovery is a new version. |
| A feed Lambda writing the channel pointer on an S3 PUT event | A PUT event cannot express "and signature verification passed". CI writes the feed synchronously, after the Gatekeeper gate. No Lambda is deployed. |
| `latest-mac.json` as the *primary* feed, with CloudFront Function query routing (`?channel=X&platform=Y`) | Static electron-updater channel files fetched directly, with client-side version compare. The `latest-mac.json` that exists is a legacy bridge, not a routing scheme. |
| A `beta` channel or path segment | The channel is `insider` everywhere, including the storage prefix. `cli.sh` must never remap it: a remapped prefix was never published and surfaces as an opaque CDN 403. |
| A forced minimum version floor | Not built. A feed-served floor that force-triggers the update flow for a critical patch remains open. |
| A fixed promote cadence | Insider bakes until judged stable. There is no calendar commitment. |
