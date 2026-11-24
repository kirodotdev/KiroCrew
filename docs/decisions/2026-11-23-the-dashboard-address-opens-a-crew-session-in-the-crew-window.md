# The dashboard address opens a remote crew's session in the crew window and never connects

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-23

## Decision

The dashboard address `/chat?crew=<crew id>&crewSid=<session key>` (the route the desktop app
builds from `kirocrew://chat?crew=<crew id>&sid=<session>`) opens that session in the crew window
over the chat page; a malformed link, an unknown id, a crew that is off or has no dashboard, or a
crew list that failed to load shows the same error as the desktop link and opens nothing; unlike
the desktop link it has no connect rule: it never switches to the crew's pane or starts a
connection, so a crew that is not connected opens the window not connected.

## Why

- It reuses the names the crew link already has (`crew` is the crew's id, never its display name)
  instead of a second name for the same id (`crew_instance`, as the issue asked).
- The session is `crewSid`, not `sid`, so a dashboard without this change, or a remote session key
  that equals a local one, never opens the wrong local session.
- Any web page can open a dashboard address, so the address only shows a window: it never starts a
  connection the app would not start on its own, the reason the 2026-11-07 crew deep link entry
  gives for its connect rule.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/41225#issuecomment-7098160049 -- the maintainer's
  ruling.
- https://github.com/kirodotdev/KiroCrew/issues/41225 -- the issue.
- [2026-11-07-a-crew-deep-link-names-the-crew-by-id-and-follows-auto-connect.md](2026-11-07-a-crew-deep-link-names-the-crew-by-id-and-follows-auto-connect.md)
  -- the desktop link this address shares its route with.
