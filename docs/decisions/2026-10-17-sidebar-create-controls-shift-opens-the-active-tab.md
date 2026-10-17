# Cmd/Ctrl-click opens a background tab everywhere in the sidebar; Cmd/Ctrl-Shift-click on a create control opens an active tab

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-10-17

## Decision

In the Sessions sidebar, Cmd/Ctrl-click means "open in a background tab" on every control, session rows and create controls alike; on the create controls (the header New button, a folder's "+" button and an empty folder's "New chat in <name>" row) Cmd/Ctrl-Shift-click creates the chat in a new tab beside the current one and switches to it; plain click and middle-click are unchanged, and a host with no tab strip ignores both modifiers.

## Why

- One modifier keeps one meaning across the sidebar, so a session row and a create control never do different things for the same click.
- Cmd/Ctrl-Shift-click is the browser convention for "open in a new tab and switch to it", and it adds a gesture without changing an existing one.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/18544#issuecomment-6231918549 -- the ruling.
- https://github.com/kirodotdev/KiroCrew/pull/18545#issuecomment-6231920572 -- the ruling restated on the pull request that adds this entry.
- https://github.com/kirodotdev/KiroCrew/pull/18545 -- the pull request that implements it.
