# A Conductor mode Sessions row wears its bolt in the agent-name slot, not before the title

Decided by: Zezhen Xu (maintainer, @CrysisDeu)
Date: 2026-11-20
Supersedes: 2026-11-17-active-conductor-mode-uses-one-bolt.md (in part: the Sessions row's bolt "directly before the session title")

## Decision

A Conductor mode Sessions row shows its one coloured lightning bolt on the
row's first line, in the slot where every other row names its agent. The title
line carries no mark and no indent, so every row's title starts at the same x.
The row still shows no visible mode name. The composer's bolt is unchanged.

## Why

- Leading the title, the bolt indented that one row's title and left a bare
  "· project" on the first line, so the list no longer lined up. The maintainer
  rejected that layout and asked for the bolt in the agent-name place.

## Evidence

- https://github.com/kirodotdev/KiroCrew/pull/39383 -- the three-line session
  row change that moves the bolt, with before/after screenshots.
- The pull request that adds this entry; the maintainer restates the decision
  in a comment there.
