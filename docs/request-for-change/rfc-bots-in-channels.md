---
title: Bots in Channels - a Bot joins a Channels room as itself, with its own memory, rules and workspace
status: draft
author: unstablebrainiac
created: 2026-11-25
last-audited: 2026-11-25
audited-at: c1612bce45
doc-pr: null
implementation-prs: []
tracking-issues: [23808]
supersedes: []
superseded-by: []
---

# RFC: Bots in Channels

**Status:** draft. Nothing in this document is built. Every "Today" statement is measured at `origin/main` `c1612bce45` (2026-11-25). Every "Proposed" statement is new.

This RFC builds on the proposal ozloti wrote in [#23808](https://github.com/kirodotdev/KiroCrew/issues/23808). Where I keep that proposal I say so. Where I change it I say why.

## 1. Summary

Today a Channels room member is a fresh session on an agent template. It has no Bot memory, no Bot rules, no Bot workspace and no Bot goals. I propose that a person can add one of their own Bots to a room by name. The Bot then speaks from its own knowledge: its memory store, its lessons and Permanent rules, its workspace steering and a read-only view of its goals.

Bots in a room talk the way people with different memories talk. Afterwards each Bot remembers the conversation the way a person does: automatically, as a memory of who said what, in which room, on which day. A remembered conversation can be wrong, and the Bot weighs it when it recalls it. A room never changes a Bot's rules.

Containment stays as it is today. A room seat never reads the Bot's own thread. Room posts reach the Bot as untrusted data. A Bot in a room discusses and works inside its own turn. It does not dispatch workers in v1.

It ships behind a new feature preview, `bots_in_channels`, off by default, inside Channels, which is itself off by default.

### 1.1 Where this differs from #23808

Most of this design is the proposal in #23808, and each kept part says so where it appears. Keeping the dispatch block is also from the issue (its item 8 and its Non-goals). This RFC only adds a manual "Send to the Bot's thread" button and names #33996 as the later path (§5.3).

| | #23808 | This RFC |
|---|---|---|
| Write-back | Item 11: on wrap-up each seat proposes entries labelled FACT or POSITION. A FACT that cites the seat's own tool result is written with no approval (`agent.channel_auto_facts`, default on). Everything else needs approval. | Automatic, with no wrap-up and no approval. The seat's room conversation is consolidated into the Bot's own store as attributed episodes (§5.2). |
| Consolidation of room sessions | Item 9: member room sessions stay out of consolidation. Part 2 is the only path into memory. | Bot seats are consolidated by the path normal chats use (§5.2). |
| Rules from rooms | Item 9 keeps role positions out of the store. Lessons and the Bot's own goal and profile tools are not named. | A seat cannot write lessons, Permanent rules, goals, key results, notes or its profile. A room's consolidation drops lessons and auto-skills (§5.2, §5.4). |
| Bot turn budget | Item 7: `agent.channel_member_turn_budget`, 20 turns per room, pauses the room until the person continues. The count does not reset on a human post. | `agent.channel_bot_turn_budget`, 20 Bot turns, resets on every human post (§5.5). |

The issue used FACT and POSITION labels so a person could approve facts more freely than opinions. This RFC does not need them. Every room memory is stored as "who said what, where", so a fact and an opinion are both kept as something a named speaker said, and recall weighs them the same way a person weighs a remembered conversation.

Two parts are additions the issue does not cover, not contradictions. A seat runs on the Bot's worker spec, never its conductor (§5.1). Recall shows where a room memory came from (§5.2).

## 2. Motivation

### 2.1 What a person wants

A change often crosses two areas, and each area has a Bot that knows it. A person wants both Bots in one room, talking to each other and to the person, each from its own memory. Today the person has two options. They can relay text between two Bot threads by hand. Or they can run a moderator session, which #23808 describes: `spawn_run(crew=...)` once per Bot per round, with the moderator relaying the text.

The moderator workaround has four limits, all reported in #23808:

- Nothing is live. Bots never address each other.
- `spawn_continue` fails with `resume_failed` from an incognito or temporary parent.
- `agent.completion_keep_chars` (3000 by default) cuts each reply.
- Nothing learned flows back into a Bot's memory.

### 2.2 Today: how a room member starts

- `run_channel_agent` starts each member with `sessions.get_or_create(agent.session_key, **_session_agent_kwargs(agent.agent_name), approval_policy=...)` (`run_channel_agent` in `src/kiro_crew/channel.py`). The key is `channel:<room>:<seat>` (`Channel.add_agent`). The same call is repeated after a clear (`_reacquire_cleared_session`) and after a busy reset (`_reset_busy_session`).
- Since [#41589](https://github.com/kirodotdev/KiroCrew/pull/41589) (merged 2026-11-24, `76f097683d`), `_session_agent_kwargs` calls `cli_chat._resolve_cli_agent` (`_session_agent_kwargs`). A Bot name now maps to its bound template and its crew identity (`_resolve_cli_agent` in `src/kiro_crew/cli_chat.py`). The memory store that function also returns is discarded (`_store` in `_session_agent_kwargs`).
- #41589's "Not a goal" section says: "Applying a crewmate's memory store or workspace to a channel session". That gap is this RFC.
- No execution binding is written for a room session. The dashboard binds a Bot's chat with `_pin_private_agent_assignment` (`src/kiro_crew/dashboard/chat_persistence_parts/member_store.py`), which calls `resolve_member_execution` and then `bind_session_execution(..., vouch=True)`. No channel path calls it.
- Each turn the member gets one line of role, team and rules, then only the triggering message, as `[{from_role}]: {content}` (the `[CHANNEL]` prompt in `run_channel_agent`). There is no untrusted-data wrapper.

### 2.3 Today: limits and routing

- `agent.max_channels`: default 1, range 1-5. `agent.max_channel_agents`: default 3, range 1-10 (`max_channels` and `max_channel_agents` in `src/kiro_crew/config/sections_agent.py`, clamped in its `__post_init__`).
- `_MAX_MESSAGES = 200`. `_MAX_A2A_EXCHANGES = 3` per sender and receiver pair, stored per room as `Channel.max_exchanges` and restored by `Channel.deserialize`. Any human post clears every pair count (`exchange_counts.clear()` in `Channel.post`).
- The orchestrator gets every top-level human post (the routing loop in `Channel.post`). Every other member acts only when @mentioned (the same loop). The first member is the orchestrator by default (`Channel.add_agent`).
- Closing a room deletes its file (`ChannelManager.close`). The conversation is gone.
- Channels is a core page now, not an app. Its switch is `dashboard.channels_enabled`, off by default (`_FOLDED_BUILTIN_SWITCHES` and `_FOLDED_OFF_BY_DEFAULT` in `src/kiro_crew/apps/builtins/__init__.py`, `channels_enabled` in `src/kiro_crew/config/sections_dashboard.py`).

### 2.4 Today: containment

- `CHANNEL_AGENT_BLOCKED_DISPATCH_TOOLS` blocks `spawn_run`, `spawn_sub_agents`, `spawn_continue`, `spawn_steer`, `workflow_run`, `workflow_author`, `workflow_rerun_subtree`, `task_run`, `register_hook`, `pod_up` and `pod_restart` (`src/kiro_crew/channel.py`). The comment above that tuple gives the reason: a descendant's key is `subagent:<id>`, so a check keyed on `channel:` would not see it.
- `CHANNEL_AGENT_BLOCKED_TOOLS` blocks `send_message`, `send_notification`, every `session_*` control and read verb, `read_slack_history`, `crew_list`, `work_dispatch` and the `kirocrew-work` tools, plus the dispatch list (`src/kiro_crew/channel.py`). The stated reason: room text other people wrote must not drive the person's private sessions, and private transcripts must not leak into a shared room.
- Both lists also hold at MCP dispatch for auto-approved calls (`_deny_channel_agent_messaging` and `_deny_channel_agent_dispatch` in `src/kiro_crew/mcp_core_parts/governance.py`, and the channel guard in `src/kiro_crew/mcp_cron.py`).
- Every channel route asks `_refuse_non_teammate` first (`src/kiro_crew/dashboard/handlers_channel.py`). The owner and admitted teammates pass. This is newer than #23808, which said these routes had no owner gate.

### 2.5 Today: how a normal chat becomes memory

- After every dashboard turn, `_maybe_consolidate` calls `consolidator.maybe_consolidate(key)` unless the session is restricted (`_maybe_consolidate` in `src/kiro_crew/dashboard/chat_utils_slots.py`). Slack and the task runner call it too (`src/kiro_crew/slack/transport_dispatch.py`, `src/kiro_crew/taskrunner.py`).
- `maybe_consolidate` records the key's last activity and runs a pass at the message threshold (`HistoryConsolidator.maybe_consolidate` in `src/kiro_crew/history_consolidation.py`). The idle sweep runs a history pass on any tracked key after `history_idle_secs` (3 hours by default) (`history_idle_secs` in `HistoryConsolidator.__init__`, and `check_idle_sessions`). Session end runs one through `consolidate_session`.
- A pass reads the session's execution record. It refuses any session whose memory mode is not `persistent`, and any transcript marked incognito (`_consolidate` in `src/kiro_crew/history_consolidation_pass.py`). It writes into the store that record names (`_resolve_memory_identity` in the same function).
- For a Bot's V2 store, the write is `apply_consolidation` in `src/kiro_crew/vector_memory_member.py`. Every row it writes carries `source = "consolidation:<session key>"` (`apply_consolidation`). Episodes also carry a conversation id and a timestamp (`episodic_insert_params` in `src/kiro_crew/memory_schema.py`). The model's answer has three parts: `semantic` facts, `episodic` episodes and `lessons` (`apply_consolidation`).
- Before the write, the pass applies `route_consolidation_by_kind` to the model's answer (`src/kiro_crew/memory_kind_routing.py`). Lessons pass through `gate_consolidation_lessons` (`src/kiro_crew/memory_origin_gate.py`). Auto-skill detection also runs from the same consolidator (`_run_skill_detection`).
- Room seats are not part of this. My reading: `channel.py` writes no conversation-log rows for a seat, and calls none of the triggers above. Room messages live only in the room file (`ChannelManager._save_channel`). The idle seeder maps only `dashboard_` stems and messaging keys (`_scan_unconsolidated_transcripts`), and `channel` is not in `CHANNEL_SESSION_NAMESPACES` (`src/kiro_crew/constants.py`).

### 2.6 Problems

1. A Bot in a room has none of the knowledge that is the reason to add it.
2. My reading, not run on a live room: a room member has no bound store, so its `memory_recall` and `learn_add` reach the global store. The lesson route falls back to the global store "when the session names none" (`api_lessons_create` in `src/kiro_crew/dashboard/handlers/cron_lessons.py`), and `memory_recall` reads "Global V1 for an unowned session" (its tool description). If that holds, room text can already drive a global lesson write, and global memory can already be posted into a room. Phase 0 measures it.
3. A Bot forgets a room completely. Nothing from it reaches the Bot's memory, and closing the room deletes the conversation.

## 3. Goals

- G1. A person adds one of their own Bots to a room by name. The seat runs with that Bot's store, lessons, Permanent rules, workspace steering and goals (read-only).
- G2. No new path from a room into a Bot's own thread, its workers, or any other private session.
- G3. A Bot remembers a persistent room automatically, through the consolidation path normal chats use. Each room memory says who said what, in which room and when.
- G4. A room never changes a Bot's rules, goals or profile.
- G5. A room with Bot seats has a cost guard a person controls.
- G6. With the preview off, Channels behaves exactly as today.

## 4. Non-goals

- No private Bot-to-Bot messages. All traffic stays in the room. (Kept from #23808.)
- No shared memory store between Bots. (Kept from #23808.)
- No lifting of the dispatch block in v1. (Kept from #23808.) Delegation waits on #33996 (§5.3).
- Rooms that admit other people (Shared Crew teammates) are out of scope for v1, because a Bot may repeat its owner's memory into the room.
- No change to messaging channels such as Telegram or Feishu ([#16177](https://github.com/kirodotdev/KiroCrew/issues/16177), closed).
- Not one chat per Bot in a team ([#22574](https://github.com/kirodotdev/KiroCrew/issues/22574), parked). That isolates Bots. This RFC puts them in one room.
- No Bot of another person, and no Bot on a remote crew, in v1.

## 5. Design

Each subsection answers one of the seven design questions. Each names the chosen design, then the alternatives and why I rejected them. All of it is proposed.

### 5.1 Identity binding

**Chosen: a room seat is a worker-style session bound to the Bot.**

- A seat names either `agent` (a template, as today) or `bot` (a Bot id), never both. A seat with neither field behaves as today. (Kept from #23808 item 1, renamed.)
- The seat runs on the Bot's worker spec, `kirocrew-mate-worker`, with the Bot's own file additions (the module docstring of `src/kiro_crew/mate_worker.py`). It is never the Bot's conductor.
- On every acquire (first start, after a clear, after a busy reset, after a restart) a new binder resolves the Bot from config with `resolve_member_execution(config, bot, memory_mode=<room mode>)` and calls `bind_session_execution(..., vouch=True)`, the pair `_pin_private_agent_assignment` uses. The vouched store is always the one config resolves now, never one read from the session record. (Kept from #23808 item 3.) This record is also what tells consolidation which store to write (§2.5).
- An unknown, deleted or non-V2 Bot is refused. The seat shows as unavailable. It never starts in a stale context.
- The seat's `cwd` is the Bot's workspace, so its steering applies.
- The new binder is declared in `test_every_binder_declares_whether_it_vouches`.
- The seat gets the Bot's essential context as its worker would: memory, lessons, Permanent rules, steering. Goals arrive read-only. The Bot's own write tools are added to the room block (§5.4).
- Seat ids are never reused for another Bot (kept from #23808 item 4).

Alternatives:

- **The Bot's conductor in a separate channel slot.** Rejected. The conductor only plans and dispatches (`docs/request-for-change/rfc-mate-owned-agent-config.md` §1 and §5.0). In a room every dispatch and ledger tool is blocked (`CHANNEL_AGENT_BLOCKED_DISPATCH_TOOLS`, and the `work_*` entries of `CHANNEL_AGENT_BLOCKED_TOOLS`), so a conductor there could do nothing it was built for. It would also be a second conductor for one Bot, and the ledger RFC assumes one board per conductor (`rfc-conductor-work-ledger.md`, its revision v3 note).
- **The Bot's own thread joins the room.** Rejected. Room text would run as turns in the Bot's private thread, and its transcript would be one tool call from the room. That is exactly what `CHANNEL_AGENT_BLOCKED_TOOLS` exists to stop.
- **Keep template-only seats (today).** Rejected. It is the gap.

How it fits the decisions already made:

- `docs/decisions/2026-11-09-only-a-bots-own-thread-hands-work-to-its-worker.md` says a Bot run outside its own thread, including in an agent channel, does the work in that chat itself. A worker-style seat fits that. It does the work in its turn and dispatches nothing.
- [#33996](https://github.com/kirodotdev/KiroCrew/issues/33996) says those chats "run on the bot's memory and workspace". For Channels that does not match the code: no store is bound (§2.2). This RFC makes it true for Channels.

### 5.2 Write-back

**Chosen: automatic consolidation of each Bot seat into its Bot's own store, as attributed episodes only.**

How a room becomes memory:

- Each Bot seat keeps a conversation-log transcript under its own session key, `channel:<room>:<seat>`. Each row is one room message the seat received or posted, and keeps the speaker's name, as the seat's prompt already does (`[{from_role}]: ...` in `run_channel_agent`). This transcript is new. Today a seat writes none (§2.5).
- After each seat turn the room calls `consolidator.maybe_consolidate(seat_key)`, as a dashboard turn does (`_maybe_consolidate`). That also puts the key on the idle sweep. Closing a room calls `consolidate_session(seat_key)` (`HistoryConsolidator.consolidate_session`). No new consolidation path is added.
- The pass already writes into the store the seat's execution record names (`_resolve_memory_identity` in `_consolidate`). Since §5.1 binds that record to the Bot, the room's memories land in that Bot's store and no other.

What a room memory looks like:

- For a room seat, the pass keeps only the `episodic` part of the model's answer. It drops `semantic` and `lessons` before the write. The seam is where the pass already calls `route_consolidation_by_kind`. A semantic fact is a bare claim with no speaker, so it does not belong here.
- The pass gives the model one extra instruction for a room seat: write each episode as who said what, in which room. For example: "In the room 'Latency page follow-up' on 2026-11-25, the code Bot said change C moved the lookup off the cache." Never "change C moved the lookup off the cache".
- Provenance needs no new field. Every row already carries `source = "consolidation:<session key>"` (`apply_consolidation`), and a seat's key names the room. Episodes already carry a timestamp and a conversation id (`episodic_insert_params` in `src/kiro_crew/memory_schema.py`). The room topic and the speaker live in the episode text.
- Recall shows the origin. When a recalled record's source starts with `consolidation:channel:`, recall adds the room topic and the date beside the text, so the Bot sees that it is remembering a room. Recall rows already carry each record's `source` (`recall_rows` in `src/kiro_crew/memory_provider/render.py`). The memory text injected into a turn does not show it today, so this rendering is the one small addition on the recall side.

What a room cannot write:

- The pass drops `lessons` for a room seat, and auto-skill detection skips a room seat.
- "A room seat" here is positive identity, never the `channel:` key prefix alone. A prefix test misses any key that does not carry it, the same blind spot §5.3 names for descendant keys. Instead, the binder in §5.1 records the seat when it writes the seat's execution record, the way `is_crewmate_worker_session` in `src/kiro_crew/members_parts/member_sessions.py` answers only for a key whose record was read as a Bot's worker. A new predicate, `is_bot_room_seat`, answers true only for such a key, and the episodes-only rule, the lesson drop and the skill skip all read it. The same predicate decides which seats write the room transcript at all, so a seat it does not name has nothing for consolidation to read.
- A seat cannot call `learn_add`, and the room block gains the Bot's own write tools (§5.4).
- The reason, in plain terms: a remembered conversation can be wrong, and the Bot weighs it each time it recalls it, knowing who said it. A rule is different. A Bot follows its lessons and Permanent rules without asking where they came from. So another Bot's message, which may be wrong or written to manipulate, must never become a rule.

Which rooms are remembered:

- Only persistent rooms. An incognito or temporary room is not consolidated, matching what those modes mean everywhere else. The seat's execution record carries the room's mode (§5.1), and the pass already refuses any session whose mode is not `persistent` (the `memory_mode` check in `_consolidate`).
- Turning memory writes off for the whole install (`memory.persistence_enabled`) also stops room consolidation, because the pass checks it first (the `_persistence_disabled` check in `_consolidate`).

Alternatives:

- **A wrap-up card the person approves entry by entry**. Rejected. It turns memory into a chore. People skip it and the Bot learns nothing. It is not how people remember a conversation.
- **The issue's auto-write rule for labelled entries** (#23808 item 11, see §1.1). Rejected. It needs labels, a source-matching rule and an approval queue for everything else. Attributed episodes keep every claim with its speaker, so nothing needs pre-sorting.
- **Consolidate the room file once at close.** Rejected. It adds a second consolidation path. It also leaves nothing remembered from a room that stays open for days.
- **Let consolidation write semantic facts from rooms too.** Rejected. A fact loses its speaker, and then a guess from another Bot reads like something the Bot knows.
- **One-way digest into the Bot's ledger or thread.** Rejected. The ledger is a channel that must not carry an instruction (`rfc-conductor-work-ledger.md`: "a channel that cannot carry an instruction").

### 5.3 Delegation

**Chosen: discussion-only in v1. The dispatch block stays as it is.** (Kept from #23808 item 8 and its Non-goals. The button and the #33996 path below are this RFC's additions.)

- A Bot seat works inside its own turn, like every room member today.
- A "Send to the Bot's thread" action on any room message copies it into the Bot's own thread as the person's own message. The person presses it, so no room text drives a private session by itself.
- Delegation from a room becomes a later phase, blocked on the #33996 design sign-off. If it lands, the worker must be the Bot's own, its key must carry the room so the dispatch block can still see it, and its report must land as a room post.

Alternatives:

- **The seat dispatches to its Bot's worker pool now.** Rejected for v1. A descendant key is `subagent:<id>` and carries no trace of the room (the comment above `CHANNEL_AGENT_BLOCKED_DISPATCH_TOOLS`). The block would have to be rebuilt per Bot. #33996 already names this as a change to how every session path recognises a Bot's own chat, and it needs maintainer sign-off.
- **Let the room's orchestrator dispatch for everyone.** Rejected. It moves the same problem to one seat.

### 5.4 Trust

**Chosen: room posts are data, room memories are attributed episodes, and a room cannot change rules.**

- Each turn's room text is wrapped as untrusted channel data, like other untrusted blocks in context. Today it is not wrapped (the `[CHANNEL]` prompt in `run_channel_agent`).
- The seat gets every room message since its last turn within a budget, not only the trigger. Oldest messages are cut first, and a visible line says how many were cut. (Kept from #23808 item 6, including its 12,000-character default.)
- Memory from a room is only ever "who said what, where" (§5.2). A claim from another Bot stays that Bot's claim in memory.
- A room cannot change rules. `learn_add` is refused for a Bot seat. The room block gains the `kirocrew-mate` write verbs on main, `set_goal`, `set_key_results`, `save_notes` and `update_self` (`src/kiro_crew/mcp_mate.py`). Phase 1 sweeps that server for any other write verb. Read verbs such as `memory_recall` stay open. The seat reads its own Bot's memory, which is the point.
- Adding a Bot seat, and waking or reopening one, is owner-only (`require_owner_dashboard_request`). (Kept from #23808 item 2.)
- Rooms that admit other people are out of scope for v1 (§4).
- The add dialog says that what the Bot recalls and posts is visible to every seat in the room, and that the Bot will remember the room.

Alternatives:

- **Let room memories become facts after N Bots agree.** Rejected. Agreement between Bots is not evidence. Two Bots can repeat the same wrong claim.
- **Block `memory_recall` in rooms.** Rejected. Without recall the Bot has no reason to be there.

### 5.5 Limits

**Chosen: keep today's defaults and add one budget for Bot turns.**

- `agent.max_channel_agents` keeps default 3 and range 1-10. A Bot seat counts as a member.
- `agent.channel_max_exchanges` becomes the configurable default for `max_exchanges` (3, as today), also settable per room at creation. (Kept from #23808 item 7.)
- New `agent.channel_bot_turn_budget`, default 20 Bot turns per room. It resets on a human post, as the pair counts do (`exchange_counts.clear()` in `Channel.post`). When it runs out the room pauses and says so. Every Bot turn carries the full essential context, so this is the cost guard. This differs from #23808 item 7, whose `agent.channel_member_turn_budget` counts 20 turns per room and pauses the room until the person continues, with no reset on a human post. I added the reset so a long, human-led session does not stall.

Alternatives:

- **Raise the default member cap to 5 for Bot rooms.** Rejected. The cap is a config field already. A person who wants more sets it.
- **Lower the pair budget to 2 for Bots.** Rejected. Two exchanges is not enough for a real disagreement to resolve.

### 5.6 Concurrency

**Chosen: one conductor per Bot (its thread) and at most one seat per Bot per room.**

- A Bot in a room runs in two sessions at once: its thread and its seat. That already happens for a Bot's thread and its workers (`docs/decisions/2026-11-22-a-bots-owner-can-let-its-workers-conduct.md`).
- The seat never reads the thread, and the thread never reads the room. They meet only through the Bot's memory.
- Both sessions consolidate into the same store under different keys. The consolidator runs one pass at a time in a process (`_consolidation_pass_gate` in `_consolidate`) and holds a cross-process lease per pass (`_acquire_consolidation_lease`). The store write itself runs inside one `BEGIN IMMEDIATE` transaction under the store's lock (`apply_consolidation`). So two passes cannot interleave a write. A seat holds no writer of its own.
- A Bot can sit in several rooms at once, one seat each. `agent.max_channels` (1-5) bounds that.

Alternatives:

- **Route an @mention in a room into the Bot's existing thread.** Rejected for the containment reason in §5.1.
- **Lock a Bot to one room at a time.** Rejected. It adds a lock and a busy state with no safety gain, since writes are already serialized.

### 5.7 UX

**Chosen:**

- The add-member dialog gets a "Bot" choice that lists the owner's Bots by name, from the same roster the Bots page reads. The template choice stays.
- A Bot seat shows the Bot's avatar and name, with a small "bot" tag. A seat whose Bot no longer resolves shows as unavailable.
- The Bot's profile lists the rooms it sits in, each as a link, in its Sessions tab (tabs per `docs/decisions/2026-10-08-crewmate-profile-card-tabs-are-sessions-schedules-goals-profile.md`).
- The Bot's memory view can filter to records whose source is a room, and a room's menu offers "Forget this room" for each Bot seat (§8).

Alternatives:

- **A new Rooms tab on the profile.** Rejected. The decided tab set is sessions, schedules, goals and profile. A room seat is a session.

Before any user-facing label lands, I grep `docs/decisions/` for the label, per `AGENTS.md`.

## 6. Benefits

- A cross-area question gets answered in one place, by the Bots that know each area.
- The person reads one window and can step in at any point.
- Each Bot remembers the room on its own, with no extra step, and knows who said what.
- Channels gets a clear use case. It is off by default today.

## 7. Use cases

### 7.1 An incident follow-up: an on-call Bot and a service-code Bot

I have two Bots for one service. The on-call Bot knows the service's alarms, dashboards, runbooks and past incidents. The code Bot knows the service's code, its recent changes and its tests. I was paged an hour ago, and the service is stable again.

1. I open a persistent room "Latency page follow-up" and add both Bots by name. The on-call Bot is the orchestrator.
2. I post: "The latency alarm fired at 14:05. Runbook step 3 did not clear it. Why?" The on-call Bot gets it (orchestrator). It recalls the alarm's history from its own memory: the same alarm fired twice last month, and both times step 3 (restart the cache tier) cleared it. This time the restart did not help.
3. The on-call Bot @mentions the code Bot: "Did anything change in the request path near the cache since last month?" The code Bot recalls from its own memory change C, merged two days ago, which moved one lookup from the cache to a direct database read.
4. They go back and forth within the pair budget of 3. The code Bot explains that the restart cannot help, because that lookup no longer uses the cache. The on-call Bot confirms that the database latency graph rose at 14:03. They agree on a root cause: change C put a hot lookup on the database.
5. I post the decision: "Root cause agreed. Put the lookup back behind the cache." The pair counts reset.
6. The code Bot proposes a fix plan in the room: revert the read path, then add a test that fails when the lookup bypasses the cache. It does not dispatch a worker. I press "Send to the Bot's thread" on that post, and the code Bot's own thread picks it up as my message.
7. I close the room. There is no wrap-up step. Each seat is consolidated into its own Bot's store. The on-call Bot now holds episodes such as "In the room 'Latency page follow-up' on 2026-11-25, the code Bot traced the alarm to change C" and "In that room, the person decided to put the lookup back behind the cache". Its runbook is not changed, and it gets no new lesson.
8. A week later the alarm fires again and I ask the on-call Bot in its own thread. It recalls: "In the incident room on 2026-11-25, the code Bot traced it to change C." It shows the room and date beside that memory, checks whether change C is still reverted, and answers from there.

Neither Bot read the other's memory. Neither Bot read its own thread. No rule changed.

### 7.2 Joint planning (from #23808)

A travel Bot and a car Bot decide whether a 600 km drive to a concert makes sense. The travel Bot knows tickets, hotels and trains. The car Bot knows the car's range and charging stops. They argue in one room. Afterwards the travel Bot remembers "In the concert planning room, the car Bot said the route needs two charging stops". Next month, planning another trip, it recalls that and asks the car Bot again instead of treating it as settled.

### 7.3 Critique panel on a design draft

I add a security-review Bot and a docs Bot to an incognito room with a template "author" seat. I post a draft. Each Bot critiques from its own lessons. The author seat revises in the room. Because the room is incognito, nothing from it is remembered, which is what I want for a one-off critique.

## 8. Downsides

- **A wrong claim can enter memory.** Another Bot can be wrong, or its text can be written to mislead. Attribution limits the harm: the claim is stored as that speaker's claim, in that room, on that day, and recall shows the room beside it. It never becomes a lesson or a rule. A person can see and remove room memories:
  - Today the Bot's memory view lists records per store (`GET /api/memory/records` in `src/kiro_crew/dashboard/routes/memory.py`) and deletes one episode or fact (`DELETE /api/memory/episodic/{id}`, `DELETE /api/memory/semantic/{key}`).
  - Proposed: a "From rooms" filter on `source` starting `consolidation:channel:`, and "Forget this room", which deletes every record from one seat key through the bulk routes (`/api/memory/bulk/preview` and `/api/memory/bulk/apply`). Whether the bulk routes accept a source filter today is not checked. Phase 3 confirms or adds it.
- **No human check before memory.** This trades a per-entry approval step (§5.2, alternatives) for convenience. A person who wants nothing remembered uses an incognito room.
- **Cost.** Every Bot turn carries the Bot's essential context, and each seat adds consolidation passes. The turn budget limits turns. Consolidation keeps its existing thresholds and throttles.
- **Disclosure between Bots.** What one Bot recalls and posts, every seat in the room reads, and then remembers as "the other Bot said ...".
- **More binding paths.** One more binder joins the paths that publish an execution record. Each one is a place a store could be bound wrong.
- **Discussion-only is limiting.** A Bot that normally hands off long work will do shallower work in a room.
- **Two live sessions per Bot** while it sits in a room.

## 9. Security considerations

- **Store binding.** A seat binds only the store config resolves for that Bot, only through the owner gate, on every acquire, with a vouch. Never from a session record. Consolidation then writes only to that store (`_resolve_memory_identity` in `_consolidate`).
- **No new outbound path.** The room block stays and grows (§5.4). No seat can reach the Bot's thread, its workers, its ledger or any other session.
- **Memory poisoning.** Room text is data. A room seat writes no memory itself. Consolidation stores only attributed episodes, never facts, lessons or skills. A poisoned message can at most become a memory of what a named speaker said, which the Bot weighs at recall.
- **Rules stay out of reach.** No lesson, Permanent rule, goal, key result, note or profile edit can come from a room, by tool or by consolidation (§5.2, §5.4).
- **Private memory in a shared room.** Rooms with other people are out of scope for v1 (§4). The add dialog states that the Bot's recall is visible in the room.
- **Incognito and temporary rooms** are never consolidated (§5.2).
- **Today's possible leak** (§2.6 item 2). If Phase 0 confirms that an unbound room member reads and writes the global store, that is a bug on main. I will file it and fix it in its own PR, ahead of this RFC's phases, because a bug fix should not wait on a design.
- **Audit.** Each refused write from a seat is logged to SEL with the room and the Bot, as the existing channel denials are (`_deny_channel_agent_messaging`).

## 10. Rollout and migration plan

Everything sits behind a new feature preview, `bots_in_channels`, in `dashboard.feature_previews` (`FEATURE_PREVIEW_FLAGS` in `src/kiro_crew/config/sections_dashboard.py`, mirrored in `website/src/utils/previewFlags.ts`). It is off by default. Channels itself stays behind `dashboard.channels_enabled`. With the preview off, the server refuses a `bot` seat and the dialog does not offer one.

Each phase is one PR and can be stopped on its own.

| Phase | What | Exit criteria |
|---|---|---|
| 0. Probe | On a real gateway: add a Bot by name to a room on main. Record which spec it runs (#41589 maps it through `_resolve_cli_agent`), which store `memory_recall` and `learn_add` reach, and whether anything consolidates the room. Write the verdict into this RFC. | The verdict is in §2.6 with the commit measured. Any confirmed bug has its own issue. |
| 1. Binding | `bot` seat field, owner-only add and wake, binder on every acquire, `cwd` from the Bot, untrusted wrapper, room transcript budget, `learn_add` and `kirocrew-mate` write verbs blocked. Behind the preview. No consolidation yet. | Tests: binder declared; seat A never recalls seat B's records; unknown or non-V2 Bot refused on add, wake, clear and reload; `learn_add` and each blocked verb refused from a seat; template-only rooms unchanged with the preview off. |
| 2. Remember the room | Seat transcript, `maybe_consolidate` after each seat turn, `consolidate_session` on close, episodic-only result for room seats, the attribution instruction, lessons and auto-skills skipped. | Tests: a persistent room's seat writes episodes into its Bot's store with `consolidation:channel:` source; no semantic row, lesson or skill comes from a seat; an incognito or temporary room writes nothing; template-only seats write nothing. |
| 3. See and forget | Recall shows the room and date beside a room memory. Memory view "From rooms" filter. "Forget this room". | A recalled room memory shows its room. "Forget this room" removes every record from that seat key and nothing else. |
| 4. Keep the room, profile, budget | Close moves the room to History instead of deleting it. Rooms on the Bot's Sessions tab. `agent.channel_bot_turn_budget`, `agent.channel_max_exchanges`. | Closing and reopening keeps every message, and seats rebind on reopen. The budget pauses a room and resets on a human post. |
| 5. Delegation | Blocked on the #33996 sign-off. | Not started until that design is accepted. |

Write-back no longer waits on keeping the room. #23808 ordered persistence first because its provenance pointed at a kept room transcript (its Part 2 intro). Here provenance points at the seat's own transcript, which Phase 2 creates.

## 11. Backward compatibility

- Room files without a `bot` field load as today (`Channel.deserialize`).
- Every new config field is optional and defaults to today's behaviour.
- Downgrade: Phase 1 also writes the Bot id into the seat's existing `agent_name`. An older build ignores the `bot` field and starts the seat from `agent_name`, which since #41589 maps a Bot to its template with no store, today's behaviour. This is the intended result, not yet tested. Phase 1 tests it.
- Room memories are ordinary episodes. An older build recalls them as plain episodes without the room label beside them.
- Nothing is removed.

## 12. How this fits other RFCs

- **`rfc-crewmate-workers-under-the-crewmate.md`** (partial). It moves a Bot's sessions under the Bot, behind a preview flag. A room seat is not a worker the Bot dispatched. It is opened by the person. So it stays out of that tree and appears as a room link on the profile (§5.7).
- **`rfc-conductor-work-ledger.md`** (partial). Its "Channel agents" paragraph blocks `kirocrew-work` for channel agents and re-checks after the read. This RFC keeps that block for Bot seats. A seat has no ledger item, and nothing from a room goes into the Bot's ledger (§5.2).
- **`rfc-mate-owned-agent-config.md`** (implemented). A Bot is a conductor plus a worker with the Bot's additions. A seat runs on the worker with those additions. The conductor never joins a room (§5.1).
- **`rfc-crewmate-session-tree.md`** (draft). It folds a Bot's session tree for apps. A seat is not in that tree, because the Bot did not open it. If that RFC lands, a room seat can be listed beside the tree, not inside it.
- **`rfc-orchestrator-chat-sessions.md`** (superseded). It notes that `Channel.is_orchestrator` is a channel feature that predates it (its disambiguation note). Here "orchestrator" means only that room role. A Bot can hold it (open question 4).
- **`rfc-bot-workers-conduct.md`** and `docs/decisions/2026-11-22-a-bots-owner-can-let-its-workers-conduct.md`. They nest workers under a Bot's thread. Nothing here changes that, and a seat never conducts.

## 13. Open questions

1. Should recall rank room memories below the Bot's own first-hand memories, or only label them? My recommendation: label only in v1, and measure.
2. Should each seat's episodes also record what the Bot itself said, or only what others said and what was decided? My recommendation: all three, since a person remembers their own words too.
3. Is there a per-Bot switch "do not remember rooms", or is the room's memory mode enough? My recommendation: the room's mode is enough for v1.
4. May the orchestrator be a Bot seat, or should it stay a template?
5. Should "keep the room on close" ship first as a standalone change outside the preview? It helps template-only rooms too.
6. When #33996 is decided, does a room's delegation report land in the room only, or also on the Bot's thread?
7. When rooms with other people come in scope, does each Bot need a per-room "may recall" switch?

## 14. Alternatives considered (whole design)

- **Moderator session** (#23808). Works today. Not live, truncated, no write-back.
- **A separate app.** Rejected. An app cannot change how channel sessions are built, and `AGENTS.md` forbids a new built-in app.
- **Bot binding on messaging channels first (#16177).** A different surface with no shared multi-agent room.
- **One chat per Bot in a team (#22574).** It isolates Bots instead of letting them talk.

## 15. References

- Issues: [#23808](https://github.com/kirodotdev/KiroCrew/issues/23808) (open, `parked`, `tier:T3`), [#16177](https://github.com/kirodotdev/KiroCrew/issues/16177) (closed), [#22574](https://github.com/kirodotdev/KiroCrew/issues/22574) (open, `parked`, `tier:T4`), [#33996](https://github.com/kirodotdev/KiroCrew/issues/33996) (open, `parked`, `tier:T3`), [#28546](https://github.com/kirodotdev/KiroCrew/issues/28546) (closed).
- PR: [#41589](https://github.com/kirodotdev/KiroCrew/pull/41589), merged 2026-11-24.
- Code, at `c1612bce45`: `src/kiro_crew/channel.py`, `src/kiro_crew/cli_chat.py`, `src/kiro_crew/mate_worker.py`, `src/kiro_crew/dashboard/chat_persistence_parts/member_store.py`, `src/kiro_crew/dashboard/handlers_channel.py`, `src/kiro_crew/dashboard/handlers/cron_lessons.py`, `src/kiro_crew/dashboard/chat_utils_slots.py`, `src/kiro_crew/dashboard/routes/memory.py`, `src/kiro_crew/mcp_core_parts/governance.py`, `src/kiro_crew/mcp_mate.py`, `src/kiro_crew/history_consolidation.py`, `src/kiro_crew/history_consolidation_pass.py`, `src/kiro_crew/vector_memory_member.py`, `src/kiro_crew/memory_schema.py`, `src/kiro_crew/memory_kind_routing.py`, `src/kiro_crew/memory_origin_gate.py`, `src/kiro_crew/constants.py`, `src/kiro_crew/config/sections_agent.py`, `src/kiro_crew/config/sections_dashboard.py`.
