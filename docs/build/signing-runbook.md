# macOS Signing and Notarization Runbook

Operational reference for Kiro Crew's macOS signing chain: signing via the
enterprise signing service (CDSigner), Apple notarization and stapling, and
rotation of the notary credential.

The pipeline lives in `.github/workflows/sign-and-notarize.yml`, a reusable
workflow called by `nightly.yml` (channel `nightly`) and `release.yml` (channels
`insider` and `stable`). The scripts it drives are in `packaging/signing/`.
Release mechanics as a whole live in [release.md](release.md); desktop packaging
in [desktop-app.md](desktop-app.md).

**Windows signing is not documented here.** `website/electron/scripts/sign-windows.js`
is an electron-builder sign hook that Authenticode-signs each binary through an
S3 round-trip against AWS Signer, driven by the five `WINDOWS_SIGNING_*` env vars
`build-windows.yml` sets. The infrastructure side (the Signer profile, the
watching Lambda, the bucket policies, the `ArtifactAccessRole`) is defined in a
separate private publishing-infrastructure repository, so there is no in-repo
document to link. Treat the hook's own header comment as the authoritative
in-repo description of its contract.

## Chain overview

The three jobs are chained so that un-notarized bytes have no path to
distribution.

```
build-desktop  ->  unsigned .app inside an electron-builder *-mac.zip
  |
sign      (ubuntu)  flatten artifacts, attest wheel/sdist/AppImage provenance,
                    upload unsigned artifacts to pre-signed/<channel>/<version>/,
                    extract the .app, run packaging/signing/sign.sh
                    -> signed/<channel>/<version>/<AppSlug>.zip
  |
notarize  (macOS)   notarytool submit --wait, stapler staple, spctl gate,
                    build a DMG from the STAPLED app, sign the DMG via a second
                    CDSigner task, notarize + staple + gate the DMG, attest the
                    DMG, attach the gated artifact to the run
  |
publish   (ubuntu)  copy the gated artifact to the public distribution bucket,
                    then write feed/<channel>/latest-mac.yml
```

Key properties, each load-bearing:

- **The signed-zip key handoff is internal** (a `sign` job output consumed by
  `notarize`), not plumbed through every caller.
- **The Apple credential is confined to the `notarize` job.** It is fetched from
  AWS Secrets Manager at runtime, masked, and used inside single steps. It is
  never written to `GITHUB_ENV`, a file, or a log, and the `publish` job never
  touches it.
- **The Gatekeeper gate fails closed.** `spctl` must report
  `source=Notarized Developer ID` for the app (`--type execute`) and for the DMG
  (`--type install`), or `notarize` fails and `publish` never runs. On a
  non-Accepted notarization the itemized Apple log is printed.
- **`publish` consumes only the artifact `notarize` attached after the gate**, in
  the same run. `release.yml`'s `github-release` job accepts macOS assets only
  from that same gated artifact, so the unsigned electron-builder zip and DMG are
  inter-job inputs and can never become release assets.
- **The feed is written last**, after both artifacts are publicly downloadable.
  An un-notarized artifact in the update feed would auto-update clients to a
  build Gatekeeper blocks.
- **Versioned distribution keys are never republished with different bytes.** Both
  the zip and the DMG are written with `--if-none-match '*'`; a `PreconditionFailed`
  on a job re-run keeps the existing bytes, which already passed the gate on the
  earlier attempt. The keys are CloudFront-immutable-cached.

`publish` is a separate ubuntu job on purpose: a transient publish failure retries
as a roughly-two-minute job rather than repeating two Apple submissions with
30-minute budgets each, and the expensive macOS runner never burns minutes on S3
uploads. Linux publishing takes no part in this trust chain; the AppImage ships
from `publish-linux.yml`.

## Why the DMG carries its own Developer ID signature

`hdiutil`-created DMGs carry an **adhoc** signature. The Apple notary service
Accepts that, but Gatekeeper treats it as no usable signature and shows "app is
damaged" when a user drags the app out of the quarantined mount (the
`syspolicy_check` reading is: app passed, DMG failed). An unsigned DMG also cannot
be stapled at all (`stapler` Error 73), so first-install verification would need
network access.

So the DMG is built **from the already-stapled app**, then signed by a second
CDSigner task with a `type: dmg` manifest
(`packaging/signing/sign-dmg.sh`), then notarized and stapled itself. The script
fails closed: it runs `codesign --verify --strict` and requires an
`Authority=Developer ID Application` line on the result. The `spctl --type install`
gate in the workflow is exactly the check that catches an adhoc regression.

The DMG signs under the **app's own** bundle identifier, read from the stapled
bundle's `Info.plist` rather than hardcoded. CDSigner authorization is
per-identifier, so an unfamiliar identifier is rejected; any future distinct
identifier needs onboarding first. `sign-dmg.sh` defaults to the onboarded app
identifier for that reason, and it must not be changed as part of a string scrub.

