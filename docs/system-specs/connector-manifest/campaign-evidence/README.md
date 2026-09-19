# Campaign evidence

A committed, distilled extract of the connector campaign's evidence catalog,
mirrored here so a `source.snapshot_ref` citing it resolves for a reader who
has only this repository checked out — see
[connector-capability-manifest.md](../../modules/connector-capability-manifest.md)'s
`source.snapshot_ref` resolution contract for the rule this directory satisfies.

## Index

- [`catalog-evidence.json`](catalog-evidence.json) — the distilled extract: the
  operations/gaps/required-acceptance reconciliation counts, the evidence-tier
  distributions, and the full `shared_contracts` table (all 72 `contract_id`
  records across the `AUTH`/`GOV`/`RUN`/`KB`/`ACL`/`DATA`/`UX`/`SURF`/`OPS`
  families). It is the authoritative in-repo source for the 273-operation
  reconciliation (`services[].operations[]` 232 +
  `services[].gaps[].demoted_operations_full_record[]` 40 + the one
  contract-attachment operation 1 = 273) and for the 247-entry
  `required_acceptance_index`. `scripts/check_connector_manifest.py` reads these
  counts and cross-checks the denominators, so the extract is machine-consumed,
  not prose-only.

## Scope

This is a distilled copy as of the commit that adds or refreshes it, not a live
sync target and not the campaign's full working catalog. Only the counts, tiers,
and the shared-contract table are consumed in-repo; the full per-service
research catalog is out of scope. A round that needs a fresher extract, or a
specific record resolvable in-repo, re-derives just that content here in the
same commit as the manifest-entry change that depends on it.
