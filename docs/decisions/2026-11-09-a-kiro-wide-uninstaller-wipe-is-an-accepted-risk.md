# A Kiro-family uninstaller that removes all of `~/.kiro/` is a documented accepted risk, not a release gate

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-09

## Decision

UNINSTALLER-EXCLUDE-CREW is no longer a release gate: a Kiro-family uninstaller
that removes `~/.kiro/` wholesale, and `~/.kiro/crew` with it, is a documented
accepted risk, and the data-home spec lists the protections Kiro Crew owns.

## Why

- Nothing in this repository can constrain another product's uninstaller, and no
  Kiro-family uninstaller owner has confirmed in writing that theirs excludes
  `~/.kiro/crew` or prompts first.
- Releases have shipped data under `~/.kiro/` since v0.1.1 without that
  confirmation, so the gate's "release-blocking" wording did not describe what
  happens.
- The protections that exist are Kiro Crew's own: its uninstall paths keep the
  data home, the `~/.kirocrew.breadcrumb` pointer, `KIROCREW_HOME`, and
  `snapshot_dir` outside `~/.kiro/`.
- The ruling is reversible: the issue is reopened if a Kiro-family uninstaller is
  found to remove `~/.kiro/` wholesale.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/355 -- the gate.
- https://github.com/kirodotdev/KiroCrew/issues/355#issuecomment-6730133114 --
  the maintainer decision on the issue.
