# macOS Signing and Notarization Runbook

Operational reference for KiroCrew's macOS signing chain: CDSigner signing,
Apple notarization, and rotation of the notary credential.

## Chain overview

```
electron-builder (unsigned .app)
  -> sign.sh: strip ad-hoc sigs, metadata-clean tar, upload to S3
  -> CDSigner /v2/sign-tasks (manifest from generate-manifest.py)
  -> signed zip in s3://kirocrew-signing-artifacts-116101834266/signed/...
  -> notarytool submit --wait (Apple notary service)
  -> stapler staple -> spctl assess (want: source=Notarized Developer ID)
```

Key facts:

- Apple Team ID: `94KV3E626L` (AMZN Mobile LLC, central ADP account). Public, safe to share.
- Bundle ID: `com.amazon.kiro.crew`
- Signing account: `116101834266`. CI role: `kirocrew-signing-invoker` (OIDC,
  main + environment:prod only). S3 access role: `kirocrew-cdsigner-access`.
- CDSigner endpoint: `https://api.signer.builder-tools.aws.dev` (SigV4,
  service `signer-builder-tools`, region `us-west-2`).
- The signing manifest is generated at sign time by
  `packaging/signing/generate-manifest.py` from the actual .app contents.
  Do not hand-maintain binary lists.

## Notary credential

The notarization credential is an Apple app-specific password tied to the
team's enrolled Apple account (Developer role under 94KV3E626L).

Storage rules:

- CI copy: AWS Secrets Manager in account `116101834266`, fetched by name at
  build time. Never a GitHub secret, never a workflow env literal
  (SAX-03: Amazon-standard custody, CloudTrail audit, rotation lifecycle).
- Local copy: macOS Keychain via
  `xcrun notarytool store-credentials "KiroCrewNotary" ...`. All local
  commands use `--keychain-profile "KiroCrewNotary"`.
- Never paste the password into chat, tickets, docs, or shell command lines
  in shared logs. Type it only into the Apple portal, `store-credentials`
  in your own terminal, or the Secrets Manager console/CLI.
- If the value is ever exposed, revoke it immediately at appleid.apple.com
  (app-specific passwords are individually revocable) and rotate.

## Rotation runbook (manual mint, zero downtime)

Apple exposes no API to mint app-specific passwords or App Store Connect API
keys, so the mint step is manual by necessity (ARCC "credential source not
controlled by the team" case). Everything around it is automated. Apple
allows up to 25 concurrent app-specific passwords, so rotate
generate-first, revoke-last and CI never breaks mid-rotation.

Procedure (about 2 minutes, quarterly or on any exposure/departure):

1. Sign in at `https://appleid.apple.com` with the enrolled account. The
   Amazon Federate step must run in the AEA-managed browser (standalone
   Safari fails device posture).
2. Sign-In and Security -> App-Specific Passwords -> generate a new one.
   Label with a version, e.g. `KiroCrew notarization v3`.
3. Put the new value into the Secrets Manager secret in `116101834266`
   yourself (console, or `aws secretsmanager put-secret-value` in your own
   terminal).
4. Verify before revoking the old one:
   `xcrun notarytool history --apple-id <account> --team-id 94KV3E626L
   --password <new>` returns history, or wait for the next green
   notarization canary run.
5. Revoke the old password at appleid.apple.com.
6. Optional: refresh your local Keychain profile with
   `xcrun notarytool store-credentials "KiroCrewNotary" ...`.

Forced-rotation triggers (do not wait for the schedule): the credential
appeared anywhere outside Keychain/Secrets Manager; the owning Apple account
holder departs; Shepherd UnrotatedSecrets finding.

Reducing forced rotations: an App Store Connect API key (team-scoped, not
person-bound) has the same manual mint but survives departures. Request via
Mobile Build (Admin-gated) when convenient; not urgent while the password
path works.

## Troubleshooting

- Notarization returns `Invalid`: pull the itemized log with
  `xcrun notarytool log <submission-id> --keychain-profile KiroCrewNotary`.
  Every listed binary must be Developer ID signed with hardened runtime and
  secure timestamp. If binaries are listed, the signing manifest coverage
  regressed; check `generate-manifest.py` scope rules.
- CDSigner fails with "detection of a security issue": generic Electric
  Company scan rejection. Known trigger: macOS tar metadata. bsdtar embeds
  `com.apple.provenance`/quarantine xattrs as pax headers unless suppressed;
  `sign.sh` packages with `COPYFILE_DISABLE=1 tar --no-xattrs
  --no-mac-metadata --no-acls --no-fflags` on Darwin. If it recurs with a
  clean tar, isolate with a probe matrix (vary manifest and input tarball
  independently) before blaming the manifest.
- Verify a signed bundle locally: `codesign --verify --deep --strict App.app`
  and `codesign -dvvv <binary>` (must show `Authority=Developer ID
  Application: AMZN Mobile LLC (94KV3E626L)` and `Timestamp=`). Use `-dvvv`,
  not `-dv`, or the Authority lines are omitted.
- Gatekeeper end state: `spctl --assess --type execute --verbose App.app`
  must print `source=Notarized Developer ID` after stapling.

## Known limitations (tracked)

1. The manifest template still hand-lists the Electron shell entries
   (framework, crashpad, helper apps); an Electron major upgrade or app
   rename can stale them. Fix: derive them from the bundle at sign time.
2. Nested bundles (.app/.framework/.appex) under Contents/Resources are not
   supported by the per-file manifest generation; they require bundle-level
   signing. The generator should fail loudly if one appears.
3. CI does not yet notarize. Until the nightly notarize+staple step lands,
   signing regressions surface only on manual notarization.
