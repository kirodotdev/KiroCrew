# A Connections card names its own next step for a spent approval

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-07

## Decision

When a pasted return address belongs to an approval that is no longer active
(the relay answers `approval_superseded`), the Connections card says so in the
reader's language and names its own next step: Cancel, then connect again. It
does not show the server's English inside "Action failed", or the chat banner's
line that points at an Authorize button the card does not have. The spent
address is cleared from the field, and the card stays in its waiting state, so
its Cancel stays on screen.

## Why

- The issue's expected result is a localized message that the approval is no
  longer valid, plus what to do.
- The chat banner's line says "Click Authorize to start fresh", but the waiting
  card has no Authorize button. Cancel followed by Connect or Reconnect is how a
  person gets a new approval.
- Of the three options in the triage (A: a card string naming its own next
  step; B: reuse the banner line; C: A plus clearing the stale address and
  ending the wait), A is the smallest that gives the reader a next step that
  exists. It takes C's clearing, because no retry can deliver that address, and
  leaves ending the wait to the Cancel the message names.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/30391#issuecomment-6705290549 --
  the ruling.
- https://github.com/kirodotdev/KiroCrew/issues/30391#issuecomment-6664312709 --
  the triage that set out options A, B and C.
- https://github.com/kirodotdev/KiroCrew/issues/30391 -- the report.
