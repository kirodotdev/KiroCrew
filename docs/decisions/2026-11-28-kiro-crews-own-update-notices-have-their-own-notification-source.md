# Kiro Crew's own update notices have their own notification source

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-28

## Decision

The notices about updating Kiro Crew itself (an update that needs a step from the user, and an agent asking to update the app) arrive on their own source in Settings > Notifications > Sources, labelled "Kiro Crew updates", not on "agent" and not on "App updates"; when the source first appears, its mute, priority and chat route start as the "agent" source's, and notes already in the bell keep the source they were posted on.

## Why

- Muting these notices meant muting "agent", which also silences the notes agents send on purpose.
- "App updates" means new versions of installed apps, so folding these notices into it would change what that source means.
- "Gateway updates" would mislabel an agent's request to update the app; "Kiro Crew updates" covers both.
- Starting from the "agent" settings keeps a mute, priority or chat route someone already set for these notices.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/40765#issuecomment-7234960848 -- the maintainer's decision.
- https://github.com/kirodotdev/KiroCrew/issues/40765 -- the request.
