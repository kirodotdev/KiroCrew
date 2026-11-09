# pip on an interpreter older than 3.12 is refused, not given a pre-0.6 release

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-09

## Decision

Every link on a channel's pip index (`feed/<channel>/simple/kirocrew/`) carries
`data-requires-python`: a new link takes the wheel's own `Requires-Python`, and a
link published before the attribute existed is stamped `>=3.12`, even where that
older wheel's METADATA says `>=3.10`.

## Why

- Without the attribute, pip on Python 3.11 skipped every 3.12-only release and
  installed 0.5.0 without a word. With it, pip refuses and prints
  "Requires-Python >=3.12".
- The index serves only the supported 3.12+ line, so the `>=3.12` stamp on the
  pre-0.6 links is deliberate policy, not a METADATA mistake to fix.
  `pip install --ignore-requires-python` is the escape hatch for anyone who
  really wants an old release on an old interpreter.
- Stamping only new links, or copying each old wheel's METADATA floor, both still
  install 0.5.0. Yanking the pre-0.6 links would need a version cutoff in the
  publish step.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/31914#issuecomment-6731390599 --
  the maintainer's ruling.
- https://github.com/kirodotdev/KiroCrew/issues/31914 -- the issue.
