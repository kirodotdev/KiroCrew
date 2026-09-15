# Connector provider-capabilities discovery

The capability manifest
(`docs/system-specs/modules/connector-capability-manifest.md`) answers a
static, version-controlled question: *which operations does Kiro Crew intend to
support for this provider.* Provider-capabilities **discovery** answers a
different, live question: *which capabilities does one specific authorized
account binding actually expose right now.* The two are never merged into one
schema — the manifest is static, discovery is a live probe.

**The discovery protocol's wire shapes are defined once, in the manifest spec's
section "Discovery is a separate protocol from the manifest, and is required
regardless of runner status."** That section is the single home of the
`discovery_request` / `discovery_response` schema; this document does not
restate it. Restating the shapes here would create a second copy of one
contract that drifts silently when the spec is edited — the same single-home
rule this module applies to individual fields, applied to the whole schema.

This document specifies only what is genuinely this slice's own: which
discovery-unique fields the validator (`scripts/check_connector_discovery.py`)
rules on, and — explicitly — which manifest fields it does not.

## What the discovery validator owns, and only that

Against the shapes defined in the manifest spec's discovery section,
`scripts/check_connector_discovery.py` validates the **discovery-unique**
structure:

- `discovery_request`: `provider` and `account_binding` present and non-empty
  strings; `requested_scope_hint`, when present, an array.
- `discovery_response`: `provider` present and non-empty; `observed_at` and
  `version_snapshot` present; `scope_snapshot` an array.
- Each `capability_rows` entry: `raw_capability_signature` a non-empty string;
  `matches_manifest` a boolean (not a truthy string or 0/1); `operation_id`
  **either** a non-empty string **or** `null` — null is a legitimate value, not
  a missing field.

### Malformed-input robustness

The validator treats every input as untrusted and never crashes on a malformed
one — a malformed document is reported and exits as a usage error, not an
unhandled traceback. Two families the file-read path guards against:

- A file whose bytes are not valid UTF-8 (`UnicodeDecodeError`) and a path
  argument containing an embedded NUL (a bare `ValueError`) are both `ValueError`
  shapes that escape `OSError` and `json.JSONDecodeError`; the read path catches
  them and exits as a usage error.
- A required field that is *present but holds `null`* is a violation, not a
  satisfied requirement (`_is_nonempty_str(None)` is false, and container fields
  are checked with `isinstance(..., list)`). `capability_rows[].operation_id` is
  the one deliberate exception: `null` is a legitimate value there, but the key
  must still be present, so "absent" and "null" are told apart.

Membership tests in this module only ever ask whether a *string literal key* is
in a dict, never whether a JSON value is in a collection, so no unhashable JSON
value (list/dict) can reach a native `in` and raise `TypeError`. `scope_snapshot`
and `requested_scope_hint` are validated as arrays; the manifest spec's discovery
section declares no per-element type for them, so this module does not impose one.

### `capability_rows[].operation_id` is nullable, and discovery never writes the manifest

`operation_id` is `null` when the vendor exposes a capability the manifest does
not yet cover. Per the manifest spec's discovery section, a
`matches_manifest: false` row is recorded as a gap that must not be silently
dropped and must not be auto-written into the manifest — a human registers a
genuinely new vendor capability. The discovery validator therefore checks only
that `operation_id` is *well-formed* (string or null); it does **not**
cross-check the id against the manifest's operation set, and discovery never
writes back to the manifest. That mapping and registration are human decisions,
deliberately outside this protocol.

## Fields this module deliberately does NOT validate (single-contract rule)

The manifest/run/receipt fields below each have exactly one canonical home and
one enforcer. This module does not re-implement their validation: a discovery
object does not carry any of them, so there is nothing here to rule on, and a
second validator ruling on one field is a defect, not independence. For each,
the discovery validator's position is **not validated here; the named enforcer
is the sole authority.** The sole enforcer for all four is the manifest
validator, `scripts/check_connector_manifest.py`.

| Field | Its single home (manifest spec section) |
|---|---|
| `snapshot_ref` | manifest entry `source.snapshot_ref` — see "Manifest entry: one row per required operation" and "`source_kind`, and how a `user_required` or not-yet-sourced entry is represented" |
| `source_status` | manifest entry `source_status` — see "`source_status` — the evidence axis" |
| `source_kind` | manifest entry `source.source_kind` — see "`source_kind`, and how a `user_required` or not-yet-sourced entry is represented" |
| `schema_version` | manifest entry `input_schema` / `output_schema` version — see "Immutable ref binding" |

(Sections are cited by heading rather than line number so a spec edit that
shifts lines does not silently invalidate these references.)

### `source_status` and `evidence_tier` are distinct fields — do not merge them

These two are easy to conflate and must never be aliased. They live in different
documents, are enforced by different work streams, and carry **different value
sets**:

- **`source_status`** — the manifest entry's *evidence axis* (home: the merged
  manifest spec, section "`source_status` — the evidence axis"). Three values:
  `user_required` / `official_baseline` / `unverified`. Records *where a
  requirement's authority comes from*.
- **`evidence_tier`** — a field of `catalog-evidence.json`, reused by
  `EvidenceReceipt.evidence_tier` per campaign contract § 6.4. Three values:
  `source_verified_strict` / `search_snippet_or_partial` / `unverified`.
  Records *how strong the captured evidence is*. Conformance/evidence
  work-stream territory (S2/S4), not the manifest spec and not discovery.

They share only the token `unverified`; the other two values differ, and the
concepts differ (authority-of-requirement vs strength-of-evidence). Merging or
aliasing them would collapse two contracts into one — a real defect. Discovery
defines neither: introducing a discovery-local `evidence_tier` would create a
*third* home for one concept, exactly the drift the single-contract rule exists
to prevent. This distinction is normative; the validator does not re-declare
either value set as code.

## Sequencing

The discovery validator is standalone and takes no dependency on the manifest
validator's code. Its CI wiring is deferred to a named integration step after
the manifest validator (`scripts/check_connector_manifest.py`) merges. **Until
that step lands, this validator is not a gate** — it ships its own unit-tested
`--test` self-check and test suite, but nothing in `.github/workflows/` invokes
it, so it guards no PR yet. The integration step is tracked as a named item in
the campaign ledger, not as an edit to any workflow or DAG node here.
