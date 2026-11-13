---
title: Agent self-rename — a core chat_rename directive for the calling session
status: in-progress
author: mohamed-youssef-bou
created: 2026-11-04
last-audited: 2026-11-10
audited-at: 77fabdd1f
doc-pr: 33633
implementation-prs: [33736]
tracking-issues: [33624]
supersedes: []
superseded-by: []
---

# RFC: Agent self-rename — a core `chat_rename` directive for the calling session

An agent on the default spec cannot rename the session it is running in. The
only agent-facing rename, `session_rename`, lives in the opt-in
`kirocrew-dashboard` server, which the default agent's spec deliberately does
not mount, and it takes a `target` the agent has no way to learn. This RFC adds
`chat_rename(title)`: a `kirocrew-core` session directive, shaped like
`chat_tag`, that renames only the session whose turn produced it and never
writes over a name a person chose. The decision this document records is
putting a self-only rename on the always-on core surface, the gate it runs
behind, and whose title it may replace.

## Summary

1. **Self only, no target.** The session-aware consumer applies the directive
   to its own slot, so there is no key lookup to get wrong.
2. **A person's turn only.** `chat_rename` joins `USER_SURFACE_DIRECTIVES` and
   also requires a person's actor, a turn still marked as the person's
   (`_turn_user_sent`) and no channel provenance. A cron, sub-agent,
   task-runner, wake or channel turn is refused, a script cron's own session
   included, and so is a person's turn once a peer's, an app's or a channel's
   text is steered into it.
3. **`session_rename`'s rules, through the same functions.** The title rules
   and the refusals for a session it cannot reach are `session_rename`'s, with
   one more for a channel's own conversation, and the write is
   `apply_manual_title`, the sidebar's own.
4. **Never over a person's name.** A title someone set deliberately (origin
   `user`) is kept. The agent's title is stored with a new origin, `agent`:
   final for the auto-titler like `user`, and replaced by the person's next
   rename. "Regenerate title" hands the name back to the agent, except on a
   crewmate's DM thread, where it changes nothing.
5. **Incognito and temporary sessions included.** Memory mode limits what is
   learned from a session and who else may address it, not its own label.
6. **Not behind `agent.session_control`.** It reaches no peer session, as
   `chat_tag` reaches none. A conductor reaches it only through an approval
   prompt.
7. **The naming convention stays the user's.** Lessons, steering and prompts
   say when and how to name a session; the tool only applies a title.

## Motivation

### Current state

Verified on `main` at `77fabdd1f`.

- `session_rename(target, title)` (`mcp_dashboard_session_tools.py`, route
  `POST /api/session-control/rename`) renames the caller or any session
  `session_stop` could reach. `rename_target` runs `_clean_rename_title`, then
  `authorize_target` with `allow_self=True`, then
  `chat_title.apply_manual_title`.
- `apply_manual_title` audits every rename it applies. Before its first await
  it calls `title_audit.record_manual_rename`, which writes a
  `chat.manual_rename` row to the security event log with the rename's `route`
  and the old and new titles; the log's writer redacts credential-shaped text
  in them. Every caller must pass a `route` and a `caller`: the sidebar passes
  `rename_route`, `session_rename` passes `session_rename`, and a channel's
  `/title` passes `channel_title`.
- `kirocrew-dashboard` is `opt_in` in `agent.py`: neither spec writer emits it,
  so the default agent mounts none of its tools.
- No tool reports the caller's own session key, and `list_sessions` does not
  mark the caller.
- `chat_tag` (`control_parts/directive.py`) is the precedent for a core,
  self-only, user-surface directive applied by `session_directive_apply.py` to
  the turn's own slot.
- `/api/chat` gives a send the user-origin mark unless an app, a crew or a
  card sent it, so a script cron's `send_to_session` carries the mark too,
  with `_turn_actor` `cron`. The user-origin gate reads only the mark. Code
  that must know a person sent the turn reads the actor as well.
  `shared_crew.turn_gate._PERSON_ACTORS` names the person actors: `""`, and
  `user`, which a person's recovered turn carries. The milestone counters count
  nothing for a marked turn with any other actor
  (`test_a_user_origin_turn_with_a_non_user_actor_counts_nothing`).
- `slot._turn_user_sent` says the running turn is still the person's. It is
  set when the turn starts from the user-origin mark, unless a channel sent
  the turn, and cleared before a peer's or an app's text is steered into it
  (`chat_delivery.steer_into_running_turn`). The guide's user-only operations
  refuse a turn without it as `not_user_turn`. A channel's steer leaves it set
  and marks the turn `_turn_channel_narrowed` instead; the guide refuses that
  turn, and one a channel opened (`_turn_channel_origin`), as `channel_caller`.
