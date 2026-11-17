# A renamed bot keeps its workspace, and the Workspace tile shows the bot's current name

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-11

## Decision

Renaming a bot renames nothing else (its workspace name, the folder on disk, its
chat thread and its memory store keep the names they were created with), and the
Workspace tile on the bot's Profile shows the bot's current name only when the
stored workspace is exactly `member-<the bot's id>`, with the stored name as the
tile's tooltip; any other workspace (`default`, a named workspace, one made for
another bot, or a `member-` name shortened or numbered at creation) keeps
showing its stored name.

## Why

- The workspace name is a key that session history search and other bots rely
  on, so renaming or moving it risks hiding past sessions or losing writes in a
  folder that is in use.
- Opaque folder names for new workspaces were considered too; they would change
  how every new workspace is named and do nothing for the bots that already
  exist.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/32466#issuecomment-6784485309 --
  the maintainer's ruling.
- https://github.com/kirodotdev/KiroCrew/issues/32466#issuecomment-6784886260 --
  the exact rule it ships with.
- https://github.com/kirodotdev/KiroCrew/issues/32466#issuecomment-6712999314 --
  the three options it chose between.
- https://github.com/kirodotdev/KiroCrew/issues/32466 -- the request.
