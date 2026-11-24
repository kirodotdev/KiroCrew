# The whole-install import names the apps this machine has not set up and installs none

Decided by: Bolin Chen (bolichen97), Kiro Crew maintainer, 2026-11-23
Date: 2026-11-23

## Decision

A whole-install export records in its manifest the apps its notice names, each with whether it was on. When that archive is imported, the Import / Export card names each recorded app that this machine does not have, or has off though it was on where the export was made, and says to install it again or turn it on and that the archive holds no app data. An app that was off there and is installed here is not named. The import installs no app and copies no app data, and an archive with no list, or a memory-only import, names none.

## Why

- The export's notice is shown once, on the old computer, and is gone when the page is left; the new computer is where the person installs or turns on each app again, and before this the import said nothing about apps. Agent templates are already named the same way at both ends.
- An app that was off there and is installed here is left unnamed so a restore onto the same computer does not ask the person to turn on an app they turned off.
- The line says the archive holds no app data, not that the data stayed on the other computer: after a restore onto a wiped computer, or of an app turned off after the export, that would be false.
- Options not chosen: installing each app on import (unchanged from the export-side decision: it clones, builds and runs each app's install script inside one import and still moves none of its data); carrying app data (which part of an app's data belongs to one computer or is a secret needs a design first).
- Not decided here: carrying app data. The issue stays open for it.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/37140#issuecomment-7107163556 -- the decision, as a maintainer listed in `MAINTAINERS.md`.
- [2026-11-22-the-whole-install-export-names-the-apps-it-leaves-out.md](2026-11-22-the-whole-install-export-names-the-apps-it-leaves-out.md) -- the export side, which left the import side undecided.
- https://github.com/kirodotdev/KiroCrew/issues/37140 -- the report.
