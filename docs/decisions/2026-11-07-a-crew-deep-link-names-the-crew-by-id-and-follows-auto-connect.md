# A crew deep link names the crew by its id and follows the auto-connect setting

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-07

## Decision

`kirocrew://chat?crew=<crew id>&sid=<session>` names the crew by its id (its
key in `instances.json`, never the display name); the app switches to that crew
the way a tab click does and then sends the crew's pane an open-session
message, and an unknown or unavailable crew shows an error and opens nothing.
The link follows the auto-connect setting: with it on, it connects the crew as
a tab click would; with it off, it never starts a connection and is refused
with "crew not connected".

## Why

- Reloading the pane on the session would discard what is open in it, and the
  pane's load hint alone cannot reach a pane that is already open.
- Display names are not unique, so a name could open the wrong crew.
- Following auto-connect means a link never starts an SSH connection the app
  would not start on its own.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/31545#issuecomment-6705953383 --
  the maintainer's ruling.
- https://github.com/kirodotdev/KiroCrew/issues/31545#issuecomment-6699310045 --
  the options it chose between.
- https://github.com/kirodotdev/KiroCrew/issues/31545 -- the issue.
