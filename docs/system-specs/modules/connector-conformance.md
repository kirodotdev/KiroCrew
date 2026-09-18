# Connector conformance foundation (W00-S4)

This document is the **conformance foundation** deliverable of stream `W00`.
It adds no validator. It records, in one place, which component discharges each
conformance responsibility the campaign requires, so that no responsibility is
silently dropped when the work is distributed across streams.

## Why this slice ships a mapping and not a validator

The `ConformanceRun` and `EvidenceReceipt` **structural shape** is defined,
inline and in full, in
[`connector-capability-manifest.md`](connector-capability-manifest.md), in its
section "Conformance and evidence: the structural contract lives here". That
spec states a validator can be built directly against the document, and the
**W00-S2 connector-manifest validator** is that validator: it rules on every
`ConformanceRun` field, every `EvidenceReceipt` field, the per-effect cleanup
rules, the readback shape, and the three-way reference identity.

The campaign's single-contract rule is that the manifest, run, and receipt
fields have **one specification source and one enforcer**. A second validator in
this slice would create a second decision point on fields the W00-S2 validator
already rules on — divergence, not independence. The slice-unique remainder for
run/receipt validation is therefore **empty**, and this slice adds no second
validator script.

An empty remainder does **not** mean the conformance obligations disappear. They
are transferred to other owners, and this document is where the transfer is
recorded so the denominator of required work stays fully visible. Each row below
names the owner, the concrete artifact that carries it, and the later step where
it lands.

## How this document names its dependencies

Dependencies are cited by **durable name** — the enforcing function, the spec
section — never by line number, PR number, or commit SHA, because those rot as
soon as a sibling PR merges or a spec is edited (and `docs/README.md` forbids PR
numbers and commit SHAs in this tree). This holds in both the machine-readable
JSON retention form; the W00-S2 validator's
enforcing functions are named so a reader can find them by symbol whichever
commit they land at.

## Responsibility mapping

One row per original conformance responsibility. `current_state` never reads as
"discharged" for a responsibility that has only been reassigned to a component
that has not yet landed on `main`.

The machine-readable JSON block below is the **retention form** of this mapping:
it keeps the required set and its owner mapping in a neutral, parseable shape so
the transferred obligations stay visible and the denominator cannot shrink. It
is retained whether or not any consumer parses it today — "nothing reads it yet"
is not a reason to remove it. This JSON block is the **sole source** for the
mapping — there is deliberately no second restatement (e.g. a parallel table) of
the same rows, because two restatements of one set drift the first time one is
edited without the other, which is the very one-source-one-enforcer discipline
this document argues for elsewhere.

<!-- machine-readable retention form: the canonical 7-row responsibility map -->

