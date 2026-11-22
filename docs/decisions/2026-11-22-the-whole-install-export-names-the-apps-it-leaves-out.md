# The whole-install export names the installed apps it leaves out and says their data stays behind

Decided by: Bolin Chen (bolichen97), Kiro Crew maintainer, 2026-11-22
Date: 2026-11-22

## Decision

When a whole-install export starts downloading, the Import / Export card names every app that is on and every app the person installed, on or off, but not a built-in app that is off, and says neither the apps nor their data are in the export; the export carries no app and no app data, and the import installs no app.

## Why

- A new computer silently lacked every app and its data after a whole-install export and import. The export is the one moment that data can still be kept, because the old computer still has it.
- A built-in app that is off is not named, including one that now updates from its own repository: every install registers each built-in app switched off, and nothing on disk tells one the person used and then turned off from one they never opened, so naming them would list twenty or so apps on every export and bury the ones that matter. The user guide says that such an app's data, if any, is not carried either.
- Options not chosen: reinstall each app automatically on import (it clones, builds and runs each app's install script inside one import and still moves none of its data); carry app data (which part of an app's data belongs to one computer or is a secret needs a design first); name every installed app, built-in ones included (twenty or so never-used names on every export).
- Not decided here: carrying app data, and naming or reinstalling apps on import; #37140 stays open for them.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/37140#issuecomment-7075581446 -- the decision, as a maintainer listed in `MAINTAINERS.md`.
- https://github.com/kirodotdev/KiroCrew/issues/37140#issuecomment-7076237052 and https://github.com/kirodotdev/KiroCrew/issues/37140#issuecomment-7076537660 -- the same maintainer's refinements, the second of which sets the rule above.
- https://github.com/kirodotdev/KiroCrew/issues/37140 -- the report.
