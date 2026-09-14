# Connector capability manifest

The field-level schema for a connector capability manifest entry, and the
work-stream DAG that sequences the connector campaign's implementation
rounds. This spec is **documentation only**: no validator, no runner, and no
CI script ship from it. It is the parent base every later connector-campaign
round (schema validation, provider-capabilities discovery, the live
conformance runner) is built against, and it is deliberately structural
enough for a validator to be written directly against it — field names,
types, required/optional-when rules, and the version relationships between
fields — without that validator's *implementation* shipping here.

Scope note: this spec governs the *campaign contract* — the shape a manifest
entry must have, and the order the campaign's provider streams unlock in. It
does not implement `src/kiro_crew/connections/**` (see
[connections.md](connections.md) for that subsystem's shipped behavior) and
does not implement `src/kiro_crew/knowledge/connectors/**` (see
[knowledge.md](knowledge.md)). A future round implements against this spec.
When that implementation changes what this spec documents, the owning-spec
rule applies exactly as it does everywhere else in this tree: the spec is
updated in the same commit as the code, the same way `connections.md` is
updated when `connections/` changes. Nothing here freezes the schema —
scoped, controlled evolution of it in a later round is the expected path, not
an exception to ask permission for.

Traceability note: the manifest is derived from campaign evidence, and that
evidence has its own provenance trail (which research pass observed which
vendor endpoint, on what date). That trail belongs in the campaign's own
evidence-catalog artifacts, not in this spec — a public, in-repo contract
states what a field means and how it is validated, in one neutral sentence of
provenance per concept, so a reader with only this repo checked out can
understand and implement it. It does not narrate a private preparation
process.

## Two axes, never collapsed into one

A manifest entry answers two independent questions, and collapsing them into
one field loses information a validator needs:

- **Evidence axis** — how well-sourced is the claim that this operation
  exists and has this shape? (`source_status`)
- **Implementation axis** — how far has Kiro Crew actually gotten building
  and verifying it? (`status`)

These mirror, deliberately, the two-axis shape the campaign's own evidence
catalog already uses (`evidence_status` × `implementation_status`) — a
manifest entry that flattened them back into one field would be a regression
against a distinction the campaign already established, not a simplification.

### `source_status` — the evidence axis

| Value | Meaning |
|---|---|
| `user_required` | The user (or the campaign's own mission brief) named this operation as required directly — the highest-authority source. A `user_required` entry outranks anything derived from a vendor-documentation sweep: user-stated scope is the boundary of the requirement, not a floor a catalog sweep can trim. |
| `official_baseline` | Confirmed against the vendor's own official documentation, API reference, or MCP server source. |
| `unverified` | Proposed (by a research pass, by inference from a sibling operation, or by any other non-authoritative route) but not yet confirmed against either the user's own statement or an official vendor source. |

An entry's `source_status` is never deleted for lack of an enum value to hold
it. If a real finding does not fit `user_required` / `official_baseline` /
`unverified`, the fix is to add a fourth value in a later, explicitly-scoped
revision of this spec — never to drop the entry the value would have held.
This applies with equal force to a `blocked` entry: a blocked capability is
evidence Kiro Crew already possesses, and a `source_status` or `status`
enum's shape is a schema question, never a reason to discard it.

**`source_status` proves neither implementation nor liveness.**
`user_required` and `official_baseline` are both claims about *where the
requirement or its shape came from*, not about whether Kiro Crew has built
or verified it — that is exclusively what `status` (below) tracks. An entry
can be `source_status: user_required` and `status: planned` at the same
time, and reading the first as evidence toward the second is a category
error a validator must not make.

### `status` — the implementation axis

| Value | Meaning |
|---|---|
| `planned` | In the manifest, not yet started. |
| `implementing` | Adapter code is being written; not yet passing its own tests. |
| `code_complete` | Adapter code is written and passes its own unit/contract tests, but has not yet run a live `ConformanceRun` against a real account. |
| `contract_verified` | A `ConformanceRun` has produced a `runtime_verified: true` `EvidenceReceipt` against at least one real, authorized `(auth_mode, account_type, surface)` combination the entry declares (see "Per-mode, per-surface, per-auth-mode evidence" below). |
| `live_verified` | Verified across the full applicable `(auth_mode, account_type, surface)` matrix the entry declares — not just the one combination `contract_verified` required. |
| `merged` | The adapter's implementation PR has merged to the default branch. |
| `release_verified` | Confirmed working in a shipped release, not merely on the default branch. |
| `blocked` | Not a rung on this ladder — see "`blocked` is a flag, not a rung" immediately below. |

`status` is a strict eight-value enum (`planned`, `implementing`,
`code_complete`, `contract_verified`, `live_verified`, `merged`,
`release_verified`, `blocked`), matching the campaign's own fixed vocabulary
for this field exactly — no rung is invented and none is dropped.

#### `blocked` is a flag, not a rung

`blocked` appears in the enum above because it is a legal **value of**
`status`, but it does not describe a position on the `planned` →
`release_verified` ladder — it describes a *stall*, which can happen at any
position on that ladder. To make this checkable rather than ambiguous, a
manifest entry stores both fields, never one standing in for the other:

- `status`: when the entry is currently stalled, this is set to `blocked`.
- `last_reached_status`: the highest rung on the `planned` →
  `release_verified` ladder the entry actually reached before the stall
  (never `blocked` itself — this field's own legal values are the other
  seven).

An entry that is not stalled leaves `last_reached_status` equal to its own
`status`; there is no third state to invent. This is the one, consistent
way to say "this entry reached `code_complete` and then got stuck" without
requiring an implementer to guess whether `status: blocked` erased prior
progress: it did not, `last_reached_status` says exactly where it stopped.

Every entry produced by this campaign's current round (the W00-S1 slice, and
the evidence catalog it draws on) has `status: planned` and
`last_reached_status: planned`: nothing has begun implementation yet. A
future round moves entries along the ladder; it does not invent new rungs
without a scoped revision of this spec, and it does not skip a rung silently
(an entry does not jump from `planned` to `merged` without passing through
the rungs a validator can check for, tracked via `last_reached_status` even
while `status` reads `blocked`).

## Manifest entry: one row per required operation

Every required operation the connector campaign tracks — one row per
`operation_id` — carries this field set in the manifest:

| Field | Type | Required | Meaning |
|---|---|---|---|
| `operation_id` | string | yes | Stable identifier. Does not change across campaign rounds once assigned. |
| `provider` | string | yes | The vendor's own name for its surface (e.g. `github`), matching that vendor's own official branding. |
| `service_id` | enum | yes | The campaign's neutral service-range identifier. One of: `github`, `gmail`, `google_drive`, `sharepoint`, `outlook`, `onedrive`, `onenote`, `teams`, `excel_shared_engine`, `office_documents`, `slack`, `asana`, `salesforce`, `zoom` — the 12 named service ranges plus the two Office capability sets (`excel_shared_engine`, `office_documents`), which are horizontal capability groups spanning SharePoint/OneDrive rather than a 13th provider (see `W07` in the DAG below). This is the complete, closed set for the current campaign round; a validator checks membership against this list directly, not against anything outside this document. Adding a service range is a scoped revision of this enum, under the same owning-spec rule any other manifest field evolves by. |
| `required` | boolean | yes | Whether the operation is in the campaign's required scope. |
| `category` | enum | yes | One of `baseline_alignment`, `production_requirement`, `user_extension`. This field renames evidence into a requirement class; it never shrinks scope on its own. |
| `source_status` | enum | yes | `user_required` / `official_baseline` / `unverified` — see "Two axes" above. |
| `source` | object | yes | `{source_kind, source_id, observed_at, snapshot_ref}` — see "`source_kind`, and how a `user_required` or not-yet-sourced entry is represented" below. |
| `observed_at` | string | yes | When the entry's shape was last confirmed against its source, so drift is detectable later. |
| `effect` | enum | yes | One of `read`, `write`, `delete`, `share`, `external_send`, `admin`, `billable`. A closed vocabulary so a governance policy hook can match on it without a free-text field, and so `EvidenceReceipt`'s per-effect verification rule (below) has something to switch on. |
| `input_schema` | object | yes | `{schema_ref, schema_version}` — where the operation's input shape is defined and which version of it this entry targets. Distinct from `output_schema`: an operation's request and response shapes version independently and a validator must be able to check each on its own. |
| `output_schema` | object | yes | `{schema_ref, schema_version}` — same shape as `input_schema`, for the operation's response. |
| `tool_names` | array | yes | The concrete tool name(s) (MCP tool name, REST-wrapper function name, etc.) this operation is invoked through. A validator checks this against the live tool inventory; a manifest entry naming no tool is not yet implementable. |
| `auth_modes` | array | yes | Every auth mode this specific operation supports (e.g. `oauth_user`, `fine_grained_pat`, `service_to_service`). Declared per operation — a manifest entry never assumes every operation on one provider shares one auth mode. Each value here is one axis of the verification matrix below; it is never inferred from `account_types`, and `account_types` is never used as a stand-in for it (see "Per-mode, per-surface, per-auth-mode evidence"). |
| `scopes` | array | yes | The minimal vendor-side scope(s) this operation needs. A manifest entry never requests a broader scope than the operation itself uses. |
| `account_types` | array | yes | Which account types (`personal`, `organization`, `enterprise_cloud`, `work_school`, …) the operation is available under. One axis of the verification matrix below. |
| `surfaces` | array | yes | Which entry points (chat, App, workflow, background) can reach this operation. One axis of the verification matrix below. |
| `policy` | object | yes | The governance hook-point structure this operation's policy attaches to — the platform ∩ workspace ∩ session ∩ connection ∩ provider intersection model. This field declares the hook shape; it carries no policy VALUE. |
| `pagination` | string | when the operation lists or searches | The operation's own pagination contract (`page`/`perPage`, a cursor, `@odata.nextLink`, `queryMore`, …). Declared per operation: two operations on the same provider are not assumed to share one pagination contract. |
| `retry` | object | when the operation writes | Which idempotency/retry class the operation actually has: `base_sha_guard`, `generate_ids_preallocation`, `external_id_upsert`, or `none_verify_by_readback`. A manifest entry never claims a generic exactly-once guarantee an operation does not have. |
| `adapter` | object | yes | `{module_ref, version}` — a placeholder pointing at the implementation module that will back this operation and the version of it a given manifest entry targets. Left with an explicit placeholder value (never silently blank) until an implementation round assigns a real module — this spec does not assign adapters. |
| `code_refs` | array | when applicable | Pointers into an existing reusable subsystem (e.g. `connections/mint.py`) an implementation round should start from. |
| `runner_version` | string | yes | The version of the conformance-runner contract (see below) this entry's verification evidence was produced against. Distinct from `adapter.version` and from `input_schema`/`output_schema` versions — all four can advance independently and a validator must not assume they move together. |
| `verification_contract` | object | yes | `{run_ref, receipt_ref}` — `run_ref` resolves to exactly one `ConformanceRun.run_id` and `receipt_ref` resolves to exactly one `EvidenceReceipt.receipt_id`; both are opaque identifiers, resolved by exact-string lookup, never by any other matching rule (nearest, latest, best-effort). The referenced `ConformanceRun.operation_id` MUST equal this entry's own `operation_id`, and the referenced `EvidenceReceipt.conformance_run_ref` MUST equal `run_ref` — a validator checks both equalities, and a pointer failing either is malformed, not stale. A `status` transition past `code_complete` is valid ONLY against a `ConformanceRun` whose `verdict` is `pass` — a `fail` or `inconclusive` run's `EvidenceReceipt`, however `runtime_verified: true` it is, never promotes an entry's `status`; only its own newer, passing re-run does. Superseded (older-`tested_sha`, or `fail`/`inconclusive`) runs are kept, never deleted — they are the record of what was tried — but a manifest entry's live `status` always resolves against its CURRENT `verification_contract` pointer, which an implementer must repoint to the newest passing run, never leave aimed at a stale one. |
| `evidence_by_mode_surface_and_auth` | array | yes | One row per applicable `(auth_mode, account_type, surface)` combination. See "Per-mode, per-surface, per-auth-mode evidence" below. An entry with an empty array here is only honest at `status: planned` — anything past `code_complete` needs at least one populated row. |
| `tested_sha` | string or null | yes | The immutable commit SHA this operation's implementation was last tested against — see "Immutable ref binding" below. `null` until `status` reaches `code_complete`. |
| `merged_sha` | string or null | yes | The commit SHA at which this operation's implementation merged to the default branch. `null` until `status` reaches `merged`. |
| `release_sha` | string or null | yes | The immutable commit SHA of the release this operation was confirmed working in — see "Immutable ref binding" below; never a tag or other movable alias. `null` until `status` reaches `release_verified`. A human-readable release identifier (e.g. a version tag) is a separate, non-normative display field if one is needed; it is never substituted for this field's SHA value. |
| `status` | enum | yes | The operation's current implementation state — see "Two axes" above. |
| `last_reached_status` | enum | yes | The highest rung reached before a stall — see "`blocked` is a flag, not a rung" above. Equal to `status` whenever the entry is not currently `blocked`. |
| `blocker` | object or null | when `status` is `blocked` | `{reason, owner, unblock_action}` — see below. |

### `source_kind`, and how a `user_required` or not-yet-sourced entry is represented

`source.source_kind` is a closed enum with six values, not four — the
original four are insufficient to represent every legitimate provenance a
`source_status` value above implies, and this spec must not force a real
entry to fake a citation it does not have just to fill a required field:

| `source_kind` | Meaning |
|---|---|
| `official_docs` | The vendor's own published documentation or API reference. |
| `repo_path` | A path inside this repository (e.g. an existing adapter or protocol module). |
| `format_spec` | A named external format specification (e.g. an RFC, an OOXML part). |
| `search_snippet_corroborated` | A search result corroborating the claim, short of a fully rendered official page. |
| `user_stated` | The user (or the campaign's mission brief) stated this requirement directly. Pairs with `source_status: user_required`; `snapshot_ref` for this kind points at the statement itself (a brief section, a decision record), never a fabricated vendor URL. A `user_stated` entry must never be represented as `official_docs` to satisfy a schema expectation of "a vendor source exists" — there may be no vendor source yet, and that is not a defect in the entry. |
| `not_yet_sourced` | No source has been captured yet. Pairs with `source_status: unverified`. `snapshot_ref` is an explicit placeholder string (never a blank, and never a real-looking but unfetched URL) until a research pass supplies one. An entry with `source_kind: not_yet_sourced` is not deleted for lacking a real citation — the absence of a snapshot is itself the state this value exists to record, not a reason to drop the row (see "An entry's `source_status` is never deleted..." above, which applies identically here). |

A validator checks `source_kind` membership against this six-value list; it
never treats an entry's citation as absent just because the citation is
`user_stated` or `not_yet_sourced` rather than a fetched vendor page.

### Per-mode, per-surface, per-auth-mode evidence

`auth_modes`, `account_types`, and `surfaces` together define a three-axis
matrix, not a two-axis one: an operation declared for two auth modes,
`[personal, organization]` account types, and `[chat, workflow]` surfaces has
up to eight cells, and evidence must be checkable cell-by-cell. **An account
type never stands in for an auth mode**: `oauth_user` against an
`organization` account and `service_to_service` against the same
`organization` account are two different authentication paths through the
same account type, and a manifest entry that verified one has said nothing
about the other.

**An auth mode is never excluded from a surface by the auth mode's name
alone.** `service_to_service` is a credential type, not a trigger boundary:
an agentic chat turn can dispatch an operation authenticated with a service
credential exactly as it can dispatch one authenticated with a user's own
OAuth token, so "service_to_service has no chat surface" is not a valid
`exclusion_reason` — it generalizes from the auth mode's name rather than
from an actual provider or policy fact. A cell is `applicable: false` only
when a stated, checkable fact rules it out: the vendor's own API rejects
that auth-mode/surface pairing, the vendor does not issue that credential
type at all, or this repository's own governance policy denies it for this
operation's `effect`. Absent such a fact, the cell is `applicable: true` and
carries its own evidence like any other.

`evidence_by_mode_surface_and_auth` is that three-axis matrix, flattened to
rows:

```
evidence_by_mode_surface_and_auth: [
  {
    auth_mode: string          // one value from this entry's own auth_modes
    account_type: string       // one value from this entry's own account_types
    surface: string            // one value from this entry's own surfaces
    applicable: boolean        // false when this combination cannot occur for this operation
    exclusion_reason: string | null   // required, non-null, when applicable=false — a stated PROVIDER or POLICY fact ruling the combination out (e.g. "the vendor's service-to-service token type carries no scope this operation's chat-surface dispatch requires" or "policy denies interactive-surface dispatch for this operation's admin effect"), never a generalization from auth-mode name alone
    verification_contract_ref: string | null   // resolves to a ConformanceRun.run_id by exact-string lookup, under the same rule as the entry's own verification_contract.run_ref above — including the no-stale-promotion rule (a fail/inconclusive run never promotes this cell); null only when applicable=false
    status: enum                // same eight-value ladder as the entry's own top-level `status`, for this cell only; null-equivalent (`planned`) when applicable=false
    last_reached_status: enum   // same rule as the entry's own top-level last_reached_status (below): the highest rung this cell reached before a stall, carried independently of status so a cell's own blocked flag never erases its own progress
  }
]
```

Every combination the entry's own `auth_modes` × `account_types` × `surfaces`
produces gets a row. A combination that genuinely cannot occur is still a
row, marked `applicable: false` with a stated `exclusion_reason` under the
rule above — silently omitting a cell is indistinguishable from forgetting
to test it, and a validator cannot tell the two apart without the row
existing either way.

**When an empty or placeholder matrix is legal, and when it is not.** At
`status: planned`, `evidence_by_mode_surface_and_auth` may be empty: no cell
has been evaluated yet, applicable or not, and an empty array is the honest
statement of that. The moment an entry's top-level `status` moves past
`planned` (including into `blocked` from any later rung — see
"`last_reached_status`" below), the matrix must be POPULATED and TOTAL: every
combination `auth_modes` × `account_types` × `surfaces` produces exactly one
row (never zero, never more than one for the same triple), each row is either
`applicable: true` with a live `status`/`verification_contract_ref`, or
`applicable: false` with a stated `exclusion_reason` per the rule above. A
validator checks totality (row count equals the cross-product size, no
duplicate triples) as a structural rule the moment `status` leaves `planned`.

A top-level `status` of `live_verified` requires every `applicable: true`
cell to itself be at `contract_verified` or later — `live_verified` is
defined as "every applicable combination is covered," not as a separate
claim asserted independently of the matrix. A validator checks this
relationship directly: it is a structural rule (an aggregate over a set of
rows), not a runtime behavior.

### Immutable ref binding

`tested_sha`, `merged_sha`, `release_sha`, `adapter.version`,
`input_schema.schema_version`, `output_schema.schema_version`, and
`runner_version` are seven independent version references, and none of them
is derived from another at read time:

- **Each is bound at the moment its evidence was produced, not read live
  from whatever the manifest says today.** A `ConformanceRun` records the
  exact `tested_sha`, `adapter.version`, `input_schema.schema_version`,
  `output_schema.schema_version`, and `runner_version` that were live *at
  the time that run executed* (see the `ConformanceRun` table below, which
  carries all five as its own fields) — never "whatever the manifest's
  current copy of those fields says," because the manifest's copy can move
  forward after the run without invalidating the run's own record of what it
  actually tested.
- **A commit SHA is a full 40-character (or the repository's configured
  abbreviated-but-unambiguous) hex string, resolved once and stored
  verbatim** — never a branch name, tag alias, or "HEAD at the time," all of
  which can point somewhere else later. `tested_sha`/`merged_sha`/
  `release_sha` are parsed as opaque strings and compared for exact byte
  equality; a validator does not attempt to resolve one against a live
  repository to check "is this still current," because immutability is the
  property being relied on, not currency.
- **Cross-consistency, not sameness, is the checked relationship.** A
  validator checks that a `ConformanceRun`'s own recorded
  `tested_sha`/`adapter.version`/schema versions/`runner_version` are
  internally consistent with each other *as of that run* (e.g. the adapter
  version that SHA actually built), not that they match the manifest
  entry's current top-level fields — the top-level fields can have advanced
  since, and requiring them to match would make evidence expire the moment
  unrelated progress happens elsewhere.

### `blocker` structure

```
blocker: {
  reason: string          // e.g. "BLOCKED_POLICY", "no_live_fixture_account", "no_console_registration"
  owner: string            // who can unblock it
  unblock_action: string   // the concrete action that unblocks it
}
```

### Discovery is a separate protocol from the manifest, and is required regardless of runner status

The manifest (above) answers "which operations does Kiro Crew intend to
support for this provider." A separate, run-time question — "which
capabilities does a specific authorized account binding actually expose right
now" — is answered by a **provider-capabilities discovery** exchange, not by
this manifest. The two must not be merged into one schema: the manifest is
static and version-controlled; discovery is a live protocol.

This protocol is specified here, in full, regardless of whether the runner
that implements it exists yet: the user has stated discovery as a required
part of the campaign's base contract, and a required contract is not removed
for being unimplemented — the same rule "What 'the required range' means"
(below) states for the manifest's operation counts applies here without
exception. Deferring the *runner* to a later round (see "What this spec
deliberately does not contain") is a statement about implementation
sequencing; it is not a statement that the *protocol* is optional.

Discovery request:

```
discovery_request: {
  provider: string
  account_binding: string       // a verified account/tenant binding reference; never empty
  requested_scope_hint: array   // optional, narrows the probe
}
```

Discovery response:

```
discovery_response: {
  provider: string
  observed_at: timestamp
  scope_snapshot: array          // the scopes this binding actually holds at probe time
  capability_rows: [
    {
      operation_id: string | null       // maps to a manifest operation_id when recognizable; null when the vendor exposes something the manifest does not yet cover
      raw_capability_signature: string  // the vendor's own capability identifier (a scope name, a tool name, …)
      matches_manifest: boolean
    }
  ]
  version_snapshot: string       // the vendor API/MCP surface's own version marker, for drift detection
}
```

`capability_rows[].matches_manifest` is the only field connecting a discovery
response back to the manifest. A `matches_manifest: false` row is recorded as
a gap; it must not be silently dropped, and it must not be auto-written into
the manifest — a human registers a genuinely new vendor capability.

### Conformance and evidence: the structural contract lives here

`verification_contract` and
`evidence_by_mode_surface_and_auth[].verification_contract_ref` on a manifest
entry point at that operation's `ConformanceRun` and `EvidenceReceipt`. Both
are defined structurally in this spec — field names, types, and which fields
are required — so a validator can be built directly against this document.
Neither is implemented here, and neither requires any file outside this
repository to be read to understand: a public spec that pointed at an
unpublished, out-of-repo concept would not be a contract a reader could act
on, so the full field set is inlined below rather than referenced elsewhere.

`ConformanceRun` — one replayable record per verification attempt:

| Field | Type | Required | Meaning |
|---|---|---|---|
| `run_id` | string | yes | Unique identifier for this run. |
| `operation_id` | string | yes | The manifest operation this run verifies. |
| `account_binding_ref` | string | yes | Which authorized account/tenant binding this run used — never a raw credential. |
| `auth_mode` | string | yes | Which of the operation's declared `auth_modes` this run covers. Never inferred from `account_type`. |
| `account_type` | string | yes | Which of the operation's declared `account_types` this run covers. |
| `surface` | string | yes | Which of the operation's declared `surfaces` this run covers. |
| `tested_sha` | string | yes | The immutable commit SHA under test at the moment this run executed — see "Immutable ref binding" above. |
| `adapter_version` | string | yes | The adapter module version under test at the moment this run executed. |
| `input_schema_version` | string | yes | The input schema version under test at the moment this run executed. |
| `output_schema_version` | string | yes | The output schema version under test at the moment this run executed. |
| `runner_version` | string | yes | The conformance-runner contract version this run itself was executed under. |
| `executed_at` | timestamp | yes | When the run executed. |
| `request_shape_hash` | string | yes | A hash of the request shape sent — never the literal request, so no account-specific parameter value is retained. |
| `response_summary` | object | yes | A structural summary of the response (which fields were present, whether types matched) — never a full unredacted response dump. |
| `verdict` | enum | yes | `pass` / `fail` / `inconclusive`. |
| `evidence_receipt_ref` | string | yes | Points at this run's `EvidenceReceipt`. |

`EvidenceReceipt` — the record a `status` transition to `contract_verified`
or later is checked against:

| Field | Type | Required | Meaning |
|---|---|---|---|
| `receipt_id` | string | yes | Unique identifier. |
| `conformance_run_ref` | string | yes | Points back at the `ConformanceRun` this receipt evidences. |
| `claim` | string | yes | A one-sentence, human-readable statement of what this receipt verifies. |
| `runtime_verified` | boolean | yes | `true` only when this receipt is the direct product of a real, live call. `false` marks a design-time placeholder — a `false` receipt can never satisfy a `status` transition past `code_complete`. |
| `readback_result` | object or null | see "Per-effect verification and cleanup" below | The independent, observable confirmation that the operation's effect actually took place, in the shape that effect allows. `null` is valid only for `effect: read`. |
| `cleanup_confirmed` | boolean | yes | Derived, never independently asserted: `true` if and only if `cleanup_status` is `confirmed`; `false` for every other `cleanup_status` value (`not_applicable`, `not_automatable`, `pending`). This field exists for a consumer that only needs a yes/no answer; `cleanup_status` is the field of record, and a receipt where the two disagree is malformed. |
| `cleanup_status` | enum | yes | `not_applicable` (this run's effect created no state needing cleanup, or the state it created was itself the sole test artifact and its own removal is the confirmed cleanup act — see the per-effect table below for which case applies to which effect), `confirmed` (cleanup ran and was independently verified), `not_automatable` (see below), or `pending`. |
| `negative_test_refs` | array | yes | Pointers at the negative-path test(s) this operation's conformance coverage includes (permission-denied, ACL-denied, idempotent-retry — at least one, chosen per the operation's own `effect`/`auth_modes`). |

#### Per-effect verification and cleanup

`effect`'s seven values do not all admit the same verification or cleanup
shape, and a receipt schema that assumed one shape for all of them either
demanded an impossible check on some effects or silently skipped verifying
others. Each value is specified on its own:

| `effect` | `readback_result` requirement | Cleanup expectation |
|---|---|---|
| `read` | `null` — nothing was changed to read back. | `cleanup_status: not_applicable`. |
| `write` | An independent read of the written resource, confirming the new state matches what was written. | Delete or revert the written resource; `cleanup_status: confirmed` once that deletion/revert is itself independently read back. |
| `delete` | An independent read confirming the resource is actually gone (404/not-found on lookup), not merely that the delete call returned success. | `cleanup_status: not_applicable` if the deleted resource was itself the test's only artifact; `confirmed` if deleting it was one step of a larger cleanup that continues. |
| `share` | An independent read, from the **grantee's** side, confirming the grantee can now reach what was shared. | Revoke the grant; `cleanup_status: confirmed` once revocation is independently read back from the grantee's side, mirroring the ACL differential test's own from-the-denied-side verification discipline. |
| `external_send` | Confirmation that the send was accepted by the vendor's own API (a message/delivery ID, a queued/sent status field) — the run cannot always observe the *recipient's* inbox, so this is the strongest observable check available, stated explicitly as weaker than a full round-trip rather than pretended otherwise. | Where the vendor exposes a recall/delete-sent-item API, use it and confirm via `write`'s rule above; where it does not, `cleanup_status: not_automatable` with the reason recorded in the receipt's `claim` field — this is not license to skip disclosure, it is the documented reason no stronger cleanup exists. |
| `admin` | An independent read of the administrative state changed (a permission, a policy setting), confirming the new value. | Revert the administrative state; `cleanup_status: confirmed` once the revert is independently read back. |
| `billable` | Confirmation the billable action was accepted (an invoice/charge/usage-record ID) plus, where the vendor's sandbox/test-mode distinguishes real charges from simulated ones, confirmation the run executed in test mode. **This spec does not itself authorize any real billable action**; it is currently `blocker.reason: "no_billable_action_authorization"` for every `billable`-effect operation until a separate, explicit user authorization for that specific operation's real-money conformance testing exists — this is a present-tense `blocked` state pending authorization, not a permanent exclusion, and a future authorization removes the blocker without any change to this spec. Once authorized (or where a vendor sandbox mode already covers it without needing one), conformance coverage is scoped to what that sandbox/authorization actually verifies. | `not_automatable` in the common case (a real charge cannot be un-charged); `confirmed` only where the vendor's sandbox mode itself exposes a verifiable void/reverse. |

`cleanup_status: not_automatable` is a legal, stable state — not a stand-in
for "not done yet." An entry can sit at `contract_verified` or later with
`cleanup_status: not_automatable` on a specific run, provided the reason is
recorded; the field exists precisely so that state is distinguishable from
`pending` (cleanup is owed and has not happened) rather than forcing every
effect into a binary the `write`/`admin` cases do not share with
`external_send`/`billable`.

A validator can check every relationship stated above directly against
these tables: whether a required field is present, whether
`runtime_verified` is `true` before a `status` transition depends on it,
whether `readback_result` and `cleanup_status` are populated in the shape
`effect` demands. That is the structural contract this slice commits to
shipping; the runner that *produces* a real `ConformanceRun`/
`EvidenceReceipt` is a distinct, separately-scoped implementation round, and
none of the per-effect verification rules above authorize this spec, or any
round consuming it, to perform a real business action (a real send, a real
charge, a real admin change) outside an explicit, separately-approved test
context.

## The work-stream DAG

The connector campaign's work is sequenced into numbered streams, `W00`
through `W16`. This section is the authoritative numbering for the current
state of the campaign; where an earlier document's numbering disagrees, this
section governs.

### The numbering

| Stream | Scope |
|---|---|
| `W00` | The campaign's foundational contract, covering four distinct deliverables: (1) directory/code audit of the existing connector-relevant surface (`src/kiro_crew/connections/**`, `src/kiro_crew/knowledge/connectors/**`, and related subsystems), (2) the machine-checkable schema/validator contract (the manifest schema, `ConformanceRun`, `EvidenceReceipt` — this document), (3) the campaign's architecture (the DAG in this section, the requirement families it references), and (4) the evidence/runner/CI foundation the later validator/discovery/conformance rounds build on. **This document (W00-S1) covers only the docs portion of (2) and (3)** — the audit (1) and the evidence/runner/CI foundation (4) are separate, not-yet-landed deliverables inside `W00`, not implied complete by this PR. |
| `W01` | Shared control plane: binding, auth, policy, reliability. **`W01` is a prerequisite of every provider stream below it** — no provider stream starts ahead of `W01`. |
| `W02` | GitHub. |
| `W03` | Gmail, plus the shared Google auth layer Gmail and Drive both need. |
| `W04` | Google Drive: enumeration, content, metadata, delta (`drive.changes`), permissions/ACL, and upload (simple/multipart/resumable) against the Drive v3 API, sequenced after the Google auth contract in `W03` stabilizes. **This scope is defined by the user's own campaign contract, not by a catalog sweep** — a catalog sweep may corroborate or extend the evidence for it, but the user's statement is what fixes the boundary (see "What 'the required range' means" below for the general rule this is one instance of). |
| `W05` | The shared Microsoft Graph runtime base: Graph's own auth/token layer and client construction, its pagination/locator conventions (`@odata.nextLink` and its cursor semantics), its upload mechanics (simple vs. resumable session), and the RUN-family mechanisms (rate-limit bucketing, write idempotency, concurrency/ETag handling) every Graph-backed stream below reuses rather than re-deriving. |
| `W06` | SharePoint and Outlook. |
| `W07` | OneDrive, OneNote, Teams, Excel, and the other Office capabilities. |
| `W08` | Asana. |
| `W09` | Slack. |
| `W10` | Salesforce. |
| `W11` | Zoom. |
| `W12` | The cloud knowledge-base connectors — explicitly Google Drive, SharePoint, and OneDrive — covering enumeration, content, metadata, delta, and ACL for each. |
| `W13` | GitHub's and Salesforce's own structured data sources, treated as data sources in their own right — each provider's schema/object-model discovery, refresh/sync cadence, query interface, ACL model, and lineage tracking. Not a cross-provider scenario: GitHub's structured-data contract and Salesforce's structured-data contract are two independent deliverables, each fully specified against its own provider's data model, and **each becomes actionable as soon as its own prerequisite stream (`W02` for GitHub's, `W10` for Salesforce's) is ready** — neither waits on the other. |
| `W14` | Product entry points (surfaces) for the campaign's capabilities. |
| `W15` | Independent acceptance: conformance, scale, permissions, end-to-end. |
| `W16` | Release: migration, packaging, post-merge regression, final review. |

### Edges

Each edge names the specific contract or interface it depends on — never an
empty arrow between two stream numbers.

| Edge | Depends on |
|---|---|
| `W00 → W01` | The full required-range index this document and the campaign evidence catalog define (the acceptance index, the shared-contract families, the cross-service scenarios). `W01` consumes this range; it does not redefine it. |
| `W01 → W02..W14` (each provider stream) | The shared control-plane primitives `W01` produces: the binding/auth/policy/reliability contracts (`AUTH-*`, `GOV-*`, `RUN-*` family entries) each provider stream's own operations reference through their manifest `policy`/`auth_modes`/`retry` fields. |
| `W03 → W04` | Google's shared auth layer (the OAuth/token contract `W03` establishes for Gmail) is the same auth contract Drive's operations authenticate through — Drive does not derive a separate Google auth mechanism. |
| `W05 → W06` | Every Graph auth/client-construction primitive `W05` establishes (token acquisition, the client each operation calls through), plus the unified typed error taxonomy (`RUN-01`: auth/scope/consent/not-found/forbidden/quota/throttle/conflict/input/temporary/partial/ambiguous), plus Graph's own pagination/locator convention (`@odata.nextLink` and cursor semantics) and upload mechanics (simple vs. resumable), must all be concrete before `W06` can consume one shared foundation instead of each operation deriving its own. |
| `W05 → W07` | The same full set `W05 → W06` depends on — auth/client construction, error taxonomy, pagination/locator convention, upload mechanics — plus the rate-limit bucketing (`RUN-03`), write idempotency (`RUN-04`), and concurrency/ETag (`RUN-05`) mechanisms, must be concrete before `W07`'s operations can declare a real `pagination`/`retry` value rather than a placeholder. |
| `W04 + W05 → W12` | `W12`'s Drive coverage needs Drive's own concrete mechanism (`W04`); `W12`'s SharePoint/OneDrive coverage needs the Graph runtime base (`W05`) plus, per the edge below, `W06`/`W07`'s own file adapters. |
| `W06 + W07 → W12` | `W12`'s SharePoint coverage consumes `W06`'s own SharePoint file adapter (content, metadata, delta) directly; `W12`'s OneDrive coverage consumes `W07`'s own OneDrive file adapter the same way. `W12` does not re-derive a separate Microsoft file interface — it is built on the concrete adapters `W06` and `W07` ship, not merely "informed by" them. |
| `W02 → W13` (GitHub's structured-data source) | GitHub's account-binding mechanism (`W02`) must be concrete before GitHub's own structured-data contract (schema/refresh/query/ACL/lineage) can authenticate its calls. Actionable independently of Salesforce's readiness. |
| `W10 → W13` (Salesforce's structured-data source) | Salesforce's account-binding mechanism (`W10`) must be concrete before Salesforce's own structured-data contract can authenticate its calls. Actionable independently of GitHub's readiness. |
| every stream's ready operations → `W15` | Each operation flows into `W15` individually, the moment its own acceptance criterion is met — not gated on a whole stream finishing. |
| all required-live streams + `W15` → `W16` | `W16` is the final integration once every provider stream (`W02`, `W03`, `W05`, `W08`, `W09`, `W10`, `W11`, `W14`, plus the streams layered on them) has reached its own ready state and `W15` has completed independent acceptance. |

### Stream-numbering rule

No requirement family occupies a stream slot. The nine cross-cutting
requirement families — AUTH, GOV, RUN, KB, ACL, DATA, UX, SURF, and OPS —
are consumed by the provider and capability-delivery streams above through
the specific edges listed in this section (each citing the exact
contract, e.g. `RUN-01`, it depends on); a family is never itself a stream
number. `W02`–`W14` are each a provider stream, a provider-group stream, or
a capability-delivery stream (`W12`/`W13`/`W14` are capability-delivery, not
providers) — never a requirement family standing in for one of these. This
document's numbering is the authoritative one for the campaign's current
state.

## What "the required range" means, and what this section does not claim

The manifest's `required` field and this DAG's dependency structure describe
the campaign's **required scope** — an operation or contract is in it, or it
is not, regardless of how well any particular research pass has confirmed
it. That determination rests on two sources, in order of authority:

1. **The user's own stated requirement** (a `source_status: user_required`
   manifest entry, or an explicit scope statement such as this document's
   own `W04` row) is authoritative and is never overridden by what a
   service-range sweep did or did not find.
2. **The campaign's evidence-catalog research passes** corroborate,
   extend, or (when a same-day gap surfaces) correct the required set the
   user has stated — a sweep is evidence toward completeness, never a
   substitute ceiling on it.

This document does not restate any particular count (operations found,
contracts found) as a completed denominator: numeric counts belong in the
campaign's evidence-catalog artifacts, which this document's fields
reference by pointer (`source.snapshot_ref`, `verification_contract`) rather
than by repeating the numbers here. What this section fixes, permanently, is
the **rule**, not a number: the required set is bounded by the user's own
statement plus whatever completeness the evidence catalog has established so
far, and a service-range sweep's current tally is never read as proof no
further required operation exists. A gap discovered on the same day a sweep
runs is folded into the required set immediately, not deferred to a future
round — deferring a same-day miss is how a snapshot gets mistaken for a
ceiling.

## What this spec deliberately does not contain

- No validator implementation for the manifest schema above (the schema is
  specified structurally enough for one to be written directly against it).
- No provider-capabilities discovery runner implementation — the protocol
  itself is specified above and is required regardless of the runner's
  implementation status; only the runner is deferred.
- No live conformance runner implementation (the `ConformanceRun` /
  `EvidenceReceipt` structural contract above is specified for a runner to
  target; the runner itself is a separate round).
- No specific manifest entries (which operations, which providers) — those
  live in the campaign's evidence catalog and are populated by a later
  round.
- The `W00` directory/code audit and the evidence/runner/CI foundation (see
  the `W00` row in the numbering table) — this document covers the schema
  and DAG portions of `W00` only.

Each of these is a distinct, separately-accepted follow-up round, sequenced
by the DAG in this document.