```json
{
  "schema": "connector-conformance-responsibility-map/v1",
  "slice": "W00-S4",
  "enforcer": "W00-S2 connector-manifest validator",
  "responsibilities": [
    {
      "id": "conformance-run-structure",
      "obligation": "ConformanceRun record structure (all required fields, types, timestamp, immutable tested_sha, verdict enum, response_summary object)",
      "owner": "W00-S2 connector-manifest validator, function _validate_run",
      "concrete_dependency": "shape defined in connector-capability-manifest.md, section 'Conformance and evidence: the structural contract lives here'; enforced by function _validate_run in the W00-S2 validator",
      "later_integration_step": "W00-S2 validator lands on main",
      "current_state": "specified_in_merged_spec; enforced_by_not-yet-merged_W00-S2_validator"
    },
    {
      "id": "evidence-receipt-structure",
      "obligation": "EvidenceReceipt record structure (receipt_id, conformance_run_ref, claim, runtime_verified, negative_test_refs, cleanup fields)",
      "owner": "W00-S2 connector-manifest validator, function _check_receipt_shape",
      "concrete_dependency": "shape defined in the same manifest section; enforced by function _check_receipt_shape",
      "later_integration_step": "W00-S2 validator lands on main",
      "current_state": "specified_in_merged_spec; enforced_by_not-yet-merged_W00-S2_validator"
    },
    {
      "id": "per-effect-cleanup",
      "obligation": "Per-effect cleanup rules (read=not_applicable; write/admin/share forbid not_automatable; cleanup_confirmed derived from cleanup_status)",
      "owner": "W00-S2 validator, _check_receipt_shape per-effect branch",
      "concrete_dependency": "rules in connector-capability-manifest.md, section 'Per-effect verification and cleanup'; enforced by _check_receipt_shape",
      "later_integration_step": "W00-S2 validator lands on main",
      "current_state": "specified_in_merged_spec; enforced_by_not-yet-merged_W00-S2_validator"
    },
    {
      "id": "readback",
      "obligation": "readback_result shape and rules (null iff effect=read; non-read requires an independent-read object with checked_at/method/matched/detail; matched must be true)",
      "owner": "W00-S2 validator, _check_receipt_shape readback branch",
      "concrete_dependency": "rules in the manifest's receipt table and section 'Per-effect verification and cleanup'; enforced by _check_receipt_shape",
      "later_integration_step": "W00-S2 validator lands on main",
      "current_state": "specified_in_merged_spec; enforced_by_not-yet-merged_W00-S2_validator"
    },
    {
      "id": "three-way-reference-identity",
      "obligation": "Three-way reference identity, three distinct equalities checked by exact-string lookup (never conflating a receipt id with a run id): (1) verification_contract.run_ref resolves to exactly one ConformanceRun.run_id and verification_contract.receipt_ref resolves to exactly one EvidenceReceipt.receipt_id; (2) the referenced EvidenceReceipt.conformance_run_ref equals verification_contract.run_ref; (3) verification_contract.receipt_ref equals that ConformanceRun's own evidence_receipt_ref. Plus orphan-evidence and path-safety scans",
      "owner": "W00-S2 validator, its three-way-equality check and whole-tree evidence scan",
      "concrete_dependency": "rule in connector-capability-manifest.md (receipt/run tables and section 'Immutable ref binding'); enforced by the W00-S2 validator's equality and repo-wide scan",
      "later_integration_step": "W00-S2 validator lands on main",
      "current_state": "specified_in_merged_spec; enforced_by_not-yet-merged_W00-S2_validator"
    },
    {
      "id": "credential-custody",
      "obligation": "Credential custody: a ConformanceRun binds to an authorized account/tenant via account_binding_ref and never carries a raw credential",
      "owner": "kiro-cli owns the OAuth chain and token custody; Kiro Crew holds no connection credential",
      "concrete_dependency": "Custody anchor is merged connections.md, its credential-boundary section: kiro-cli owns the OAuth mint/status/ownership path, the runner never holds a credential, and account_binding_ref is 'never a raw credential' per the manifest — this custody property is established today by that merged section. Separately, the concrete interface that resolves account_binding_ref to an authorized binding is not yet in the merged tree (it is sequenced under W01); this slice does NOT assume it, and reports a missing binding-resolution interface as a named gap rather than inventing one",
      "later_integration_step": "W01 (shared control plane: binding/auth/policy), per the W00->W01 DAG edge",
      "current_state": "boundary_defined_in_merged_connections_md; binding_interface_pending_W01"
    },
    {
      "id": "live-run-dependency",
      "obligation": "Live conformance run producing a real ConformanceRun/EvidenceReceipt with runtime_verified=true",
      "owner": "conformance runner (not yet implemented) + authorized-account evidence",
      "concrete_dependency": "runtime_verified stays false until a real live call. Real live capability depends on a later W15 implementation plus authorized-account evidence. Contract section 5.5 records that no fixture-account owner has been designated — an unassigned-owner fact, NOT a finding that accounts are externally unavailable",
      "later_integration_step": "W00(4) evidence/runner/CI foundation, then W15 independent acceptance",
      "current_state": "no_live_run_in_this_slice; runner_unbuilt_and_fixture-account_owner_unassigned (contract section 5.5)"
    }
  ],
  "pending_contract_items": [
    {
      "id": "evidence-tier-pending-contract-item",
      "statement": "The campaign contract's EvidenceReceipt Schema section (out-of-repo) specifies evidence_tier as a field ON EvidenceReceipt; the merged in-repo receipt table defines no such field and the W00-S2 validator checks none.",
      "governing_rule": "The merged in-repo spec governs on conflict, so the merged receipt shape is authoritative and W00-S2 is correct — NOT a W00-S2 defect. This slice invents no receipt field and adds no check.",
      "drafting_defect": "The contract's evidence_tier comment breaks off at '外加的第四态：' and never names the fourth state, so that schema section is incomplete on its own terms.",
      "resolution": "A decision owned by the campaign-contract owner: amend the contract to match the merged spec, or scope a real evidence_tier field into a later slice.",
      "owner": "campaign-contract owner",
      "not_conflated_with": "source_status (a different field in a different document with a different value set) and the catalog-level operations_by_evidence_tier count (a different proposition from a per-receipt evidence_tier binding); evidence_tier is never aliased to source_status.",
      "current_state": "pending_contract_decision"
    }
  ],
  "open_decisions": [
    {
      "id": "serialization-format-and-path",
      "statement": "The serialization format and in-repo storage location of a manifest entry, ConformanceRun, and EvidenceReceipt remain the manifest spec's explicitly open decision, named in that spec's own conformance section. This slice does not invent them.",
      "owner": "the validator round and the entry-population round jointly"
    }
  ],
  "sequencing_dependencies": [
    {
      "id": "w00-s2-symbol-names",
      "statement": "Rows conformance-run-structure, evidence-receipt-structure, per-effect-cleanup, readback and three-way-reference-identity cite the enforcing symbols _validate_run and _check_receipt_shape in the W00-S2 connector-manifest validator, which is NOT yet on main.",
      "kind": "SEQUENCING_DEPENDENT",
      "depends_on": "W00-S2 connector-manifest validator merging to main",
      "verification_condition": "When W00-S2 lands on main, verify that the symbols _validate_run and _check_receipt_shape still exist under those names in the merged validator; if they were renamed, update these rows' owner/concrete_dependency in the same change, or this map is stale on arrival.",
      "owner": "whoever lands W00-S2, or this slice's follow-up once W00-S2 is merged"
    }
  ]
}
```

