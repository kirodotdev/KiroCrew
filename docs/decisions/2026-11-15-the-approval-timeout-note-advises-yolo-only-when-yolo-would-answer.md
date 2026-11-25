# The approval-timeout note advises YOLO mode only when YOLO would have answered the prompt

Decided by: Bolin Chen (@bolichen97, maintainer)
Date: 2026-11-15

## Decision

The note shown when an approval prompt times out advises turning on YOLO mode only
when YOLO mode would have answered that prompt; otherwise it says YOLO mode does not
cover this call and, for a deny-rules override card, names the rule.

## Why

- The note told people to turn on YOLO mode for an unattended run while the gateway
  was already in `--approval yolo`, which is advice they cannot act on.
- Some prompts reach a person whatever YOLO mode says: a deny-rules override card is
  answered only by the owner's own click, and a channel-linked chat is not reached by
  the `--approval yolo` grant. For those the note names why instead.
- The maintainer chose this option alone. How long an unattended run waits on such a
  prompt is a separate question, left to its own issue and design sign-off.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/37488#issuecomment-6897116265 -- the
  maintainer's decision.
- https://github.com/kirodotdev/KiroCrew/issues/37488 -- the report.
- https://github.com/kirodotdev/KiroCrew/issues/37642 -- the approval wait, split out.
