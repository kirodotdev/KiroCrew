# A person can end a Bot's held limits from its Profile tab, and until then each one keeps asking

Decided by: Ray Xu (maintainer, @buluoray); placement and wording by Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-17
Supersedes: 2026-11-14-a-bots-upgrade-move-carries-its-limits-or-holds-them-for-a-person.md (in part: a held limit makes the actions it covers ask a person until a person ends it on the Bot's Profile tab)

## Decision

While a Bot holds a limit that makes it ask, its Profile tab shows a section headed "Asks you first", under About, listing each held limit in plain words with the actions it covers, and one button, "Stop asking for these", whose confirmation names every limit that makes it ask; confirming ends all of that Bot's held limits at once, only while they are still the ones shown, after which the Bot's approval settings decide those actions; only a person using the dashboard as its owner or as a Shared Crew teammate can do it, never a Bot, tool, sub-agent, schedule or app, and each release writes one security event log row naming the Bot, its member id, the limits and who released them. A held limit that never makes the Bot ask is not listed, and the release ends it with the rest; a limit's text is shown with its credentials redacted. Turning a held limit into a rule its worker enforces is not part of it.

## Why

- Nothing cleared a hold: a user who had read the held limits and no longer needed them had to edit `migrated.json` by hand, and changing the Bot's approval mode is no way out, since an `auto` schedule's approval does not depend on it.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/37321 -- the maintainer's request: each held limit shown on the Bot's page, a separate, clearly labelled action that names the limits it ends, an audit row, and only that Bot's entry cleared.
- https://github.com/kirodotdev/KiroCrew/issues/37321#issuecomment-6943974121 -- the decision on placement, wording and who may release, as maintainer.
- https://github.com/kirodotdev/KiroCrew/pull/35011 -- the change that made the hold.
