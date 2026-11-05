# A Crew member's tool approval always alerts the person, pointed at its conductor

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-10-29

## Decision

A Crew Mode crew member's tool approval raises the chime, the unread mark, the
bell note, the in-app banner and the OS toast like any chat's. While its
conductor is open, the unread mark lands on the conductor's row; the bell note's
slot is the conductor and its title the crew member's name, opening it leads to
the conductor's approval card, and it offers no Approve or Reject, since that card
is the only place the approval is decided; an OS toast carries the crew member's
name. The conductor's session row shows the wait even while the conductor is
running, unless the conductor itself owes the person a decision.

## Why

- A crew member that waits on an approval has stopped. Muting it while the person
  is in another chat or away from the window left it waiting with nobody told;
  the approval card can decide it only once someone looks.
- The person's lists leave the crew member's own row out, so the conductor's row
  and the conductor's card are where the wait has to be seen and decided.
- The banner already stays quiet while the conductor is on screen
  (`targetsCurrentView`), where the card itself shows the request.

## Evidence

- https://github.com/kirodotdev/KiroCrew/pull/25183#pullrequestreview-5700912106 --
  the review that found the mute and proposed this fix.
- https://github.com/kirodotdev/KiroCrew/issues/25810 -- the issue carrying the
  maintainer's ruling.
- https://github.com/kirodotdev/KiroCrew/issues/24860 -- the Crew Mode design.
