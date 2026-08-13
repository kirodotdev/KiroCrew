# Signing Infrastructure

This directory contains the macOS code signing and notarization scaffolding
for the KiroCrew desktop app, using an enterprise code-signing service.

## Why identifiers are committed here

KiroCrew is distributed as a signed desktop application under a shared Apple
Developer identity. The bundle identifier and team ID are required by Apple's
code signing infrastructure and are not secrets — they're embedded in every
signed `.app` bundle users download.

These files are gated behind `CDSIGNER_API_ENDPOINT` and `AWS_SIGNER_ROLE_ARN`
secrets that only the upstream repository has. Forks without these secrets
skip signing entirely (the workflow produces unsigned builds that work but
trigger macOS Gatekeeper warnings).

## Files

- `Entitlements.entitlements` — macOS entitlements for the Electron app.
  JIT + disable-library-validation are required for V8/Node.js + native addons.
- `manifest-template.json` — signing manifest with embedded requirements
  for all Electron helper processes and frameworks.
- `sign.sh` — CI script that packages, uploads, submits to the signing
  service, polls, downloads, and verifies the signed artifact.
- `build-dmg.sh` — replaces the unsigned app inside electron-builder's branded
  DMG layout template with the signed/stapled app, then shrinks and recompresses
  the image before the DMG signing and notarization stages.

  The mounted-volume phase races Spotlight and XProtect, which start reading
  the freshly-copied app and can hold the volume against ejection ("Resource
  busy") — the same transient class electron-builder retries on. The script
  layers its defenses: `-nobrowse` keeps the volume out of Finder, and the
  eject gets bounded retries with a synced force fallback. hdiutil calls run without `-quiet`, because
  that flag suppresses stderr too and previously reduced failures of this
  script to bare exit codes.

  The branded background is a **volume-bound alias recorded inside `.DS_Store`**,
  which is why the image is reused rather than rebuilt from a folder: recreating
  it drops the layout. The script fingerprints the template's `.DS_Store` before
  the swap and requires the final image to carry the same bytes, which is
  stronger than checking the files exist — a broken alias leaves every file in
  place. It still cannot prove Finder *resolves* the alias, since that also
  depends on the volume identity the alias binds to, and nothing on a CI runner
  re-renders the window.

  **So two things are worth doing rather than assuming green CI covers them.**
  First, watch the next real signing run: the unsigned-DMG S3 round trip, the
  sector-resize arithmetic and the app swap all execute for the first time
  there, not in PR CI. Second, know the fallback — if the alias ever stops
  surviving, run `dmgbuild` against the **stapled** app inside the notarize job.
  That writes a fresh, correct `.DS_Store` and removes the template round trip,
  the resize arithmetic and the survival question in one move; the only reason
  it is not the default is that it re-derives the layout on every release
  instead of preserving the one the build already produced.

## Prerequisites

Access to the signing service must be onboarded (a security review plus
sign-off). See `docs/build/release.md` for the full onboarding runbook.

## CLI artifact manifests (separate trust domain)

The wheel installer does **not** reuse Apple/CDSigner. `publish-cli.yml` signs a
canonical JSON artifact manifest with an asymmetric AWS KMS key and publishes the
same signed JSON at both:

- `cli/<channel>/<version>/cli-manifest.json` (immutable, used by `--version`)
- `feed/<channel>/latest-cli.json` (mutable channel pointer)

The legacy `channel`, `version`, `wheel_url`, `sha256`, `python_requires`, and
`pub_date` fields remain top-level for older installers. Schema v1 adds
`schema`, `algorithm`, `key_id`, and `signature`. The signature is RSA
PKCS#1 v1.5 with SHA-256 over canonical JSON containing every field except
`signature`. `cli.sh` reconstructs those exact bytes, verifies them with the
offline public key embedded in the installer, validates the authenticated URL,
channel, version, and digest, and only then downloads the wheel. The SHA-256
check remains a second fail-closed check over the downloaded bytes. There is no
`SHA256SUMS` or unsigned-feed fallback in the strict installer.

Threat model: this protects against unauthorized mutation of distribution
objects or channel feeds while the installer trust root and signing-enabled
publisher role remain trusted. The publisher role holds `kms:Sign`, so its
compromise can produce a valid manifest and is explicitly out of scope; the
signature does not create a separate trust boundary from that role.

### Repository bootstrap state

The repository intentionally carries `UNCONFIGURED` in both
`cli-manifest-public.pem` and the two `CLI_MANIFEST_*` constants in `cli.sh`.
This is fail-closed: the installer exits before network I/O, and a trusted
publisher configuration with only one of the role/key settings fails before any
upload. Forks with neither setting still skip publication.

No private key should be generated, exported, committed, pasted into CI, or
handled by an agent. Operational enablement is a human/infrastructure step:

1. Create a non-exportable asymmetric KMS key in `us-west-2` with key usage
   `SIGN_VERIFY` and key spec `RSA_3072` or `RSA_4096`.
2. Grant the existing CLI publication role only `kms:GetPublicKey` and
   `kms:Sign` on that one key. Keep the existing OIDC subject/environment
   restriction; do not grant decrypt or broad `kms:*` access.
3. Retrieve the **public** key with `kms:GetPublicKey`, convert its
   SubjectPublicKeyInfo DER bytes to PEM, and replace
   `packaging/signing/cli-manifest-public.pem`.
4. Run
   `python3 packaging/signing/cli-manifest.py key-info --public-key packaging/signing/cli-manifest-public.pem`.
   Copy the returned public `key_id` and `public_key_pem_base64` values into the
   matching constants in `cli.sh`. Commit the public-key pin normally.
5. Set the protected `prod` environment variable
   `CLI_MANIFEST_SIGNING_KEY_ARN` to the key ARN. The workflow compares KMS
   `GetPublicKey` output byte-for-byte with the committed key before every sign,
   and verifies the returned signature locally before publishing.
6. Run `test/test_cli_manifest_signature.py`, then dispatch a publish and verify
   the immutable manifest is present. Publish the strict `cli.sh` only after a
   signed channel feed exists. Because the added fields are backward-compatible,
   the signed feed may safely go live before the strict installer. This ordering
   is enforced mechanically: `publish-installer.yml` refuses to publish while
   `cli.sh` still pins `CLI_MANIFEST_KEY_ID="UNCONFIGURED"`, and — once a key
   is pinned — refuses unless every LIVE channel feed verifies against that
   key (`cli-manifest.py verify`, the same checks the installer runs), so
   neither the pin commit nor any later merge can replace the live installer
   with one that refuses the feeds it is pointed at.

Pinned versions released before enablement have no immutable signed manifest and
therefore fail closed under the new installer unless an authorized backfill signs
the already-published digest. Do not replace the KMS key in place: schema v1 pins
one key. For rotation, first ship an installer revision that trusts both old and
new public keys, then switch the publisher, and retire the old key only after the
overlap window.
