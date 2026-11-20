# The folder menu has a Close all sessions row above Delete folder, and its confirmation panel always shows

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-24

## Decision

A folder's ⋮ menu has a **Close all sessions** row directly above **Delete folder**, in the same red group, and choosing it always opens a confirmation panel that lists the sessions it will close, even when every session is idle.

## Why

- Closing every session in a folder and keeping the folder took one close per session; the menu offered only rows that delete the folder.
- The row sits next to two rows that delete, so a bulk close never happens on one click.
- The panel is the one place that shows which sessions will close, including ones in collapsed sub-folders.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/40014#issuecomment-7123593889 --
  the maintainer's decision.
- https://github.com/kirodotdev/KiroCrew/issues/40014 -- the issue.
