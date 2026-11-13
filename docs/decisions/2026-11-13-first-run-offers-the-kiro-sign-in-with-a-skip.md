# First run offers the Kiro sign-in with a Skip when KAS runs Kiro Crew's own engine with no usable sign-in

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-13

## Decision

On a page load that began before first-run setup finished, when the next KAS session takes the bundle route (`kas_bundle_route()`) and no usable Kiro sign-in is stored (`KasToken.is_usable`), the dashboard shows the full-screen Kiro sign-in with a Skip; the server decides through one owner-only status field that fails open to false; Skip saves nothing and lets the app render for that page load; after a Skip the tour's last card names the Settings sign-in instead of saying the agent is ready; a returning user is never held, signed out or not.

## Why

- A new user finished onboarding, was told "your agent is ready", and learned otherwise only from a failed first message after a long wait; the only sign-in was two levels deep in Settings.
- Holding on every admission (the earlier proposal) would also hold returning users; the hold is first run only, and the held screen needs a way out that works without signing in.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/32058#issuecomment-6854926521 -- the maintainer's ruling.
- https://github.com/kirodotdev/KiroCrew/issues/32058 -- the issue.
- https://github.com/kirodotdev/KiroCrew/pull/31793 -- the earlier proposal it re-keys.
