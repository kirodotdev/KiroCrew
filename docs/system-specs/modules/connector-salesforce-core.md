# Salesforce connector core (W10 / L1)

The **pure vendor-offline core** for Salesforce: a describe-driven object/field
model, typed payload parsing, an error-classification skeleton, the REST
query-locator pagination contract, the Bulk API 2.0 partial-results contract,
the idempotency closed set, and the Apex `@RestResource` capability gate. It
holds no token, opens no socket, and does no runtime wiring — the shared control
plane (W01's binding/auth/policy/retry) consumes this core at dispatch time. It
lives in `src/kiro_crew/connections/vendors/salesforce/` and is lazy-imported by
a caller; it defines nothing outside its own `vendors/salesforce/**` and does not
import the connections runtime registry, the `connections/control_plane/**`
seam, or `platform/governance.py`.

The `src/kiro_crew/connections/vendors/__init__.py` container anchor is **owned
by W01** (the control-plane seam), not by this stream. W01 mints it once so
parallel provider streams do not collide creating it, and each provider stream
owns only its own `vendors/<slug>/`. This anchor is a real package `__init__.py`
(not a PEP 420 namespace package) precisely because `setup.cfg`'s
`packages = find:` (setuptools `find_packages()`) collects only directories that
carry an `__init__.py`; without the anchor the wheel would silently omit
`vendors/**`. This stream therefore depends on W01's anchor being present (via a
merged base or a legal stack), and never creates or copies it.

This spec is the owning spec for that module tree, in the same sense
[connector-capability-manifest.md](connector-capability-manifest.md) is the
owning spec for the manifest schema: when the code below changes what this
document states, the two are updated in the same commit.

## Two access axes, never merged

A Salesforce *describe* answers two different access questions, and the core
keeps them in two different dataclasses so a consumer never confuses them:

- **Field-level security (FLS)** — the per-field `createable` / `updateable` /
  `accessible` booleans on each field of a describe. Per Salesforce's own
  documented semantics these already fold in *both* the object's security and
  the field's security for the calling user — a two-tier result. They live on
  `FieldLevelSecurity`.
- **Org-level object permissions** — whether the object *as a whole* is
  createable / queryable / updateable / deletable, exposed as object-level flags
  on the describe result (and, authoritatively at runtime, via the
  `ObjectPermissions` sObject the offline core does not query). They live on
  `ObjectPermissions`.

"May this user set THIS FIELD on create" and "may this user create THIS OBJECT"
are different questions; merging their booleans loses the distinction a governed
dispatch needs. A value a describe omits is kept as the named `UNKNOWN` sentinel
— never guessed, never defaulted to `True`/`False`, and not truth-valued (a
`bool(UNKNOWN)` raises, so a caller cannot accidentally read "unknown" as
"denied").

## REST query-locator pagination

The REST query endpoint returns a page as `{totalSize, done, records,
[nextRecordsUrl]}`. When `done` is `false` the page carries a `nextRecordsUrl`
locator the caller fetches for the next page, until `done` is `true`.

- **This is the REST contract only.** `queryMore` is the SOAP API's mechanism;
  REST has no `queryMore` endpoint, so the core does not model one and ignores a
  stray `queryMore` key.
- **Page-size hint:** the `Sforce-Query-Options` request header (`batchSize=N`),
  corroborated bounds minimum 200, default/maximum 2000. The two-page pagination
  test drives a batchSize just past the fixture's threshold to force two real
  pages.
- **Checkpoint discipline:** the durable cursor advances only after a page is
  *fully consumed*. `next_locator(page, page_fully_consumed=...)` returns the
  cursor to persist: it never jumps past an unconsumed page, and the terminal
  page yields no further cursor. The test asserts the cursor is monotonic, ids
  do not repeat across pages, and the terminal page is detectable.

## Bulk API 2.0 partial results

A Bulk 2.0 ingest job moves `Open → UploadComplete → InProgress → JobComplete`
(or `Failed` / `Aborted`). **`JobComplete` means the results are available to
download — not that the caller already holds them, and not that every row
succeeded.** The per-row outcomes live in three separate result sets, each from
its own endpoint: `successfulResults`, `failedResults`, `unprocessedRecords`.

