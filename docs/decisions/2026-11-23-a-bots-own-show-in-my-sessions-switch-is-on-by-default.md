# A bot's own Show in my Sessions switch is on by default

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-23
Supersedes: 2026-11-23-bot-sessions-can-be-listed-in-the-session-list-by-one-global-and-a-per-bot-switch.md

## Decision
A bot's own **Show {name} in my Sessions** switch (`show_sessions_in_list`) is on by default, so a bot whose config does not set it lists its sessions in the person's Sessions list, in its bot's folder, until the person turns it off; this supersedes, in part, `2026-11-23-bot-sessions-can-be-listed-in-the-session-list-by-one-global-and-a-per-bot-switch.md` (its "`show_sessions_in_list`, off by default" clause), and every other rule of that entry stands, the global **Show all bots in my Sessions** switch staying off by default.

## Why
- Operators lost their bots' sessions from their Sessions list when the per-bot switch was removed, and the restored switch kept them hidden until each bot was turned on by hand.
- With the per-bot switch on by default and the global switch off, every bot lists its sessions and each bot can still be turned off on its own.

## Evidence
- The pull request that restored the switches: https://github.com/kirodotdev/KiroCrew/pull/41536
- The maintainer's decision on this entry's pull request: https://github.com/kirodotdev/KiroCrew/pull/41832#issuecomment-7123250970
