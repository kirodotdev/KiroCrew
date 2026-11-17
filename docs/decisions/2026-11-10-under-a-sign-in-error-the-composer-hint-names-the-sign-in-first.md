# Under a sign-in error the composer's hint names the sign-in first

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-10

## Decision

While a sign-in error is the newest row of a chat, the composer's interrupted
hint reads "Last turn needs a sign-in — follow the message above, then press
Resume or send your message again" instead of "Last turn was interrupted —
press Resume, or just type".

## Why

- Resume does work once the sign-in lands, so hiding it would remove the
  one-press way back.
- The hint fixes the advice without guessing whether the sign-in has happened
  yet.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/32065#issuecomment-6768064108 --
  the maintainer's ruling.
- https://github.com/kirodotdev/KiroCrew/issues/32065 -- the issue.
