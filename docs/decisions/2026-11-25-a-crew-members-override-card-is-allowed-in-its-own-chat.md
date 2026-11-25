# A crew member's security-rule override card is allowed in that crew member's own chat

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-25
Supersedes: 2026-10-27-crew-mode-is-a-composer-switch-to-the-conductor-template.md (in part: "A crew member's tool approval is decided by its conductor.", for security-rule override cards only)
Supersedes: 2026-10-29-crew-member-approvals-notify.md (in part: "the only place the approval is decided" and the second Why bullet, "the conductor's row and the conductor's card are where the wait has to be seen and decided", for security-rule override cards only)

## Decision

On a crew member's security-rule override card, that crew member's own chat (the
main pane or a Split View pane) offers Allow once, Allow for this session and
Reject. The conductor's card and the Board rows offer Reject and say where to
allow it. Every other surface stays information only, and every other crew member
approval is still decided on the conductor's card. Every other rule of the two
superseded entries stands.

The maintainer's ruling, verbatim:

> Maintainer decision: yes, for security-rule override cards only. On a crew
> member's override card, that crew member's own chat (main pane or Split View
> pane) offers Allow once, Allow for this session and Reject; the conductor's card
> and the Board rows offer Reject and say where to allow it; every other surface
> stays information only, and every other crew member approval is still decided on
> the conductor's card.

## Why

- The server takes an override allow only from a click on the session itself, so
  the conductor's card can never allow it. Without this exception the command can
  only be rejected.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/40581#issuecomment-7165607859 --
  the maintainer's ruling on the issue that asked for it.
- https://github.com/kirodotdev/KiroCrew/issues/40581 -- the issue: an override
  card offers a plain Approve the server always refuses.
- https://github.com/kirodotdev/KiroCrew/pull/40603 -- the pull request that
  changes the approval surfaces to match.
