# Ultra Mode is marked with a lightning bolt, and its tab is the Board

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-10-30
Supersedes: 2026-10-29-crew-mode-ships-behind-a-default-off-feature-preview.md (in part: "the Crew tab keeps its name")

## Decision

With the Ultra Mode preview on, Ultra Mode is marked with a lightning bolt wherever it
is marked: the Sessions row's ULTRA MODE label, the composer chip and mark, and the
Sessions filter. The Kiro ghost no longer stands for it. The Sessions row counts
sessions with the chat icon.

The side-panel tab for the Ultra Mode task board is named **Board** and wears the
bolt. Its view id stays `workers`, so stored tabs need no migration. Crew members keep
their name.

An Ultra Mode row's own state is one short word (Needs you, Running, Done or Paused),
with the full sentence in its tooltip. Plain chats keep their full state line. No user
text says "conductor" for Ultra Mode: a message the main chat delivered reads "From the
main chat".

Everything else in the 2026-10-29 entry applies unchanged.

## Why

- The ghost is the product's own mascot, so it did not say "this chat runs Ultra
  Mode", and on the Sessions row it also stood for the session count.
- The tab is the task board for a goal; "Board" names what it shows.
- A long state line truncated on the narrow Sessions row and hid the counts.

## Evidence

- https://github.com/kirodotdev/KiroCrew/pull/26291#issuecomment-6530077537 -- the
  maintainer's ruling, restated on the pull request that adds this entry.
- https://github.com/kirodotdev/KiroCrew/pull/25872 -- the pull request that shipped
  Ultra Mode with the ghost mark and the Crew tab.
