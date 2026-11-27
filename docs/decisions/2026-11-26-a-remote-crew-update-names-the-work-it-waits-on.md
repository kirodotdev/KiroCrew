# A Remote Crew update names the work it waits on

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-26

## Decision

While a Remote Crew update waits for the crew to go idle, the hub's job carries the first holders the crew's idle check names (`waiting.holders`, `waiting.holders_more`: slot key or cron job name, kind and age, at most five) and the Remote Crew row lists them under its waiting line in the restart dialog's words, read from one extra stdout line `kirocrew restart --when-idle` prints just before its unchanged counts marker; the counts marker, every sentence, the `still_busy` error, the Restart route's `409 busy` and every log line stay counts-only.

## Why

- The job is read only by the hub's machine owner (every `/api/instances` route refuses Shared Crew teammates, app tokens and links minted from a chat channel), who can already list every chat and cron job on that crew, so the names reach no one new.
- A caller acting for a session on the crew (a process Kiro Crew started for an agent: a chat's tool call, a shared-runtime worker, a sandboxed agent, a script cron) keeps the counts alone, as the crew's own idle check intends for such a caller.
- The crew prints the line only when the hub's stdout handling (its tail length and credential redaction, as the crew's own build has it) leaves it and the counts marker exactly as they are, so on a hub of the same build a name cannot turn a busy crew into a failed check.
- On an SSM crew the line stays in that instance's SSM command history like the rest of that command's output: an accepted trade-off, since it carries redacted slot keys and job names and never message text.

## Evidence

- Maintainer decision: https://github.com/kirodotdev/KiroCrew/issues/42272#issuecomment-7178866433
- Issue: https://github.com/kirodotdev/KiroCrew/issues/42272
- Names on the host first: https://github.com/kirodotdev/KiroCrew/pull/40188
- Names in the restart dialog: https://github.com/kirodotdev/KiroCrew/pull/42270
