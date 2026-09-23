---
title: Agent self-tagging on the board — governed self-tagging via a protected grants store
status: partial
author: jeeshofone
created: 2026-09-16
last-audited: 2026-09-22
audited-at: 80bd0a81f
doc-pr: null
implementation-prs: [7779]
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
v1, which is on main via [#7779](https://github.com/kirodotdev/KiroCrew/pull/7779);
the tag-manager policy UI, provenance-required minting, and broader v2 scope
remain open.

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
   code-constant workflow states as `add-remove` and mints new status tags
   `add-remove`; an upgrade starts with an **empty** store, so every pre-existing
   tag is human-only until an authenticated dashboard PATCH re-mints it.
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
`main`, and the tag-manager policy control, provenance-required minting and the
v2 scope remain open; the maintainers' merge of this RFC records the default the
lane reads from the base branch.

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
- **Not a per-tag policy UI control.** In v1 the per-tag `agent` policy is
  API-only; the dashboard tag manager control is a tracked follow-up (§Open
  questions, [#7774](https://github.com/kirodotdev/KiroCrew/issues/7774)).
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
  planted self-signed one are indistinguishable. Recovery is authenticated
  re-minting, the same recovery class as any other token-key reset.
- **Closed grant grammar.** A grant may exist only for an id in
  `is_grantable_tag_id`: a 12-hex id the dashboard minted, or one of the five
  code-constant defaults. A hand-planted id outside that grammar is refused any
  grant (`tag_id_not_grantable`). This closes, structurally, the class where an
  agent hand-writes a tag whose *name* reads benign but whose *id* spells an
  instruction and then rides the trusted `[BOARD]` rail once a human toggles its
  status.
- **The recorded status bit drives semantics.** `set_state` eligibility, the
  mutual-exclusivity peer strip, and the "no status tag through `add`" rule key
  on the bit **recorded in the protected store**, not on `tags.json`'s own
  `status` field, so a forged `status` cannot re-route those authorization
  decisions.
- **Fail closed to none/non-status.** An unreadable, malformed, oversized,
  unknown-id, or newer-schema store resolves to `("none", False)`. `remove` —
  including the implicit removal inside `set_state` — requires `add-remove`.

The writers are the authenticated dashboard tag CRUD handlers only, plus a
one-time boot seed from code constants: create mints `add-remove` for a new
status tag; PATCH re-mints/revokes on a status flip and accepts an explicit
`agent` value; delete revokes. Each write is ordered fail-closed (grant minted
before the vocabulary commit on create, revoked before it on delete, downgraded
to `("none", <status>)` across a PATCH window) so a crash between the two stores
never leaves a durable status tag without an authorization row.

`chat_auto_tag.py` does not interact with the grants store: it creates
`status=False` definitions and never applies a status tag, so it neither mints
a row (only the dashboard CRUD does) nor needs one (a topic tag without a row
resolves `none`, which is exactly the authority auto-tagging has never had —
it writes `slot.tags` directly as the gateway, not through `chat_tag`).

### Defaults — the product-shape decision

This is the reversible-once-shipped decision:

- **Fresh install: default-on.** The boot seed mints the five code-constant
  default states — `planned`, `todo`, `implementation`, `review`, `done` — as
  `add-remove`, and `create_tag_definition_off_loop` mints any newly created
  status tag `add-remove`. Rationale: the feature is inert otherwise (there is no
  per-tag policy UI in v1), and workflow states are exactly the tags an agent
  should drive. The seed reads **only** the code-constant ids, never anything
  from `tags.json`, which is agent-writable and must not be promoted into
  authorization.
- **Upgrade: default-off.** An existing install starts with an **empty** store,
  so every pre-existing tag resolves human-only until an authenticated dashboard
  PATCH re-mints it (a status double-toggle, or an explicit `agent` value via
  the API). Rationale: an upgrade must never promote agent-writable `tags.json`
  rows into authorization. A separate identity-only seed
  (`seed_status_identity_rows`) mints `{"policy": "none", "status": True}` rows
  for the default state ids so exclusivity still holds on upgraded installs — the
  bit constrains, and grants no write authority, which is what keeps it safe
  where whole-grant upgrade seeding is not.

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

`POST /api/chat/tags` and `PATCH /api/chat/tags/{id}` now return `400
invalid_status` for a non-boolean `status` where they used to coerce
(`bool("false")` is `True`, which would mint authority from a mistyped string). A
client that serialized the flag as a string must send a JSON boolean. PATCH also
accepts `agent: add-remove | add-only | none`, which is **API-only** in v1.

## Migration plan

Design of record; nothing is on main. This is one PR ([#7779](https://github.com/kirodotdev/KiroCrew/pull/7779)),
not a phased stack, because the directive, the grants store, the defaults, and
the `[BOARD]` line are one indivisible authorization surface — shipping the
directive without the protected store would be the forgeable design this RFC
rejects. **Exit criteria** (assertable, and covered by
`test/test_chat_tag_directive.py`, `test_chat_tags.py`, and the
`TestBoardContextLineAssembly` suite in #7779): `set_state review` replaces an
existing workflow tag; a `none`-policy tag is refused `tag_policy_denied`; an
`add-only` tag adds but refuses removal; an unknown tag is refused `unknown_tag`;
a headless surface is refused; the HMAC provenance chain quarantines on key
rotation; an upgraded install resolves every tag human-only until a PATCH
re-mints; a non-hex/non-default id is refused `tag_id_not_grantable`; a
non-boolean `status` returns `400 invalid_status`; and the `[BOARD]` line renders
row-backed ids only with the agent-writable subset named.

## Backward compatibility

- **Fresh install** gains agent-drivable default states and mints new status
  tags `add-remove`.
- **Upgrade** is inert until a human re-mints, so no pre-existing tag silently
  becomes agent-writable.
- **API:** a client sending `status` as a string now gets `400 invalid_status`
  instead of silent truthy coercion — a corrective break, called out above.
- A build predating the directive simply never advertises `chat_tag`. Rollback
  is deletion of the directive, the grants module, and the `[BOARD]` line.

## Security considerations

- **The authorization store is unforgeable by the party it authorizes** —
  masked, fenced, gateway-only, HMAC-chained, quarantined on unobserved key
  rotation.
- **The closed grant grammar** makes an instruction-shaped planted id
  unrepresentable both as a grant and on the `[BOARD]` rail.
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
- An upgraded install needs one authenticated PATCH per custom tag to make it
  agent-writable; until the per-tag policy UI lands, that is a status
  double-toggle or an API call.

## Open questions

1. **Per-tag policy UI control.** v1 is API-only; the dashboard tag-manager
   control (which also gives upgraded installs a discoverable re-mint affordance
   instead of a status double-toggle) is the tracked follow-up on
   [#7774](https://github.com/kirodotdev/KiroCrew/issues/7774).
2. **v2 scope.** Folder moves, conductor-over-children, propose-confirm for
   human-placed state, and move budgets/cooldowns are deferred to v2, tracked in
   the same issue.
3. **Grant-mint provenance for dashboard ids.** `is_grantable_tag_id` admits
   a 12-hex id by *syntax*, not by recorded provenance. A security review of
   #7779 traced the gap: an agent can plant a 12-hex-id row with
   `status: true` in agent-writable `tags.json`; if a human then toggles that
   tag's status in the tag manager (or PATCHes it with `status`), the mint
   grants the agent `add-remove` on it — authority over a lane the human never
   deliberately created. Two candidate answers; the v1 decision is recorded
   below:
   - **Provenance required.** Record every dashboard-created id in the
     protected store at create time (an identity row, `policy: none`), and let
     the PATCH mint only for ids that carry one. Closes the plant entirely.
     Cost: on an upgraded install every pre-existing custom tag is permanently
     ungrantable — a legitimate old tag and a planted one look identical to the
     store — so the recovery becomes "recreate the tag in the dashboard", not a
     status toggle or PATCH.
   - **Human action as consent (v1 as implemented).** The mint follows only an
     authenticated human action on a tag visible in the tag manager, the same
     action that mints authority for a legitimately created tag; the agent gains
     write on a tag it authored, which a human then chose to make a workflow
     state. Keeps the upgrade re-mint path. Cost: a human who does not notice
     the tag is agent-planted extends agent authority by toggling it.

   The trade is stricter provenance against an upgrade path, which is a
   default-shape decision rather than an implementation detail.

   **v1 decision: human action as consent.** v1 ships with the syntax grammar
   and the human's authenticated status toggle (or explicit-`status` PATCH) as
   the mint's consent step. Reasons: the mint never happens without a human
   acting on a tag visible in the tag manager; the authority granted covers
   only the tag the agent itself authored (a human-created tag with a row is
   unaffected); and provenance-required would strand every pre-existing custom
   tag on upgraded installs behind "recreate the tag" while v1 has no UI to
   show a tag's origin. **Provenance-required is the v2 follow-up**, tracked
   with the tag-manager control on
   [#7774](https://github.com/kirodotdev/KiroCrew/issues/7774): once the UI
   can show which tags carry a dashboard-minted identity row, dashboard create
   records one for every tag, the PATCH mint requires it, and an upgraded
   install's legacy tags are re-identified from the same control. Maintainers
   who prefer provenance-required in v1 should say so on this RFC; the
   implementation change is bounded (identity row at create, `has_grant_row`
   at the mint) and the cost is the upgrade path above.
