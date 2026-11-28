# Escape in the message box does not stop the running turn

Decided by: Ray Xu (maintainer, @buluoray)
Date: 2026-11-24
Supersedes: 2026-11-12-the-stop-key-is-named-in-the-shortcuts-reference-and-the-stop-tooltip.md

## Decision

No key in the message box stops the running turn: the Stop button is the one
way to stop it, so the shortcuts reference lists no stop key, the Stop tooltip
is the plain "Stop generation", and the "Require a second Escape to stop the
turn" setting is gone.

## Why

- The maintainer called Escape-to-stop an odd design and asked for it to be
  removed.
- With the chat side panel open, one Escape meant to close the panel stopped
  the reply instead.

## Evidence

- https://github.com/kirodotdev/KiroCrew/pull/42216#issuecomment-7148519392 --
  the maintainer's ruling.
- https://github.com/kirodotdev/KiroCrew/issues/42207 -- the issue.
- https://github.com/kirodotdev/KiroCrew/issues/22315 -- the issue that added
  Escape-to-stop.
- https://github.com/kirodotdev/KiroCrew/issues/23462 -- the issue that added
  the second-Escape setting.
