# Heading navigation mounts the chat transcript's next turns through the landing scroll

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-25

## Decision

The chat transcript keeps mounting only the rows around its viewport, and a screen reader's heading key reaches the turns past the window edge through its own scroll of the heading it lands on: a screen reader that scrolls its cursor into view moves the transcript scroller, the virtualizer moves its window as for any scroll, and the overscan mounts the following turns, whose speaker headings are then the next heading; there is no offscreen heading for each unmounted turn.

## Why

- The virtualizer emits only mounted rows, so its work and DOM grow with the window, not with the transcript; a heading per unmounted turn would add DOM for every turn of a long transcript.
- No browser event reports a heading-key move, so the transcript cannot mount turns in response to the key itself; the scroll of the landed heading is the signal there already is. For that scroll to move the transcript, each speaker heading has a containing block inside the scroller.
- Residual: a screen reader's list of all headings (an elements list or a rotor) shows the mounted turns only, and the heading key can still stop at a run of more than six rows without a heading (loose tool groups, notices, an interim turn whose fold is hidden). Older history not yet loaded still comes in through the Earlier messages bar, and the main chat page's Alt+Up and Alt+Down turn stepper already reaches unmounted turns.

## Evidence

- Maintainer decision: https://github.com/kirodotdev/KiroCrew/issues/39542#issuecomment-7155353917
- Issue: https://github.com/kirodotdev/KiroCrew/issues/39542
- The change that added the speaker headings: https://github.com/kirodotdev/KiroCrew/pull/31860
