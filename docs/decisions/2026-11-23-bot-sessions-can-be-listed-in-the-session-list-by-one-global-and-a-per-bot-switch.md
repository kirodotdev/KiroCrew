# Bot sessions can be listed in the person's Sessions list by one global switch and a per-bot switch

Decided by: Ray Xu (maintainer, @buluoray)
Date: 2026-11-23
Supersedes: 2026-11-16-a-bots-sessions-open-in-its-side-panel-and-always-leave-the-session-list.md

## Decision
A global **Show all bots in my Sessions** switch (`dashboard.show_bot_sessions_in_list`, off by default) lives in Settings > Chat > Sessions and at the top of every bot's Sessions list (Profile > Sessions and the bot page side panel's Sessions tab), and a per-bot **Show {name} in my Sessions** switch (`show_sessions_in_list`, off by default) sits under it there; while the global switch is on every bot's sessions are listed in the person's Sessions list, each in its bot's folder, and the per-bot switch shows on and cannot be pressed; while it is off each bot's own switch decides; this supersedes, in part, `2026-11-16-a-bots-sessions-open-in-its-side-panel-and-always-leave-the-session-list.md` (its "the per-Bot **Show in my Sessions list** switch is removed, so a live Bot thread's sessions always leave the person's own session lists and a stored `show_sessions_in_list` value is ignored" clause), and every other rule of that entry stands, the side panel's Sessions tab included.

## Why
- An operator running five bots, each on its own context, switches between their sessions from one Sessions list; without a switch they must open each bot's page in turn.
- One global switch covers every bot at once; with it on, per-bot switches do not apply, because mixing the two is too hard to manage.
- Labels must never be cut off, so these switch labels wrap.

## Evidence
- The issue asking for an opt-in, with the operator report: https://github.com/kirodotdev/KiroCrew/issues/40445
- The maintainer's decision on the issue: https://github.com/kirodotdev/KiroCrew/issues/40445#issuecomment-7112083340
- The pull request that adds this entry: https://github.com/kirodotdev/KiroCrew/pull/41536