Published **filenames** are pinned to the `KiroCrew` basename on every channel,
even though the nightly bundle is `KiroCrew Nightly.app`. CDN keys and the
latest-DMG permalink (`desktop/<channel>/latest/KiroCrew.dmg`) are a public
contract, so deriving filenames from the bundle name would silently rename keys
and break the permalink. The DMG's **volume** name does follow the bundle.

## The signing manifest is generated, never hand-maintained

Apple notarization requires **every** nested Mach-O binary to be Developer ID
signed with hardened runtime and a secure timestamp. The signing service
auto-detects frameworks and dylibs under `Contents/Frameworks`, but everything
under `Contents/Resources` (the embedded Python backend: the interpreter, every
`.so` C-extension, every vendored `.dylib`) plus Squirrel's ShipIt helper must be
listed explicitly in `embedded_requirements`, or notarization returns `Invalid`.

The binary set changes whenever a Python dependency changes, the app is renamed,
or Electron is upgraded, so `packaging/signing/generate-manifest.py` enumerates
everything at sign time from the actual `.app`:

- `collect_entries()` for the backend Mach-Os under `Contents/Resources` plus
  ShipIt.
- `collect_shell_entries()` for the Electron shell: every helper `.app` under
  `Contents/Frameworks` gets an entry, and every framework that ships loose
  `Helpers` executables (Electron Framework's `chrome_crashpad_handler`) gets one
  entry for the framework plus one per helper, all under the framework's own
  identifier. Identifiers are **read** from each bundle's `Info.plist`, never
  synthesized. Frameworks with no `Helpers` (Mantle, ReactiveObjC, Squirrel) stay
  unlisted because the service's app pass auto-signs them.

`validate_layout()` is a fail-closed tripwire that aborts the sign with a clear
message on three classes of unknown layout, each of which would otherwise surface
as a notarization `Invalid` weeks later with no bisectable trail:

1. A Mach-O outside `Contents/MacOS/`, `Contents/Frameworks/` or
   `Contents/Resources/` (`Contents/PlugIns/*.appex`,
   `Contents/Library/LoginItems`): nothing signs it.
2. A **nested bundle** under `Contents/Resources` (any path segment ending in
   `.app`, `.framework`, `.appex`, `.xpc`, `.bundle` or `.plugin`). Its binaries
   would be signed per file, but Apple requires bundle-level signing (identifier
   plus sealed resources) for a nested bundle, which per-file entries cannot
   provide.
3. A loose Mach-O **executable** directly under `Contents/Frameworks` with no
   `.app`/`.framework` in its path. Loose `.dylib`s there are auto-signed by the
   service's app pass; a bare executable has no signing rule.

Extending the generator for a new layout is a deliberate act, and the change must
be verified through a real notarization.

## Entitlements: two files, one contract

`packaging/signing/Entitlements.entitlements` is the release-lane entitlements
file. `website/electron/build/entitlements.mac.plist` is the electron-builder-lane
twin. **The two signing paths read their OWN file**, so a key present in only one
of them means that lane ships a broken bundle. `website/electron/packaging.test.js`
pins both.

Under the hardened runtime an entitlement, not the `Info.plist` usage string, is
what grants a device capability. `com.apple.security.device.audio-input` is what
makes the microphone work for voice input and streaming STT; without it the mic is
refused with **no prompt at all** and no System Settings toggle to fix it. Camera
is deliberately absent, because `permission-handler.js` denies video.

## Supply-chain ordering inside the jobs

Two orderings in the workflow are deliberate and must be preserved:

- **`awscurl` is installed BEFORE AWS credentials are configured**, in both the
  `sign` and `notarize` jobs, so a compromised or version-drifted release of that
  package can never observe the signing-role credentials at install time. It is
  version-pinned for the same reason, and installed into a dedicated venv with
  only the `awscurl` binary symlinked onto PATH (PEP 668 refuses
  `pip install --user` on the runners' managed Pythons, and the venv's python must
  not shadow the system `python3` later steps use).
- **Provenance is attested only for bytes that are final.** The `sign` job attests
  the wheel, sdist and AppImage. It deliberately omits the macOS `.zip` and the
  build job's DMG, because those are re-signed downstream and a pre-notarization
  attestation would bind a digest that never ships. The shipping DMG is attested in
  the `notarize` job **after** stapling, since stapling embeds the ticket into the
  file and therefore changes the released bytes.

## OIDC subject alignment and the `prod` environment

All three jobs declare `environment: prod`. Tag-triggered callers (`release.yml`)
present an OIDC subject of `ref:refs/tags/<tag>`, which the signing role does not
trust; the `prod` environment switches the subject to `environment:prod`, which it
does. Nightly runs on `main` present either trusted form.

`prod` is protected by GitHub-side **ref restrictions** rather than required
reviewers, which would stall the unattended scheduled nightly: a deployment
branch/tag policy limits the environment to `main` and `v*` tags, and a repository
ruleset restricts `v*` tag creation, update and deletion to repository admins. An
unmerged commit can therefore only reach this environment if an admin deliberately
tags it, which is the same principal who could merge it.

## The notary credential

The credential is an Apple app-specific password for the team's enrolled Apple
account. Custody rules:

- **CI copy: AWS Secrets Manager**, secret id `kirocrew/signing/apple-notary`,
  fetched by the same OIDC role used for signing. The JSON carries `apple_id`,
  `password` and `team_id`. It is never a GitHub secret and never a workflow env
  literal: a dedicated secret store gives custody, an audit trail, and a rotation
  lifecycle that a repository secret does not.
- **Local copy: the macOS Keychain**, via
  `xcrun notarytool store-credentials "KiroCrewNotary" ...`. Every local command
  then uses `--keychain-profile "KiroCrewNotary"`.
- **Never paste the password** into chat, tickets, docs, or a shell command line
  that lands in a shared log. Type it only into the Apple portal, into
  `store-credentials` in your own terminal, or into the Secrets Manager console
  or CLI.
- **If it is ever exposed, revoke it immediately** at `appleid.apple.com`
  (app-specific passwords are individually revocable) and rotate.

### Rotation: manual mint, zero downtime

Apple exposes no API to mint app-specific passwords or App Store Connect API keys,
so the mint step is manual by necessity. Everything around it is automated. Apple
allows up to 25 concurrent app-specific passwords, so rotate generate-first and
revoke-last, and CI never breaks mid-rotation.

The procedure takes a couple of minutes, quarterly or on any exposure:

1. Sign in at `https://appleid.apple.com` with the enrolled account. The
   enterprise federation step must run in the managed browser; a standalone
   browser fails device posture.
2. Sign-In and Security, then App-Specific Passwords, then generate a new one.
   Label it with a version.
3. Put the new value into the Secrets Manager secret yourself (console, or
   `aws secretsmanager put-secret-value` in your own terminal).
4. **Verify before revoking the old one:**
   `xcrun notarytool history --apple-id <account> --team-id <team> --password <new>`
   must return history, or wait for the next green notarization run.
5. Revoke the old password at `appleid.apple.com`.
6. Optionally refresh your local Keychain profile with `store-credentials`.

Rotate immediately, without waiting for the schedule, when the credential has
appeared anywhere outside the Keychain and Secrets Manager, when the owning Apple
account holder departs, or when an automated secret-age finding names it.

An App Store Connect API key is team-scoped rather than person-bound, so it has
the same manual mint but survives a departure. Requesting one is worthwhile
whenever convenient, and not urgent while the password path works.

## Troubleshooting

**Notarization returns `Invalid`.** Pull the itemized log with
`xcrun notarytool log <submission-id> --keychain-profile KiroCrewNotary`. Every
listed binary must be Developer ID signed with hardened runtime and a secure
timestamp. If binaries are listed, manifest coverage regressed: check
`generate-manifest.py`'s scope rules and its layout tripwire.

**CDSigner rejects the submission for "detection of a security issue".** This is a
generic scan rejection. The known trigger is macOS tar metadata: `bsdtar` embeds
`com.apple.provenance` and quarantine xattrs as pax headers unless suppressed, so
`sign.sh` packages with `COPYFILE_DISABLE=1 tar --no-xattrs --no-mac-metadata
--no-acls --no-fflags` on Darwin. If it recurs with a clean tar, isolate it with a
probe matrix (vary the manifest and the input tarball independently) before
blaming the manifest.

**Signing times out.** `sign.sh` polls for 15 minutes (`MAX_WAIT`, 30s interval)
and gates on the explicit `success` status flag rather than elapsed time, so a
success arriving on the final tick is not misread as a timeout. Exit code 5 is a
genuine timeout and carries the sign-task id.

**Verify a signed bundle locally.** `codesign --verify --deep --strict App.app`,
then `codesign -dvvv <binary>`, which must show an
`Authority=Developer ID Application: ...` line and a `Timestamp=`. Use `-dvvv`,
not `-dv`, or the Authority lines are omitted.

**Confirm the Gatekeeper end state.**
`spctl --assess --type execute --verbose App.app` must print
`source=Notarized Developer ID` after stapling; the DMG's equivalent is
`--type install`.

## Known limitations

1. Nested bundles (`.app`, `.framework`, `.appex`) under `Contents/Resources` are
   not supported by per-file manifest generation; they need bundle-level signing.
   `validate_layout()` fails the sign loudly if one appears, which is the intended
   behavior, but supporting one would require real generator work.
2. `generate-manifest.py` falls back to its hardcoded `APP_ID` when a bundle's own
   `CFBundleIdentifier` cannot be read. That is never expected for a real
   electron-builder output, so a build that reaches the fallback is a signal that
   something is wrong with the bundle rather than a supported path.
