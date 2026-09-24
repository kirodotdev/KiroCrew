---
title: Agent self-tagging on the board — governed self-tagging via a protected grants store
status: partial
author: jeeshofone
created: 2026-09-16
last-audited: 2026-09-24
audited-at: c079a117d
doc-pr: null
implementation-prs: [7779, 12983]
tracking-issues: [7774]
supersedes: []
superseded-by: []
---

# RFC: Agent self-tagging on the board — governed self-tagging via a protected grants store

The session board is the human's tool for tracking many concurrent
conversations, but an agent that finishes its work cannot mark its own session
`Review`, so a "what needs me" view rots the moment the user stops hand-grooming
it. This RFC records the design that lets an agent move its own session between
workflow states while keeping the board trustworthy for the human: the agent
owns the judgment of *when* to move; a fixed backend authorization layer owns
*who* may move *what*; and a transition is always an explicit declaration,
never inferred. The reversible-once-shipped decision this document exists to
record is the **default grant shape** — which tags are agent-writable out of the
box, and why a fresh install and an upgrade differ. It is a design of record for
v1, which is on main via [#7779](https://github.com/kirodotdev/KiroCrew/pull/7779),
and for the protected-identity requirement recorded below. A1's inline policy
control and explicit adoption are part of this scope; ambient provenance,
history/undo, and proposal-confirm movement controls remain open.

## Summary

Add a `chat_tag` session directive letting an agent set its own session's
workflow state and add/remove non-state labels, gated by a per-tag agent policy
(`add-remove` | `add-only` | `none`) read from a **protected grants store** the
agent cannot write. The positions this RFC takes, one line each:

1. **The record IS the authorization**, so the policy cannot live where its
   subject can write it. Grants live in a dedicated crew-home leaf
   (`<data home>/tag-grants/agent-tag-policy.json`) that is masked from
   sandboxed processes and fenced from the agent file tools and shell — never on
   the agent-writable `tags.json`.
2. **Agents own *when*, a fixed layer owns *who*/*what*.** The verbs are
   explicit (`set_state` / `add` / `remove`); "turn ended with nothing to do ⇒
   Review" inference is rejected.
3. **Defaults are the product-shape decision.** A fresh install seeds the five
   code-constant workflow states as `add-remove`; every positively verified
   owner-dashboard browser create records protected identity, using `add-remove`
   for status tags and `none` for non-status tags. Internal agent/MCP creates
   remain rowless until owner adoption. An upgrade never derives identity from
   `tags.json`, so pre-existing custom tags remain human-only until the
   dashboard owner explicitly adopts them for agent policy.
4. **Fail closed, everywhere.** An absent, unreadable, malformed, or
   newer-schema store resolves to `("none", False)` — human-only, not a workflow
   state — rather than a permissive default.
5. **The trusted `[BOARD]` context line carries a closed grammar.** Only
   grant-row-backed ids in the closed grant grammar render, and the
   agent-writable subset is named beside them.

## Motivation

### Current state

The state before #7779, verified on `main` at `69aeb2803`; #7779 merged as
`780e75e2d` on 2026-09-16 and is what this document records.

- The board vocabulary and slot assignment live in
  `dashboard/chat_tags.py`: tags are user-defined `{id, name, color, status}`
  rows, a session carries a list of tag ids, and a `status: true` tag is a
  mutually-exclusive workflow lane. The human write surface is
  `api_chat_slot_tags` (the PUT) and `api_chat_slot_drop` (the drag), each
  ending in `push_slots_update()`. One non-human writer exists:
  `dashboard/chat_auto_tag.py` (`maybe_auto_tag`), a background pass that mints
  topic tag definitions and appends them to `slot.tags`. It never applies a
  `status: true` tag and creates definitions only with `status=False`, so it
  cannot move a session between workflow lanes.
- No MCP tool can set a workflow-state tag, and an agent cannot even *read* its
  own session's tags. There is no agent-facing tag surface at all.

### Problems

- The core value of a session board is handoff visibility: filtering by `Review`
  should answer "what needs me" across dozens of sessions. With no agent write
  path, workflow tags are truthful only while the human hand-maintains them, and
  they rot as soon as attention lapses.
- Any agent write path is also a hazard, because the board is *also* the human's
  tool. An agent must not be able to move a lane the human reserved for
  themselves, and it must not be able to *grant itself* that authority. The
  naïve design — a policy field on the tag row — fails the second test: `tags.json`
  is an ordinary agent-writable data-home file, so an agent's own file tools
  could forge an `agent`/`status` field and restart-persistently grant itself
  write access to a human-reserved tag.

### Why this needs an accepted RFC

`README.md`'s status contract is read by the First-Principles review lane: a
change to a default — here, *which tags an agent may write by default* — must
trace to an accepted design of record. The default grant shape (§Design →
Defaults) is precisely the reversible-once-shipped product decision that lane
asks to see agreed before it ships, which is why this document exists even
though the mechanism is small. The scoping comment on
[#7774](https://github.com/kirodotdev/KiroCrew/issues/7774) records the same
requirement from the review of #7779. This document is filed `partial`: v1 (#7779) is on
`main`, protected identity is resolved below, and the tag-manager policy control
and broader movement controls remain open; the maintainers' merge of this RFC
records the default the lane reads from the base branch.

## Goals

- Let an agent move its **own** session between workflow states and manage
  non-state labels, under a policy it cannot rewrite.
- Give the agent its **first** tag read surface (the directive's result reports
  the resulting tag list).
- Make the authorization record unforgeable by construction, and fail closed on
  every ambiguity.

## Non-goals

- **Not tagging other sessions.** The directive applies only to the session
  whose turn produced it (self-only).
- **Not folder moves, conductor-over-children, propose-confirm for
  human-placed state, or move budgets/cooldowns.** Those are the v2 scope
  tracked in [#7774](https://github.com/kirodotdev/KiroCrew/issues/7774).
- **Not ambient provenance, history/undo, or proposal confirmation.** B1, B2,
  C1, and C2 depend on the later movement contracts and remain deferred.
- **Not a lane-transition event.** Reacting server-side to a tag change is
  `rfc-session-tag-change-event.md`; that is the read-after direction, this is
  the write direction, and they compose without implying each other.

## Design

### The decision: agents own *when*, a fixed layer owns *who*/*what*

An agent decides *when* to move its session; a fixed backend authorization layer
decides *who* may move *what*. Every transition is an explicit declaration
through one of three verbs — `set_state` (the single mutually-exclusive workflow
state), `add` (non-state labels), `remove` (labels) — with a `custom_validator`
requiring at least one. Inference ("turn ended with nothing actionable ⇒
Review") is rejected: the alternatives-rejected section records why. The
`chat_tag` tool joins `DIRECTIVE_TOOLS` and is applied through the same
`tags_write_lock → validate_folder_tag_ids → save_slot_off_loop` chokepoint the
human PUT uses, with a fresh `slot.tags` read inside the lock (a concurrent
folder/board edit landing mid-apply is the stale-read class the lock closes).

### The trust model

The authorization record cannot live where its subject can write it (the same
class of control as `computer_use.json`). So grants live in a dedicated
crew-home leaf, `<data home>/tag-grants/agent-tag-policy.json`, with these
properties, all traceable to `dashboard/chat_tag_grants.py`:

- **Masked and fenced.** `tag-grants` sits in `sandbox._CREW_HIDDEN_LEAVES`
  (and `sandbox._CREW_PRECREATE_HIDDEN_DIR_LEAVES`, pre-created 0700 before every
  namespace spawn) so a sandboxed process cannot see it, and in
  `security._CREW_SECRET_LEAVES` so the agent file tools and shell cannot read
  or forge it. It is deliberately **not** under `trust/`, which must stay
  sandbox read-write for the SEL log; only the gateway process opens the path.
- **HMAC provenance.** Grant rows carry a MAC under a per-store key that is
  itself certified against `token_signing.key`. A token-key rotation the gateway
  did not observe breaks the certificate; the boot pass then **quarantines** the
  store (renames it aside, never deletes evidence) and reseeds from trusted
  constants — fail closed, because at that moment a legitimate store and a
  planted self-signed one are indistinguishable. Code-constant defaults reseed;
  a custom tag whose identity row was quarantined must be explicitly adopted
  again by the dashboard owner.
- **Closed grant grammar plus recorded identity.** A positively verified
  owner-dashboard browser create generates a closed-grammar id and records a
  protected row. Internal agent/MCP creation uses the same bounded vocabulary
  writer but records no protected row. A later
  PATCH may mint or change policy only when that row already exists; 12-hex
  syntax alone is never provenance. A hand-planted row in agent-writable
  `tags.json` therefore stays human-only after any status or policy toggle.
  Pre-existing custom tags without rows are indistinguishable from planted
  rows and remain human-only until explicit owner adoption. The grammar remains
  defense in depth for the trusted `[BOARD]` rail.
- **The recorded status bit drives semantics.** `set_state` eligibility, the
  mutual-exclusivity peer strip, and the "no status tag through `add`" rule key
  on the bit **recorded in the protected store**, not on `tags.json`'s own
  `status` field, so a forged `status` cannot re-route those authorization
  decisions.
- **Fail closed to none/non-status.** An unreadable, malformed, oversized,
  unknown-id, or newer-schema store resolves to `("none", False)`. `remove` —
  including the implicit removal inside `set_state` — requires `add-remove`.

The protected-store writers are positively verified owner-dashboard browser
create/adoption, provenance-backed PATCH, delete, and a one-time boot seed from
code constants. Owner-browser create records an identity row (`none`/non-status
for labels, `add-remove`/status for workflow states). Internal agent/MCP create
still persists a bounded vocabulary row but records no protected identity; PATCH
therefore cannot grant it until owner adoption. Delete revokes. Each write is ordered fail-closed (row minted before the vocabulary
commit on create, revoked before it on delete, downgraded to
`("none", <status>)` across a PATCH window) so a crash between the two stores
never leaves stale authority. `GET /api/chat/tags` preserves its list shape and
decorates copied rows with `agent`, `agent_provenanced`, and
`agent_store_degraded`; those derived fields are never written to `tags.json`.

A create whose vocabulary write reports failure reconciles while still holding
the tag-write lock. A bounded no-link reread that contains the generated id
commits the durable snapshot back to memory and returns success. Positive
absence removes the in-memory candidate and revokes its identity row when
one was minted. An
unreadable reread fails closed the same way: it withdraws the in-memory
candidate, revokes any minted identity row, and returns failure, because the
caller was answered with a 5xx and an unconfirmed tag must not keep agent
authority in this process. The accepted cost: if that write did in fact
commit, a later vocabulary write from this process that runs before any
reload persists the withdrawn snapshot and removes the row, so the tag the
caller was told had failed is gone rather than half-present. The regression
`test_create_unreadable_reconciliation_withdraws_memory_and_identity` pins
this behavior.

`chat_auto_tag.py` does not interact with the grants store: it creates
`status=False` definitions and never applies a status tag, so it neither mints
a row (only verified owner-browser create/adoption does) nor needs one (a topic tag without a row
resolves `none`, which is exactly the authority auto-tagging has never had —
it writes `slot.tags` directly as the gateway, not through `chat_tag`).

### Defaults — the product-shape decision

This is the reversible-once-shipped decision:

- **Fresh install: default-on for workflow states, identity-only for labels.**
  The boot seed mints the five code-constant default states — `planned`, `todo`,
  `implementation`, `review`, `done` — as `add-remove`. A positively verified
  owner-dashboard browser create records every new id: status tags use
  `add-remove`, and non-status tags use `none`. Internal agent/MCP creates stay
  rowless. The latter grants no write authority but proves the id came through
  the trusted create path, so a later human policy change can safely update it.
  The seed reads **only** code-constant ids, never anything from `tags.json`.
- **Upgrade: legacy custom tags stay human-only.** No row is inferred from
  `tags.json`. A pre-existing custom tag and a planted tag are indistinguishable,
  so neither can acquire agent authority through ordinary PATCH. The A1 tag
  manager identifies rowless tags and offers the dashboard owner a separate
  adoption action that records `none` policy plus the explicitly confirmed
  status bit; policy selection remains a later PATCH. A separate identity-only
  seed (`seed_status_identity_rows`) records `{"policy": "none", "status": true}`
  for code-constant default state ids so exclusivity still holds without
  granting write authority.

### The `[BOARD]` context line

`context.py`'s `build_message` emits one per-turn line —
`[BOARD] tags: … · agent-writable: …` — from a pre-resolved `[(tag_id, policy)]`
list the runner (`chat_runner`) supplies, so `context.py` stays free of
dashboard/state imports. It carries **ids, never names** (names are
agent-writable prose), only grant-row-backed ids in the closed grammar
(`resolve_board_tags` drops the rest), and names the agent-writable subset
(policy ≠ `none`) beside the full set. `_board_safe_tag_name` is an allowlist to
`is_grantable_tag_id` plus a redundant injection screen — the closed grammar is
the defense, the screen is defense in depth. The line is omitted when the slot
has no tags, and `(none)` renders when all tags are human-only.

### Refusals, surface gating, and audit

- **Named refusals** surface as the directive result string:
  `tag_policy_denied:<id>` (not agent-writable for the op), `unknown_tag:<id>`,
  `status_tag_requires_set_state:<id>` (a status id smuggled through `add`),
  `not_a_status_tag:<id>` (`set_state` on a non-status tag),
  `status_identity_unprotected:<id>` (a vocabulary status tag with no protected
  row on an upgraded install), and a `no_op` that still returns the current tag
  list (the READ path).
- **Surface gating.** `chat_tag` is in `_USER_SURFACE_DIRECTIVES`, so a headless
  caller (cron injection, sub-agent sharing the slot) is refused, and a
  slot-less channel turn is refused because the effect targets the slot.
- **Audit.** Every application emits a SEL `chat.self_tag` event
  (`allowed`/`denied`) at the applier, which is where the effect runs.

### Declared API change

`POST /api/chat/tags` and `PATCH /api/chat/tags/{id}` return `400
invalid_status` for a non-boolean `status` where coercion would turn a string
such as `"false"` into true. PATCH accepts
`agent: add-remove | add-only | none` only for tags carrying a protected
identity row. `GET /api/chat/tags` remains a JSON list; each copied row includes
`agent`, `agent_provenanced`, and `agent_store_degraded`, and none of those
projection fields is persisted to `tags.json`. `POST
/api/chat/tags/{id}/adopt` accepts only the status bit the owner is looking at,
records a `none` identity row, and never sets agent policy. It requires an
authenticated dashboard-owner browser request; app, member, internal agent/MCP,
unattributable, and body- or header-forged callers are refused. The create route
still accepts a verified internal MCP vocabulary write, but it records no
protected identity or default policy for that caller.

## Migration plan

The protected store remains schema version 1: an identity row is the same
`{policy, status}` row already understood by all readers, and `policy: none`
adds provenance without authority. No row is inferred from `tags.json`.
Code-constant default-state identity seeding remains unchanged. Custom tags
without protected rows remain usable by humans but cannot acquire an agent
policy through PATCH; the owner-only adoption action records identity first.

Exit criteria: positively verified owner-dashboard browser create records
`none`/non-status for a plain tag and `add-remove`/status for a workflow state;
internal agent/MCP create persists vocabulary but remains rowless; ordinary
PATCH refuses every mint for a rowless 12-hex id; owner-only adoption records
identity without setting policy; GET projects coherent policy, provenance, and degraded-store state
without persisting derived fields; HMAC verification, quarantine, caps, and
rollback compensation retain their fail-closed behavior.

## Backward compatibility

- **Fresh install:** default workflow states remain agent-drivable; every new
  owner-browser-created tag gets protected provenance, with no authority for a
  plain label until its policy is changed. Internal agent/MCP-created tags stay
  rowless until owner adoption.
- **Upgrade:** pre-existing custom tags remain human-usable and rowless until
  the dashboard owner explicitly adopts them. Adoption preserves the tag,
  records identity with `none` policy, and leaves policy selection to a later
  PATCH. The protected store schema and code-default seed remain compatible.
- **API:** GET adds fields to each list row without changing the list envelope or
  persisting them. A client sending `status` as a string gets `400
  invalid_status`.

## Security considerations

- **The authorization store is unforgeable by the party it authorizes** —
  masked, fenced, gateway-only, HMAC-chained, quarantined on unobserved key
  rotation.
- **Protected identity precedes policy.** Closed-grammar syntax constrains the
  trusted rail, while an existing protected row is what authorizes PATCH to
  mint or change policy. A planted 12-hex id satisfies the former and fails the
  latter.
- **Semantics key on the recorded status bit**, not the agent-writable field, so
  a forged `status` cannot re-route exclusivity or verb eligibility.
- **Fail closed** on every ambiguity: none/non-status, never a permissive
  default.
- **Surface gating** keeps cron and sub-agent turns from retagging a user's
  session.
- **The `[BOARD]` line is a trusted rail** carrying only sanitized, row-backed
  ids.

## Alternatives considered

- **Policy fields on `tags.json` rows.** Rejected: `tags.json` is
  agent-writable, so an agent could forge its own `agent`/`status` and
  restart-persistently grant itself a human-reserved tag. This is the forgeable
  design the protected store replaces.
- **Seed grants from `tags.json` on upgrade (TOFU).** Rejected: it launders
  agent-controlled data into authorization — edit the file before the upgrade,
  get a protected grant after it. Upgrade seeds only identity-preserving
  `none`-rows from code constants.
- **A backend rules engine / inference ("nothing to do ⇒ Review").** Rejected:
  the agent must explicitly declare the transition; inference makes the board
  move without an owner of the decision.
- **Unrestricted tooling (an ungated tag write).** Rejected: the board is also
  the human's tool, and a human-reserved lane must be unwritable by the agent.
- **Reusing `trust/` for the store.** Rejected: `trust/` is sandbox read-write
  (the SEL log appends there), so an authorization store living there is
  forgeable by a spawned agent script's plain `open()`.

## Consequences

- Filtering by `Review` answers "what needs me" without hand-grooming, because
  an agent tags itself on its last action.
- The board carries a real authorization surface with its own store, provenance
  chain, and recovery semantics — more moving parts than a tag field, in
  exchange for being unforgeable.
- An upgraded install keeps pre-existing custom tags human-only until the
  dashboard owner explicitly adopts them; adoption preserves the tag and
  records identity without granting agent writes.

## Open questions

1. **Further B/C scope.** Ambient provenance, history and undo, direct-apply
   undo, persistent proposal confirmation, folder moves,
   conductor-over-children, propose-confirm for human-placed state, and move
   budgets/cooldowns remain deferred.

## Resolved decisions

### Grant-mint provenance for dashboard ids

A 12-hex id is syntax, not provenance. A positively verified owner-dashboard
browser create records a protected row: `policy: none, status: false` for a plain
label, and the existing `policy: add-remove, status: true` default for a workflow
state. Internal agent/MCP creation persists only the vocabulary row, so the same
agent cannot turn one approved create into durable `chat_tag` authority. PATCH may mint or change policy only when `has_grant_row(tag_id)` is true.
The rule applies even when the caller explicitly supplies `status` and `agent`.

This chooses provenance-required over human-action-as-consent. A hand-planted
12-hex tag can be visible in the tag manager and can still be renamed,
recolored, reordered, or deleted by a human, but no status or policy toggle can
promote it into the protected store through ordinary PATCH. A pre-existing
custom tag without an identity row has the same treatment because the store
cannot distinguish it from a planted row. The A1 tag-manager action lets the
dashboard owner deliberately adopt either one: it records `none` policy with
the status bit the owner saw, then exposes the ordinary three-state policy
control. Adoption never sets policy itself. The store schema stays at version 1
because identity uses an ordinary `none` row rather than a second record type.
