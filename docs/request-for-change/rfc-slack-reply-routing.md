---
title: Slack page replies route back to the sending session
status: draft
author: phantom-tim
created: 2026-10-08
last-audited: 2026-10-08
audited-at: 8c7417ea82b843875507593d712ac0eff6491644
doc-pr:
implementation-prs: []
tracking-issues: [17933]
supersedes: []
superseded-by: []
---

# RFC: Slack page replies route back to the sending session

- Status: draft. Nothing built. Acceptance requested from a maintainer; the
  status flips to `accepted` when one records it here. This document lands first
  because it narrows what a reply to an agent's Slack page does, which the First
  Principles lane reads as a product-shape decision off the base branch.
- Author: phantom-tim
- Created: 2026-10-08
- Related: `rfc-notification-bridge.md` (accepted, not built; its Loop safety
  section records that a reply to a bridged DM is "an ordinary inbound message
  to the channel agent", which is the behavior this document narrows for
  opted-in pages), `rfc-session-address-model.md` (its surface model, with
  channels as attachments, is the natural home for the mirrored-session question
  this document leaves out), issue #17933

## Summary

`send_message` gains an opt-in flag that binds a Slack DM to the session that
sent it. A threaded reply from the owner to that DM is delivered to the sending
session as a user turn, instead of starting a new session. Every message and
reply that does not opt in behaves exactly as it does today.

The binding is its own persisted record, separate from the Slack link and the
outbound mirror. The mirror readers never see it, so the sending session is
never marked mirrored and keeps every tool it has.

## Motivation

### Current state

Observed by direct testing and confirmed in source at main `8c7417ea8`:

- `send_message(session="slack")` delivers one targeted DM to the owner and
  returns its `ts`. It does not mirror the sending session. The handler returns
  `ts` and `delivered_to` from `_send_message_response` in
  `dashboard/messaging_api/proactive_send.py`, and the tool surfaces it in
  `mcp_tools/messaging.py`.
- A threaded reply to that DM starts a new Slack-born session. The inbound path
  resolves a thread to its owner with `get_session_for_thread(reply_ts)` and,
  finding none, uses `canonical_key(reply_ts)`, a fresh `slack:<ts>` key, in
  `handle_message` (`slack/handler.py`). `slack/thread_parent.py` names this
  exact case in its module docstring: "the owner answering a DM an agent sent
  with `send_message(session="slack")`". The sending session receives nothing.
- A session mirrored to Slack loses peer-session reach. In testing, a dashboard
  session mirrored to the owner's own Slack DM through the connector toggle was
  refused `session_create` while mirrored and worked again as soon as it was
  un-mirrored, and a second session mirrored the same way became unaddressable
  (`sessions mirrored to a channel are not addressable`). In source: an outbound
  mirror makes the session's turns republish to a channel, so a read would pull
  that channel's content back; `session_send` and `session_read_message`
  targeting it return `mirrored_target`, and `session_create` from it is refused
  `mirrored_caller` in `_refuse_ineligible_creator` (`dashboard/session_control.py`).
  There is an owner-DM exemption (`judge_owner_dm`), but it covers only a 1:1 DM
  on a verified conductor surface, and `OWNER_DM_CONDUCTOR_SURFACES` is
  `{"discord", "telegram"}` (`session_control.py`). Slack is not in that set, so
  `judge_owner_dm` refuses a Slack mirror with "the outbound mirror is not on a
  verified owner-DM surface", which is why the Slack-mirrored dashboard session
  in the test lost `session_create`. The exemption was extended to the
  Discord/Telegram owner-DM case by #15301 (closed #15288); it was never a Slack
  path.

### Problem

An orchestrating session, one that drives worker sessions with `session_create`
and `session_send`, needs a person's input at a few points in a long run. It can
page that person with `send_message`, but it cannot receive the answer. The
reply lands in a new session, and the person has to open the dashboard and find
the right session to respond in.

Mirroring the orchestrator to Slack does not work around this. A mirrored
session cannot create peer sessions, and a mirrored worker cannot be reached by
its orchestrator. The session that needs the answer is the one session that
cannot wear the mirror.

## Goals

1. A session can send a Slack page the owner can reply to, and the reply arrives
   in that session as a user turn.
2. No change for any message, or any reply, that does not opt in.
3. The sending session keeps every tool it has. Nothing about it becomes
   mirrored.

## Non-goals

