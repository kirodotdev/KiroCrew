# The stop key is named in the shortcuts reference and the Stop tooltip

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-12

## Decision

The keyboard shortcuts reference lists the key that stops the running turn
from the message box under the Stop button's own name, "Stop generation", and
the composer's armed Stop button names that key in its tooltip; while "Require
a second Escape to stop the turn" is on, the row is not listed and the tooltip
is the plain "Stop generation".

## Why

- One press of the key in the message box already stopped the turn, and
  nothing on screen said so.
- With the second-Escape setting on, one press only arms the stop, so naming a
  single press there would teach a key press that does not stop the turn.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/23462#issuecomment-6829782391 --
  the maintainer's ruling.
- https://github.com/kirodotdev/KiroCrew/issues/23462 -- the issue.
- https://github.com/kirodotdev/KiroCrew/pull/31473 -- the second-Escape
  setting.