Nothing in the JSON above reads as discharged by this slice: rows 1–5 are
enforced by the W00-S2 validator (not yet merged), row 6's binding interface is
pending W01, and row 7 is pending W15. The denominator of required conformance
work is unchanged by this document — it is made visible, not reduced.

## Pending contract item (owned by the campaign-contract owner)

**`evidence_tier` — a receipt field the campaign contract specifies but the
merged spec does not define.** The campaign contract's "EvidenceReceipt Schema"
section (a document not in this repository) specifies `evidence_tier` as a field
**on `EvidenceReceipt`**, verbatim inside the schema block:

```
evidence_tier: enum   // 复用 catalog-evidence.json 已定义的三档：
                      // source_verified_strict / search_snippet_or_partial / unverified，
                      // 此处指"运行时验证"这一档外加的第四态：
```

The merged in-repo receipt table (in
[`connector-capability-manifest.md`](connector-capability-manifest.md)) defines
no such field, and the W00-S2 validator checks none. **The merged in-repo spec
governs on conflict**, so the merged receipt shape is authoritative and the
W00-S2 validator is correct — **this is not a W00-S2 defect.** This slice does
**not** invent a receipt `evidence_tier` field and adds no check for it: a
contract sentence is not a licence to mint a required field.

Resolution is a decision owned by the campaign-contract owner — amend the
contract's schema section to match the merged spec, or scope a real
`evidence_tier` field into a later slice. It is recorded here as **pending, not
discharged and not deleted**.

Drafting note: the contract's `evidence_tier` comment breaks off at "外加的第四态："
and never names the fourth state, so that schema section is **incomplete on its
own terms** — a drafting defect in the contract, not a field to interpret into
existence.

`evidence_tier` is never aliased to `source_status` (defined in
`connector-capability-manifest.md` §"`source_status` — the evidence axis";
values `user_required` / `official_baseline` / `unverified`), a different field
in a different document with a different value set. A catalog-level
`operations_by_evidence_tier` count is a different proposition again from a
per-receipt `evidence_tier` binding and does not stand in for it.

## Fixed constraints

- **Serialization format and in-repo path** for a manifest entry,
  `ConformanceRun`, and `EvidenceReceipt` remain the manifest spec's explicitly
  open decision (named in that spec's own conformance section); they are **not
  invented here**.
- **Live run** is not performed in this slice. Real live capability depends on a
  later **W15** implementation plus authorized-account evidence; contract §5.5
  records that **no fixture-account owner has been designated** (an
  unassigned-owner fact, not a finding that accounts are externally
  unavailable).
- **`runtime_verified` stays false**; nothing in this slice runs live, so this
  slice produces no `contract_verified` claim, no placeholder `tested_sha`, and
  no all-zero hashes.
