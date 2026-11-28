# A Crew several people use is a Shared Crew, never a Team Crew

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-22

## Decision

Everywhere a person reads it, a Crew several people use is a **Shared Crew** (plural **Shared Crews**), never a Team Crew: the bell notes, the Notifications tip, the Remote Crew and Docker guides, the Settings help for the Kiro relay fields, the tunnel's stop reason and the first-run link to the Docker guide. A sentence that points at the Crew switcher's relay group, or at its Notifications channel, names that group's heading, **Remote Crews**. Translations use the words their catalog already has for those two. Code identifiers, i18n keys and API values (`TeamCrewsGroup`, `group_team_crews`, `TEAM`) keep their spelling.

## Why

- The same feature already reads "Shared" on the switcher rows and "Shared Crews" in Settings and in the relay refusals, so the leftover "Team Crew" gave one feature two names.
- The switcher's relay group is headed "Remote Crews", so a note that said "Open it from Team Crews" pointed at a heading that is not there.
- The switcher group is not renamed "Shared Crews": it also lists Private SSH, SSM, Fargate and personal relay Crews.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/38133 -- the issue asking for one name.
- https://github.com/kirodotdev/KiroCrew/issues/38133#issuecomment-7075623419 -- the maintainer decision and its scope.
