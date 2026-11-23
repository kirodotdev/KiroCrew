---
title: Slack thread conductor — turn one Slack thread into a Conductor, for good
status: draft
author: iamwhatever
created: 2026-11-23
last-audited: 2026-11-23
audited-at: dcf3c38117
doc-pr:
implementation-prs: []
tracking-issues: [41212]
supersedes: []
superseded-by: []
revision: 2
---

# RFC: Slack thread conductor

- Status: draft, revision 2. Nothing is built. Acceptance is requested from a
  maintainer who owns Slack code channels; the status flips to `accepted` when
  one records it here. Every statement about today's code was checked against
  `main` at `dcf3c38117` and is cited by file and symbol.
- Revision 2 answers an independent review (findings 1 to 9, listed under
  "Review record" at the end) and records the owner's rulings: the conductor's
  own replies show normally, public channels need an explicit opt-in, closing
  the chat in the dashboard ends the thread, and the work ships as four PRs
  (A to D) with D landing before the switch can be turned on.
- Related: [rfc-slack-code-channels.md](rfc-slack-code-channels.md) owns the
  code-channel dispatch exemption and the work board this document reuses.
  [#41222](https://github.com/kirodotdev/KiroCrew/pull/41222) adds the
  per-thread Bot command `!bot <name>`; this document names it only where the
  two commands meet (section 3) and does not design it.
  [#23362](https://github.com/kirodotdev/KiroCrew/pull/23362) (Slack channel
  roles, draft) and [#40210](https://github.com/kirodotdev/KiroCrew/pull/40210)
  (shared users open their own code channels, draft) change who may drive a
  conductor; [#30956](https://github.com/kirodotdev/KiroCrew/issues/30956) asks
  for worker progress in a thread;
  [#40478](https://github.com/kirodotdev/KiroCrew/pull/40478) proposes wrapping
  up a conductor chat in place.

## Summary

Add one owner-only way to make a Slack thread a Conductor thread:

- `!conductor [goal]`, typed in a thread, turns that thread into a Conductor
  thread.
- `/kirocrew conductor <goal>`, typed in a channel or the app's DM, posts a new
  parent message there and starts a Conductor thread under it.

The change is one way. A Conductor thread never goes back to a plain thread.
In the thread the conductor talks to the owner as any agent does: its replies
show normally. Its workers show as ONE status message, edited in place: each
worker's name, state, PR and CI. Worker details stay out of the thread; each
row links to the worker's dashboard chat.

Under the hood a Conductor thread is a code-channel binding whose "room" is one
thread of an ordinary channel. It reuses the code-channel binding store, the
dispatch exemption, the work board, worker approvals, Stop, restart resume and
the shared-user refusal. It needs no new Slack scope. By default it works in the
app's DM and in private channels; a public channel needs an explicit opt-in.
One switch, `slack.thread_conductor`, default off, turns it on.

## Motivation

### Current state

- **A plain Slack thread cannot dispatch.** Setting a channel's agent to
  `kirocrew-conductor` (`!channel agent`, `_bang_channel` in
  `src/kiro_crew/slack/handler_runtime/commands.py`) or a thread's agent (`!ta`,
  `_bang_thread_agent`) only changes the agent's name. The first
  `session_create` its turn makes is refused by `_refuse_ineligible_creator` in
  `src/kiro_crew/dashboard/session_control_parts/target_authorization.py`:
  "channel-linked sessions cannot create sessions", or for a mirrored slot
  "sessions mirrored to a channel cannot create sessions". The only exemptions
  are an owner DM on Discord or Telegram (`OWNER_DM_CONDUCTOR_SURFACES`) and a
  Slack code channel's own session (`judge_code_channel` in
  `src/kiro_crew/dashboard/session_control_parts/code_channel_exemption.py`).
- **Full conductor mode needs a code channel.** A code channel is opened through
  `agents.conversations.create` and needs `slack.code_channels`,
  `slack.code_channel_dispatch` and six more bot scopes:
  `code_channels:manage`, `chat:write.customize`, `canvases:write`,
  `channels:write.invites`, `groups:write.invites` and `pins:write` (the
  `#@code_channels` lines of `src/kiro_crew/slack-manifest.yaml`). That means a
  manifest update, a reinstall, and the workspace allowing Slack code channels.
  In some organisations a scope change also means a security review.
- **Every code-channel runtime path asks for those scopes.** `code_channels_on`
  in `src/kiro_crew/slack/code_channels/binding.py` is "the setting is on AND
  the installed app can run code channels (scope granted, not latched)". It
  gates routing (9 call sites in `routing.py`), restart resume (`resume.py`),
  worker approval cards (`worker_approvals.py`), worker Stop (`worker_stop.py`),
  the approval picker (`approval_picker.py`) and the surface (`surface.py`).
- **Each code channel is a new channel.** Its session is bound one-to-one to a
  channel (`CodeChannelStore` keys `bindings` by `channel_id`, in
  `src/kiro_crew/slack/code_channels/store.py`). The owner leaves the thread
  where the work came up and follows it somewhere else.
- **`/kirocrew` cannot act on a thread.** Slack does not let an app's slash
  command run in a thread, and the payload names no thread
  ([Slack: Implementing slash commands](https://docs.slack.dev/interactivity/implementing-slash-commands)).
  `/kirocrew agent` typed outside a code channel changes the global default
  agent (`_handle_agent` in `src/kiro_crew/slack/events_slash.py`).

### Problems

1. Using a Conductor from Slack takes either a new app install with new scopes,
   or a trip to the dashboard. Neither is a Slack gesture.
2. The one Slack path that runs a conductor, a code channel, moves the work out
   of the thread it started in.
3. A thread whose agent is `kirocrew-conductor` looks like a conductor and
   cannot do the one thing a conductor does. Nothing tells the owner why.

## Goals

1. The owner turns the current thread into a Conductor thread with one message,
   and starts a fresh Conductor thread from a channel or the app's DM with one
   slash command.
2. In a Conductor thread the conductor can create workers, bind them to its
   ledger and follow them: the same verbs a code channel's conductor has, and no
   more.
3. The conductor's replies show in the thread as usual. Its workers show as one
   status message that is edited in place. Worker details are hidden; each row
   links to the worker's chat.
4. No new Slack scope. It works in the app's DM and in private channels the bot
   is in; in public channels after an explicit opt-in.
5. One way. Once a thread is a Conductor thread it stays one; every attempt to
   change that gets a fixed answer, and closing its chat ends it.
6. Reuse the code-channel parts. A second exemption, store, board publisher or
   resume path is a design failure.

## Non-goals

- The per-thread Bot command (`!bot <name>`), which #41222 builds.
- Letting shared users (`slack.shared_access`) start or drive a Conductor
  thread. Section 4 keeps them out, as code channels do; #40210 and #23362 are
  where that changes.
- Group DMs (`mpim`). The app holds no `mpim:*` scope.
- Channels shared outside the workspace (Slack Connect). They stay excluded, as
  for code channels.
- A per-thread git worktree. Section 2 states what workers share.
- A dashboard UI. The conductor's dashboard chat is an ordinary chat.
- Changing code channels. They keep every rule they have.

## Design

### 1. Starting a Conductor thread

#### `!conductor [goal]`, in a thread

An owner-only bang command, added to the table in `_handle_slash_command`
(`src/kiro_crew/slack/handler_runtime/commands.py`) beside `!ta` and
`!link-to-dashboard`. It is not added to `_BANG_TO_SLASH` in
`src/kiro_crew/slack/handler.py`, because that map also drives the deprecation
notice and no slash command can replace it.

**Reaching the handler from a linked thread.** `route_linked_thread` in
`src/kiro_crew/slack/handler_runtime/inbound.py` lets a message in a linked
thread reach the bang handlers only when its first word is in `_BANG_TO_SLASH`;
any other word is delivered to the linked slot as a prompt. A Conductor thread
is a linked thread, so without a change `!conductor` and `!bot` typed there
would reach the conductor's model. One new set beside `_BANG_TO_SLASH`,
`THREAD_SWITCH_COMMANDS = {"!conductor", "!bot"}`, is read by that same
fall-through test, so both words always reach their handlers, which answer with
section 3's table. #41222 adds `!bot`; this set is shared with it, not copied
(section 9, PR B).

The owner gate is the one every `!` command already passes in
`_route_bang_command` ("All other ! commands are owner-only"), so a shared user
gets "Owner-only command." and an SEL `slack.owner_command` denial.

What it does, in order:

1. **Refuse when it cannot apply** (fixed text, one SEL row each, nothing
   changes):
   - `slack.thread_conductor` is off: "Conductor threads are off. Turn on
     `slack.thread_conductor`."
   - Not in a thread (`reply_ts == msg_ts`, the same test
     `_bang_link_to_dashboard` uses): "Use `!conductor` inside a thread, or
     `/kirocrew conductor <goal>` to start one."
   - The channel is a code channel (`active_binding` in
     `src/kiro_crew/slack/code_channels/binding.py`): "This channel already
     runs a conductor."
   - The thread is already a Conductor thread: section 3.
   - The thread is already linked to a dashboard chat
     (`DashboardState.get_linked_slot(reply_ts)` returns a slot): "This
     thread is linked to a dashboard chat. Start a Conductor in a new thread."
     A linked chat has its own agent, history and approval posture; adopting
     it would grant dispatch to a session the owner did not start as a
     conductor. `get_linked_slot` (`src/kiro_crew/dashboard/slot_registry.py`)
     answers only for slots linked through `link_slack`, so an ordinary
     Slack-born thread is not refused here.
   - The thread runs a Bot (#41222 records the Bot as a `member` execution in
     the thread's metadata, read by `_read_thread_overrides`): "This thread
     runs a Bot. Start a Conductor in a new thread."
   - The room is not admitted (section 4, "Where"): a public channel without
     the opt-in, a group DM, or a channel shared outside the workspace (or whose
     share state is unknown): "Conductor threads are not available here." with
     the reason.
2. **Open the conductor's chat.** A new dashboard slot is opened on the
   conductor mode agent, `crew_mode.crew_mode_agent`
   (`src/kiro_crew/crew_mode.py`). It does NOT go through
   `code_channel_conductor_agent` in `src/kiro_crew/slack/features.py`, which
   answers `None` unless `slack.code_channel_defaults.conductor_mode` is set,
   a code-channel setting a thread conductor does not need. The build must
   offer conductor mode (`conductor_mode_offered`), so not on LTS. The slot is
   filed in its own folder, `Conductor threads/<thread title>`, made with
   `ensure_folder_path` (`binding.py`), because `session_create` refuses a
   code-channel caller with no folder and files its workers in that folder
   (section 4). The thread's history is imported by the same reader
   `!link-to-dashboard` uses (`_import_thread_to_slot` in
   `src/kiro_crew/slack/interactions_options.py`). History by anyone but the
   owner is imported as fenced untrusted context, the way a code channel's
   first turn carries its origin thread. A channel pinned to another agent
   (`channel_pinned_agent`) refuses here exactly as that import refuses today.
3. **Bind it.** One thread binding is written to the code-channel store
   (section 2), with the payload's `team_id`, while `hold_anchor(thread_ts)`
   holds the thread, so a second `!conductor` racing this one meets the anchor
   guard (`check_anchor_handoff`) and gets the "already a Conductor thread"
   answer.
4. **Record the owner as the turn's author.** The goal turn is started by the
   command, not routed, so `route_linked_thread` never calls
   `note_channel_sender` for it, and `_sender_refusal` would refuse the
   conductor's first `create` ("the author of this turn's channel message is
   not on record"). The start calls `note_channel_sender(slot, owner_id)`
   before it queues the turn, and marks the turn channel-origin, as a routed
   message does. The author is the owner by identity: the command already
   passed the owner gate.
5. **Say so, once.** One line in the thread: "This thread is now a Conductor
   thread. It stays one. Workers show below." If a goal was given, it is the
   conductor's first turn. With no goal, the conductor waits for the owner's
   next message.

#### `/kirocrew conductor <goal>`, in a channel or the app's DM

A new sub-command in `SLASH_REGISTRY` (`register_slash_command("conductor", ...)`
in `src/kiro_crew/slack/events_slash.py`), in the style of `agent` and
`dashboard`. `_handle_slash` already lets only `is_allowed_user` through
(which is `is_owner` today, `src/kiro_crew/slack/handler_runtime/access.py`);
this handler asks `is_owner` itself, so it does not widen if the allow-list
does.

1. An empty goal gets the usage line, ephemeral:
   "Usage: `/kirocrew conductor <goal>`".
2. The same refusals as `!conductor` step 1, answered ephemerally through
   `response_url`.
3. It posts a parent message in the payload's `channel_id`: "Conductor:
   <goal, first line, defused>" (the goal passes `published_goal` in
   `code_channel_exemption.py`, so it carries no mention markup and no
   credential). A channel the bot cannot post in (not a member, a DM between
   two people) gets an ephemeral "I can't post here. Invite me, or use my DM."
4. Then steps 2 to 5 of `!conductor` run with that message as the thread
   parent and the goal as the first turn.

The slash command is the only way to start from outside a thread because Slack
gives it no thread. It never acts on an existing thread.

#### Naming

`!conductor` and `/kirocrew conductor` follow #41222's `!bot <name>`: a bare
noun naming what the thread becomes. The goal is free text, and no `on` / `off`
argument exists, because there is no off (section 3).

### 2. Identity: a code-channel binding whose room is a thread

The code-channel exemption proves "this slot is that room's session" from one
record: a `Binding` (`src/kiro_crew/slack/code_channels/store.py`) naming the
slot key, the channel, the workspace (`team_id`) and an `anchor_ts`,
cross-checked against the live Slack link index and the slot's own link
(`code_channel_binding_of` in
`src/kiro_crew/dashboard/session_control_parts/caller_access.py`). A Conductor
thread uses the same record. Its anchor is the thread's parent ts, which is
already what a code channel's anchor is: the ts the slot's Slack link is keyed
by.

The changes:

- **`Binding` gains a `thread: bool` field**, default `False`, so every record
  on disk loads unchanged. `thread=True` means the anchor is a thread and the
  session's replies go INTO that thread. `mirror_post_thread` in
  `binding.py` returns `None` (top level) only for a binding with
  `thread=False`. `team_id` is filled from the start command's event or
  payload, so `workspace_admitted` judges a thread as it judges a channel.
- **The store holds thread bindings in a second map**, `thread_bindings`,
  keyed `"<channel_id>:<anchor_ts>"`, in the same write-protected
  `slack-code-channels.json`. A channel can have many Conductor threads, and
  `bindings` stays one per channel, so the code-channel readers that mean "the
  channel's binding" (`get_binding`, `active_binding`, `thread_opened_channel`)
  keep their meaning. `loaded_bindings()` and `bindings()` keep returning
  code-channel bindings only; a new `loaded_thread_bindings()` and
  `thread_binding_for(channel_id, anchor_ts)` return the others.
- **Every reader that answers "is this slot, key or thread bound" reads both
  maps.** That is not only the exemption. The list, checked at `dcf3c38117`:
  - `code_channel_binding_of` (`caller_access.py`): the cross-check is
    unchanged; the anchor must resolve to THIS slot through `get_linked_slot`,
    and the slot's `_slack_channel` must name the binding's channel. A forged
    row in either map fails the same way.
  - `anchor_owner_by_ts` (`binding.py`), so `check_anchor_handoff` keeps a
    Conductor thread's anchor from being linked to any other slot, including
    by `!link-to-dashboard` and the dashboard's own link routes.
  - `CodeChannelStore.session_keys()` and `revoked_anchor()`, which the
    shared-user refusal reads.
  - `shared_user_refused_in(channel)` and `shared_user_refused_for_slot` in
    `src/kiro_crew/slack/code_channels/shared_refusal.py`. Today the first takes
    a channel only and the second walks `bindings()` and `session_keys()`, so
    without this change a shared user's message in a Conductor thread would
    pass. The first gains a `thread_ts` argument; the second walks both maps.
  - `binding_for_slot` (`surface.py`), which worker approvals and Stop use to
    find a worker's conductor.
- **One predicate replaces `code_channels_on` wherever the binding is known.**
  `room_runtime_on(binding)` answers `code_channels_on(cfg)` for a channel
  binding and `slack.thread_conductor` for a thread binding (with the same
  degraded-config reading). Every runtime site that gates on a binding it
  already holds calls it instead: in `resume.py`, `worker_approvals.py`,
  `worker_stop.py`, `approval_picker.py`, `surface.py` and the bound-session
  paths of `routing.py` (`_stop_bound`, `dispatch` for a directed message). The
  sites that gate BEFORE any binding is known (`inbound_gate`'s first check,
  `maybe_open_code_channel`) keep `code_channels_on` and get a thread branch
  that looks up `thread_binding_for(channel, thread_ts)` first. Without this,
  the board, approvals, Stop and resume would stay off on an install without
  the code-channel scopes, which is every install this design is for.
- The worker registry (`record_worker`, `workers_of`, `worker_conductor`) and
  the resume records are keyed by slot key, not channel, and are reused as
  they are.

**Project and checkout.** A thread binding has no worktree, no canvas, no
channel name and no `origin_*` fields. The conductor's folder gets the
conductor slot's project (the thread's `!project`, else the dashboard default),
as a code channel's folder does. Its workers are filed in that folder and run
in that project's checkout: the same checkout, as workers of a dashboard
conductor do today. A worker that must not share it is the conductor's call, as
in the dashboard. A worktree per Conductor thread (what `workspace/` cuts for a
code channel) is left out: it would bring the Code tab and the archive cleanup
with it, and nothing here needs them.

### 3. One way, and what each attempt gets

A Conductor thread never goes back to a plain thread or to another agent.

**Why:**

1. Workers are bound to the conductor's slot (`_created_by`) and its ledger.
   Switching the thread back would leave live workers whose creator no longer
   holds dispatch, with approvals and a status message nobody owns.
2. The exemption keys on the binding, not the agent name. If the agent could
   change, a non-conductor agent would hold the conductor's dispatch rights.
   Keeping the binding and the agent fixed together is the simplest true rule.
3. The plain thread's own Slack session and the conductor's dashboard slot are
   two sessions. Going back would mean a second history import into a third
   session, and the owner could not tell which one answers.
4. A one-way state is easy to reason about for security: a thread moves from
   "contained" to "owner's conductor" once, and never toggles while workers
   run. The owner asked for this rule.

**What each attempt gets** (fixed text in the thread, one SEL row, nothing
changes). Each word reaches its handler through `_BANG_TO_SLASH` or
`THREAD_SWITCH_COMMANDS` (section 1); `/agent <name>` is intercepted before
`route_linked_thread` hands it to the slot's runner.

| Attempt in a Conductor thread | Answer |
|---|---|
| `!conductor` again | "This thread is already a Conductor thread." |
| `!ta <name>`, `/agent <name>`, `!agent <name>` | "This is a Conductor thread for good. Start a new thread for another agent." |
| `!bot <name>` (#41222) | the same line |
| `!link-to-dashboard` | "This thread is already a dashboard chat: <link>." |
| An agent switch on the conductor's chat in the dashboard | refused by `switch_slot_agent` (`src/kiro_crew/dashboard/chat_api/agent_switch.py`) while the slot has a thread binding, with the same reason |

**Stop.** `!stop` in the thread is routed to the code-channel stop
(`_stop_bound` in `src/kiro_crew/slack/code_channels/routing.py`, with
`src/kiro_crew/slack/code_channels/stop_helpers.py`), so it stops the
conductor, its running workers and their sub-agents, as in a code channel. The
binding stays, so the thread is still a Conductor thread and the next owner
message continues it.

**Ending.** A Conductor thread ends in one of two ways, and an ended thread is
read-only: it admits no more dispatch, and every message in it, the owner's
included, gets one fixed notice ("This Conductor thread has ended. Start a new
one.") instead of a turn. It never falls back to a plain agent.

1. **Wrap-up**, as #40478 proposes for a conductor chat, stamps
   `wrapped_up_at` on the binding.
2. **Closing the conductor's chat in the dashboard.** `close_slot`
   (`src/kiro_crew/dashboard/chat_api/slot_lifecycle.py`) on a slot that has a
   thread binding stamps `ended_at` on that binding and posts the notice once
   in the thread. Without this, the binding would name a slot that no longer
   exists, `get_linked_slot` would answer `None`, and the thread's next
   message would take the plain Slack route on the thread's default agent: a
   switch back in all but name. With `ended_at` set, the linked-thread router
   finds the binding by anchor (`thread_binding_for`) and answers with the
   notice before the plain route. The workers keep running in the dashboard,
   where the owner can still see and stop them.

**Cleanup.** An ended binding is kept for 30 days, then moved out of
`thread_bindings` into the store's existing `revoked` map (`Revoked`, with its
`anchor_ts`), which the inbound gate already keeps closed
(`revoked_anchor`). `revoked` is bounded to `MAX_REVOKED` (2048) newest
entries. Shortcut: an anchor evicted from `revoked` by 2048 newer endings
answers as a plain thread again; upgrade to a dedicated "ended anchors" set if
an install ever ends that many. A live (not ended) binding is never removed by
a command or by cleanup.

### 4. Dispatch authorization

The code-channel exemption is extended, not copied. `judge_code_channel` stays
the ONE predicate the three gates consult: `session_create` and the
session-control caller half (`_refuse_ineligible_creator`,
`refuse_caller_surface` in `target_authorization.py`) and the work ledger
(`src/kiro_crew/dashboard/handlers/work_ledger_caller.py`). With a thread
binding found, every clause holds as it is, with these differences:

| Clause | Code channel today | Conductor thread |
|---|---|---|
| Identity | `code_channel_binding_of` | the same, reading `thread_bindings` too |
| Switch (`_switch_refusal`) | `slack.code_channels` on and capable, `slack.code_channel_dispatch` true | `slack.thread_conductor` true; the same degraded-config and torn-file checks |
| Who drives (`_sender_refusal`) | every routed Slack author is the owner, by `is_owner` | the same; the start records the owner (section 1, step 4) |
| Audience (`_audience_refusal`) | the slot publishes only to `(channel, anchor)` | the same tuple, which for a thread is the thread itself |
| Channel state (`_channel_state_refusal`) | archived, Stop, workspace admitted, channel policy, not shared outside | the same, plus: ended, and the room is admitted (below) |
| Reach | `EXEMPT_TARGET_OPERATIONS` (status, send, close, stop, end_wait), creator fence, one level deep (`code_channel_root`) | the same |

So only the owner can start a Conductor thread, and only turns the owner drove
can dispatch. `read` and `summary` stay refused, so no worker transcript is
read into the thread.

`_switch_refusal` is the one new branch: a thread binding asks
`slack.thread_conductor` and does NOT ask `code_channels_on`, because that
needs the code-channel scopes this design avoids. `_note_handoff_state` (the
"Turn on helper agents" notice in `handoff.py`) does not run for a thread
binding: the start command already refused while the switch was off.

**Workers' folder.** `create_session`
(`src/kiro_crew/dashboard/session_control_parts/session_create.py`) files a
code-channel caller's workers in the caller's own folder and refuses a caller
with none (`code_channel_folder`). The conductor's slot is filed at start
(section 1, step 2), so that rule holds unchanged.

**Who may write in the thread.** The exemption refuses a turn steered by
anyone but the owner, but it refuses `create` for the whole turn, so one
non-owner message would block the owner's dispatch until the next turn. A code
channel avoids this by dropping non-owner text at `inbound_gate` before it is
routed. A Conductor thread gets the same owner-only gate in
`route_linked_thread`: for a slot with a thread binding, a message whose author
is not `is_owner` is not routed, gets one notice per author ("This thread is the
owner's Conductor thread."), and writes one SEL row. `is_allowed_user` is
`is_owner` on main today, so the people this stops are shared users
(`is_shared_user`, which `route_linked_thread` admits) and anyone the allow-list
admits if it ever widens. Bots and the app's own echo stay dropped as today.

**Shared users** (`slack.shared_access`): refused, as in a code channel, by the
gate above and by `shared_user_refused_in` / `shared_user_refused_for_slot`
reading both maps (section 2). Their `!stop`, `!compact` and `!title` are
refused by the existing `shared_user_refused_for_session` check in
`_route_bang_command`. The rest of the channel is unchanged for them. If
#40210 lands, Conductor threads can follow it in a later RFC.

**Where: rooms that are admitted.** A code channel is a channel the bot made
for the work; a thread sits where other people read. The status message, worker
titles and summaries, and worker approval cards (which show a redacted tool
input) are posted into the thread, so its readers are the room's members. So:

- **Admitted by default:** the app's DM with the owner (`im`), and private
  channels the bot is a member of (`is_private` from `conversations.info`).
- **Public channels:** only with `slack.thread_conductor_public_channels: true`
  (default off). Its description says that every member of the channel can read
  the workers' titles, states and approval cards.
- **Never:** group DMs, channels shared outside the workspace, and any room
  whose share or privacy reading is unknown. `audience.channel_shared` and
  `channel_archived` (`src/kiro_crew/slack/code_channels/audience.py`) already
  read `conversations.info` and fail closed while unread; PR A adds
  `channel_private` beside them, from the same answer's `is_private` / `is_im`,
  with the same fail-closed rule.

The room check is part of `_channel_state_refusal` for a thread binding, so a
channel that turns public or gets shared after the start stops dispatching on
the next judgement, not only at the start.

**Approval mode.** The conductor's slot starts on `normal` (Ask), the posture a
plain Slack thread's turn has today. Its workers inherit it through
`approval_inheritance` (`src/kiro_crew/dashboard/approval_inheritance.py`),
which never puts a descendant above its creator. A worker's approval card is
posted in the thread by `worker_approvals.py`, threaded under the anchor
instead of top level. **Trust this channel** on such a card becomes **Trust
this thread** and runs the same `approval_picker.set_channel_mode` mutation on
the conductor's slot, with its warning line. Like every owner chat's Trust, it
survives each gateway exit the owner did not choose and ends only on a stop the
owner chose (`src/kiro_crew/dashboard/chat_trust_restart.py`, per
[the 2026-11-18 decision](../decisions/2026-11-18-yolo-and-chat-trust-survive-every-exit-the-user-did-not-choose.md)).
A restore is bounded by the policy ceiling in force at that boot
(`session_mode_permitted` in `src/kiro_crew/approval_mode_policy.py`), and the
workers follow it as a picker pick makes them. The thread never runs above the
posture the owner chose for it, and a worker never runs above the thread.

### 5. What the thread shows

- **The conductor's own replies show normally**, streamed into the thread as
  any linked chat's replies are. The owner talks to the conductor, and the
  conductor's questions and plans are part of that conversation.
- **Workers show only as status.** No worker transcript, tool output or report
  body is posted. Their details are one click away: each row links to the
  worker's dashboard chat.

The status message is the code-channel work board posted in the thread and
edited in place. It reuses the board package
(`src/kiro_crew/slack/code_channels/board/`):

- **Data.** `read_board` (`board/source.py`) reads only the conductor's own
  ledger and its own workers, behind `scope_admits`. A thread binding passes
  the same scope check as a code channel binding.
- **Rows.** `WorkerRow` (`board/model.py`) already carries the fields asked
  for: the worker's title, its state, the current step, `pr_number`, `pr_state`
  and `pr_ci`. Summaries go through `published_summary`. Nothing else is
  published.
- **Links.** Each row's title links to the worker's dashboard chat
  (`worker_dashboard_link` in `code_channel_exemption.py`: a path link with no
  sign-in token). The header links to the conductor's chat.
- **One message.** The publisher (`board/publisher.py`) posts with `thread_ts`
  set to the anchor, and later edits it with `chat.update`. It is not pinned:
  `pins:write` is a code-channel scope, and a pin on a thread reply would
  surface in the whole channel. The per-room records the publisher and the
  surface store keep (`board_ts`, `board_digest`, the edit gap and retry maps)
  are keyed by the binding's room, `channel_id` for a code channel and
  `"<channel_id>:<anchor_ts>"` for a thread.
- **Fallback.** Where Slack refuses the `plan` block, the existing `rows` look
  is used (`_PLAN_REFUSED`), so the board never needs a Slack feature the
  workspace lacks.
- **When it refreshes.** The same triggers as a code channel, without the
  workspace's folder watcher: the conductor's turn end, a worker's turn start
  or end and an approval (`note_session_turn`), and a ledger move
  (`note_ledger_write`, `_on_work_fold`). A thread binding is found through the
  store's in-memory record of which conductor created each worker, as
  `_lineage_channel_of` does today.
- **PR and CI.** A code channel fills them from its own repository
  (`workspace/worker_prs.py`). A thread has no worktree, so the row shows the
  PR number the worker reported (`artifacts["pr"]`), and CI when the
  conductor's project is a GitHub checkout `gh` can read. Otherwise no CI
  column.
- **Needs you.** A worker that waits on a person gets the board's one threaded
  reply mentioning the owner (`render`'s needs-you reply), in the thread.

The Code tab, the Workers tab, the context bar and canvases are code-channel
surfaces (they need `agents.conversations.*` or `canvases:write`) and are not
offered.

### 6. Slack scopes

None new. Every call is one the base manifest
(`src/kiro_crew/slack-manifest.yaml`, the lines without `#@code_channels`)
already grants:

| Call | Scope |
|---|---|
| Read the thread to import it (`conversations.replies`) | `channels:history`, `groups:history`, `im:history` |
| Share, privacy and archive reading (`conversations.info`) | `channels:read`, `groups:read`, `im:read` |
| Post the parent, the notices and the status message; edit it (`chat.postMessage`, `chat.update`) | `chat:write` |
| `/kirocrew conductor` | `commands` |

`assistant:write` (agent sessions) is used when the install already has it, for
the thread's native status, and is not required. Because the runtime paths ask
`room_runtime_on` instead of `code_channels_on` (section 2), none of them waits
for a code-channel scope.

### 7. Restart and resume

- The binding is on disk, so after a restart the thread's next message reaches
  the same conductor slot.
- `resume_after_restart` (`src/kiro_crew/slack/code_channels/resume.py`) walks
  thread bindings too, gated by `room_runtime_on`: a turn the restart cut off
  is continued once, with the same "I resumed where I left off" notice and
  owner-only Stop or Resume buttons, posted in the thread. An ended binding is
  not resumed.
- The board's message ts is on disk (the surface store), so the next refresh
  edits the same message. A message Slack has lost (`cant_update_message`) is
  posted again in the thread, as today.
- The conductor's approval mode comes back as section 4 says: kept across an
  exit the owner did not choose, cleared by a stop the owner chose.
- The sender record (`note_channel_sender`) is in memory. After a restart, a
  turn started from the thread before the owner writes again is refused with
  the existing "send a message in the channel and retry" reason; a resumed turn
  carries no channel text and passes.

### 8. Limits

1. Owner only. Shared users cannot start, write in or drive one.
2. By default only the app's DM and private channels. Public channels need
   `slack.thread_conductor_public_channels`. Never group DMs, channels shared
   outside the workspace, code channels, or threads already linked to a
   dashboard chat or running a Bot.
3. Workers are one level deep, run at most at the thread's approval mode, and
   share the conductor's project checkout.
4. Worker details are never shown in the thread, only status and links.
5. No Code tab, canvas or pinned board.
6. Not on the LTS build (`conductor_mode_offered` is false there).
7. One way: no command removes a live binding. Closing the chat ends the
   thread; it never becomes a plain thread again (within the `MAX_REVOKED`
   bound, section 3).

### 9. Implementation: four PRs

**PR D must merge before the switch can be turned on**: dispatch with no Stop
button and no approval card in Slack is not safe. So the config key
`slack.thread_conductor` (and `slack.thread_conductor_public_channels`) is
added to `src/kiro_crew/config/sections_slack.py` in PR D, not earlier. Until
then every reader takes the absent field as off (the `getattr(..., False) is
True` reading `_switch_refusal` already uses), the start commands refuse, and
tests in A, B and C turn it on by patching the reader. C and D are independent
of each other.

**A. Identity and dispatch** (findings 3, 4, 7).
Files: `slack/code_channels/binding.py`, `store.py`,
`dashboard/session_control_parts/caller_access.py`, `code_channel_exemption.py`,
`slack/code_channels/shared_refusal.py`, `slack/code_channels/audience.py`,
`session_create.py`.
Exit criteria:
- A store written before this PR loads unchanged; every existing code-channel
  test passes.
- A thread binding for a slot linked to its anchor and filed in a folder makes
  `judge_code_channel` admit `create` on an owner-recorded turn, and a forged
  `thread_bindings` row naming a slot not linked to its anchor is refused.
- The same binding with the switch off, an unknown share or privacy state, a
  public channel without the opt-in, or an `ended_at` stamp is refused.
- `shared_user_refused_in(channel, thread_ts)` and
  `shared_user_refused_for_slot` refuse a shared user in a Conductor thread.
- A slot with no folder is refused `code_channel_folder`, as for a code channel.

**B. Commands and locks** (findings 1, 5, 8; needs A; lands after #41222).
Files: `slack/handler_runtime/commands.py`, `inbound.py`,
`slack/events_slash.py`, `slack/handler.py`,
`dashboard/chat_api/agent_switch.py`, `dashboard/chat_api/slot_lifecycle.py`.
It shares #41222's `!bot` handler table and adds `THREAD_SWITCH_COMMANDS` and
one Bot-versus-Conductor refusal used by both commands.
Exit criteria:
- With the switch forced on in tests, owner `!conductor` in a private-channel
  thread creates one slot filed in its folder, one thread binding with
  `team_id`, records the owner as author, and the goal turn's
  `session_create` is admitted.
- `/kirocrew conductor <goal>` posts one parent and does the same under it; in
  a channel the bot is not in, it answers ephemerally and posts nothing.
- In a Conductor thread, `!conductor`, `!bot`, `!ta`, `/agent`, `!agent`,
  `!link-to-dashboard` and a dashboard agent switch each get section 3's
  answer and change nothing; none reaches the model.
- A shared user's message in a Conductor thread is not routed and gets one
  notice; the owner's next turn can still dispatch.
- Closing the conductor's chat stamps `ended_at`, posts the notice once, and
  the thread's next message gets the notice and no turn.

**C. Board in the thread** (needs A).
Files: `slack/code_channels/board/publisher.py`, `source.py`, `render.py`,
`slack/code_channels/surface.py`, `surface_store.py`.
Exit criteria:
- Two workers dispatched produce exactly one bot message in the thread with two
  rows; later state changes edit it and post nothing new; no `pins.add` call.
- No row contains transcript text; each title links to the worker's chat.
- A code channel's board is unchanged (its tests pass).

**D. Approvals, Stop and resume** (finding 2; needs A).
Files: `slack/code_channels/worker_approvals.py`, `approval_picker.py`,
`worker_stop.py`, `stop_helpers.py`, `resume.py`, `routing.py`, and the two
config keys in `config/sections_slack.py`.
Exit criteria:
- On an install with no code-channel scope, `room_runtime_on` is true for a
  thread binding and every listed site uses it.
- A worker's approval card appears in the thread; Trust this thread raises the
  conductor and its workers, never above it.
- `!stop` in the thread stops the conductor and its workers.
- A restart during a worker's turn resumes it once and posts one notice in the
  thread.
- `kirocrew config set slack.thread_conductor true` is accepted only from this PR on.

**Rollout.** After D, turn on with `kirocrew config set slack.thread_conductor
true`; it applies on the next call, no restart, no manifest change. Default-on
is a separate decision for a later change once the four PRs have run on real
gateways.

## Test plan

- **Unit, exemption (A):** `judge_code_channel` with a thread binding for each
  clause in the section 4 table, admitted and refused, including the room
  rules and `ended_at`.
- **Unit, store (A):** round-trip of `thread_bindings`; an old file without it;
  many threads in one channel; `bindings` readers unaffected; ended bindings
  moved to `revoked` after 30 days.
- **Unit, shared users (A, B):** both refusal readers and the thread gate.
- **Unit, commands (B):** every refusal in section 1 and every row of the
  section 3 table, with its SEL row; `!conductor` reaches its handler from a
  linked thread; the `/kirocrew conductor` owner check by `is_owner`; the
  owner recorded as author at start.
- **Unit, board (C):** threaded post and edit, no `pins.add` call, room-keyed
  records, `rows` fallback.
- **Unit, runtime gate (D):** each site that took `code_channels_on` with a
  binding answers by `room_runtime_on`; with code channels off and no scopes,
  approvals, Stop and resume work for a thread binding.
- **Integration (after D):** a fake Slack client drives owner `!conductor` →
  conductor `session_create` → worker approval card → worker report → one
  edited status message; a shared user's message mid-turn is dropped and the
  owner's dispatch still works; closing the chat ends the thread.
- **Manual, real Slack workspace** with the app as installed today (no
  manifest change): the app's DM, a private channel thread, a public channel
  thread refused and then admitted with the opt-in, a restart mid-turn.

## Backward compatibility

Compatible. Every change is behind `slack.thread_conductor`, default off, and
the switch cannot be turned on before PR D. With it off, `!conductor` answers
that the feature is off and changes nothing, `/kirocrew conductor` answers the
same, and every existing path reads the same data: a store without
`thread_bindings` loads as today, and `Binding.thread` defaults to `False`.
`room_runtime_on` answers exactly `code_channels_on` for every code-channel
binding. `!conductor` is a new word; today it gets "Unknown command", so
nothing that works stops working. No scope, manifest or code-channel behaviour
changes.

## Security considerations

- **Audience.** A thread is read by its room's members, unlike a code channel,
  which the bot makes for the work. So the default rooms are the owner's DM
  with the app and private channels; a public channel takes an explicit opt-in
  whose description names what its members will see; a room shared outside the
  workspace is never admitted, and the room is re-judged on every dispatch.
  What is posted is only the `WorkerRow` fields, defused and redacted as a code
  channel's board is, and redacted approval cards; worker transcripts are never
  read into the thread (`read` and `summary` stay outside
  `EXEMPT_TARGET_OPERATIONS`).
- **Who drives.** Only the owner by identity, on every turn: non-owner text is
  not routed into a Conductor thread at all, and `_sender_refusal` still checks
  what was routed. Other people's thread text imported at the start is fenced
  untrusted context; it is content, which no author check closes, and is
  bounded by reach (creator fence, one level, the thread's approval mode), as
  for a code channel's origin thread.
- **No widening by agent.** The agent is locked to the conductor for the
  thread's life (section 3), and closing the chat ends the thread instead of
  handing it to a plain agent.
- **No dispatch without brakes.** The switch cannot be turned on until Slack has
  the Stop button and the approval cards for this thread (PR D).
- **Fails closed.** An unloaded store, an unknown share or privacy state, an
  unreadable config or an unrecorded sender refuses, as today.
- **Every decision is audited** with an SEL row: start, refusal, judgement,
  end.

## Alternatives considered

1. **Thread-scoped slash command.** Slack does not deliver an app's slash
   command from a thread and sends no thread ts, so it cannot be built.
2. **Relax the containment for any thread whose agent is the conductor.** That
   keys a security exemption on an agent name anyone with `!ta` can set, and
   brings no binding, board or resume. Rejected.
3. **Open a code channel from the thread** (the code channels' own
   `maybe_open_code_channel`). Works today, but needs the six code-channel
   scopes and moves the work out of the thread. It stays available.
4. **A two-way toggle (`!conductor off`).** Rejected for the reasons in
   section 3.
5. **A separate thread-conductor store and exemption.** Duplicates every
   clause the code-channel exemption already proves, and two predicates would
   drift. Rejected for one predicate with a room that can be a thread.
6. **Public channels on by default.** Rejected by the owner's ruling: the
   room's members would read worker titles and approval cards without anyone
   choosing that.

## Open questions

1. Should `/kirocrew conductor` with no goal open a modal for it instead of the
   usage line?
2. When #40478 (conductor wrap-up) lands, should wrap-up of a Conductor thread
   also post the board's outcome lines (`outcome_lines`) in the thread, as
   `/wrap-up` does in a code channel? No PR here depends on the answer.

Revision 1's open question on the conductor's own replies is closed by the
owner's ruling: they show normally (section 5).

## Review record

Revision 2 answers an independent review read against `main` at `dcf3c38117`.
Each finding was checked against the code before it was taken.

| # | Finding | Checked | Where answered |
|---|---|---|---|
| 1 | `!conductor` / `!bot` in a linked thread reach the model | true: `route_linked_thread` falls through only for `_BANG_TO_SLASH` words | section 1, `THREAD_SWITCH_COMMANDS` |
| 2 | `code_channels_on` (toggle and scopes) gates board, approvals, Stop and resume | true: `routing.py`, `resume.py`, `worker_approvals.py`, `worker_stop.py`, `approval_picker.py`, `surface.py` | section 2, `room_runtime_on`; PR D |
| 3 | `session_create` refuses a code-channel caller with no folder; no worktree | true: `code_channel_folder` in `create_session` | section 1 step 2, section 2 "Project and checkout" |
| 4 | the shared-user refusal reads `bindings()` and channels only | true: `shared_user_refused_in`, `shared_user_refused_for_slot` | section 2 reader list, section 4 |
| 5 | non-owner users route into a linked thread | partly: `is_allowed_user` is `is_owner` on main, so no allowed non-owner exists; shared users do route in | section 4, owner-only thread gate |
| 6 | the audience is not a code channel's | true | section 4 "Where", Security |
| 7 | the goal turn records no author, so `create` is refused | true: only `route_linked_thread` calls `note_channel_sender` | section 1 step 4 |
| 8 | closing the chat falls back to a plain thread; no cleanup | true | section 3 "Ending", "Cleanup" |
| 9 | `code_channel_conductor_agent` needs `conductor_mode` | true | section 1 step 2 uses `crew_mode.crew_mode_agent` |
