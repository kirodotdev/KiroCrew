# Crew Conversation Module

## Overview

A crew member's DM thread on the Crew Members page is a **conversation**
between one human and one member. Its lifetime is longer than any one session:
the pinned DM slot can be rebuilt or re-bound, and a worker session the member
dispatched may hand a result back into it. The conversation therefore has an
identity of its own — but it is deliberately **not** a second transcript.

`kiro_crew.crew_conversation` keeps that identity thin. The index stores
**pointers and lifecycle**, never bodies:

| Entry type | Shape | Meaning |
|------------|-------|---------|
| `ref` | `{session_key, mid, role, ts}` | "This transcript row belongs in the conversation" — used when a row from a session *other than* the DM slot is surfaced (a worker's final report). Rows in the DM slot itself need no ref; the projection reads that session whole. |
| `escalation` | `{id, session_key, mid, from_session, state, created_ts, deadline, default_action, goal, options, answered_ts}` | One `session_send target="user"` delivery and where it stands. The text lives on the transcript row the pointer names. |

`state` ∈ `pending | answered | expired | defaulted`. The record also carries
`participants` (`{kind: human}` + `{kind: member, slug, name}`) and `sessions`
(every session key the conversation spans). The key is `dm:<slug>` today; a
later multi-member `goal:<id>` conversation is a new key shape and a longer
`participants` list, not a schema migration — which is why the record is shaped
as lists rather than a `member` / `session` pair.

## Storage

`$KIROCREW_HOME/members/<slug>/conversation.json`, beside `activity.jsonl`,
written whole with `atomic_write` under a per-slug lock (every writer is a
thread of the one gateway process; the lock is what keeps two concurrent
read-modify-writes from dropping each other's entry). It is small: entries
are capped at 500 (~100 KiB), evicting the oldest *settled* entries first — a
pending escalation is never dropped by the cap. It is **not** the trust binding (`trust/member-bindings/<slug>.json`):
the binding is the identity authority, strict-shape and keystone-gated; this is
mutable UI state and must stay out of that subtree. An unreadable or missing
file reads as an empty scaffold — the index is derived state and never fails a
roster, a slots frame, or an append.

`read_conversation` always parses (callers mutate what they get back, so no
shared record is ever cached). The hot path is different: `needs_you` runs
inside the slot projection on every sidebar push for every member slot, so it
reads a per-slug cache of the pending records' deadlines keyed on the file's
`(mtime_ns, size)` — a `stat` per call, a parse only when the file changed.

## Derived state, not stored state

`needs_you` is **derived** from the pending escalation records at read time,
projected on the member slot's `slots` frame (`slot_projection.to_dict`) and on
the `GET /api/members` roster row (`needs_you`, `pending_escalations`). It is
never written to the slot: the slot is a process; the conversation is the thing
the human is in.

Lifecycle transitions:

- **pending → answered** — a *live* `user` row appended to the member DM slot
  (`_ChatSlot.append`, `role == "user"`, `broadcast=True`, `mode == "member"`).
  Which record it answers is one rule, shared with the chat projection that
  draws the card: a row carrying `meta.escalation_id` (an option chip) answers
  exactly that record; a row without one (typed text) answers the pending
  record only when exactly one is pending — with none or several it answers
  nothing, so an unrelated message cannot retire N open decisions. A replayed
  row (`broadcast=False`: transcript rotation, fork, transfer) answers nothing.
  A thread with nothing to change costs no write on an ordinary turn.
- **pending → defaulted / expired** — a passed `deadline` is applied lazily on
  every read (`sweep_deadlines`): `defaulted` when a `default_action` was
  declared (the member proceeds on it), `expired` otherwise. The file is only
  rewritten the next time something else writes it (an answer, a new record),
  so a deadline passing while the gateway is down still reads correctly on
  restart.

There is no background poller: nothing needs to *fire* at the deadline, because
the member that set it is the one that acts on it (it stated the default), and
the human-facing card counts down client-side from `meta.deadline`.

## Read surface

`GET /api/members/{slug}/conversation` (owner-only, like the thread endpoint;
app tokens refused) returns `public_view(record)`: the swept index plus
`needs_you` / `pending_escalations`. The chat projection on the Crew Members
page does not depend on it — it derives card state from the DM slot's own rows
(a later `user` row = answered; `now > deadline` = expired) so the view can never
disagree with the transcript it is rendered from.

## What is deliberately not here

- **No bodies.** A conversation never stores message text; a body lives in
  exactly one place, the session JSONL, and is reached through `(session_key,
  mid)`. `mid` is minted once (`history.mint_row_mid`) and survives restore,
  which is what makes the pointer stable.
- **No per-message refs for the DM slot.** The DM session *is* the
  conversation's main body; indexing every row of it would duplicate the
  transcript's own ordering for nothing.
- **No approval records.** An escalation is a decision the member may take on
  its own if unanswered; approvals (which block) are a different object and
  are not modelled here.
