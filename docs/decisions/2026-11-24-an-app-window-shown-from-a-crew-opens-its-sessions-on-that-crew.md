# An app window shown from a crew opens its sessions on that crew

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-24

## Decision

When an app window the desktop app shows from a connected crew asks to show a
session, the dashboard opens that session on that crew, the way a crew link
does, and never a session with the same name on this computer; sessions on a
crew reach an app through the connection the dashboard already holds to that
crew, not through a new relay, port or credential.

## Why

- A session on a crew is owned by that crew, and the local dashboard is only a
  window onto it.
- A crew's session key can also exist on this computer, so opening it as a local
  session shows the wrong conversation.
- The dashboard already holds an authenticated connection to each connected
  crew, so a relay or a second credential adds a mechanism and a credential
  holder without adding anything a person can do.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/39982#issuecomment-7133119451 --
  the maintainer's ruling.
- https://github.com/kirodotdev/KiroCrew/issues/39982#issuecomment-7057497099 --
  the options it chose between.
- https://github.com/kirodotdev/KiroCrew/issues/39982 -- the issue.
