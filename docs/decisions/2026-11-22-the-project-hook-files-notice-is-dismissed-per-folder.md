# The project hook files notice is dismissed per folder, with no config setting

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-22

## Decision

The "This project has hook files in .kiro/hooks that do not run in this session" notice gets a Dismiss link that hides it for that folder and its current set of hook file names, in every chat and tab, shows again in another folder or once a hook file there is added, removed or renamed, and has no config setting.

## Why

- The request was a per-project config setting. The ruling chose a Dismiss link on the notice instead: no config setting, no config scope and no settings row.
- One switch for every folder would also hide the notice for folders and hook files the user has not seen yet, where a file may be a hook meant to block a tool call. So the dismissal covers one folder and its hook file names, and the notice comes back for anything new.
- The choice is a dashboard preference the host backs up, so it survives a fresh browser profile or a reinstalled desktop app while the data folder is kept.
- The mark covers file names only, so an existing file whose content changes stays hidden, the same limit the once-per-chat mark already has.

## Evidence

- The ruling: https://github.com/kirodotdev/KiroCrew/issues/37944#issuecomment-7077621038
- The request: https://github.com/kirodotdev/KiroCrew/issues/37944
