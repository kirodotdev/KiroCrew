# The person may delete an empty folder inside a Bot's home folder, and the empty-folder cleanup offers it

Decided by: Bolin Chen (maintainer)
Date: 2026-11-17

## Decision
The person's browser, and only it, may delete an empty sub-folder inside a Bot's home folder and the empty-folder cleanup may offer such folders, while the home folder itself stays the Bot's.

## Why
A busy Bot makes many short-lived sub-folders that the person could not remove, and the cleanup action silently skipped them. Bolin approved the design in `docs/request-for-change/rfc-bot-subfolder-person-delete.md`, saying that the person, and only the person, may delete an empty sub-folder under a Bot's home folder, that "Clean up empty folders" may offer those, and that the home folder itself stays the Bot's.

## Evidence
- Design sign-off: https://github.com/kirodotdev/KiroCrew/pull/38152#pullrequestreview-5947865558
- RFC: https://github.com/kirodotdev/KiroCrew/pull/38152
- Report: https://github.com/kirodotdev/KiroCrew/issues/37434