- Changing how mirrored sessions are addressed (Part 2 of #17933). With replies
  routed back, an orchestrator never needs mirroring for this use case. If the
  mirrored-session restriction is to change, `rfc-session-address-model.md` is
  where it belongs, since it changes isolation that existing users rely on. The
  owner-DM exemption that relaxes it for Discord and Telegram 1:1 DMs (#15301,
  closed #15288) does not extend to Slack, so it does not help this case.
- Interactive buttons on a page. `rfc-notification-bridge.md` Phase B4 owns that.
- Channels other than Slack, and pages to anyone but the owner.

## Design

**Opt-in flag.** `send_message` gains `reply_to_sender: true` (name open). It is
valid only for a DM to the owner (`session="slack"`), and it is refused for
headless callers, which have no session of their own to receive a reply. The
plain Slack send degrades open on identity today; this flag requires a resolved
session key (`require_strict_session_key` in `mcp_tools/messaging.py`), because
a binding to "the sending session" is meaningless without one.

**Binding, as its own persisted record.** The binding cannot reuse Slack's
existing thread-to-session reverse index. That index, `_thread_to_session` in
`session_map.py`, is rebuilt on load (`_rebuild_thread_index`) only from each
entry's `slack_thread_ts`, and `slack_thread_ts` is exactly the field the mirror
readers count as a room: `_mirror_identity_of` in `session_control.py` builds a
session's mirror identity from `get_slack_link` (through `_slack_thread_of`) as
well as `get_mirror_link`, and `get_slack_link` returns the entry's
`slack_thread_ts`. So a binding written there would make the orchestrator read as
`mirrored_caller` and lose `session_create`, breaking Goal 3; a binding written
anywhere the rebuild does not read would be lost on restart.

The binding is therefore a dedicated field on the `SessionMap` entry,
`reply_binding`, carrying the owner's Slack user id, the DM's channel id, the
thread root `ts` the send already returns, a created-at, and a TTL. It is not
`slack_thread_ts` and not a `mirror` row, so `get_slack_link` and
`get_mirror_link` never return it and `_slack_thread_of` / `_mirror_identity_of`
find no room: the sending session stays non-mirrored and keeps every peer tool.
`SessionMap._load` already preserves the whole entry dict across the disk
round-trip, so the field persists for free; a dedicated reverse index,
`_reply_thread_to_session`, is rebuilt from `reply_binding` on load beside
`_rebuild_thread_index`, which is what reloads the binding after a restart. The
same single-owner-per-thread discipline the Slack link uses
(`_evict_rival_claimants`) is applied to this field, keyed on the new reverse
index, and the TTL plus an eviction accessor bound and clear it.

A session holds at most one live reply binding. A flagged page in a new thread
replaces the previous binding and evicts that thread from
`_reply_thread_to_session`, so a later reply to the earlier thread falls back to
today's behavior. A follow-up page with `thread_ts` set to the bound thread root
keeps the existing binding and restarts its TTL.

The tool result reports the binding beside the `ts` it already returns.

**Inbound routing, as a new delivery into the sending session's slot.** A Slack
message whose `thread_ts` matches a live `reply_binding`, sent by the bound user,
is delivered to the sending session as a Slack-origin user turn and queued if that
session is busy. Neither existing inbound path does this. The owner path that
`get_session_for_thread(reply_ts)` resolves runs the turn inside the Slack
handler (`slack/handler.py` builds `_AnswerStream(slack, channel, reply_ts, ...)`
and `get_or_create(session_key)` under the resolved key) and streams the answer
back into the thread, and it self-links the resolved session with
`set_slack_link`, which is the `slack_thread_ts` write that marks it mirrored. The
only path that delivers cleanly into a live dashboard slot,
`maybe_route_linked_thread` in `slack/handler_runtime/inbound.py`, fires only for
a `get_linked_slot(reply_ts)` slot, and such a slot carries `linked_session_key`,
so `containment_snapshot` reads it as `linked=True` and `session_create` is
refused `linked_session_caller`. Both break Goal 3.

So a bound reply takes a new path: resolve the sending session's slot by its own
key (`resolve_slot`), then `append_and_surface` the reply and, if the slot is
busy, `queue_append` it, as a Slack-origin user turn, without calling
`link_slack` / `set_slack_link` and without setting `linked_session_key`. The
slot's `linked` containment stays False, so the session keeps `session_create`.
The turn runs under the sending session's own dashboard runner (`_run_chat`), the
same runner a dashboard-typed turn uses, so no `_AnswerStream` is constructed for
it and nothing is posted back to the thread: the bound-reply branch returns before
the `handler.py` region that builds the stream and runs the turn under the
thread owner. It is not the peer `session_send` route, and no new session is
created.

**Follow-ups.** The sending session's own turns are not posted to the thread. To
ask again, it sends another flagged page with `thread_ts` set to the same thread
root. This needs no new field: `send_message(session="slack", thread_ts=...)`
already threads a reply today (`thread_ts` is refused only alongside a non-Slack
channel session, not alongside `session="slack"`), so the exchange stays in one
thread and keeps routing back.

**Fallback.** No binding, an expired binding, a thread whose binding was
replaced by a later page, a closed or archived sending session, a sender other
than the bound user, and a top-level (non-threaded) DM are all handled exactly
as today. When the new reply-binding lookup finds no live match, the inbound
path falls through unchanged: `get_session_for_thread` returns nothing and it
mints `slack:<ts>`, which is the current behavior, so the fallback needs no new
code beyond not finding a binding.

## Implementation sketch

Opt-in and backward compatible throughout; the only behavior that changes is a
flagged page's threaded reply.

- `src/kiro_crew/mcp_tools/messaging.py` (`send_message` schema and handler):
  add `reply_to_sender`, forward it on the Slack leg only, require a strict
  session key when set, and surface the binding in the result string.
- `src/kiro_crew/dashboard/messaging_api/proactive_send.py`
  (`_read_send_message`, `_post_send_message_to_slack`,
  `_send_message_response`): accept the flag, record the `reply_binding` against
  the posted `ts` after a successful owner-DM post, and report it in the response
  beside `ts`.
- `src/kiro_crew/session_map.py` (`SessionMap`): add a `reply_binding` entry
  field (owner user id, channel id, thread root `ts`, created-at, TTL) and its
  accessors, written without touching `slack_thread_ts` or any `mirror` row;
  rebuild a dedicated `_reply_thread_to_session` reverse index from it on load,
  beside `_rebuild_thread_index`; reuse the `_evict_rival_claimants` discipline
  keyed on the new index for single-owner-per-thread, and add a TTL-expiry read
  and an eviction accessor. `src/kiro_crew/session.py` forwards the new
  accessors.
- `src/kiro_crew/slack/handler.py` and `slack/handler_runtime/inbound.py`:
  before the thread-owner resolution and the `slack:<ts>` fallback, check the
  `reply_binding` reverse index bounded by the bound user id and the TTL, and on
  a live match deliver the reply into the sending session's dashboard slot
  (`resolve_slot` then `append_and_surface` / `queue_append`) as a Slack-origin
  user turn, returning before the `_AnswerStream` turn-running region so nothing
  is posted back to the thread and no `set_slack_link` / `linked_session_key` is
  set.

Tests go beside the code they cover:

- `test/test_mcp_send_message_routing.py`, `test/test_send_message_targeted.py`,
  `test/test_send_message_session_link.py`,
  `test/test_bug_validation_send_message_schema.py`: the flag, its headless
  refusal, and the result string.
- `test/test_session_map_mirror.py`, `test/test_session_map_unlink.py`,
  `test/test_session_map_conv_state.py`: the `reply_binding` field, its reload
  into `_reply_thread_to_session` across a load, its TTL, its single-owner
  eviction, the replace-and-evict case for a page in a new thread, the follow-up
  case that keeps the binding and restarts its TTL, and that it sets neither
  `slack_thread_ts` nor a `mirror` row (so `get_slack_link` and `get_mirror_link`
  do not return it).
- `test/test_slack_handler.py` (and the `*_coverage*` siblings),
  `test/test_slack_thread_parent_transcript.py`,
  `test/test_thread_parent_context.py`: a flagged page's threaded reply lands in
  the sending session's slot as a Slack-origin turn, posts nothing back to the
  thread, and creates no session; an unflagged page's reply, a top-level DM, an
  expired binding and a closed sending session each still start a new session.
- `test/test_session_control_owner_dm.py`,
  `test/test_session_control_boundaries.py`: a session carrying a `reply_binding`
  reads as non-mirrored (`_mirror_identity_of` returns no room for it) and still
  passes `session_create` / `session_send` / `session_read_message`.

Docs updated in the same change (CONTRIBUTING requires it):

- `docs/architecture/design-notes/session-slack-linking.md`: the reply binding
  beside the existing link and mirror.
- `docs/system-specs/modules/slack-gateway.md` and
  `docs/system-specs/modules/messaging.md`: the `send_message` flag and the
  inbound routing of a bound reply.

## Migration plan

| Phase | Scope | Exit criteria |
|---|---|---|
| **R1** | Flag, `reply_binding` field and its reverse index in `SessionMap`, the new slot-delivery inbound path, docs for `send_message` and Slack inbound routing | A flagged page's threaded reply lands in the sending session's slot as a Slack-origin turn, posts nothing back to the thread, and creates no session. An unflagged page's reply, a top-level DM, an expired binding and a closed sending session each still start a new session. The sending session reads as non-mirrored and still passes `session_create` / `session_send` / `session_read_message` |
| **R2** | Fallback notice in the thread | When a reply falls back because the sending session is gone, the thread says a new session was started. Blocked on open question 2 |

R2 is independently abandonable. Without it, a fallback looks the same as it
does today.

## Backward compatibility

| Surface | Guarantee |
|---|---|
| `send_message` without the flag | Unchanged |
| Top-level DMs and unbound replies | Still start a new session |
| Mirrored sessions | Addressing rules unchanged |
| `rfc-notification-bridge.md` | Its loop-safety rule still holds for every bridged and unflagged message |

## Security considerations

Only the bound user's replies route, and only into the session that paged them.
In this version that user is the owner, who can already type into the session
from the dashboard, so the binding grants no new reach. Extending it to other
allowed users would let a non-owner write into an owner's session, so it is an
open question rather than part of the design.

The binding sets no outbound mirror and writes no `slack_thread_ts`, so it does
not make the sending session readable from Slack: the session's turns are not
republished, and the mirrored caller and mirrored target refusals in
`session_control.py` are untouched. The TTL bounds how long a thread stays
routable, and the single-owner-per-thread eviction (`_evict_rival_claimants`,
keyed on the reply-binding index) keeps one thread from routing to two sessions.
Each routed reply passes the same `is_allowed_user` gate the Slack inbound path
already applies before it is delivered, and is marked Slack-origin in the
transcript (`source_thread` / `source_user`), so governance applies to it
exactly as to any Slack-born turn. The existing clean slot-delivery record,
`linked_thread_intercept` (`slack/handler_runtime/inbound.py`), is tied to a
`linked=True` slot, which this path deliberately does not create, so the new
path emits its own SEL record instead, `bound_reply_intercept`
(`tool_kind="permission"`, outcome `allowed` or `denied` on the `is_allowed_user`
gate, metadata naming the bound user and the delivered-to session), so audit
reflects a reply-binding delivery rather than a link that does not exist.

## Alternatives considered

- **Mirror the orchestrator and pause replies.** This gives working two-way
  messaging, but a session mirrored to a Slack DM cannot call `session_create`.
  The owner-DM exemption that would admit it covers only Discord and Telegram
  1:1 DMs (#15301), not Slack.
- **A dedicated mirrored relay session.** The orchestrator cannot address a
  mirrored session.
- **Poll Slack from the session.** Slack read tools authenticate as the user,
  and the app's DM is not visible to them.
- **Poll for reply-spawned sessions.** This works, but it matches a reply to its
  page only by recency, leaves one sidebar session per reply, and costs a model
  turn per poll.
- **Route replies through the notification bridge.** The bridge is not built,
  and it carries notifications rather than a conversation with a sender. The
  binding here could serve its Phase B4 later.
- **Reuse the existing thread-to-session index instead of a dedicated record.**
  Rejected. The shared index cannot carry this binding. `_rebuild_thread_index`
  (`session_map.py`) rebuilds `_thread_to_session` only from each entry's
  `slack_thread_ts`, and `_slack_thread_of` / `_mirror_identity_of`
  (`session_control.py`) read that same `slack_thread_ts` through `get_slack_link`
  as a mirror room. So a binding stored there either sets `slack_thread_ts`, which
  makes the orchestrator `mirrored_caller` and breaks Goal 3, or lives where the
  rebuild never reads and is lost on restart. A dedicated `reply_binding` field
  with its own reloaded reverse index is what keeps the binding durable and
  invisible to the mirror readers; it reuses `SessionMap`'s single-owner and
  eviction discipline rather than duplicating it.
- **Let mirrored sessions keep peer tools.** This is a larger change to behavior
  users rely on, and it is not needed once replies route back.

## Open questions

1. The flag name, and the default TTL (24 hours is a starting proposal).
2. Should a reply that falls back post a note in the thread saying a new session
   was started?
3. Should a later version allow pages to allowed users other than the owner?
