# Installed apps update automatically through one setting, with a per-app Follow, Always or Never

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-13

## Decision

Automatic app updates ship as one setting on Settings → About, "Update apps automatically" (off by default, checking every 6 hours), plus a per-app Follow / Always / Never choice on the app's page; there is no `ask` mode and no check-interval setting, a version that adds permissions is never installed automatically and posts a note instead, and the manual Update gains a retryable `409 app_changed` when the app changed while it waited for the update to start.

## Why

- The off setting already sends a note that opens the app's page one click from Update, so an `ask` mode adds nothing a person needs.
- The gateway's own update check runs on a fixed interval, so the app check does too.
- This smaller shape meets every acceptance point of #25511.
- Syncing apps installed from a local folder stays a separate request.

## Evidence

- Maintainer decision: https://github.com/kirodotdev/KiroCrew/issues/25511#issuecomment-6853201632
- Issue: https://github.com/kirodotdev/KiroCrew/issues/25511
- Pull request: https://github.com/kirodotdev/KiroCrew/pull/25512
