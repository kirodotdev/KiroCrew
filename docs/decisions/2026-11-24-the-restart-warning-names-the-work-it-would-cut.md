# The restart warning names the work a restart would cut

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-24

## Decision

When a restart from the dashboard would cut running work, its "Work is still running" dialog lists which chats and scheduled jobs it is (name, kind and age; at most five, then a count of the rest), taken from the `holders` of `POST /api/restart`'s `409 work_in_flight` answer; the progress step, the refusal sentence and every log line stay counts-only.

## Why

- `POST /api/restart` answers only the machine owner or a Shared Crew teammate, and both can already list every chat and cron job, so the names reach no one new.
- The progress step and the refusal sentence reach every app, and the log reaches anyone who can read it, so they keep the counts alone.

## Evidence

- Maintainer decision: https://github.com/kirodotdev/KiroCrew/issues/39555#issuecomment-7147540757
- Issue: https://github.com/kirodotdev/KiroCrew/issues/39555
- Names on the host first: https://github.com/kirodotdev/KiroCrew/pull/40188
