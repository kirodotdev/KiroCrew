# The work ledger is a core component, on by default for Bots and conductor mode, and every Bot thread tracks its work in it

Decided by: Joe Guo (maintainer, @iamwhatever)
Date: 2026-11-15
Supersedes: 2026-11-06-every-task-a-bot-dispatches-goes-into-its-work-ledger.md (in part: "a Bot without the ledger keeps its old text"; every other part of that entry stands)

## Decision

The work ledger (`kirocrew-work`) is a core component of Kiro Crew and is on by default for a Bot's own thread and for conductor mode. A switch-off on the default agent no longer reaches either: a Bot's conductor and its worker each mount their own `kirocrew-work` entry, as conductor mode and its worker already do, and the only limit that can narrow it is the governance ceiling withholding a verb, in which case the Bot is still taught the ledger and the withheld verb asks for approval when called. A Bot's own thread always runs in the work-ledger mode, and the cases where it does not today are settled one by one:

| Case | Today | Decided |
|---|---|---|
| L0: own thread, `kirocrew-work` mounted, `work_ledger_record` granted | ledger text, patrol, `goal-conductor` mapped | unchanged |
| N1: the governance ceiling leaves `work_ledger_record` mounted but gated | no-ledger text; the tool exists but is never taught | taught like L0; the gated verb asks for approval when called |
| N2: `kirocrew-work` muted, or limited by `disabledTools` or `excludedTools`, on the default agent | copied onto the Bot's conductor, which then gets the no-ledger text silently | no longer copied: the Bot's conductor mounts its own entry and is taught like L0, as conductor mode is; the governance ceiling still applies as in N1 |
| N3: the Bot's own configuration (not the default agent) excludes `kirocrew-work` or `work_ledger_record` | no-ledger text, silently | keeps the no-ledger behaviour, and the Bot's page and thread show one line saying tracking is off and why |
| N4: the Bot's conductor spec is missing, unsafe or unreadable | no-ledger text, silently | as N3 (fails closed), the line naming the unreadable spec |
| N5: the Bot's binding is not its own file (the default template or another agent) | runs that template's text | moved onto its own file at the next start, as the startup migration already moves other bindings; the bindings that migration keeps in place on purpose stay, listed below |
| N6: an older binding that never moved | as N5 | as N5 |
| N7: the session cannot hand off (no `session_*` mount, or the dashboard mount withheld) | names the ledger tools with no worker to bind | no ledger sentence when the operating-mode block is withheld; shipped by the follow-up PR `fix(crew): close the two conductor-mode rule gaps` |
| N8: the same Bot on another surface (agent picker, folder default, agent channel) | does the work in that chat | unchanged, per `2026-11-09-only-a-bots-own-thread-hands-work-to-its-worker.md` |
| N9: a code-channel session | the channel's own approval mode | unchanged, per the same entry |
| N10: a Bot cron turn | runs on the Bot's worker; `work_brief` answers `not_bound` | unchanged: the cron route reports through its cron title |
| N11: `spawn_run(crew=<Bot>)` | runs on the Bot's worker, `not_bound` | unchanged |

Bindings the startup migration keeps in place on purpose, and that stay exceptions to N5: the reserved `default` member; a row with no `member_id` that is not a crewmate; a Bot named after a spec Kiro Crew generates; a Bot whose id is also the name of a user-level agent Kiro Crew does not own (`2026-10-28-every-crewmate-on-a-user-level-agent-moves-to-the-conductor.md`); a move that would loosen a limit or add a grant (`2026-10-27-every-used-crewmate-moves-to-the-conductor.md`, kept by the 2026-10-28 entry); and a binding the person pointed back by hand after its move, which the migration record keeps from moving again. Those migration rules are about the agent a Bot's worker inherits and are unchanged by this entry.

## Why

- The maintainer's two rulings, given in conversation and restated on the pull request (Evidence): "Bot应该是必须用work ledger的模式才好" (a Bot should always use the work-ledger mode), restated in the first comment; and "work ledger 属于我们的核心组件，对于 Bot 和 conductor mode，这个功能是需要默认打开的" (the work ledger is one of our core components and must be on by default for Bots and conductor mode), restated in the second.
- Conductor mode and its worker already mount a fresh `kirocrew-work` entry on every install, so a switch-off on the default agent never reached them; a Bot's conductor and worker copied that switch-off, so turning the ledger off for ordinary chats silently turned it off for every Bot too.
- A Bot outside the ledger is a second mode the person cannot see: its Sessions tab shows "No report yet" for every task, and nothing on the page says why.
- N1 reverses the reason `spec_has_ledger_tools` states ("a patrol whose every bind waits on an approval is not one to teach"): the server is mounted, so hiding it is what made the Bot another mode, and an approval prompt is the ceiling working as meant.
- A restriction in the Bot's own configuration (N3) or a spec that cannot be read (N4) still wins: teaching tools the Bot does not hold would only produce refused calls, so those cases keep the no-ledger text, and the visible line removes the silence.
- N8 to N11 are not hand-offs from the Bot's own thread; the 2026-11-09 entry and the cron route already define them.

## Evidence

- The audit this table comes from: every way a Bot chat ends up without the ledger, checked on `origin/main` `f1a5a16cae` (2026-11-15), carried in the pull request that adds this entry: https://github.com/kirodotdev/KiroCrew/pull/37611
- The maintainer's restatements on that pull request, the on-record artifacts this entry rests on:
  - https://github.com/kirodotdev/KiroCrew/pull/37611#issuecomment-6895202686 -- a Bot must use the work ledger: a Bot's own thread always runs in the work-ledger mode.
  - https://github.com/kirodotdev/KiroCrew/pull/37611#issuecomment-6895439227 -- the work ledger is a core component, on by default for Bots and conductor mode: a mute or exclusion on the default agent no longer reaches a Bot, and only the governance ceiling narrows it.
- The entry it supersedes in part, and its pull request: https://github.com/kirodotdev/KiroCrew/pull/31770
