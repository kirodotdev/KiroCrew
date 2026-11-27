# A Discover card's curator copy reads in the dashboard language when the dashboard knows it

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-23

## Decision

The dashboard holds translations for the collection titles and blurbs the
official catalog publishes, matched by their exact English, and shows a
featured card's title and blurb in the dashboard language only when it knows
every one of them the card carries; any other card shows exactly as published,
the card's and its dialog's accessible names use the title shown, and the
published document does not change.

## Why

- Copy the dashboard shows has to exist in every language it ships, and the
  published document carries one string, so the dashboard holds the
  translations, as it does for category labels.
- Matching the exact English, not an id, means a reworded title or blurb shows
  as published instead of as a translation of words the curator no longer uses;
  a new or reworded collection reads in English until a release adds it.
- Translating a card whole or not at all keeps a reworded blurb from sitting in
  English under a translated title.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/41229#issuecomment-7099792127 --
  the maintainer's ruling.
- https://github.com/kirodotdev/KiroCrew/issues/41229 -- the issue.
