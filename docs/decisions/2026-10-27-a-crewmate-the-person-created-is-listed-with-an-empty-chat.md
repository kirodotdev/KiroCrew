# A crewmate the person created is listed on the Mates page even with an empty chat; the reserved default member only once the person talked to it

Decided by: Ray Xu (owner of the crewmate design, @buluoray)
Date: 2026-10-27

## Decision

The Mates page lists a crewmate the person deliberately created -- on the page, or by approving Mate's `crewmate.create` or `crewmate.import` card -- from the moment it exists, even when its chat is empty and was never opened; a row the product made on its own (template-derived, synced, app-created, a legacy built-in binding) is still listed only once its thread holds a message, and the reserved `default` member is listed only once its thread holds a message the person sent, so a fresh install's Mates page shows only Mate.

## Why

- The empty-thread hide rule, and the `default` row, exist because every agent template used to generate a crewmate the person never asked for. A crewmate the person asked for is not that case, and hiding it read as the create having failed: a crewmate made from Mate's card opens no chat, so it never got a message and never appeared.
- The page's own create already lands in the new chat, where the first welcome gives the thread a message; Mate's card path opens nothing, so the two create paths listed differently for the same act.
- `default` is not a crewmate the person created. A first-run person should see only Mate. A welcome, a scheduled run, an auto-nudge or a relayed message is not the person talking to it, so none of them lists it.
- Out of scope: removing an untouched `default` row or untouched legacy rows on upgrade. `default` is the roster's default crew alias (`cfg.default_agent`): the config loader re-seeds it on an empty roster and re-points the alias to another crewmate when it is missing, the delete route refuses it, and the existing startup prune skips it, so removing it is a separate decision.

## Evidence

- The owner's restatement on the pull request that adds this entry. A maintainer listed in `MAINTAINERS.md` restates it there too; that comment is the other on-record artifact this entry rests on.
- `docs/system-specs/modules/crew-mode.md` -- the roster listing rule this applies.
