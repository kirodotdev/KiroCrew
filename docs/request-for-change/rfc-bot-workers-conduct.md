---
title: Bot workers that conduct — an owner-set, default-off switch per Bot
status: accepted
author: kmurtaza-dev
created: 2026-11-21
last-audited: 2026-11-22
audited-at: fc2c76715b
doc-pr: 40538
implementation-prs: []
tracking-issues: []
supersedes: [rfc-conductor-split-default.md]
superseded-by: []
---

# RFC: Bot workers that conduct — an owner-set, default-off switch per Bot

- Status: accepted — by maintainer bolichen97 on
  [#40538](https://github.com/kirodotdev/KiroCrew/pull/40538)
  ([comment 7085631323](https://github.com/kirodotdev/KiroCrew/pull/40538#issuecomment-7085631323),
  2026-11-22). Nothing is built yet: Phase 1 is `Part of #40535` and Phase 2 is
  `Closes #40535`. Every "exists today" claim below names a symbol verified
  against the working tree at `fe326b45c8`; re-verify at implementation time.
- Author: kmurtaza-dev
- Related: [rfc-mate-owned-agent-config.md](rfc-mate-owned-agent-config.md)
  (the two roles a Bot runs as, §5.0, and the worker-extension file, §5.1/§5.4),
  [rfc-crewmate-workers-under-the-crewmate.md](rfc-crewmate-workers-under-the-crewmate.md)
  (a Bot's sessions are one tree under its thread), and the open RFC pull request
  [#31281](https://github.com/kirodotdev/KiroCrew/pull/31281) (a per-Bot
  `base_agent`; §9 says how this proposal relates).
- Decisions this asks a maintainer to revisit (§8):
  [2026-11-09](../decisions/2026-11-09-only-a-bots-own-thread-hands-work-to-its-worker.md),
  [2026-11-04](../decisions/2026-11-04-conductors-stay-simple-and-new-abilities-go-to-workers.md),
  and the sentence "A worker does not promote itself" in
  rfc-mate-owned-agent-config.md §5.0.

## 1. Summary

A Bot hands every task to one worker, and that worker does the whole task
itself. This RFC adds one owner-set, default-off switch per Bot,
`agents.<name>.worker_conducts`. When it is on, each worker the Bot's own thread
opens can run as a conductor for its item: record sub-items in a work ledger of
its own, open further workers **of the same Bot** for them, patrol those workers
and verify their results, then report the item back to the Bot's thread exactly
as a worker does today. Those further workers are ordinary workers of the Bot:
same identity, memory store, permission rules, deny rules, harness pin, Profile
approval posture and `Workers at once` limit. The nesting is one level deep —
the Bot's thread, its conducting workers, their plain workers — and the work
ledger's existing depth cap holds it there. While the switch is on, the Bot's
`Workers at once` limit always applies: 4 when the owner has set none. A Bot
that leaves the switch off is
not changed in any way: no mount, no grant, no prompt text, no extra process.

What exists today, by role, and what the switch adds:

| | the Bot's thread (`kirocrew-mate-conductor`) | a worker today (`kirocrew-mate-worker`) | a worker with the switch on |
|---|---|---|---|
| session control (`kirocrew-dashboard`: `session_create`, `session_send`, `session_read_message`, `session_status`, `session_stop`, `session_close`, `work_dispatch`) | mounted per session, because its key is `member-<slug>` (`members.is_member_session_key`, `members.member_dispatch_session_server`); on KAS the projection pre-approves the Bot's set (`agent._MEMBER_DASHBOARD_GRANTS`, through `kas_agents.to_client_custom_agent`'s `member_dispatch`) | never mounted (its key is `chat-<n>-<ts>`) | the same mount entry, for the sessions the thread marked; pre-approved verbs are the conductor-mode set (`agent._CONDUCTOR_DASHBOARD_GRANTS`) plus send/close into its own children (`agent.conductor_own_child_verbs`) |
| work ledger (`kirocrew-work`) | mounted, conductor grants (`agent._LEDGER_CONDUCTOR_WORK_GRANTS`) | mounted, `work_brief` + `work_report` only (`agent._WORKER_WORK_GRANTS`) | the conductor's ledger verbs added to its grants |
| conductor rules and the `goal-conductor` procedure | `conductor.patrol` piece, rendered from the shared rules (`context_assembly.member.ledger_rules_block`) | the worker protocol only (`mate_worker.mate_worker_prompt`) | one added prompt piece rendered from the same shared rules |
| whose worker it dispatches | always its own (`session_create._hands_off_to_own_worker`) | its unnamed child inherits the worker; a named template escapes the Bot's worker template | always its Bot's own worker, whatever `agent` it names |

## 2. Motivation

### Current state

A Bot (crewmate) is two source-defined agents plus its own file
(rfc-mate-owned-agent-config.md): the thread it chats on, which dispatches, and
the worker it hands work to, which does the work. The split is deliberate and
is enforced at four places:

1. The worker base spec mounts no `kirocrew-dashboard`
   (`service_agents._install_mate_worker_agent`; the server is `opt_in` in
   `agent._MANAGED_MCP_SERVERS`, which no spec-writing loop emits) and grants
   only `work_brief` and `work_report` on `kirocrew-work`
   (`service_agents._mount_work_ledger` with `agent._WORKER_WORK_GRANTS`).
2. The per-session dashboard mount is keyed on the thread's session key:
   `members.is_member_session_key` is what both array composers test
   (`acp_runtime_start._member_session_key`, read by
   `runtime_session_create.create_session`; and
   `client_mcp_array` on the per-chat client hosts), and the KAS projection
   widens `tools` and `allowedTools` only on that answer (the `member_dispatch`
   argument of `kas_agents.to_client_custom_agent`).
3. `session_create` forces a child onto the Bot's worker template only when the
   caller is the Bot's thread (`session_create._hands_off_to_own_worker` and
   `session_create._conducts_for_a_worker`, both reading
   `session_create._dispatching_crewmate`, which answers `None` for a template
   caller by design). A worker that does hold the tools (a Bot whose file adds
   the server) opens children that inherit the worker when unnamed, and lands
   them on another template when it names one — outside the Bot's worker
   template and so outside its `Workers at once` count (#37538).
4. The Profile's approval posture is seeded onto a child only for an own worker
   (`profile_worker` in `session_create.create_session`); any other child
   inherits its creator's live posture (`inherit_posture`).

Server-side, a worker IS already admitted to session control: a chat slot on a
Bot's private store is a `_member_caller` (case (b),
`caller_access._member_caller`, `caller_access._store_is_member_owned`),
creator-fenced (`caller_access._caller_is_ownership_fenced`), and
[session-control.md](../system-specs/modules/session-control.md) ("A
member-created worker can itself dispatch — the nested-conductor design")
records nested dispatch from a member-created worker as INTENDED ("the conductor
model is recursive by design ... depth is bounded elsewhere"). The ledger's
depth machinery is also already there: a session bound to an item that opens its
own ledger gets `depth = parent.depth + 1` (`work_ledger_conductor._bootstrap`),
refused past `work_ledger.max_depth` (default 2, bounds 2–10,
`work_vocab.WORK_MAX_DEPTH_DEFAULT`; `service_sections.WorkLedgerConfig`), and
conductor mode already dispatches a second-level conductor this way
(`builtin_skills/goal-conductor/SKILL.md`, "Which agent" and "When a conductor
dispatched you"). The hybrid "does the work and conducts" agent exists for
conductor mode as `kirocrew-team-lead` (`service_agents._install_team_lead_agent`,
`service_agents._mount_team_lead_dispatch`).

So what is missing is not authority or machinery. It is that a Bot's worker
never receives the tools, the grants, the procedure, and the forcing that would
make its children the Bot's own workers.

### Problems

- A task that is three or four independent pieces ("compare these three
  libraries", "fix the same defect in four services") runs serially in one
  worker, with one context window, and the person is the only one who can split
  it — in the Bot's chat, by hand, before dispatch.
- The Bot's thread cannot split it either without doing the planning work in
  the thread, which the front-desk rule tells it to keep light
  (`crewmate_prompts.json`, `conductor.how_you_work` item 2).
- A Bot built from a template whose file adds `kirocrew-dashboard` already gets
  a worker that can open children, but with none of the conductor's grants or
  rules, and with children that escape the Bot's worker template and worker cap
  (#37538). The capability exists by accident in a half-finished shape.

### What the person gets

Nothing is built, so the times below are estimates from the shape of the work,
each with its assumptions stated, not measurements.

- **A split task finishes sooner.** A task of k independent pieces, each about
  t of work for one worker, takes about k × t in one worker today. With the
  switch on the pieces run at once, as far as `Workers at once` leaves seats:
  about t, plus the time the conducting worker spends splitting, seeding and
  checking. Estimate, assuming four independent pieces of about 20 minutes each,
  the default limit of 4 (the conducting worker takes one piece and three
  children take the others) and 5 to 10 minutes of coordination: roughly 25 to
  30 minutes instead of about 80. A request-rate limit on the account slows
  every parallel session, so the gain shrinks when the account is throttled.
- **One ask instead of a hand-split.** For the same parallelism today the person
  splits the task in the Bot's chat: k asks, then k reports to read and combine.
  With the switch on it is one ask and one report. The split is made by the
  worker that owns the item, with its files and context in front of it, and the
  Bot's thread stays a front desk.
- **A context window per piece.** A long task no longer fills one worker's
  context and compacts mid-task: each piece runs in its own session, and the
  conducting worker keeps only the plan and the results.
- **Results checked before they count.** Each piece's `done` is checked against
  its own acceptance (`accept_eval`) before the item is reported done; today a
  worker reports its own result.
- **Nothing else to set up.** Every session is still the Bot: identity, memory,
  permissions, limits and folder.

### Use cases

1. **Compare three libraries (read-only), end to end.** The person asks the Bot:
   "Which of these three YAML parsers should our CLI use? Compare license,
   release activity, parse time on our 2 MB fixture, and fit with
   `config/load.py`." The Bot's thread opens one worker for it, as it does
   today. With the switch on, that worker:
   1. records three sub-items in its own ledger, one per parser, each with the
      same acceptance: one filled row (license, last release, median parse time
      of the fixture, API notes) with a source for every cell;
   2. opens three workers of the same Bot (the three seats the default limit
      leaves beside itself), seeds each with its parser and that acceptance, and
      arms its work-ledger patrol;
   3. each child reads its parser's repository and documentation, times the
      fixture in a scratch directory, and reports `done` with its row;
   4. the conducting worker checks each row against the acceptance
      (`accept_eval`); one row lacks a source, so it sends that child back
      (`session_send`), and the child adds it;
   5. with all three rows accepted, it writes the comparison and a
      recommendation, closes the three children (`session_close`) and reports
      `done` to the Bot's thread, which answers the person.

   The person sent one message and read one answer, and the three reads ran at
   once.
2. **Fix the same defect in four services, one repository each.** "Our four
   services treat a 429 from the billing API as fatal; retry it with backoff,
   add a test, one pull request per service." The conducting worker records one
   sub-item per service, opens three children and takes the fourth service
   itself. Each child works in the Bot's worktree of its own repository
   (`crewmate_worktree.ensure` keeps one per Bot per repository), so no two of
   them share a working tree. Each reports `done` with its pull request, which
   the conducting worker checks before it reports the four to the thread.
3. **Release notes from a month of merged pull requests (read-only).** Three
   children summarize one area each (API, UI, docs); the conducting worker
   merges the three summaries into one set of notes and checks that every pull
   request appears once.

Leave the switch off for:

- tasks one worker finishes in a few minutes: the split, seeds and checks cost
  more than they save (§11);
- steps of one change that each need the previous one's output: nothing can run
  at once;
- several code changes in one repository at once: a Bot has one worktree per
  repository, and all its workers run in it (§11);
- work that needs the person's approval at many steps while they are away: each
  child waits on its own prompt (§11);
- a Bot pinned to the claude backend, where the own-child exemption does not
  apply and every seed and close prompts (§10);
- a Bot whose spend matters more than its elapsed time (§11).

## 3. Goals

1. One owner-set switch per Bot, off by default, that makes the Bot's direct
   workers conductors of their own item.
2. Every session in the Bot's tree stays the Bot: identity, memory, permission
   and deny rules, harness pin, Profile posture, `Workers at once`, folder, and
   the Sessions tab and Crew panel listing (`crew_mode.stamp_rows`,
   `mate_root` / `mate_dispatched_by`).
3. Nothing changes for a Bot with the switch off, byte for byte, in the worker
   spec it projects and the session array it is given.
4. No new nesting machinery: the ledger's depth cap, the creator fence, the
   Bot's caps and the governance ceiling are the bounds. The one addition is
   that `Workers at once` fails closed while the switch is on (§5.5).
5. The conductor rules are written once and rendered, never copied
   (`test/test_mate_conductor_parity.py`).

## 4. Non-goals

- Making any chat other than the Bot's own thread and its marked workers a
  dispatcher of the Bot's worker (#33996 is that question; §8).
- Changing the Bot's thread (its conductor) in any way.
- Nesting deeper than one level by default, or changing `work_ledger.max_depth`.
- Letting a worker decide for itself to become a conductor ("promote itself").
- Counting a worker's children on *other* templates toward `Workers at once`
  (#37538, owned elsewhere; with the switch on such children do not arise, §5.4).
- A new approval surface. Approvals are the Profile's and the person's, as today.
- Any change to `dashboard/state_parts/bot_worker_cap.py` (#37538 edits it).

## 5. Design

### 5.1 The switch

`agents.<name>.worker_conducts: bool`, default `false`, a field of
`KiroCrewAgentConfig` beside `dynamic_capabilities`
(`config/sections_session.py`), read with the same `_safe_bool` reader in
`config/loader_pipeline.py`, accepted by `PUT /api/agents/<name>`
(`agent_admin/crew_update.py`, `api_kirocrew_agent_update`, on the
`dynamic_capabilities` bool precedent; a non-bool is
`400 invalid_worker_conducts`), returned by `GET /api/members`
(`handlers/members_roster.py`). It is owner-set: the Bot cannot change it (the
same rule as `max_workers`). The key, the API and the behaviour are Phase 1;
the Profile switch is Phase 2 (§6).

Settings text for the Profile switch (English; the eleven other shipped
catalogs are translated in the same commit and `en-XA` regenerated, per
[i18n-catalog.md](../../website/docs/i18n-catalog.md)):

- label: "Workers can conduct"
- switch: "Let {{name}}'s workers hand parts of a task to further workers"
- hint: "On: each worker {{name}} opens can split its task into tracked items,
  open further workers of {{name}} for them, follow them and check their
  results. Those workers keep {{name}}'s identity, memory, permissions and
  limits and count toward Workers at once. While this is on and Workers at once
  has no limit, at most 4 of {{name}}'s workers are open at once. Off (the
  default): a worker does its task itself."

### 5.2 Which sessions conduct: marked at create by the Bot's thread, switch read at start

The lineage is recorded **once per create or fork, when the caller is the Bot's
own thread** (a `session_create` or a `session_fork`;
`members.is_member_session_key` on the caller's slot key, the same test the
restore feeder below applies to the verified `created_by`): the child is marked
*thread-born*. The switch itself is read at each marked worker's session start
(below), never stored on the mark, so a worker minted while the switch was off
conducts at its next start once it is on, the same way when the reload below
resumes it as after a gateway restart. The mark is a members-level
in-memory registry beside the backend-pick table
(`members_parts/member_sessions.py`, `_SESSION_BACKEND_PICKS`, written at mint by
`session_create.create_session` through `members.note_session_backend_pick` and
at restore by `metadata_codec._read_backend`), fed three ways:

- at mint, right where the backend pick is noted, by `session_create` and by
  `session_fork`, from the caller's key: the thread's fork of its worker is
  thread-born, and any other caller's fork is plain;
- at restore, from the slot's verified `created_by`
  (`metadata_codec._read_created_by`): a worker slot whose verified creator is a
  member DM key (`members.is_member_session_key`) is thread-born. An unverified
  creator (`UNVERIFIED_CREATOR`) is not, so a restart fails closed;
- at a revive, from the same verified creator `session_revive` restores, so a
  revived worker is thread-born whether or not the gateway restarted since it
  was archived.

A History resume restores no creator, so it drops the mark: the worker it
reopens is plain whether or not the gateway restarted in between.

A session conducts when all three hold at its start: it is a crewmate worker this
process knows (`members.is_crewmate_worker_session`, filled from the attested
execution record), it is thread-born, and its Bot's record has `worker_conducts`
**now** (a cached `KiroCrewConfig.load()`, failing closed on an unreadable or
degraded config, the way `caller_access.member_dispatch_enabled` does). Reading
the switch again at every session start means a change to it takes effect at
each worker's next start, including when the reload below resumes it; nothing
about a running worker changes mid-turn, except the bound of 4 (§5.5), which
starts the moment the switch is on, even for a worker that is already running.

Turning the switch off reloads the Bot's workers so that start comes soon, the
way a change to the Bot's policy reloads them
(`crewmate_worker_reload.schedule_reload_workers`): an idle worker restarts at
once and a busy one at the end of its current turn, each keeping its
conversation, and from then on it is a plain worker. Until then a running
conducting worker keeps all of it, the kit and the parts read per call alike:
its children are still the Bot's workers with the Profile seed, its own-child
sends and closes are still exempt, and the bound of 4 (§5.5) still holds for
its creates. Turning the switch off closes nothing. Turning it on reloads the
workers the same way, so each becomes a conducting worker at its next start.

Why thread-born and not "every session of the Bot": it is what makes "one level
deep" a property of lineage rather than of a cap someone can raise. A conducting
worker's children are created by a `chat-` key, so they never carry the mark,
whatever `work_ledger.max_depth` says (§5.5).

### 5.3 What a conducting worker receives

| piece | where it is decided | what |
|---|---|---|
| session control | the two composers that mount it for the thread, through the one branch each already has: `runtime_session_create.create_session` (and the load path, so a `session/load` keeps it), which the provider keys for both populations as `acp_runtime_start._member_session_key` keys the thread (`members.mounts_session_control(key)`: a member DM key, or `members.conducting_worker_session(key)`), read off the event loop through the cached config load the worker's spec reads, so the mount and the spec agree; and the client array composer in `acp/client_mcp_array.py`, which reads the same predicate. A session that is neither a thread nor a conducting worker is keyed on neither path and enters the branch on neither (harness parity H13). | `members.member_dispatch_session_server(session_key, token)`: the same entry, identity env (`KIROCREW_SESSION_KEY`, the signed session token, `KIROCREW_BOUND_PORT`) and withholds (`ACP_BACKENDS_MEMBER_DISPATCH`, the server's `disabled` switch-off) as the thread. The crew **panel** (`runtime_session_setup._mount_member_panel`, and the client composer's `_append_member_panel_server`) keeps its own thread-only test (`members.is_member_session_key`) and is never mounted for a worker. |
| KAS grants for it | `runtime_session_create.create_session` hands `kas_agents.to_client_custom_agent` the same `member_dispatch` flag it hands for the thread (it already follows the mount and its withhold), and the projection picks the grant set by the session's population: a mounted thread-born worker of the Bot (`members.is_thread_born_crewmate_worker(session_key)`; its start took the mount only because the switch was on, and the switch is not read a second time, so a save between the mount and the projection cannot widen its set) takes the conductor-mode set below, every other mounted session the thread's `agent._MEMBER_DASHBOARD_GRANTS` | `@kirocrew-dashboard` joins `tools`; `agent._CONDUCTOR_DASHBOARD_GRANTS` (`session_create`, `session_read_message`, `session_status`) join the ceiling-filtered `allowedTools` — the verbs conductor mode already grants its conductors, and no more. The seed and the close of a child go through `session_send` / `session_close`, which stay gated and are approved without a prompt only for a session the caller itself created: the own-child exemption (`tool_approval._conductor_own_child_call`, switched by `agent.conductor_own_child_verbs`, vouched lineage through `tool_approval._vouched_creator_of`) today admits a caller by `slot.agent` (`kirocrew-conductor`, `kirocrew-team-lead`), so it admits a conducting worker too, by the same kit test (`mate_worker_conducts.holds_conducting_kit`) and never by the worker template's name; every other target and verb (`session_stop`, `session_retry`, `session_broadcast`, `work_dispatch`) prompts, exactly as for a conductor-mode conductor. The protocol needs no verb beyond these: restarting a stalled child (`session_stop`) is a gated step in conductor mode too. |
| ledger grants | the per-session worker projection every reader uses, `mate_worker.worker_session_spec` (the seam that resolves the session's Bot and calls `resolve_worker_spec`), when `members.conducting_worker_session(key)` holds (marked, known as the Bot's worker, switch on) | `allowedTools` += `agent._LEDGER_CONDUCTOR_WORK_GRANTS` (`work_ledger_read`, `work_ledger_record`, `work_ask_record`, `work_ledger_rebuild`, `accept_eval`; `work_brief` and `work_report` are already granted), through `sanitize_agent_config_governance` as every other grant, so a withheld verb stays mounted and prompts. `agent._WORKER_EXCLUDED_GRANTS` (cron authoring) stay withheld. `kirocrew-work` is already a base server. |
| prompt | the same projection; a new piece `worker.conducts` in `crewmate_prompts.json`, rendered from `agent._LEDGER_CONDUCTOR_RULES` placeholders exactly as `conductor.patrol` is (`context_assembly.member.ledger_rules_block`) | You may split your item into independently acceptable sub-items. Read the `goal-conductor` skill first ("When a conductor dispatched you"); the dispatch order is `work_ledger_record action=create`, `session_create` (pass no `agent`: every child is your Bot's own worker; the goal-conductor skill's "never leave `agent` unset" rule is for `kirocrew-conductor`, whose inherited agent cannot write files, while your create is forced onto your Bot's worker whatever `agent` says), `bind`, then the seed. Your Bot has one worktree per repository and every worker of it runs there: give changes in one repository to one child at a time; children that only read may share it. A child's turn end does not wake you: patrol with `monitor_start` and `work_ledger_read`; verify with `accept_eval`. A `depth_exceeded` on your first `goal` or `create` means you are at the last conducting level: do the item yourself and say so in a `progress` report. Your `work_report` to your Bot is unchanged (`done` only when every sub-item is accepted). If `session_create` is not in your tool list, this Bot's harness could not carry it: work the item yourself and say so. A patrol wake with no news ends with `nothing_to_do` and no text. |
| skills | the same projection | `kirocrew-mate-worker` is a whole-catalog template: a spec with no `skill://` entry inherits every skill (`agent_discovery.agent_skill_globs` with `inherit_defaults`; [memory-skills-hooks.md](../system-specs/modules/memory-skills-hooks.md), "KAS skill scope"), so `goal-conductor` is already reachable and a mapping would narrow the worker to that one skill. The `skill://` URI for `goal-conductor` is added only when the merged spec already carries a mapping (a Bot whose file maps skills), the case where the pointer would otherwise be false. |

Everything else is untouched: the Bot's file additions and restrictions
(`mate_worker.worker_additions`, `mate_worker.merge_worker_spec`), `hooks` /
`permissions` / `toolsSettings` layering (`mate_spec_policy`), held approvals
and revocations, the governance ceiling (POLICY ∩ PROFILE), the Bot's harness
pin (`mate_worker.crewmate_worker_backend`), `spawn_run` for the worker's own
sub-tasks (the spawn refusal is the conductor chat's,
`subagent_parts.agent_policy.conductor_spawn_refusal`).

### 5.4 Children of a conducting worker are the Bot's own workers

`session_create._dispatching_crewmate` answers the Bot's record for one more
caller: a `template` caller that `mate_worker.is_own_worker_execution` accepts
and that holds the conducting kit (`mate_worker_conducts.holds_conducting_kit`:
its thread-born mark and the session-control mount its running handle took at
start), so the answer lasts until that session next starts (§5.2). Its two
readers then answer as for the thread: `_hands_off_to_own_worker` forces the
child onto `kirocrew-mate-worker` as a template on the Bot's store with its
`member_id`, whatever `agent` the
model names (naming another Bot keeps that Bot's refusing resolution, as today),
and `_conducts_for_a_worker` files the child in the Bot's home folder
(`mate_folders.mate_home_filing`). From that one answer the existing code gives
the child: the Bot's harness pin (`mate_worker.crewmate_worker_backend`), the
Profile's `worker_model` / `worker_reasoning_effort`
(`sections_agent_fields.worker_session_defaults`), the Profile approval seed
(`profile_worker`), the `Workers at once` count (`bot_worker_cap.own_worker_member`
already counts every session on the Bot's worker template with its member id —
no edit to that module), and the `mate_root` / `mate_dispatched_by` stamps that
list it on the Bot's Sessions tab and Crew panel (`crew_mode.stamp_rows`).
Identity, memory and permission rules already carry to a template child on the
Bot's store (the template arm of `session_create.create_session`:
`ExecutionContext.with_template` keeps the store and the member id) and are not
redone.

A conducting worker's child is NOT marked (its creator is a `chat-` key), so it
is a plain worker: no mount, no conductor grants, no conducting prompt. This is
the whole of "one level deep" (§5.5), and it is why #37538's gap (children on
another template) does not arise for a conducting worker.

A Bot record with no persisted `member_id` (the id-less member
`_dispatching_crewmate` still resolves by selection name) is left out: its
worker never satisfies `mate_worker.is_own_worker_execution`, so it is never a
crewmate worker this process knows and the switch does nothing for it. That is
the legacy shape only; a `member_id` is assigned when member memory is created.

### 5.5 Depth, caps, memory

**Depth.** The Bot's thread holds the depth-0 ledger. A conducting worker bound
to one of its items opens its own ledger at depth 1
(`work_ledger_conductor._bootstrap`: a bound session's depth is its parent's
plus one), allowed under the default cap (`work_ledger.may_conduct`, cap 2). Its
children would open at depth 2 and are refused `depth_exceeded`, but they never
try: they are not marked and carry no ledger grants. Raising
`work_ledger.max_depth` therefore changes nothing for Bots: the lineage rule
holds the tree at one conducting level, and the ledger's cap stays a second,
independent bound. The ledger already bounds one population this way: a code
channel's workers may not conduct at any cap, because, as
`work_ledger_conductor._bootstrap` puts it, "their lineage is" what keeps them
from conducting, not the depth. A conducting worker learns its level from the
ledger's refusal (`work_ledger.depth_cap_hint`), and `work_ledger_read` reports
`max_depth` and each item's `depth`.

**Caps.** `agents.<name>.max_workers` ("Workers at once", 0 = no limit, 1–32,
`sections_session.BOT_MAX_WORKERS_MAX`) counts every open session on the Bot's
worker template with its member id, so conducting workers and their children
share one Bot-wide budget; a create past it is refused `bot_worker_cap_reached`.
`session.max_per_creator` (default 50,
`sections_defaults.DEFAULT_MAX_PER_CREATOR`) bounds each creator separately: the
thread and each conducting worker.

With the switch on, that budget fails closed rather than resting on the owner
reading a hint (settled in review, §13): when `max_workers` is 0, the Bot's
effective `Workers at once` is 4, a named constant added in Phase 1
(`sections_session.BOT_MAX_WORKERS_CONDUCTING`, beside `BOT_MAX_WORKERS_MAX`); a
limit the owner set is used as is, and with the switch off nothing changes. The
setting is not refused; the switch's hint says which limit applies (§5.1). The
three verbs that ask the Bot's limit today, `session_create`, `session_revive`
and `session_fork`, each through `bot_worker_cap.worker_cap_refusal`, ask a
Phase 1 helper instead, `mate_worker_conducts.effective_worker_cap_refusal`:
for a Bot with the switch on and no limit set (and, until it restarts, for a
create, revive or fork whose caller holds the conducting kit,
`holds_conducting_kit`, §5.2) it calls the existing
`bot_worker_cap.bot_worker_cap_refusal(state, member_id, cap)` with that
constant, and in every other case it is `bot_worker_cap.worker_cap_refusal`
itself, so nothing in `bot_worker_cap.py` changes (§4) and a Bot with the
switch off, once its workers have restarted, runs exactly today's check.

**Memory.** Every level runs on the Bot's one V2 store, as the thread and its
workers already do: no new store, no new writer kind, only more concurrent
sessions, bounded by `Workers at once`. Each session's startup envelope is
capped per session (`member_essential_context.ESSENTIAL_MAX_CHARS`, 65,000
characters); the cap is not pooled across sessions. Lessons and memory
consolidation are the store's existing per-session append-and-consolidate
paths; this RFC adds no write to them.

### 5.6 Trust and approvals

A Bot's thread opens in `trust` when its Profile never chose a posture
(`members_thread._seed_member_approval`;
`sections_agent_fields.MEMBER_APPROVAL_MODE_UNSET_EFFECTIVE` is `"trust"`), and
its workers are seeded from the same Profile at mint and at every restore
(`profile_worker` in `session_create.create_session`, read through
`crewmate_approval.crewmate_name_for`). With the switch on, a conducting
worker's children are own workers (§5.4) and take the same Profile seed. A fork
the thread makes of its worker follows main's trust rule while the switch is
off: it copies its caller's trust (`trust_carries`). With the switch on, the
thread's fork starts untrusted and takes the Bot's Profile mode on its first
turn, as a created worker does. When a thread-born worker first starts with the
conducting kit, including after the reload that follows turning the switch on,
its posture becomes the stricter of its current posture and the Bot's Profile
mode, so a copied trust never outlives the switch and nothing is ever raised. So
the posture of the whole tree is the Profile's, set by the owner, read again at
every restore; nothing in this RFC changes who approves: a prompt in any session
of the tree is answered by the person, where a worker's prompts are answered
today (the Bot's Sessions tab and the Crew panel's approve route). A conducting
worker has no approval authority over its children; it sees a child's
`blocked reason=approval` report through `work_ledger_read` and relays it to the
Bot's thread through `work_report`, which escalates with `work_ask_record` as it
does for any worker's ask.

Which fence applies: the ownership fence
(`caller_access._caller_is_ownership_fenced`), unchanged. A conducting worker
reaches only the sessions it created, never the Bot's other workers, never the
person's own sessions; its children are fenced the same way at the next hop
(session-control.md, the nested-conductor design). What is pre-approved is the
conductor-mode set (§5.3): `session_create`, `session_read_message`,
`session_status` by grant, and `session_send` / `session_close` only into a
child the caller created, by the own-child exemption the operator can switch
off (`agent.conductor_own_child_verbs`). Every other session-control verb
prompts, so the posture a conducting worker runs at decides those prompts
exactly as it decides a plain worker's today.

### 5.7 Wakes and patrol

A worker's turn end wakes its creator only when the creator is the Bot's thread
(`mate_thread_wakes.reporting_creator`, `is_crewmate_thread`). Two
consequences, both handled by the shared conductor rules the prompt piece
renders:

- A conducting worker's children wake nobody. The conducting worker patrols the
  way every other conductor does: `monitor_start` on the work-ledger watch
  (`monitor_start(watch="work-ledger")`), `work_ledger_read` each wake,
  `accept_eval` on a `done`. The
  default template already grants `@kirocrew-core` whole
  (`config/defaults.json`), so the patrol verbs do not prompt.
- A conducting worker's own turn ends wake the Bot's thread with `[Worker report]`
  turns, as today. A patrol wake with no news ends with `nothing_to_do` and no
  text, which wakes nobody (`mate_thread_wakes`: a worker turn that ends quietly
  reports nothing); the piece carries the shared "speak only for news" rule, so
  the Bot's thread hears one report per real change, not one per wake.

## 6. Migration plan

**Phase 0 — decision (this document).** Exit criteria: a maintainer sets this
RFC to `accepted` on main and records the superseding decision entry under
`docs/decisions/` (§8; only a maintainer writes one). Blocked on nothing.

Two code pull requests follow, each complete and usable on its own: a backend
one and a frontend one. The split is by surface, not by size: a reviewer of the
behaviour does not read twelve catalog files, and the setting is reachable from
day one through `config.json` and `PUT /api/agents/<name>`.

**Phase 1 — backend (one PR, `Part of #N`).** The field, the API, the mark and
its three feeders, the predicate and the two mounts, the KAS grant set, the
projection's grants and prompt piece, the `_dispatching_crewmate` extension,
the own-child exemption's second identity (`tool_approval._conductor_own_child_call`
admits a conducting worker beside the two conductor agents), the effective
`Workers at once` limit and the `max_workers` description that names it
(§5.5), the workers' reload when the switch changes (§5.2), the fork's posture
rule (§5.6), and the docs:
[crew-mode.md](../system-specs/modules/crew-mode.md) ("Conductor and worker",
"The two workers stay in step"),
[session-control.md](../system-specs/modules/session-control.md) ("What a
created child inherits", the nested-conductor design),
[config.md](../system-specs/modules/config.md) (the key and the effective limit),
[agent-mcp-and-skill-reach.md](../architecture/agent-mcp-and-skill-reach.md)
("Mate worker"), [rfc-conductor-own-child-verbs.md](rfc-conductor-own-child-verbs.md)
§5.2 (the admitted callers), and this document's status. Phase 1 implements the
answers settled in review (§13); a different ruling at sign-off may change exit
criteria 1, 5 and 8 before Phase 1 opens. Exit criteria, each a test that fails
on main:

1. `worker_conducts` loads (default `False`), a non-bool is refused at load and
   `400 invalid_worker_conducts` at `PUT /api/agents/<name>`; `GET /api/members`
   carries it.
2. With the switch on, a thread-born worker's projected spec carries the
   conductor ledger grants and the `worker.conducts` piece; with it off, the
   spec equals today's byte for byte.
3. With the switch on, a thread-born worker's session array carries the
   `kirocrew-dashboard` entry and its KAS projection names `@kirocrew-dashboard`
   with exactly `agent._CONDUCTOR_DASHBOARD_GRANTS`, on `session/new` and
   `session/load`; the panel is never mounted for a worker; a worker created by a
   worker gets none of it.
4. A conducting worker's `session_create`, with no `agent` or any template
   named, opens `kirocrew-mate-worker` on the Bot's store with its `member_id`,
   Profile-seeded, counted by `Workers at once`, filed in the Bot's home.
5. A conducting worker's `session_send` / `session_close` into a child it
   created runs without a prompt while `agent.conductor_own_child_verbs` is on,
   and prompts for any other target, for `session_stop`, and with the switch
   off once the worker has restarted (§5.2); an unmarked worker on the same
   template never gets the exemption.
6. The piece renders every shared rule the team lead carries, names only shared
   rules and copies no charter sentence (`test_mate_conductor_parity.py`).
7. A gateway restarted with a marked worker open re-marks it from its verified
   `created_by`, and a `session_revive` of an archived marked worker re-marks it
   from the same verified creator, whether or not the gateway restarted in
   between; an unverified creator re-marks nothing on either path. A
   `session_fork` is marked by the rule a create follows: the thread's fork of
   its worker is thread-born at the fork, after a restart and after a revive,
   and a conducting worker's fork is plain at all three. A History resume
   reopens a marked worker plain, whether or not the gateway restarted in
   between.
8. With the switch on and `max_workers` 0, a `session_create` (from the thread
   or a conducting worker), `session_revive` or `session_fork` that would open
   the Bot's fifth worker is refused `bot_worker_cap_reached`; with the switch
   off it is not (a conducting worker still running from before keeps the bound
   until it restarts, §5.2), and a limit the owner set applies as is.
9. Turning the switch off or on schedules the Bot's workers' reload
   (`crewmate_worker_reload.schedule_reload_workers`): an idle worker restarts
   at once and a busy one at its turn end, each on its conversation; until then
   a running worker keeps everything it started with, the parts read per call
   included, except the bound of 4, which holds for every create from the
   moment the switch is on (§5.5): turning the switch on tightens at once, and
   turning it off loosens only at each worker's restart.
10. With the switch on, the thread's fork of its worker starts untrusted and
    takes the Profile mode on its first turn, and with it off a fork copies its
    caller's trust as today; a thread-born worker's first start with the
    conducting kit leaves it at the stricter of its posture and the Profile
    mode.

**Phase 2 — the switch on the Profile tab (one PR, `Closes #N`).** The row in
`website/src/pages/members/CrewProfileSettings.tsx` on the `dynamic_capabilities`
pattern, the `agents.ts` type, the catalog strings in every shipped language.
Exit criteria: the row renders off by default, its hint names the limit that
applies while Workers at once has none (§5.1), and a flip calls
`updateKirocrewAgent(name, { worker_conducts: true })`; the i18n parity and
pseudolocale tests pass. Phase 1 is usable without Phase 2 through the API and
`config.json`; Phase 2 is abandonable on its own.

## 7. Backward compatibility

Compatible. The key is additive and defaults to off; an older build ignores it.
A Bot with the switch off projects the same worker spec, receives the same
session array and keeps the same worker limit as today (Phase 1 exit criteria 2,
3 and 8 pin this). No persisted
format changes: the mark is derived from `created_by`, which the slot metadata
already persists and verifies. `git revert` fully undoes the change.

Removes nothing.

## 8. Decisions this proposal touches

Three recorded statements say, in different words, that a Bot's worker does not
dispatch. This RFC asks a maintainer to supersede them for the opt-in case, and
argues the owner-set, default-off switch is compatible with each one's intent.

1. **[2026-11-09, only a bot's own thread hands work to its worker](../decisions/2026-11-09-only-a-bots-own-thread-hands-work-to-its-worker.md)**
   (decided on #28546; the open follow-up is #33996). Decision: only a bot's
   own thread hands work to its worker; a chat running the bot elsewhere does
   the work itself. Its two reasons: binding other chats as the bot's own chat
   would change how every session path recognises that chat, and refusing them
   would remove a choice the picker offers. **What this RFC reverses:** with the
   switch on, a second population — the thread's own workers — also hands work
   to the Bot's worker. **What it keeps:** no chat is bound as the Bot's own
   chat; the mount reaches the worker through the member path it already uses,
   keyed on a mark the thread itself wrote at create, and every other chat
   (picker, folder default, channel) is exactly as decided. The population the
   decision excluded (chats outside the thread) stays excluded; the population
   this RFC adds was not before the maintainer then. #33996 is independent: it
   is about binding outside chats, which this RFC does not do.
2. **[2026-11-04, conductors stay simple](../decisions/2026-11-04-conductors-stay-simple-and-new-abilities-go-to-workers.md).**
   Decision: a conductor mounts only Crew's own servers and the skills it names;
   new abilities go to workers; adding capability to a conductor needs an owner
   decision. **What this RFC reverses:** a conducting worker is a conductor that
   carries the user's servers and skills (the Bot's additions), as the team lead
   already does in conductor mode. **What it keeps:** the Bot's own conductor
   (its thread) gains nothing; the simplicity reason — "A conductor only
   analyzes the problem, dispatches tasks and organizes their execution." —
   does not describe a worker that also does the work, which is why the team
   lead exists; and the owner decision the entry asks for is exactly what
   Phase 0 requests.
3. **rfc-mate-owned-agent-config.md §5.0, "A worker does not promote itself."**
   "A worker that finds its task too large reports back to its conductor; the
   conductor decides whether to spin up a sub-conductor. The dispatch tools a
   worker holds and the ones a conductor holds are separate, so the roles cannot
   blur." **What this RFC reverses:** for a Bot with the switch on, a direct
   worker holds both tool sets. **What it keeps:** the worker still does not
   promote itself — the owner did, in advance, for all of that Bot's direct
   workers, and the Bot's thread still decides what to dispatch. That RFC's own
   P1 row ("the conductor assembles a worker per task") already anticipated
   roles chosen above the worker; this is the owner choosing. §5.0 gets a
   one-line amendment pointing here when Phase 1 lands.

The server-side spec already records nested dispatch from a member-created
worker as intended (session-control.md, the nested-conductor design), so what
the three statements govern is product shape, not the fence — which is why this
is an RFC and not a fix.

This RFC also supersedes #40737's no-dispatch rule when the per-Bot switch is
on: the rule that a plain worker never dispatches and a split goes to a child
conductor ([rfc-conductor-split-default.md](rfc-conductor-split-default.md)
§2.5). With the switch on, the Bot's worker may dispatch. With the switch off
(the default), #40737 still holds unchanged.

## 9. Relation to other open work

- **Pull request #31281, a per-Bot `base_agent` (open; status `draft`,
  merge-conflicted since 2026-11-11).** Same module
  (`mate_worker.resolve_worker_spec`, which `worker_session_spec` calls), different layer: it inserts an installed
  agent's current spec between the worker base and the Bot's file; this RFC adds
  gateway-owned grants and a prompt piece on the governed merge, conditioned on
  a mark, and no transport entry. The two are independent and compose: layering
  order base → (base agent) → file → (this kit), each already additive-or-refuse.
  Neither depends on the other; if both land, the kit applies to the merged
  result whatever the middle layer is. Both documents ask a maintainer to
  supersede part of 2026-11-04, for different halves: that one for what a worker
  follows, this one for what a conducting worker may carry. Its tracking issue
  is #31219.
- **#37538 (count a Bot worker's children on other templates toward
  `max_workers`).** Independent; this RFC edits nothing in `bot_worker_cap.py`.
  For a conducting worker the gap does not arise (§5.4); for every other worker
  #37538's fix applies as before. The effective limit (§5.5) calls that module's
  public `worker_cap_refusal` for every create but the bounded ones, and
  `bot_worker_cap_refusal` for those, so a Bot with the switch off keeps
  whatever #37538 changes, and for a conducting worker the gap #37538 closes
  does not arise (§5.4).
- **#33996 (a chat running a bot outside its thread hands work to its worker).**
  Independent population (§8.1); a later design there could reuse the mark and
  predicate this RFC adds, but nothing here presumes it.

## 10. Security considerations

- Authority does not widen: the server-side admission and fence are unchanged
  (`caller_access._member_caller`, `caller_access._caller_is_ownership_fenced`),
  and a conducting worker reaches only the sessions it created. What the switch
  adds is the client-side mount, grants and procedure to a population the server
  already admits, and only when the owner opts in per Bot.
- A conducting worker reads untrusted content (web pages, repository text, the
  output of the Bot's own MCP servers) and can now open sessions without a
  prompt; the tree's posture is the Profile's, `trust` when unset, and a trust
  a fork copied from the thread never outlives the switch (§5.6). The unit of
  consent is the owner's per-Bot switch, and `agent.conductor_own_child_verbs`
  off returns every seed and close to a prompt. The 2026-11-04 decision keeps
  conductors free of user servers so that a conductor stays simple; a conducting
  worker carries them because it is a worker, as the team lead does in conductor
  mode, and the owner, not the Bot, decides which Bot runs this way.
- Pre-approved verbs are exactly the conductor-mode set
  (`agent._CONDUCTOR_DASHBOARD_GRANTS`: create or read, never a mutation of
  something that exists) plus the own-child exemption for `session_send` /
  `session_close` into a session the caller created by vouched lineage, which
  the operator can switch off (`agent.conductor_own_child_verbs`). Every grant
  crosses the governance ceiling; a withheld verb prompts; a conducting worker
  is admitted to the exemption by holding the conducting kit (§5.3), never by
  its template's name, so an unmarked worker on the same template keeps
  prompting.
- Spend: an unattended worker can now start further unattended sessions. With
  no Bot-wide limit, today's defaults would let the thread open 50 workers and
  each conducting worker 50 children (`session.max_per_creator`), 2,550 worker
  sessions in all, so only the 500 live-session total
  (`slot_directory.MAX_LIVE_SLOTS`) would stop the tree. With the switch on,
  `Workers at once` therefore always applies: the owner's limit N, or 4 when
  none is set (§5.5), so the whole tree under the thread is at most N (or 4)
  worker sessions. The one-level lineage rule and the ledger's depth cap bound
  its depth.
- Fail-closed: an unreadable or degraded config, an unverified `created_by`, a
  harness outside `ACP_BACKENDS_MEMBER_DISPATCH`, or a switched-off dashboard
  server each leave the worker a plain worker, logged once. On the claude
  backend the own-child exemption does not apply
  ([rfc-conductor-own-child-verbs.md](rfc-conductor-own-child-verbs.md): its
  permission requests do not say which session sent them), so a Bot pinned to
  it gets the mount and the grants but every seed and close prompts.
- No credential enters any file, log or PR: the mount's identity is the signed
  per-session token and the session key, as for the thread.

## 11. Costs and downsides

What a person pays for the switch, beyond the security bounds in §10. With the
switch on, the Bot's `Workers at once` limit always applies (the owner's N, or 4
when none is set, §5.5), and most of these costs scale with it.

- **More spend per task.** Each child is a full worker session: its own startup
  envelope (up to 65,000 characters,
  `member_essential_context.ESSENTIAL_MAX_CHARS`), its own reads of the material
  it needs and its own turns, on top of the conducting worker's split, seed,
  patrol and check turns. Worst case with the default limit: four worker
  sessions running for the task's length instead of one, about four times the
  token rate while they run, and a total above the serial cost by three more
  startups and the coordination. *Bound:* the limit caps how many run at once;
  the Profile's worker model (`worker_model`) can run the Bot's workers on a
  cheaper model; a quiet cycle of the work-ledger patrol costs no turn
  (`agent._LEDGER_CONDUCTOR_RULES`, §5.7); the switch is per Bot and off by
  default.
- **More sessions to follow.** One ask can open up to four worker sessions at
  once instead of one. *Bound:* they are filed in the Bot's home folder
  (`mate_folders.mate_home_filing`) and listed on the Bot's Sessions tab and
  Crew panel, which list a child of a child too (`crew_mode.stamp_rows` stamps
  every session the thread dispatched, directly or through another); the thread
  hears one report per item, not one per child; closing the Bot's thread closes
  the sessions it manages, down the line (`chat_api/conductor_close.py`).
- **Harder debugging when a child fails.** A failure is one hop further from
  the thread: the child reports to the conducting worker, which relays it.
  *Bound:* there is only one conducting level (§5.2), so a failure is never more
  than two hops from the thread; the child's `blocked` or `question` report is a
  ledger record the conducting worker relays through its own `work_report` and
  escalates with `work_ask_record` (§5.6); the child is its own row on the Bot's
  Crew panel, with its transcript; and the conducting worker's ledger keeps each
  item's status, acceptance and verdict (`work_ledger_read`).
- **Slower answers when a split is not worth it.** Splitting, seeding and
  checking take the conducting worker several turns, and each child's startup
  adds more, before any piece is done. *Bound:* splitting is optional in the
  prompt piece ("You may split", §5.3); the goal-conductor procedure keeps a
  small piece with the conductor ("not worth a session's context"); and an
  owner whose Bot runs short tasks leaves the switch off.
- **Memory-store write contention.** Every session of the tree writes to the
  Bot's one V2 store, which serializes its writes on one connection and lock
  (`VectorMemoryStore`,
  [memory-skills-hooks.md](../system-specs/modules/memory-skills-hooks.md)), so
  more sessions at once means more writes waiting and more episodes to
  consolidate. *Bound:* the writers at once are the same limit a Bot's thread
  can already fill today with its own workers; no new store, writer kind or
  consolidation path (§5.5).
- **Several approvals at once.** Each session of the tree prompts in its own
  session, so up to four approvals can wait at once. *Bound:* they collect on
  the Bot's Crew panel, whose approve route answers them as it answers a
  worker's today, and on its Sessions tab (§5.6); the Profile's posture decides
  how many prompts occur at all; and a child stuck on one reports
  `blocked reason=approval`, which reaches the thread through the conducting
  worker.
- **One worktree per repository.** A Bot has one git worktree per repository
  and every one of its workers runs in it (`crewmate_worktree.ensure`;
  [crew-mode.md](../system-specs/modules/crew-mode.md), "Each Bot works in a
  git worktree of its own"), so two children changing one repository at once
  would share a working tree and a checked-out branch. *Bound:* the prompt
  piece tells a conducting worker to give changes in one repository to one
  child at a time (children that only read may share it), beside the shared
  "one owner per shared file" rule it already renders (§5.3); and §2's use cases
  keep the switch off for parallel edits in one repository.

## 12. Alternatives considered

- **Grant a conducting worker the Bot thread's own dashboard set**
  (`agent._MEMBER_DASHBOARD_GRANTS`: create, read, status, send, broadcast,
  stop, retry, close). The worker is the Bot and the `created_by` fence bounds
  the write verbs, so it would be sound; but it is wider than the conductor
  protocol needs (no step broadcasts, retries or stops without a person
  reading it), and a trust-boundary change should carry the smallest set that
  runs the protocol. Rejected for the conductor-mode set plus the own-child
  exemption (§5.3), which is what the existing conductors already run on.
- **Mount the kit on every session of the Bot and let the ledger's depth cap
  decide who conducts.** Simpler to mark, but then raising `work_ledger.max_depth`
  for conductor mode also deepens every opted-in Bot's tree, and a last-level
  worker holds tools it can never use. Rejected for the lineage mark (§5.2).
- **Declare `kirocrew-dashboard` on the worker's merged spec** (as the team lead
  does on its file). A projection-only entry has no broker stub and no session
  identity on its element; the member path's session-level entry carries both.
  Rejected.
- **Declare the server in the worker base and select it only when on.** KAS
  launches every declared server whether `tools` names it or not
  (`kas_agents_mcp_servers._project_mcp_servers`), so every worker would start
  an extra process. Rejected.
- **Let the Bot's thread choose per item** (`session_create(agent=...)`, as the
  goal conductor picks `kirocrew-conductor` for a decomposable item). A Bot's
  create is forced onto its worker, and the thread would need a second worker
  template to choose from; a per-Bot switch keeps one worker and one decision.
  Could be a later refinement on top of this RFC's mark.
- **Have the Bot's file add the server (Add abilities).** It already half-works
  by accident (§2) and puts a control-plane server in a user-edited file; the
  grants, the rules and the forcing would still be missing. Rejected.

## 13. Open questions

None open. The two questions this RFC put to a maintainer were settled in review
on its pull request; the design as a whole still waits for a maintainer's
sign-off (Phase 0).

1. Should turning the switch on require `Workers at once` to be set? Settled:
   the limit fails closed instead. With none set, the effective limit is 4
   while the switch is on, and the setting is not refused (§5.5). A hint alone
   would leave the bound on an unattended tree to the owner reading it.
2. Should the own-child exemption (`agent.conductor_own_child_verbs`) cover a
   conducting worker? Settled: yes, as proposed (§5.3). Without it every
   dispatch in an unattended worker stalls on an approval nobody is reading,
   which is the stall the exemption was added to end for the conductor-mode
   conductors; it covers only `session_send` / `session_close` into a child the
   caller created by vouched lineage, and the operator can switch it off.