The core preserves all three row-by-row and never aggregates them into one
verdict: a `JobComplete` job with failed rows produces a result with both
`successful` and `failed` populated, so a caller can retry exactly the failed
rows. `results_downloadable(state)` answers "can I fetch results yet" (true for
`JobComplete`/`Failed`/`Aborted`), and supplying per-row results for an
in-flight state is refused as a caller bug.

## Idempotency (closed set)

`retry.idempotency_class` is the manifest's closed four-value enum. Only two
apply to Salesforce writes in the core:

- `external_id_upsert` — a PATCH keyed on a caller-supplied External Id, which
  Salesforce upserts; a retried upsert with the same External Id converges on
  one record.
- `none_verify_by_readback` — no vendor-side key; the only safe retry is
  verify-then-retry.

`base_sha_guard` and `generate_ids_preallocation` remain in the closed enum
(faithful to the manifest) but are never returned for a Salesforce operation.
**A timeout-ambiguous keyless write resolves to `none_verify_by_readback` — there
is no third "retry anyway" class**, which is the whole reason the set is closed.

## Apex capability gate

Two categorically different ways to run Apex, treated as different capabilities:

- **Authorized `@RestResource`** — a named method at a fixed URL mapping, one of
  a fixed pre-declared set. `register_authorized_rest_resource(...)` admits one
  method only if it is in the deployment's explicit authorized allow-list; the
  annotation alone does not authorize it.
- **Anonymous Apex (`executeAnonymous`)** — arbitrary Apex in system mode,
  requiring the high-privilege "Author Apex" permission and bypassing FLS/object
  permissions. `reject_execute_anonymous(...)` **always raises**, under any
  argument; a negative test proves the reject path fires.

## Governance policy scope references

The manifest `policy` object names, per layer of the
platform ∩ workspace ∩ session ∩ connection ∩ provider model, the
`governance.md` `SCOPE_CATALOG` entry that layer's dispatch is gated by, or
`null` when that layer adds no scope. The offline core declares no `policy`
values itself (it does no dispatch); when a later runtime-wiring round declares
them for a Salesforce operation, `connection_scope` / `provider_scope` non-null
values must be exact members of the authoritative `SCOPE_CATALOG` — the layer
field names *which layer*, not a name prefix, so it is not required that a
`connection_scope` value be spelled `connection.*`, and no new scope is
registered to hold one.

## Evidence provenance

`developer.salesforce.com` rejected every automated fetch with HTTP 403, so
**every Salesforce fact encoded in this core is `search_snippet_corroborated`**
— none is backed by a fully-rendered official page, and no fixture is a
live-call capture (each is stamped `source_kind: search_snippet_corroborated`
with a `source` note). Anything that could not be corroborated is kept `UNKNOWN`.

| Encoded fact | `source_kind` | Corroboration |
|---|---|---|
| Per-field `createable`/`updateable`/`accessible` fold in object + field security (two-tier) | `search_snippet_corroborated` | `DescribeFieldResult` semantics; official page 403 |
| Object-level `createable`/`queryable`/`updateable`/`deletable` are a separate axis (`ObjectPermissions`) | `search_snippet_corroborated` | describe object flags / `ObjectPermissions` sObject; official page 403 |
| REST query page shape `{totalSize, done, records, nextRecordsUrl}` | `search_snippet_corroborated` | REST guide snippet; rendered page 403 |
| `done=false` ⇒ `nextRecordsUrl` present; `queryMore` is SOAP-only | `search_snippet_corroborated` | multiple corroborating snippets |
| `Sforce-Query-Options` `batchSize` min 200 / max 2000 | `search_snippet_corroborated` | QueryOptions.batchSize snippet |
| Bulk 2.0 states `Open`/`UploadComplete`/`InProgress`/`JobComplete`(+`Failed`/`Aborted`) | `search_snippet_corroborated` | ingestion/job-states snippets |
| Bulk 2.0 per-row sets `successfulResults`/`failedResults`/`unprocessedRecords`, downloadable at JobComplete | `search_snippet_corroborated` | ingest results snippets |
| `@RestResource` = named/authorized; `executeAnonymous` = arbitrary, "Author Apex", system-mode | `search_snippet_corroborated` | Apex integration snippets |
| Vendor `errorCode` → taxonomy mappings | `search_snippet_corroborated` | REST error-code snippets |
