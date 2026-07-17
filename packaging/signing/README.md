# Signing Infrastructure

This directory contains the macOS code signing and notarization scaffolding
for the KiroCrew desktop app, using Amazon's internal CDSigner service.

## Why internal identifiers are committed here

KiroCrew is distributed as an Amazon-signed desktop application under the
`AMZN Mobile LLC (94KV3E626L)` Apple Developer identity — the same identity
used by kiro-cli, Kiro IDE, and other Amazon developer tools. The bundle
identifier (`com.amazon.kiro.crew`) and team ID are required by Apple's
code signing infrastructure and are not secrets — they're embedded in every
signed `.app` bundle users download.

These files are gated behind `CDSIGNER_API_ENDPOINT` and `AWS_SIGNER_ROLE_ARN`
secrets that only the upstream repository has. Forks without these secrets
skip signing entirely (the workflow produces unsigned builds that work but
trigger macOS Gatekeeper warnings).

## Files

- `Entitlements.entitlements` — macOS entitlements for the Electron app.
  JIT + disable-library-validation are required for V8/Node.js + native addons.
- `manifest-template.json` — CDSigner signing manifest with embedded requirements
  for all Electron helper processes and frameworks.
- `sign.sh` — CI script that packages, uploads, submits to CDSigner, polls,
  downloads, and verifies the signed artifact.

## Prerequisites

CDSigner access must be onboarded (AppSec review + SIM approval). See
`docs/RELEASE_AUTOMATION.md` for the full onboarding runbook.
