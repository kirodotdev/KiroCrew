# The first `kirocrew uninstall` deletes no user data

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-22

## Decision

The first version of `kirocrew uninstall` stops Kiro Crew, removes the
service and says how to remove the program, and has no `--purge`: it deletes
nothing under the data home (`~/.kiro/crew`, or `KIROCREW_HOME` when set) or
the folders beside it.

## Why

- Deleting memory, history and configuration cannot be undone.
- The install guide's "Removing user data" steps already cover it.
- Kiro Crew's own uninstall paths keep the data home, which
  `2026-11-09-a-kiro-wide-uninstaller-wipe-is-an-accepted-risk.md` rests on.
- A `--purge` is a later decision, recorded by an entry that supersedes this
  one.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/38467#issuecomment-7075366841 --
  the maintainer's ruling.
- https://github.com/kirodotdev/KiroCrew/issues/38467 -- the issue.
