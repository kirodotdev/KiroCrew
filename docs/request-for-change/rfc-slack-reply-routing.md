---
title: Slack page replies route back to the sending session
status: draft
author: phantom-tim
created: 2026-10-08
last-audited: 2026-10-08
audited-at: 230b9141c888a4b2a53cd236328a7e42035389b8
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

The binding reuses the thread-to-session reverse index Slack routing already
keeps, so the sending session is never marked mirrored and keeps every tool it
has.

## Motivation

### Current state

Observed by direct testing and confirmed in source at main `230b9141`:

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

**Binding, reusing the existing reverse index.** Slack already keeps a
thread-to-session reverse index, `_thread_to_session` in
`session_map.py`, written by `set_slack_link` and read by
`get_session_for_thread`. A new store is not needed, and reusing this one is the
smaller change the project asks for. When the flag is set, the gateway records a
binding from the DM's thread root (channel id plus the message `ts` it already
returns) to the sending session's key, with the owner's Slack user id and a TTL.
The one thing it must not do is set an outbound mirror: the refusals in
`session_control.py` are all keyed on the outbound mirror, so a reply-only
binding leaves the sending session fully addressable and able to create peers.
`SessionMap` already carries the nearest concept, a resume binding that accepts
inbound without being a full mirror (`set_mirror_link(..., accepts_inbound=True)`),
and the single-owner-per-thread discipline the binding needs
(`_evict_rival_claimants`). The tool result reports the binding beside the `ts`
it already returns.

**Inbound routing.** A Slack message whose `thread_ts` matches a live binding,
sent by the bound user, is delivered to the sending session as a Slack-origin
user turn and queued if that session is busy. This is the path the inbound
handler already takes once `get_session_for_thread(reply_ts)` resolves an owner:
the reply is appended as an ordinary user turn and run or queued
(`slack/handler.py`, `slack/handler_runtime/inbound.py`). It is not the peer
`session_send` route, and no new session is created.

**Follow-ups.** The sending session's own turns are not posted to the thread. To
ask again, it sends another flagged page with `thread_ts` set to the same thread
root. This needs no new field: `send_message(session="slack", thread_ts=...)`
already threads a reply today (`thread_ts` is refused only alongside a non-Slack
channel session, not alongside `session="slack"`), so the exchange stays in one
thread and keeps routing back.

**Fallback.** No binding, an expired binding, a closed or archived sending
session, a sender other than the bound user, and a top-level (non-threaded) DM
are all handled exactly as today. No binding means `get_session_for_thread`
returns nothing and the inbound path mints `slack:<ts>`, which is the current
behavior, so the fallback needs no new code beyond not finding a binding.

## Implementation sketch

Opt-in and backward compatible throughout; the only behavior that changes is a
flagged page's threaded reply.

- `src/kiro_crew/mcp_tools/messaging.py` (`send_message` schema and handler):
  add `reply_to_sender`, forward it on the Slack leg only, require a strict
  session key when set, and surface the binding in the result string.
- `src/kiro_crew/dashboard/messaging_api/proactive_send.py`
  (`_read_send_message`, `_post_send_message_to_slack`,
  `_send_message_response`): accept the flag, record the binding against the
  posted `ts` after a successful owner-DM post, and report it in the response
  beside `ts`.
- `src/kiro_crew/session_map.py` (`SessionMap`): add a reply-only binding that
  writes the `_thread_to_session` reverse index and an inbound-accept marker
  without an outbound mirror, plus a TTL field and its expiry read; reuse
  `_evict_rival_claimants` for single-owner-per-thread and `clear_slack_link`
  semantics for eviction. `src/kiro_crew/session.py` forwards the new accessor.
- `src/kiro_crew/slack/handler.py` and `slack/handler_runtime/inbound.py`:
  honor a live reply-only binding when resolving the thread owner, bounded by
  the bound user id and the TTL, before the `slack:<ts>` fallback.

Tests go beside the code they cover:

- `test/test_mcp_send_message_routing.py`, `test/test_send_message_targeted.py`,
  `test/test_send_message_session_link.py`,
  `test/test_bug_validation_send_message_schema.py`: the flag, its headless
  refusal, and the result string.
- `test/test_session_map_mirror.py`, `test/test_session_map_unlink.py`,
  `test/test_session_map_conv_state.py`: the reply-only binding, its TTL, its
  single-owner eviction, and that it sets no outbound mirror.
- `test/test_slack_handler.py` (and the `*_coverage*` siblings),
  `test/test_slack_thread_parent_transcript.py`,
  `test/test_thread_parent_context.py`: a flagged page's threaded reply lands in
  the sending session and creates none; an unflagged page's reply, a top-level
  DM, an expired binding and a closed sending session each still start a new
  session.
- `test/test_session_control_owner_dm.py`,
  `test/test_session_control_boundaries.py`: a session carrying a reply-only
  binding still passes `session_create` / `session_send` /
  `session_read_message`.

Docs updated in the same change (CONTRIBUTING requires it):

- `docs/architecture/design-notes/session-slack-linking.md`: the reply-only
  binding beside the existing link and mirror.
- `docs/system-specs/modules/slack-gateway.md` and
  `docs/system-specs/modules/messaging.md`: the `send_message` flag and the
  inbound routing of a bound reply.

## Migration plan

| Phase | Scope | Exit criteria |
|---|---|---|
| **R1** | Flag, reply-only binding in `SessionMap`, inbound routing, docs for `send_message` and Slack inbound routing | A flagged page's threaded reply lands in the sending session and creates no session. An unflagged page's reply, a top-level DM, an expired binding and a closed sending session each still start a new session. The sending session still passes `session_create` / `session_send` / `session_read_message` |
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

The binding sets no outbound mirror, so it does not make the sending session
readable from Slack: the session's turns are not republished, and the mirrored
caller and mirrored target refusals in `session_control.py` are untouched. The
TTL bounds how long a thread stays routable, and the single-owner-per-thread
eviction (`_evict_rival_claimants`) keeps one thread from routing to two
sessions. Each routed reply arrives through the inbound path that already gates
on `is_allowed_user` and writes an SEL record (`linked_thread_intercept` in
`slack/handler_runtime/inbound.py`), and is marked Slack-origin in the
transcript (`source_thread` / `source_user`), so governance and audit apply to
it exactly as to any Slack-born turn.

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
- **A new reply-binding store.** Rejected. Slack already keeps the
  thread-to-session reverse index this needs; a parallel store would duplicate
  the single-owner and eviction logic that `SessionMap` already has.
- **Let mirrored sessions keep peer tools.** This is a larger change to behavior
  users rely on, and it is not needed once replies route back.

## Open questions

1. The flag name, and the default TTL (24 hours is a starting proposal).
2. Should a reply that falls back post a note in the thread saying a new session
   was started?
3. Should a later version allow pages to allowed users other than the owner?
