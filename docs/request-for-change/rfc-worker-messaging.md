---
title: Worker messaging — routing, receipts and asks on the existing work report
status: draft
author: kirocrew agent session, directed by zejiangg
created: 2026-10-31
last-audited: 2026-10-31
audited-at: f358d9a217
doc-pr: null
implementation-prs: []
tracking-issues: [26499]
supersedes: []
superseded-by: []
---

# RFC: Worker messaging — routing, receipts and asks on the existing work report

Status: draft. Nothing built; this document changes no code. Every code reference
was read at `f358d9a217`.

The worker-debate protocol
([#26316](https://github.com/kirodotdev/KiroCrew/pull/26316), merged) is phase 1.
It is skill text only:
[`goal-conductor/references/worker-debate.md`](../../src/kiro_crew/builtin_skills/goal-conductor/references/worker-debate.md).
A code-level approval exemption for Conductor relays
([#26485](https://github.com/kirodotdev/KiroCrew/pull/26485)) was closed on
2026-10-31 as not needed. On main, a Conductor chat with session trust runs its
`session_send` without a prompt: the `slot_trusted or yolo_active` branch of the
tool-approval path in `dashboard/chat_turn/tool_approval.py` (`_slot_is_trusted`)
approves it. Without trust a send is never auto-approved and keeps prompting, by
design: it runs text as the target's user turn, and a Conductor reads untrusted
content (the comment on `_CREW_ROOT_OWN_WORKER_VERBS` in the same file).

This RFC is the next step after phase 1. It adds no delivery path and no approval
exemption. If accepted, it replaces the Phase 5 design in
[`rfc-conductor-work-ledger.md`](rfc-conductor-work-ledger.md) (`work_request`,
`work_message`, channels), which was never built.

**Decision D1 = Z (zero code), the owner's decision of 2026-10-31.** This RFC
ships as text. G-min (§G-min) is fully specified but **not implemented**; it is
built only when trigger T-a fires. Code-checked rounds are parked the same way,
behind trigger T-b.

---

## Summary

1. The owner's three rules hold today with phase 1 plus Conductor chat trust,
   and need no product code (§How the three rules are met).
2. This RFC writes down three assumptions every relay rests on: T1 (trust), T2
   (receipts and relay content) and T3 (no tamper detection).
3. It specifies **G-min** for the wider scope N1-N4: two optional caller fields
   (`to`, `reply_to`) and one server-minted `message_id` on the existing
   `work_report`, with limits L1-L4.
4. D1 = Z: G-min is not built now. It is built when T-a fires (§D1).

## Motivation

### What the phase-1 protocol leaves to judgment

The debate protocol is honest about its limits ("Any participant can edit any file
in the directory", worker-debate.md §Limits). The owner's Phase 3 scope asks four
things of it:

- **N1.** How a message for the human differs from one for a worker.
- **N2.** Whether a message got through.
- **N3.** How many workers avoid mixing things up.
- **N4.** Point-to-point asks and handoffs.

| # | Gap | Today | This RFC |
|---|---|---|---|
| 1 | Worker-to-worker vs worker-to-human | Both arrive as a `question` report. Only the optional `reason=needs_human` (`WORK_BLOCKED_REASONS` in `work_vocab.py`) and the Conductor's reading tell them apart. | N1 |
| 2 | Delivery | No message id exists in code. With several workers, correctness rests on the Conductor's round log in `note.md`. | N2, N3 |
| 3 | Rounds and stop rules | Counted and applied by the Conductor. A miscount is caught only by a critic reading the note. | Not addressed by code; parked behind T-b |
| 4 | Tampering | Round files and `note.md` sit in a shared directory every participant can write. | Not addressed; see T3 |
| 5 | Scope | Debates only. A worker that needs one fact from a sibling, or hands a branch to one, has no record for it. | N4 |

### What the trial measured

The protocol's own trial (one proposer, one critic, three rounds) was tallied at
about 7 to 9 Conductor turns and about 9 to 11 approval prompts: one prompt per
seed and per relayed round file. The trial ran before the Conductor chat was
trusted. Session trust removes the Conductor-side prompts. It does not remove the
turns or the judgment.

### Why the obvious fixes are withheld

- **Let workers `session_send` each other.** The creator fence forbids it on
  purpose. `_caller_is_ownership_fenced` in `dashboard/session_control_parts/caller_access.py`
  fences every agent-created session to sessions it created, because a session that
  could reach a sibling would be "an unfenced deputy for free" (same docstring). A
  worker did not create its siblings; the Conductor did.
- **Ship the work-ledger RFC's Phase 5 as written.** It gives the right gate (the
  Conductor) but two shapes for one thing (`request` and channel `message`), a
  500-character body with no note pointer, no recipient for the Conductor or the
  human, no reply link, and no read receipt. It also predates the debate protocol,
  so it has no notion of rounds.

## Goals

1. **N1.** A message for the human can be told from one for a worker.
2. **N2.** The sender and the Conductor can tell whether a message got through.
3. **N3.** Several workers on one ledger do not mix messages up.
4. **N4.** Point-to-point asks and handoffs have a record.
5. No new delivery path and no new approval exemption.

## Non-goals

- No direct worker-to-worker session access. The creator fence stays as it is.
- A relay is the Conductor's own words; it never pastes a participant's text.
- No automatic judging of objections or of convergence.
- No fan-out outside one Conductor's ledger, and no cross-crew messaging (the
  remote-dispatch `reply` status in [`rfc-conductor-remote-dispatch.md`](rfc-conductor-remote-dispatch.md) §3b is left alone).
- No code-checked rounds. Parked behind T-b.
- No tamper detection. See T3.
- No new worker tool.

## Design

### Routing

The Conductor relays with its own `session_send` to the worker's bound session.
A worker never reaches a sibling's session.

A relay opens with the question in one or two sentences, then `anchor_v`
(debates), the rounds left, the ids the recipient must answer, the Conductor's
rulings, and the paths (under G-min, also the message ids) to read. It never
pastes a participant's text (worker-debate.md §A round, "Every relay message").

### Receipts

**T2. Receipts and relay content.**
Under Z: *sent* means the worker's report landed, and the route tells the
caller. *Relayed* means the Conductor's `session_send` returned `ok` with
`started: true` (a turn started) or `started: false` (queued behind a running
turn). That guides retries and sets no state. *Answered* means a later report
from the recipient cites `re=<path>` in its summary, which is the recipient's
own claim.
Under G-min: the same, except that a report which sets `to` gets a
server-minted `message_id`. *Answered* then means a report whose `reply_to`
names that id. It is accepted only when its `to` is `conductor` or the original
sender's item, and the Conductor relays it. It shows the recipient later named
the id.
Under both: no receipt means the recipient read or understood the note. A relay
opens with the question, `anchor_v` (debates), rounds left, the ids to answer,
the Conductor's rulings, and the paths (and, under G-min, ids) to read. It never
pastes a participant's text.

### The human's ruling

Phase 1's text stands (worker-debate.md §The human's ruling). The user answers in
a free-form message to the Conductor. The Conductor maps it to the open ids, asks
back once if it is unclear, and sends the participants the ruling plus the user's
words quoted verbatim. There is no code gate and no human-source record.

### Asks and handoffs

Under Z, by convention (§D1): the summary starts `to=<human|conductor|item_x>`,
an answer cites `re=<path>`, and a handoff's `artifacts` name the branch.

Under G-min, the same through the `to` and `reply_to` fields.

Either way, a handoff never moves an acceptance bar. The Conductor's `accept` rule
for a claimed `pr` is unchanged.

### G-min (specified, not built)

Not implemented. See D1: it is built when T-a fires.

Two caller fields plus one server id on `work_report`:

| field | set by | rule |
|---|---|---|
| `to` | worker, optional | `conductor` (default when absent), `human`, or `item:<id>` (an open item of the same ledger, not the caller's own) |
| `reply_to` | worker, optional | a `message_id` of this ledger, addressed to the caller's item |
| `message_id` | server | minted when `to` is set; never accepted from the caller |

Limits:

- **L1.** `question` keeps today's meaning: a decision only the Conductor can
  make. A report with `to=item:<id>` and status `question` is refused. A peer
  ask the worker cannot go on without is `blocked` with no `reason`; "another
  item's output" is already a `blocked` cause. It wakes the Conductor through
  today's wake set. A peer ask that does not stop the work is `progress`, which
  does not wake.
- **L2.** If `reply_to` is set, `to` must be `conductor` or the original
  sender's item. Any other audience is refused.
- **L3.** A routed report still replaces the item's status and summary, as
  every report does. So it states the sender's true state. `blocked` with no
  `reason` is not a person-wait, so it causes no needs-you hold.
- **L4.** Wakes are exactly today's set. An answering worker that is otherwise
  moving reports `progress`, and its answer waits for the next patrol tick.
  `reply` would wake, but today it sits behind `work_ledger.remote_dispatch`.
  G-min does not ungate it (open question).

Tests (about 16):

1. A report without `to` produces a byte-equal event.
2. `to` set to human, conductor, or an item is accepted.
3. `to` naming a closed item, a foreign item, the caller's own item, or a
   non-minted string is refused; one test each.
4. `from` cannot be supplied.
5. `message_id` is minted and never set by the caller.
6. `question` with `to=item` is refused.
7. `reply_to` naming a message not addressed to the caller is refused.
8. `reply_to` with a wrong audience is refused.
9. `reply_to` naming a foreign ledger's message is refused.
10. `blocked to=item` wakes, and the wake set is unchanged.
11. An unknown field leaves the fold unchanged.

## D1: decided — Z (owner, 2026-10-31)

The owner chose Z on 2026-10-31. The table records both options as they were
weighed.

| | Z: zero code now (**chosen**) | G-min now |
|---|---|---|
| Ships | RFC + skill text only. N1-N4 by convention: the summary starts `to=<human\|conductor\|item_x>`, an answer cites `re=<path>`, a handoff's `artifacts` name the branch. | The same skill text, plus `to`, `reply_to` and server `message_id` on `work_report`, with L1-L4 |
| N1 human vs worker | prose the Conductor reads | a field |
| N2 got through | `session_send` result (both options) | same |
| N3, N4 | file names, note table, `re=` claims | server ids, `reply_to` checked for audience and ledger |
| Product code | 0 | about 160-220 lines in 6-7 files: `mcp_work.py`, `validation_parts/opt_in_server_schemas.py`, `dashboard/handlers/work_ledger_worker.py`, `work_ledger_records.py`, `crew_log/projection_work.py`, `dashboard/handlers/work_ledger_read.py`, `agent_parts/spec_prompts.py` (worker prompt text) |
| Skill / doc | `goal-conductor/references/worker-debate.md`, `goal-conductor/SKILL.md`; about 40-50 lines | same, plus [`session-work-ledger.md`](../system-specs/modules/session-work-ledger.md); about 70-90 lines |
| Tests | 0 | about 16 (§G-min) |
| Other cost | every rule is kept by judgment; a misroute is caught only by a reader | the schema every worker sees gets three fields and four limits, which costs prompt tokens on every worker; status rules L1/L3 that workers must follow; answers wait for patrol unless `reply` is ungated (L4) |
| Turns, prompts | same as today | same as today |
| Risk | N1-N4 stay prose, with nothing to check | code with no measured failure behind it |

**Triggers.** Each is measured on a trusted Conductor chat, over at least three
real runs that use debates or worker-to-worker asks.

- **T-a, build G-min:** any one of these happens.
  1. A message is relayed to the wrong worker.
  2. A message for the human is handled as one for a worker, or the reverse.
  3. An ask or handoff is lost or answered twice because the convention was
     unclear.
  4. The owner asks for typed routing.
- **T-b, write a new RFC section on code-checked rounds:** a real debate in
  which the Conductor miscounts a round, misses a critic, or goes past
  `max_rounds`, and no critic or note keeper catches it.

## How the three rules are met

The owner's rules for Phase 3, and how each holds with no product code:

| rule | met by | code |
|---|---|---|
| R1. Free-form chat ruling; the Conductor coordinates; any number of proposers and single-aspect critics | worker-debate.md §The human's ruling; aspect and target in the note's objection table | none |
| R2. The Conductor relays on its own | the Conductor's `session_send` under chat trust (`_slot_is_trusted` in `tool_approval.py`); T1 | none |
| R3. The ledger holds a summary and a pointer | `decide` line, report summary ≤ 500 characters, `artifacts` path; the discussion stays in the note (worker-debate.md §What the ledger holds) | none |

## Cost

Per debate of three rounds, one proposer and one critic, no nudges:

| | Conductor turns | approval prompts |
|---|---|---|
| Phase 1 trial, untrusted Conductor chat (tallied) | about 7 to 9 | about 9 to 11 |
| Phase 1, trusted Conductor chat (main today) | about 7 to 9 | unmeasured for the Conductor (see open question 5); the participants saw none |
| Z | same as the row above | same as the row above |
| G-min | same turns | prompts follow the chat's trust |

This RFC buys typed routing and receipts, not turns.

## Tamper resistance

| property | holds | how |
|---|---|---|
| A report's sender is who it says | yes | resolved from the calling session, as `work_report` does today |
| Ledger entries are append-only | yes | crew-log entries |
| A note edited after send is detected | **no** | T3 |
| Rounds and caps are checked by code | **no** | parked behind T-b |
| A ruling came from the human | **no** by code | it is the Conductor's verbatim quote (T1) |
| The recipient read the note | **no** | T2 |
| The record holds across a trust boundary | **no** | same owner, same trust level only |

**T3. No tamper detection.**
Notes are files any participant can edit. The ledger holds the path, not a
hash. A note is a working record, not integrity proof, even among same-trust
workers. Critics still compare the note keeper's copy with the Conductor's
relay, and a wrong copy is blocking (worker-debate.md §The debate note).

## Security considerations

**T1. Trust is an assumption, not a guarantee.**
Relays in this design rely on the Conductor chat's session trust. With it, the
Conductor's `session_send` runs without a prompt. This is the path the design
relies on. It is not the only way a call can skip a prompt: an operator hook
grant or a trusted pattern can also approve a call. Global YOLO also stops the
prompts, but it is process-wide, outranks the chat's trust, and is not the
normal setup for a debate. Session trust approves every eligible tool the
Conductor calls, not only sends. A peer's note can steer the Conductor before
it sends. Neither the SEL row (tool, outcome, reason) nor the send audit
(`started`, `steered`, length) holds the note or the send text. The words are
visible afterwards only in the Conductor chat and the target's transcript. So
debates and messages run only among workers with the same owner and trust
level. Secrets and customer text stay out of notes. Peer text is data. Only the
Conductor's own relay words instruct a worker. The Conductor's verbatim quote of
a human ruling is its own record. It can be checked against the Conductor chat
only afterwards.

- **What trust gives up.** `goal-conductor/SKILL.md` names the per-send prompt
  as the only call-time check on `session_send`. Turning on chat trust removes it
  while the Conductor reads peer notes. That is the operator's choice, and T1 is
  the price.
- **Relay content.** A relay is the Conductor's own words and never pastes a
  participant's text (§Routing).
- **Reach is unchanged.** There is no new send path. `session_send`'s creator
  fence and target checks apply as they do today.
- **Privacy.** The phase-1 rule stands: no private memory, credentials or
  customer text in a note.

## Migration plan

The skill and doc changes ship either way, as a separate follow-up PR once this
RFC is accepted: T1-T3, the relay-content rule and the Z conventions go into
`goal-conductor/references/worker-debate.md` and `goal-conductor/SKILL.md`. This
RFC's PR changes no skill text; until the follow-up lands, the conventions live
only here.

### G-min (optional, not scheduled)

- **Entry:** per D1 = Z, only when T-a fires.
- **Scope:** §G-min.
- **Exit criteria:** the G-min tests in §G-min pass.

## Backward compatibility

Additive. A `work_report` without `to` is byte-equal to today's report. An unknown
field leaves the fold unchanged. The work-ledger RFC's Phase 5 is dropped, and its
`channels/` directory is never created.

## Alternatives considered

| alternative | why not |
|---|---|
| **Z: skill-only on a trusted Conductor chat.** | Chosen (D1). It costs no code. N1-N4 stay conventions that nothing checks; T-a is the backstop. |
| **Code relay / `auto_relay`.** | A prompt-free send that skips chat trust: the closed #26485 by another name. |
| **`work_message` / `work_inbox` tools.** | A second uplink beside `work_report`. G-min's report fields replace it. |
| **Frozen notes + hash.** | Not needed by R3 or N1-N4. T3 states plainly that there is no tamper detection. |
| **Threads with caps.** | Parked behind T-b. |
| **A `workflow_run` debate script.** One worker runs a script that alternates `ctx.agent(session=...)` calls. | Zero relays and deterministic caps. But participants are workflow-internal sessions a person cannot open or steer, and the Conductor never runs `workflow_run` itself (`goal-conductor/SKILL.md`, "A large item's seed may name `workflow_run`"), so a worker sits in between. It suits a closed, one-off argument, not asks or handoffs between dispatched workers. |
| **Direct worker-to-worker send.** Loosen the fence for siblings of one Conductor. | Cheapest per message, but it makes every worker a deputy over its siblings and puts one worker's text into another's turn with no gate. The Conductor would also lose sight of traffic it must referee. |
| **The work-ledger RFC's Phase 5 as written.** | Same gate, but two record shapes, no human or Conductor recipient, no note pointer, no receipt and no rounds. |

## Open questions

1. **D1.** Decided: Z (owner, 2026-10-31). Revisit only when T-a fires.
2. **T-b.** Code-checked rounds stay parked until a measured miss.
3. **L4.** Under G-min, should `reply` be ungated so answers wake the Conductor?
   Matters only once G-min is built.
4. **Round check-ins.** The owner's note: the 3-round cap is only approximate;
   the core rule is that the Conductor must check every N rounds whether the
   discussion is still worth continuing. A separate study in progress will design
   this; this RFC does not.
5. **Numbers.** The line and test counts are estimates, and nothing was built.
   The trusted-chat cost row needs the Conductor's own prompt count from
   debate-2.

## Risks kept

1. The T1 assumption: a misled Conductor can relay or rule badly, and that
   shows up only afterwards, in the chat and transcripts.
2. Notes stay editable (T3), as in phase 1.
3. Under Z, routing is prose that nothing checks. T-a is the backstop.
