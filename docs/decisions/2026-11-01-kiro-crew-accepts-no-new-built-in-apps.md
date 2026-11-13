# Kiro Crew accepts no new built-in apps; existing built-ins move to dedicated repositories listed in the official registry

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-01

## Decision

No new app is added to Kiro Crew as a built-in: every app, first-party included, lives in a dedicated GitHub repository listed in the official registry (`kirodotdev/KiroCrewApps`), and the built-ins that still ship move to dedicated repositories listed there, in stages, without changing anything for the people who already use them. Each app moves out completely: no part of it (backend, UI, scripts, CI, specs) stays in Kiro Crew core, core never imports or hard-codes a specific app, and a capability an app needs from core is a generic, app-agnostic App Kit seam.

## Why

- A built-in rides the wheel: it grows every install, couples the app's fix cadence to Kiro Crew releases, and cannot be uninstalled.
- The maintainer asked that the docs and `AGENTS.md` forbid built-in apps from now on, with every app in the official registry or a dedicated GitHub repository.
- The official registry's catalog repository holds listing data; an app's code lives in its own repository.
- The move is staged, never one deletion: deleting a built-in orphans the user's record, so a person who already has one of these apps keeps its enabled state, data, secrets, grants and crons, and install, uninstall, enable, disable and update keep working for every formerly built-in app.
- This entry does not say Kiro Crew ships no apps today. The built-ins in the wheel at the time of this decision still ship until each one's own move lands.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/26967 -- the maintainer's tracking issue: move every built-in app out of this repository, listed through the official registry, and requirement 4, "docs and `AGENTS.md` forbid built-in apps from now on: every app lives in the official registry or a dedicated GitHub repository".
- https://github.com/kirodotdev/KiroCrew/issues/26967#issuecomment-6555107898 -- the maintainer's recorded rulings: one dedicated repository per app listed in the official registry, and each app leaves core completely.
- `AUTOSDE.yaml` -- the `no-new-builtin-apps` review rule that holds pull requests to this decision.
