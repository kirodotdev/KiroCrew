# A sidebar bot row's "..." menu carries Profile and Remove from the sidebar, on the expanded desktop rail only

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-13

## Decision

Every bot row in the left sidebar's Bots group carries a "..." menu with two items, Profile
(opens that bot's Profile card on its Profile tab) and Remove from the sidebar (takes the row
off the rail), drawn on the expanded desktop sidebar where the rail's other row controls are
drawn, with both actions still reachable on the Bots page on every surface; renaming and
copying a bot are not in it.

## Why

- The menu and those two actions are the smallest change that answers the request and need
  nothing new behind them.
- Renaming a bot is a separate request, kept open rather than on the current plan, and a bot's
  display name is already editable on the Profile card this menu opens.
- Copying a bot needs a decision of its own: a copy has to settle what it does with a bot's
  goal, key results and check-ins, which are kept separately from the bot itself. Bundling
  all three would hold back the part that is ready.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/35646#issuecomment-6865410214 -- the maintainer's ruling.
- https://github.com/kirodotdev/KiroCrew/issues/35646 -- the request.
- https://github.com/kirodotdev/KiroCrew/issues/35645 -- renaming a bot, kept as demand.
- https://github.com/kirodotdev/KiroCrew/issues/36866 -- copying a bot, split out of the request.
