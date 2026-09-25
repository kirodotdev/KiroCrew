---
title: Federated lesson-sharing — replicate learn_add lessons between isolated crews instead of sharing one memory
status: draft
author: mrpackethead
created: 2026-09-25
last-audited: 2026-09-25
audited-at: 4926e50b3c
revision: 2
doc-pr:
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: Federated lesson-sharing — replicate `learn_add` lessons between isolated crews instead of sharing one memory

- Status: draft — no implementation. Every "exists today" claim below was
  checked at `4926e50b3c` (main, 2026-09-25); citations name symbols, not line
  numbers.
- Revision (2026-09-25, in response to the PR's AI-review findings, re-verified
  against `4926e50b3c`): ingest is now specified **merge-only** through
  `set_semantic_if_absent`, never `write_lesson` (which deletes an overlapping
  local lesson); the rule-gate now **holds the complement** (anything not
  explicitly `on_topic`), because an unstated tier is served as a standing rule;
  §5 extends the gate question to prompt-injected `on_topic` findings; §6 gains
  an **outbound** safety bullet (redaction + capability scope + operator
  consent before the first event leaves the host); and §6/§7 require the ingest
  primitive to re-evaluate `capabilities.memory_writes`, which today is enforced
  only at the MCP tool layer.
- Author: mrpackethead
- Related: [rfc-webhook-subscriptions.md](rfc-webhook-subscriptions.md) (the
  inbound-webhook extension this RFC's delivery half rides on — the event
  buffer, the source authentication schemes, the wake), and the two open
  discussions this RFC records the decision from:
  [#13785](https://github.com/kirodotdev/KiroCrew/discussions/13785)
  (federated lesson-sharing — the topology proposed here) and
  [#10836](https://github.com/kirodotdev/KiroCrew/discussions/10836)
  (multi-principal collaboration — the *shared-state* topology this RFC is the
  loosely-coupled alternative to, and whose "multi-host teams / fleet-distributed
  membership" it lists as out of scope is exactly this RFC's case).

## 1. Summary

A team commonly runs **one Kiro Crew instance per engineer, all working on the
same codebase**. That gives each crew its own isolated memory, tools,
credentials and owner identity, which is correct — but it means every crew
**re-learns the same project facts independently**. One engineer's crew works
out why a deploy is flaky, or a build quirk, or a convention; that lesson is
recorded with `learn_add` into *that crew's* memory and stays there. The other
crews on the same repo hit the same wall and solve it again.

This RFC proposes **lesson replication between isolated crews**: a crew that
records a team-relevant lesson optionally **emits it as an event**; other crews
**ingest** that event into their own local memory. A durable log of emitted
lessons lets a *new* teammate's crew replay the team's accumulated project
knowledge on day one. No crew shares a memory store, a control plane, or a
trust boundary with another. It is federation of *data*, not centralization of
*control*.

Two new objects, and nothing about a crew's local memory model changes:

| New object | What it is |
|---|---|
| **Lesson export event** | A structured, provenance-tagged record emitted when a team-relevant lesson is added, carrying the lesson id, text, negative clause, tier, repo scope, and origin (which crew, which repo, when) — published to a configurable sink (webhook / SNS / EventBridge). |
| **Lesson ingest path** | A supported way to feed such an event into a crew's memory, idempotent by lesson id, without writing the internal memory store by hand. |

The transport already has a home. [rfc-webhook-subscriptions.md](rfc-webhook-subscriptions.md)
extends `POST /api/hooks/agent` into a subscription + durable-event-buffer
system with per-source authentication schemes (including a non-bearer
`X-Hub-Signature-256` scheme for GitHub) and a coalescing wake. A lesson-ingest
subscription is one more consumer of that buffer. This RFC's novel surface is
therefore small: the **export event contract** and the **ingest primitive**;
the fan-out, the durable buffer, the auth and the wake are the webhook
subsystem's job, not this one's.

## 2. Motivation

### 2.1 The re-learning tax on a shared codebase

The value of a lesson scales with **shared context**. Two crews on unrelated
projects have little to teach each other — `repo_scope` on a lesson would filter
it out anyway. But *N* crews on **one repo** share the entire problem space:
their lessons are overwhelmingly relevant to each other. Today that relevance is
wasted, because memory is per-owner and lessons never cross the instance
boundary. Every crew pays the same discovery cost independently, and a new
teammate's crew starts from zero.

`learn_add` already carries the exact metadata this needs. Verified at
`4926e50b3c`, `mcp_tools/learn.py` exposes `learn_add` with a `rule`, an
optional `negative` (what NOT to do), a `category`, an `applies` tier
(`always` for standing rules vs `on_topic` for findings), and a `repo_scope`
that restricts a lesson to one codebase. The store behind it distinguishes
Global V1 from a Crew Member's private V2. So the wire schema is not a new
vocabulary — it is the fields `learn_add` already takes, plus provenance.

### 2.2 Why replication, not a shared brain

[#10836](https://github.com/kirodotdev/KiroCrew/discussions/10836) proposes the
other topology: many human principals reaching **one Gateway** over *shared*,
membership-gated memory. Its author is candid that this "cuts against the
project's single-tenant identity philosophy" — it introduces distinct human
identities and a shared trust boundary, which is the hard part. That discussion
scopes its v1 to a single host and lists **multi-host teams and
fleet-distributed membership as out of scope**.

Replication is the loosely-coupled inverse. Each crew stays single-tenant and
isolated; only *knowledge* moves, as events. There is no shared control plane to
authorize, no shared store to lock, no cross-tenant identity to model. A crew
being offline just means it consumes the pending lessons when it returns. This
is precisely the multi-host case #10836 defers, and it is achievable without
touching the identity model that makes the shared-state design contentious.

### 2.3 Consumers can already build the transport — so what is missing is the contract

Nothing in this RFC's *delivery* half is unbuildable today. A consumer can
publish a lesson to an SNS topic, fan out to a Lambda per subscribed crew, and
have the Lambda POST the crew's authenticated agent webhook with a "learn this"
instruction; a DynamoDB table is the durable log. What is missing, and what this
RFC asks for, is a **supported contract** so every team does not reverse-engineer
the memory store or hand-roll the adapter:

1. a documented **lesson-export event** the crew emits, rather than each team
   scraping it out of a transcript, and
2. a supported **ingest primitive** that writes a received lesson into memory
   idempotently, rather than each team writing the internal V1/V2 store
   directly (fragile and version-coupled).

## 3. What exists today, and what each piece becomes

Verified at `4926e50b3c`. Paths are relative to `src/kiro_crew/`.

| Piece | What it does today | In this RFC |
|---|---|---|
| `mcp_tools/learn.py` `learn_add` | Records a lesson with `rule`, `negative`, `category`, `applies` (`always`/`on_topic`), `repo_scope`; refuses runtime-identity assertions via `lesson_validation.contains_volatile_lesson_fact` (called from `vector_memory.write_lesson`, `learn.py`, `dashboard/handlers/cron.py` and `onboarding_import.py`); gates the write on `capabilities.memory_writes` through `mcp_core._vet_memory_writes_governance`; returns `refused`/`deduped`/`unchanged`/saved | Gains an **optional emit**: on a successful, team-relevant save, it publishes a lesson-export event (§4) to the configured sink, after an outbound redaction pass (§6). No change to what it stores or to callers that do not opt in. |
| The lessons store (Global V1; per-member private V2) | Local, owner-bound; a member's V2 is never readable by another member. Two writers exist: `vector_memory.write_lesson` DELETES an overlapping stored lesson on exact-substring or `>=50%` topic overlap ("newer replaces older"), while `vector_memory.set_semantic_if_absent` is **merge-only** and never tombstones a row another writer owns | **Unchanged, and ingest is merge-only.** Ingest writes through `set_semantic_if_absent` (the absent-only writer `onboarding_import._write_instruction` already uses for exactly this reason — its own comment: *"NOT `write_lesson`: it deletes an existing lesson on exact-substring OR >50% topic overlap … a foreign directive can delete a correction the USER taught the agent. Import is merge-only"*), behind a recognise-but-do-not-replace overlap test. An inbound foreign lesson therefore never retires a correction the local user typed by hand; on overlap the local lesson wins and the inbound one is dropped. No store is ever shared or read across instances. |
| `POST /api/hooks/agent` (`dashboard/handlers/hooks.py` `api_hooks_agent`) | Runs one turn in a `hook:*` session from a caller-shaped body | The **delivery seam** for an ingest wake, via the subscription in [rfc-webhook-subscriptions.md](rfc-webhook-subscriptions.md). A `lesson-ingest` subscription receives a batch of pending lesson events and applies them. |
| `webhooks.py` source store + auth schemes (per rfc-webhook-subscriptions) | Named tokens; `bearer+signed` and (proposed) `github-hmac` schemes | The **inbound authentication** for lesson events arriving from the team's sink. A `lessons` source verifies the sink's signature. |
| The webhook event buffer (proposed in rfc-webhook-subscriptions) | Durable table between ingress and delivery; idempotency, per-subject coalescing, leases, dead list | Where inbound lesson events land before ingest. Idempotency by lesson id is exactly the dedupe this buffer already provides; a burst of lessons coalesces into one wake carrying a batch. |

Nothing in this RFC's ingest half exists yet: there is no `learn_ingest`
primitive, no lesson-export emit in `learn_add`, and no lesson-shaped source or
subscription. Its transport half is the webhook subsystem, which is itself a
`draft` (see the [rfc-webhook-subscriptions.md](rfc-webhook-subscriptions.md)
row), so this RFC is stacked on that one and should not land its delivery phases
before it.

## 4. The event contract

A lesson-export event is the `learn_add` payload plus provenance and an id.

| Field | Source | Purpose |
|---|---|---|
| `lesson_id` | stable hash of `(rule, negative, repo_scope, origin_crew)` | Idempotency and echo-loop prevention: a consumer that already holds this id drops the event and never re-emits it. |
| `rule`, `negative`, `category`, `applies`, `repo_scope` | the `learn_add` fields verbatim | The lesson itself, ingested through the same validation `learn_add` applies locally. |
| `origin` `{crew, repo, ts}` | the emitting crew | Provenance: a bad lesson is traceable to its source and revocable; a consumer can weight or filter by origin. |
| `tier` (**required**, no default) | the lesson's `applies` | Drives the rule-gate (§5). It is **required on the wire**: `learn_add`'s `applies` is optional, and an *unstated* tier is not neutral — `lesson_validation.LESSON_APPLIES_UNSTATED` is served with the **standing-rule** (`always`) treatment (`LEARN_ADD_SCHEMA`: "Absent leaves the row unstated, which is served as a standing rule"). So an emitted lesson that omitted `applies` would ingest with standing-rule effect. The emitter must resolve the tier to an explicit `always`/`on_topic` before publishing; an event without an explicit `tier` is rejected, never defaulted to a permissive value. |

The event is envelope-compatible with the `{v, kind, src, key, ts_ms, data}`
shape the webhook RFC carries in its buffer design. That envelope is **not a
symbol on main**: `src/kiro_crew/events/` is absent — it was the package deleted
with the lifecycle-event log (see the
[rfc-mcp-lifecycle-event-log.md](rfc-mcp-lifecycle-event-log.md) row, which
records that package as deleted), and the webhook RFC carries the shape forward
as a proposed buffer envelope. Treated as that proposed shape, the buffer handles
a lesson like any other event and `key = lesson_id` gives per-lesson coalescing
for free.

## 5. Curation — the crux

An unfiltered firehose makes every crew *worse*, not better: memory is bounded
(the startup rule budget already drops rules beyond a character ceiling), so
replicating personal or environment-specific quirks is active harm. Two policies
make replication help rather than spam, and they are the real design questions:

- **Publish filter (emit side).** Only team-relevant lessons are emitted:
  `repo_scope`d lessons for a repo the team shares, and project-general
  findings. A crew's personal or environment quirk ("this one box has a stale
  plugin") is never published. The default should be *opt-in per lesson or per
  repo scope*, not emit-everything.
- **Rule-gate (ingest side).** The gate holds the **complement**: anything not
  explicitly `on_topic` is held for a human check before it installs — an
  explicit `always`, and (per §4) an untiered lesson too, since an unstated tier
  is served as a standing rule. Only an explicitly-`on_topic` **finding** may be
  ingested automatically. A standing rule changes the consuming crew's behaviour
  in every future session, so it should not auto-install fleet-wide. This
  matches the project's own "turn what you think into what you know" posture:
  broadcast *knowledge* freely; gate *rules* that reshape behaviour.

Two open questions for the discussion and for maintainers:

1. What is the right *default* rule-gate posture, and should it be configurable
   per team?
2. **Poisoned findings.** The sink authenticates the *sender*, not the
   *content*: an authenticated teammate crew can itself be prompt-injected into
   emitting a well-formed, poisoned `on_topic` finding, which "flows freely"
   under the rule above. So "findings flow freely" deserves the same maintainer
   decision as the standing-rule default — e.g. a reputation/quarantine window
   on a new origin, a sampled human check on findings, or origin-scoped trust —
   rather than being treated as automatically safe.

## 6. Safety

- **Outbound egress (emit side).** The export event is the design's first
  egress of durable memory *text* (`rule`/`negative`) off the host, to a
  configurable sink (webhook / SNS / EventBridge). It must not be a bypass of
  the host's exfiltration controls. Before an event leaves the host: (1) the
  `rule` and `negative` pass the host's `redact_credentials` and
  `redact_exfiltration_urls` scrubbers (a lesson can quote a token or a
  presigned URL); (2) emit is behind a **default-off capability scope** so no
  event ever leaves without the operator turning it on; and (3) the operator
  sees what the scope covers (which repo scopes, which sink) before the first
  event is published. Emit is opt-in per §5's publish filter *and* gated by this
  scope.
- **Ingest honours `capabilities.memory_writes`.** Durable memory writes are
  gated by `capabilities.memory_writes` (default on), but today that gate is
  evaluated only at the MCP tool layer — `mcp_tools/learn.py` calls
  `mcp_core._vet_memory_writes_governance`; it is **not** re-checked in
  `dashboard/handlers/cron.py` `api_lessons_create` or in
  `vector_memory.write_lesson`. The ingest primitive is a new, externally-driven,
  higher-volume write path, so it MUST re-evaluate `capabilities.memory_writes`
  itself and refuse when denied. Otherwise an operator (or a tightest-wins
  enterprise policy) that denies durable memory writes would still be silently
  written to through ingest while reading as enforced.
- **Poisoning.** If one crew learns something wrong and emits it, every
  subscriber could install it. Provenance (`origin`) makes a bad lesson
  traceable and revocable; the rule-gate stops a non-`on_topic` bad lesson from
  auto-installing fleet-wide (§5), and merge-only ingest (§3) means even an
  installed bad lesson never deletes a local correction. A future extension
  could sign lessons and support an explicit revocation event.
- **Echo loops.** A consumer that ingests a lesson must not re-emit it as its
  own. The stable `lesson_id` is the guard: ingest is idempotent by id, and an
  ingested lesson is marked non-origin so it is never re-published.
- **Trust framing.** An inbound lesson event is untrusted third-party data, not
  an instruction. It is applied only through the ingest primitive's validation
  (the same `learn_add` refuses runtime-identity assertions and model-selection
  imperatives), never executed.

## 7. Rollout

This RFC is stacked on [rfc-webhook-subscriptions.md](rfc-webhook-subscriptions.md);
its delivery phases assume that subsystem's buffer and subscription exist.

1. **Event contract + emit (opt-in, default-off scope).** Add the lesson-export
   event shape and an opt-in emit hook in `learn_add` behind the publish filter
   and a default-off capability scope, with the outbound redaction pass (§6). No
   consumer yet; a team can point the sink at their own log and inspect what is
   emitted.
2. **Ingest primitive.** A supported `learn_ingest` path that is **merge-only**
   (writes through `set_semantic_if_absent`, never `write_lesson`), idempotent
   by `lesson_id`, applies the rule-gate (holding anything not explicitly
   `on_topic`), and re-evaluates `capabilities.memory_writes` before writing.
   Testable in isolation by feeding it a synthetic event.
3. **Lesson-ingest subscription.** Wire the ingest primitive as a consumer of
   the webhook event buffer: a `lessons` source authenticates the team's sink,
   the buffer coalesces a burst, one wake applies a batch.
4. **Bootstrap replay.** A supported way for a new crew to replay the team's
   durable lesson log on first start, filtered by the publish filter, so it
   arrives current instead of cold.

Phase 1 is buildable and useful before the webhook subsystem lands (a team can
consume the emitted events with their own infrastructure); phases 3–4 depend on
it.

## 8. Decisions and alternatives

- **Ingest primitive vs. a webhook handler that calls `learn_add`.** A native
  `learn_ingest` primitive is preferred over "wake the agent and have it call
  `learn_add`": the latter spends a model turn per lesson per crew and relies on
  the agent to faithfully transcribe the event, whereas a primitive is
  deterministic, idempotent, and free of a turn. Being turn-free, the primitive
  does not inherit the MCP tool layer's checks for free, so it must re-implement
  the ones that matter: it re-evaluates `capabilities.memory_writes` (§6), runs
  `contains_volatile_lesson_fact`, and writes merge-only through
  `set_semantic_if_absent` (§3) — which also means the `superseded` outcome
  `learn_add` prints for a human reader is a non-event here, because a merge-only
  write never supersedes a local row. The subscription still uses the webhook
  wake for delivery; the *application* is the primitive, not an agent turn.
- **Replication vs. shared live memory (#10836 / a shared backend).** Shared
  live memory gives instant propagation but requires a shared store and a trust
  boundary, and reverses single-tenant identity. Replication keeps isolation and
  accepts eventual consistency (a crew sees a lesson at its next wake, not
  mid-turn). For a team of isolated crews on one repo, eventual consistency is
  adequate and isolation is the feature.
- **Emit-everything vs. publish filter.** Rejected emit-everything: bounded
  memory makes an unfiltered firehose harmful. The filter is not optional
  polish; it is the thing that makes the feature a net gain.
