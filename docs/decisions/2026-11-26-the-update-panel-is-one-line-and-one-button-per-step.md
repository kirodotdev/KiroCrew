# The update panel is one line and one button per step

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-26

## Decision

The update panel in Settings > About, the update dialog and the update pill show
one short line and one button for each step, and every explanatory sentence sits
in an info button (the small (i) beside the line or label) instead of the body:

- available: the one button, "Update to vX", which names the version, and a small
  "Release notes" link when the notes are not shown in the panel;
- installing: one line, "Installing vX...", with what happens next in the info
  button;
- installed: no line, one button, "Restart to use vX", which keeps its two-step
  confirm;
- host approval: one line, "Run this on the gateway host.", the command and Copy,
  with what the command installs and why it takes the host in the info button;
- "Update the gateway automatically": the label and the switch, with its
  description in the info button;
- the update dialog: the title, the one button, "Skip this version" and "Remind
  me tomorrow", with "Update anytime from Settings > About." in the info button.

The "re-run the installer manually" fold-out and the approval countdown are gone
from that path.

## Why

The maintainer ruled it in the same record as the one-click same-channel update
(`docs/decisions/2026-11-26-a-same-channel-signed-update-installs-with-one-click.md`),
which leaves the panel's common path with one step to show at a time.

## Evidence

- The maintainer's ruling, section 3:
  https://github.com/kirodotdev/KiroCrew/issues/43374
- The maintainer's follow-up on the pull request that implements it: each step
  keeps one short line and one button, and its explanations move into info
  buttons (https://github.com/kirodotdev/KiroCrew/pull/43521).
