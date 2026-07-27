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

## Prerequisites

Access to the signing service must be onboarded (a security review plus
sign-off). See `docs/release-automation.md` for the full onboarding runbook.
