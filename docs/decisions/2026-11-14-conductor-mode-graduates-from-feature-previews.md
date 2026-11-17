# Conductor mode graduates from Feature previews and is on by default

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-14
Supersedes: 2026-10-29-crew-mode-ships-behind-a-default-off-feature-preview.md (in part: the default-off preview)

## Decision

Conductor mode leaves Settings > Developer > Feature previews and is on by default in
standard builds: there is no switch for it, and a stored `crew_mode` preview switch is
ignored. "Start New Chats in conductor mode" stays a setting, off by default, and no
longer needs a preview. The LTS edition keeps conductor mode off, by the maintainer's
ruling.

## Why

- The maintainer ruled: "we are moving conductor mode out of Feature previews; it is
  on by default."
- The maintainer ruled that the LTS edition keeps conductor mode off.

## Evidence

- https://github.com/kirodotdev/KiroCrew/pull/37550#issuecomment-6891060952 -- the
  maintainer's ruling, restated on the pull request that adds this entry.
- https://github.com/kirodotdev/KiroCrew/issues/37536 -- the issue that states the
  ruling.