- A titled session carries a `title_origin` (`chat_title._TITLE_ORIGINS`):
  `auto` from the background titler, which alone it refreshes; `user` from a
  sidebar rename, a channel `/title`, `session_rename`, a title pinned at
  creation or a workflow session's title; `member` for a crewmate's DM thread,
  whose writer (`name_member_thread`) already keeps a `user` title. A few
  writers record none (`session_create` with a title); a reload reads a missing
  origin as `user` (`_rehydrate_title_origin`).
- `mrkdwn_title.stored_title_is_mrkdwn` reads a Slack session's stored title
  as Slack mrkdwn unless its origin is in `_TYPED_TITLE_ORIGINS` (`user`,
  `member`): a title a person typed is kept as typed.
- The dashboard shows a channel's conversation in a tab marked
  `channel_origin` (`channel_slots.surface_channel_session`). The tab is bound
  to the channel session through `linked_session_key`, or left unbound with
  none when that key cannot be resolved. `_live_target_refusal` reads only
  `linked_session_key`, so it refuses a bound tab as `linked_session_target`
  and lets an unbound one through.
- The background titler names incognito and temporary sessions like any other
  (`chat_title.py`: titling "runs for EVERY `memory_mode`").

### Problems

- Users keep naming conventions in their own instructions (#13797's example is
  `JIRA-XXXX — <ticket title>`). On the default agent the instruction cannot be
  carried out; the agent can only ask the person to rename by hand.
- Generated titles are often weak, which is why the dashboard offers
  "Regenerate title". The agent working in a session knows what it is about and
  can name it better, but only if it can never overwrite a name a person chose.
- #13797 asked for this and was closed as completed by #15164, which serves
  only agents that mount the dashboard server.
- Mounting `kirocrew-dashboard` on the default agent to get rename grants the
  whole server unless the spec is hand-edited with a per-tool ref and
  `managedToolPolicy.honourToolRefs`. Even narrowed, `session_rename` reaches
  every live session in the workspace from an unfenced owner session, it
  replaces any title, and the agent still has to guess its own `target`.

### Why this needs an RFC

It adds a tool to `kirocrew-core`, the surface every session carries, and a
title origin every reader of `title_origin` must accept. The default agent's
surface is a product decision the First Principles lane reads from base.

## Goals

- An agent can rename the session it is running in, on the default spec, when
  a person's turn asks for it or the person's own instructions call for it.
  Either way the rename happens on a turn a person sent: a convention kept in
  lessons or steering is not applied on a cron or wake turn.
- No new reach: the directive can touch only its own session.
- A name a person chose is never replaced by an agent.

## Non-goals

- Renaming another session. That is `session_rename`'s job, unchanged; it
  still writes `user` and may replace any title.
- An unattended or prompt-free self-rename for conductors (#17585, closed as
  not planned). A patrol wake is not a person's turn, so this gate refuses it,
  and no conductor grant list auto-approves the tool.
