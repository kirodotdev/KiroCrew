# Active conductor mode uses one bolt in the composer and Sessions title

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-17
Supersedes: 2026-10-30-the-ultra-mode-name-wears-the-purple-to-blue-gradient.md (in part: the active composer label and Sessions row label)
Supersedes: 2026-11-13-the-crew-mode-feature-is-named-conductor-mode.md (in part: the name appears on every active-state mark)

## Decision

An active conductor mode chat shows one coloured lightning bolt and no visible
mode name in the composer. Its Sessions row removes the separate mode label and
shows one coloured lightning bolt directly before the session title. The
labelled control that turns conductor mode on keeps its name.

## Why

- The full name repeats a state the bolt already marks.
- Removing the words gives the message box and session title more room.
- The name stays on the control where a person chooses the mode.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/38887 -- the approved scope and
  rendered desktop and phone result.
