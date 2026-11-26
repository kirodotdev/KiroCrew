# An app egress blocked-link placeholder names the rule that blocked the link

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-25

## Decision

Where Kiro Crew still writes the suspicious-URL placeholder in place of a blocked link (app egress, `ctx.scrub.outbound`), a short fixed reason for the rule that blocked it follows the unchanged tag in parentheses, `[REDACTED: suspicious URL to <host>] (<reason>)`, for example `(long query)`; the reason comes from a fixed table keyed by that rule, is never built from URL text, and no pattern that reads placeholders is added or widened.

## Why

- The maintainer's decision on the issue: the placeholder names the kind of rule that removed the link, as a short fixed reason after the unchanged tag.
- A reader of the placeholder alone cannot tell a likely secret from a shape check a legitimate link can trip, so cannot decide whether to shorten the link and retry or to report a false positive.
- Chat, channels and the model's context keep a flagged link as written, and the dashboard's Blocked link card already explains it; app egress is the one place the placeholder is still written, so it is the one place that changes.
- The rule is already known where the placeholder is written, so nothing parses the reason back out of the text.

## Evidence

- The maintainer's decision: https://github.com/kirodotdev/KiroCrew/issues/14782#issuecomment-7164840161
- The issue: https://github.com/kirodotdev/KiroCrew/issues/14782
- The earlier pull request whose reason table and after-the-tag shape this builds on: https://github.com/kirodotdev/KiroCrew/pull/14783