- Changing what the auto-titler writes (#27705) or reading a title directive
  from a message (#13144).
- A configurable naming pattern. A convention needs context the titler does
  not have; the agent that has the context applies it.
- Renaming a channel's own conversation, including the dashboard tab that
  shows it. Channels have their own title path.

## Design

### The tool

`chat_rename` on `kirocrew-core` takes one required string, `title`. The schema
bounds only the transport; the 200-character rule applies after stripping, in
the consumer, exactly as `SESSION_RENAME_SCHEMA` documents. The tool validates
its arguments and returns a directive (`_emit_directive("chat_rename",
{"title": ...}, ...)`), so it resolves no session identity, like every other
directive tool.

The rename is applied after the tool returns, so the tool's own reply is the
only text the agent reads in that turn; the outcome reaches the transcript's
tool row. The reply therefore says the rename was requested, that a title the
person set by hand is kept, and which callers and sessions are refused, so the
agent reports a request rather than a finished rename.

The name follows the server it lives on. In `kirocrew-core`, the one self-only
directive that changes the session's sidebar row is `chat_tag`; the `session_*`
names belong to the dashboard server's 23 session-control tools, among them
`session_rename(target, title)`, so `session_rename_self` (#17585's name) would
read as one of them. A `_self` suffix appears once in the codebase, on the
crewmate server's `update_self`. The tool's description says it renames only the
calling session.

### Who may issue it

`chat_rename` joins `DIRECTIVE_TOOLS` and `USER_SURFACE_DIRECTIVES`. The
existing gates in `apply_session_directive_outcome` then apply unchanged: a
slot-less caller is refused, and the user-origin gate admits only a turn
carrying the user-origin mark on a user-facing conversation. A cron turn on a
person's slot, a sub-agent sharing its parent's slot and a message an app sent
carry no such mark. A cron job or a workflow agent step, whose stream no
session applier reads, has the call refused and audited like every other
user-origin directive (`person_only_directive_refuser`).

The mark alone does not prove a person sent the turn. A script cron's
`send_to_session` carries it too, with `_turn_actor` `cron`, so the gate
admits that turn on a session the cron created. `chat_rename` therefore also
checks what main reads elsewhere to tell a person's turn. Unless all three
hold, it refuses as `not_user_turn`, the guide's code for a turn the person
did not send:

- a person's actor: `producer_turn_actor` in `_PERSON_ACTORS`, which the
  milestone counters also require;
- `slot._turn_user_sent`, which the guide's user-only operations require; the
  dashboard clears it before a peer's or an app's text is steered into the
  turn;
- no channel provenance: neither `slot._turn_channel_origin` nor
  `slot._turn_channel_narrowed`, which the guide also refuses.

A typed send, a person's recovered turn and a Spec Builder decision answer the
person picked pass all three. A `cron`, `app` or `crew` turn, a channel turn
and a turn with a peer's, an app's or a channel's steer do not. All three are
main's own checks: `chat_rename` combines them and adds no classification of
its own.

Every spec that mounts `@kirocrew-core` sees the tool. The default agent
auto-approves `@kirocrew-core`, so it renames without a prompt. The conductor
specs (`agent_materialization/conductor_agents.py`) mount `@kirocrew-core`
whole but auto-approve only the verbs in their own grant lists, so a conductor
reaches `chat_rename` only through an approval prompt, only on a turn the person
sent in that chat, and never over a title the person set. `chat_tag` reaches
them the same way today. The conductor decision of 2026-11-04
(`docs/decisions/2026-11-04-conductors-stay-simple-and-new-abilities-go-to-workers.md`)
requires an explicit owner decision on any capability a conductor gains, so
conductors reaching the tool is its own sign-off item, separate from accepting
the tool (see Sign-off).

Like `chat_tag`, the tool has no setting that turns it on or off, so rolling
it back means reverting the implementation.

### Whose title it may replace

| Current title | Written by | Auto-titler refreshes it | `chat_rename` replaces it |
|---|---|---|---|
| none | — | — | yes |
| `auto` | background titler, "Regenerate title" | yes | yes |
| `member` | the system, on a crewmate's DM thread | no | yes |
| `agent` | `chat_rename` | no | yes |
| `user` | sidebar, `/title`, `session_rename`, a pinned or workflow title | no | no: refused as `manual_title` |
| none recorded | `session_create` with a title, some app and channel slots | no (a reload reads it as `user`) | no: refused as `manual_title` |

A `member` title is not a person's choice for that thread:
`name_member_thread` writes the crewmate's display name each time the thread
is opened, over no title, an `auto` one or an earlier `member` one. A person
who renames the thread writes `user`, which `name_member_thread` then keeps.

A person's rename always wins: it writes `user` over any origin. To let the
agent name a session the person named, they choose "Regenerate title", which
writes `auto`, or rename it themselves. `name_member_thread` keeps an `agent`
title as it keeps a `user` one, so reopening a crewmate's thread does not undo
a rename the person asked for. `user` keeps its existing, broader meaning: some
writers of `user` are tools, and reading all of them as a person's choice is the
conservative side.

### Application

`_apply_chat_rename` runs on the turn's own slot, in this order:

1. `_clean_rename_title(title)`: empty, over 200 characters (before or after
   redaction), control, text-direction, invisible format and U+2800 characters
   are refused as `invalid_title`; credential-shaped text is redacted. As in
   `rename_target`, a malformed title is refused before any access check.
2. The slot checks: it is still the live slot for its key and not closing
   (else `target_replaced`); `_live_target_refusal(slot, allow_ephemeral=True)`
   passes, so an unattended, app-scoped or channel-linked session is refused
   with the code `session_rename` uses; it is not a channel's tab (a
   `channel_origin` tab is refused as `linked_session_target`, bound or not);
   the turn is still the person's (a person's actor, `slot._turn_user_sent`
   and neither channel flag, else `not_user_turn`); and its title is one an
   agent may replace: untitled,
   or an origin in
   `chat_title._AGENT_REPLACEABLE_ORIGINS` (`auto`, `member`, `agent`). Any
   other titled origin, a missing one included, is refused as `manual_title`:
   the list is an allowlist so a writer that records no origin is protected at
   once, not only after a reload reads it as `user`.
3. `_has_channel_mirror(state, slot)` refuses a session mirrored to a channel
   (`mirrored_target`). It reads the session store through getters whose lock
   an off-loop writer holds across a disk write, so it runs in a worker thread.
4. The slot checks run again, so a close, replacement, channel link, a
   person's rename, or a peer's, an app's or a channel's steer during the probe
   is refused.
   Nothing awaits between this
   check and the write. The allowed decision is audited here, before the
   write: closing the session cancels its turn, which can land during the
   save, and the audit must still hold the decision behind the new title.
5. `apply_manual_title(state, slot, title, route="chat_rename",
   caller=session_key, origin="agent")`, with the session's own key as the
   caller: epoch bump, the `chat.manual_rename` audit row, persist, broadcast.
   A failed save reports that the title is live but not yet saved, as
   `rename_target` does.

`allow_ephemeral` waives only the memory-mode refusal. `session_rename` refuses
an incognito or temporary target because another session must not address it;
a session renaming itself addresses nothing else, the background titler
already names such sessions, and the person can rename them by hand.

The `channel_origin` check covers the tab `_live_target_refusal` lets through:
one the dashboard left unbound because it could not resolve the channel
session. That tab still shows the channel's own transcript, and a later rebind
links it to the channel. `_live_target_refusal` itself stays as it is, because
`session_rename` and resuming a session share it. The person can still rename
a channel's tab by hand in the sidebar.

The mirror probe is a point-in-time read: a mirror linked after it reads and
before the write is not seen. `rename_target`'s on-loop read has the same
window against an off-loop link writer, and the sidebar's own rename has no
mirror check at all.

The reply carries the stored title. The directive's own audit line,
`chat.self_rename`, records each decision with the title length or the refusal
code, never the title text. A rename it applies is recorded like every manual
rename as well: `apply_manual_title` writes its `chat.manual_rename` row, with
route `chat_rename` and the old and new titles.

## Migration plan

One phase, one PR, which links this document.

Exit criteria:

- `kirocrew-core` lists `chat_rename`, and it is in `DIRECTIVE_TOOLS` and
  `USER_SURFACE_DIRECTIVES`.
- A person's turn renames its own slot with title origin `agent`, and the
  auto-titler no longer refreshes it.
- A `user` title is never replaced, including one set while the mirror probe
  runs; after "Regenerate title" the agent may rename again.
- A titled session with no recorded origin is never replaced either, before a
  reload as well as after one. That case is why `_AGENT_REPLACEABLE_ORIGINS` is
  an allowlist, and a test pins it
  (`test_a_title_given_at_creation_with_no_recorded_origin_is_kept`).
- An incognito or temporary session renames itself.
- A cron, sub-agent, wake or channel-opened turn is refused and the title is
  unchanged, including a script cron's `send_to_session` turn on a session it
  created. A person's recovered turn, actor `user`, still renames.
- A person's turn into which a peer's, an app's or a channel's text was
  steered is refused as `not_user_turn`, and the title is unchanged. After a
  channel's steer, the rename stays refused even if the session's channel
  mirror is removed before the directive applies.
- A `channel_origin` tab is refused as `linked_session_target`, bound or not,
  and its title is unchanged.
- A title with origin `agent` is never read as Slack mrkdwn.
- An `agent` origin survives a reload, and an older version reads it as `user`.
- The `chat.self_rename` line carries the title length or the refusal code,
  never the title, and an applied rename writes the `chat.manual_rename` row
  that every rename writes, with route `chat_rename`.

## Backward compatibility

Additive. No existing tool, route or default changes, and `session_rename` and
the sidebar rename behave as before. `apply_manual_title` gains an `origin`
keyword that defaults to `user`, and `_live_target_refusal` an `allow_ephemeral`
keyword that defaults to `False`. A stored `agent` origin read by a version that
predates it falls through `_rehydrate_title_origin` to `user`, which is final,
so a downgrade can never make an agent's title refreshable. `agent` joins
`mrkdwn_title._TYPED_TITLE_ORIGINS`, so a title the agent wrote is never read
as Slack mrkdwn, as a person's is not.

A crewmate's DM thread behaves differently once the agent names it: the thread
keeps the agent's title instead of taking the crewmate's display name each time
it is opened, so reopening it does not undo a rename the person asked for.
"Regenerate title" changes nothing on a crewmate's thread, so a person who wants
the crewmate's name back renames the thread to it; that writes `user`, and the
thread keeps that name from then on.

## Security considerations

- No new reach: no target argument exists, and the consumer applies the change
  to the slot it already holds.
- Holding the slot rather than resolving a key removes the key-reuse window
  #20064's review found, where a tab closed and recreated under the same key
  could receive a self-rename meant for its predecessor.
- The same redaction and character rules as `session_rename`, through the same
  function, so the two paths cannot store titles under different rules.
- Only a turn that main's own checks count as the person's can apply it, so
  ingested content in an unattended turn cannot relabel the session, and
  neither can a peer's, an app's or a channel's text steered into a person's
  turn.
- Inside a person's turn, content the agent reads, such as a page or a tool
  result, could still steer it into a rename. The result is a label on the
  person's own sidebar, bounded by the same character and redaction rules,
  which they see and can change, and it can never replace a name they chose.
- Allowing incognito and temporary sessions exposes nothing new: the title is
  the same label the background titler or the person could already set, and
  the security event log records it in the same `chat.manual_rename` row that
  the person's own rename of that session writes.

## Alternatives considered

- **Mount `@kirocrew-dashboard/session_rename` in the default spec.** Rejected:
  it reverses the opt-in default, keeps the cross-session reach and leaves the
  agent without its own key.
- **`session_rename_self` in `kirocrew-dashboard` (#17585, #20064).** Aimed at
  conductors, which mount that server, as an auto-granted rename; unreachable
  for the default agent. #17585 was closed as not planned because a final,
  prompt-free self-rename would let an unattended conductor replace a title the
  person typed. This design is neither prompt-free for conductors nor able to
  replace a title the person set.
- **A `target`-less mode on `session_rename`.** Same reachability problem.
- **A naming pattern in the auto-titler.** The titler reads the opening turns
  only, and #13144 declined message-borne directives.
- **Let the agent replace any title, as `session_rename` does.** Rejected: a
  name a person chose is the strongest signal about a session, and an agent
  overwriting it would make the agent's name as unwelcome as a bad generated one.
- **A separate origin for a title a person typed, distinct from the tools that
  write `user`.** Not needed now: it would change what every stored `user`
  title means, and treating them all as chosen only makes the agent more careful.
- **Refuse incognito and temporary sessions, for parity with `session_rename`.**
  Rejected: the refusal exists so other sessions cannot address them, which a
  self-rename does not do.
- **Do nothing.** People keep renaming by hand.

## Consequences

- An agent that knows the person's convention can apply it without a prompt or
  a spec edit.
- `kirocrew-core` carries one more small tool schema, and `title_origin` one
  more value.
- A person who named a session and then asks the agent to rename it is told to
  rename it in the sidebar or choose "Regenerate title" first.
- A fork copies any titled origin other than `auto` as `user`
  (`chat_fork._shape_fork_child`, unchanged), so a fork of an agent-named
  session keeps its name until the person renames it or regenerates it.
- An agent-set title is final for the auto-titler, so a wrong one stays until
  the person or the agent renames it again.
- `session_rename` keeps writing `user` (open question 1), so a title an agent
  set through `session_rename` cannot later be replaced by `chat_rename`, which
  refuses it as `manual_title`. The agent can ask the person to choose
  "Regenerate title" first. This is the cost of leaving `session_rename`
  unchanged, not a defect.

## Sign-off

Two decisions, recorded separately:

1. **The tool: accepted 2026-11-10.** The maintainer Bolin Chen (@bolichen97)
   accepted `chat_rename` as designed in the
   [approving review of #33633](https://github.com/kirodotdev/KiroCrew/pull/33633#pullrequestreview-5846058182),
   and settled open question 1 as proposed.
2. **Conductors: open.** Whether a conductor may reach `chat_rename` behind its
   approval prompt. Under the 2026-11-04 conductor decision this is an explicit
   owner decision, separate from item 1, and the review above does not cover
   it. Proposed answer: open question 2.

## Open questions

1. **`session_rename` and origin `agent`.** `session_rename` still writes
   `user`, so a session an agent renamed through it is protected from
   `chat_rename`. Proposed: leave it, since it may rename other sessions and
   changing what it writes would widen this RFC. Consequences records the cost.
   Settled as proposed on 2026-11-10 (Sign-off, item 1).
2. **Conductors.** A conductor sees `chat_rename` behind an approval prompt, as
   it sees `chat_tag`. Proposed: keep it. That needs no new code, a patrol or
   wake turn is refused anyway because it is not a person's turn, and the tool
   cannot replace the person's title. The alternative, a per-tool deny, would
   add a mechanism the conductor specs do not have today. This is the owner's
   decision (Sign-off, item 2).
