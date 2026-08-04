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
| `stable` | `release.yml`: push of a bare semver tag (`v0.2.0`) | `<x.y.z>` |

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

## Workflows in the release path

Every one of these exists on `main`. Two trigger workflows call the same set of
reusable ones, which hold all of the build, sign, and publish logic so the
recipe exists exactly once; the triggers carry only their trigger, their
concurrency group, and their version derivation.

| Workflow | Kind | Role |
|---|---|---|
| `nightly.yml` | trigger (schedule + dispatch) | Derives the date stamp, then calls everything below. `concurrency: nightly-build` with `cancel-in-progress: true`. |
| `release.yml` | trigger (`push` on `v*` tags) | Derives version + channel + wheel version from the tag, calls everything below, then creates the GitHub Release. `concurrency: release-publish` with `cancel-in-progress: false` (queued). |
| `dependency-vulnerability.yml` | reusable gate | `scripts/check_npm_audit.py`. Runs first; every build job needs it. |
| `build-wheel.yml` | reusable build | Stamps the PEP 440 version into `pyproject.toml` and `__init__.py`, stamps the distribution channel, builds the frontend and stages it into the package, then `python -m build`. Uploads artifact `cli-wheel` (wheel + sdist). Credential-free. |
| `build-desktop.yml` | reusable build | Matrix `macos-15` (universal macOS app) and `ubuntu-22.04` (AppImage) via `packaging/build-desktop.sh`. Deliberately credential-free (`contents: read` only, pinned by `test_workflow_permissions.py`), so it builds **unsigned** and hands the `.app` downstream. |
| `build-windows.yml` | reusable build | `windows-latest`, an NSIS `Setup.exe`. Separate from `build-desktop.yml` because Authenticode signing has to happen *inside* the build (the installer compresses its own already-signed executable), so this job holds an AWS Signer identity and `build-desktop.yml` can stay credential-free. Callers pass `soft_fail: true`, so a Windows failure cannot skip the mac/Linux lanes. |
| `publish-cli.yml` | reusable publish | Wheel + `SHA256SUMS` + KMS-signed `cli-manifest.json` to `cli/<channel>/<version>/`, the same signed manifest to `feed/<channel>/latest-cli.json`, and a PEP 503 index under `feed/<channel>/simple/`. |
| `publish-linux.yml` | reusable publish | AppImage to `desktop/<channel>/<version>/`, `feed/<channel>/latest-linux.yml`, then the `latest/` alias. |
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
desktop/<channel>/latest/KiroCrew.dmg                         pointer, max-age=300
desktop/<channel>/latest/KiroCrew-x86_64.AppImage             pointer, max-age=300
feed/<channel>/latest-mac.yml                                 pointer, max-age=300
feed/<channel>/latest-mac.json                                pointer, max-age=300 (legacy bridge)
feed/<channel>/latest-linux.yml                               pointer, max-age=300
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
   the wheel, sdist, and AppImage (not the mac zip or DMG, whose bytes are not
   final yet), uploads everything to `pre-signed/`, extracts the `.app` from the
   `*-mac.zip`, and submits it to CDSigner with a manifest generated at sign
   time from the actual bundle contents by
   `packaging/signing/generate-manifest.py`. `packaging/signing/sign.sh` polls
   every 30s with a 15-minute ceiling. `awscurl` is installed **before** AWS
   credentials are configured, so a drifted release of it can never observe the
   signing credentials.
2. **notarize** (macos-15). `notarytool submit --wait`, `stapler staple`, then a
   fail-closed `spctl --assess` that must report `Notarized Developer ID`. On an
   `Invalid` verdict the itemized Apple log is printed. The DMG is then **rebuilt
   from the stapled app** (`hdiutil`, plus an `/Applications` symlink), signed by
   a second CDSigner task with a `type: dmg` manifest, notarized, stapled, and
   held to the same `spctl` gate. The DMG signature is load-bearing twice over:
   an `hdiutil` DMG carries an adhoc signature that the Apple notary accepts but
   Gatekeeper treats as "no usable signature" ("app is damaged" on drag-out),
   and an unsigned DMG cannot be stapled at all (`stapler` Error 73), so
   first-install verification would need network. The stapled DMG is attested
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
| stable | `0.2.0` | `0.2.0` |

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
It runs in packaged macOS and Linux builds only: `SUPPORTED_PLATFORMS` is exactly
`{darwin, linux}`. The NSIS target removes the packaging blocker, since
electron-updater's win32 path is `NsisUpdater`, but win32 stays out until a
`latest.yml` feed is published and Authenticode signing is active: `NsisUpdater`
verifies signatures fail-closed, so an unsigned installer would make every update
fail rather than warn.
On macOS electron-updater's `MacUpdater` downloads the archive itself and serves
it to Electron's built-in `autoUpdater` (Squirrel.Mac) over a loopback proxy, so
the atomic bundle swap is unchanged and `NSURLCache` is no longer in the feed
path. On Linux it replaces the AppImage in place.

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
`autoDownload=false` (consent-first: discovery must never pull megabytes),
`autoInstallOnAppQuit=false` (the default would swap the bundle on quit without
stopping the embedded Python gateway), `allowDowngrade=true` (the gate is
difference-based, so a feed pointed at an older version is offered, which is
what makes a channel switch-back work), and `allowPrerelease=true` (every
nightly and insider stamp is a semver prerelease and would otherwise be
invisible to its own channel). The library still refuses an equal version before
the `allowDowngrade` branch, which is what prevents a self-reinstall loop.

The specific to Kiro Crew part is install ordering: the app supervises a bundled
Python gateway child, so before `quitAndInstall` the client stops it gracefully
(`POST /api/shutdown`, then SIGTERM, then SIGKILL) and disarms the liveness
watchdog that would otherwise resurrect it mid-swap. Choosing "Later" defers to
natural quit through a `before-quit` hook in the same stop-gateway-first order.

## Windows

`build-windows.yml` builds and **Authenticode-signs** the NSIS `Setup.exe`
through AWS Signer during the build (signing profile `KiroCrewWindowsExe`),
whenever `AWS_WINDOWS_SIGNING_ROLE_ARN` is present and the caller passed
`use_prod_environment: true`. The lane is **installer-only**: it publishes
nothing, and electron-builder emits the installer flat into `dist/`. Because no publish
lane consumes them, the artifacts are not attested yet; provenance will land
in-lane the way `publish-linux.yml` does it. win32 auto-update stays disabled in
the client. The supported Windows install path is source: see
[../guides/windows-install.md](../guides/windows-install.md).

`build-desktop.yml`'s matrix also leaves Linux arm64 open. A new platform lane
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
curl -fsS  "$PTR/feed/$CH/latest-mac.yml"
curl -fsS  "$PTR/feed/$CH/latest-linux.yml"
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

Every release lands a `## [X.Y.Z] — YYYY-MM-DD` section in `CHANGELOG.md`
through a normal PR, alongside any version bump. The section format (ordering,
tone, contributor lines) is specified once in
[AGENTS.md](../../AGENTS.md) → "Release Changelog". The dashboard reads the
changelog from `KIROCREW_PROJECT_DIR/CHANGELOG.md` for source installs and from
the bundled copy inside the package for wheel installs.

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
