# Asana connector: vendor-protocol logic (W08)

The Asana provider stream's first slice: the network-free **vendor-protocol
logic** for Asana — field shaping, pagination, error classification, create
semantics, and the two authorization surfaces. This is the owning doc for
`src/kiro_crew/connections/vendors/asana/`.

Scope note: this slice is pure logic and Asana-specific shaping only. It builds
no request, holds no credential, and performs no I/O. Real authorization and
transport are a later stream that consumes the shared control plane's landed
interfaces (`kiro_crew.connections.control_plane`); a test double is never a
production-ready claim. What this slice consumes from single sources it does not
own:

- the shared control plane's dispatch/error vocabulary, from
  `kiro_crew.connections.control_plane` (the seam this stream stacks on), when a
  later slice wires real calls;
- the manifest-entry field schema, from
  [connector-capability-manifest.md](connector-capability-manifest.md);
- the governance scope catalog, from [governance.md](governance.md) — this
  stream registers **no** new scope.

## What this stream owns, and what it consumes

| Owns (here) | Consumes (elsewhere, unchanged) |
|---|---|
| Asana field rules (opaque GID, `due_on`/`due_at` separation, many-to-many projects, explicit workspace) | The manifest-entry schema those facts mirror |
| The offset/limit pagination contract, the opaque expirable offset cursor, the legacy non-paginated truncation ceiling | The retry/backoff strategy a stale cursor triggers |
| Asana's own HTTP+JSON error taxonomy, including the hard cross-workspace denial | The neutral control-plane error envelope a later adapter maps onto |
| Create semantics: no idempotency key modeled as attempt-scoped readback, batch partial failure per item | The dispatch seam and credential/transport a later slice wires |
| The two authorization surfaces (MCP vs native REST) kept non-interchangeable | The governance `SCOPE_CATALOG` a later policy value must be a member of |

The stream does **not** define the shared error/result/context envelope, a
generic backoff, a second auth/governance/runtime, or any live call. Those are
the shared control plane's, consumed by a later slice, not restated here.

**Deliberate deviation from the sibling Zoom slice (recorded, not an oversight).**
W01 is now merged to main with RUN-01's `ErrorClass` / `operation_error()` /
opaque `next_cursor`, and the Zoom slice's `classify_error` maps straight into
that shared `ErrorClass`. W08 instead keeps an Asana-local taxonomy
(`AsanaErrorCategory` / `NextPage(offset=...)`) and defers the fold onto the
shared envelope to the wiring slice. This is intentional: this slice is a
network-free, single-concern vendor-fact model, and the vendor->shared mapping is
a wiring concern (it needs the live dispatch seam to observe real responses),
not a pure-logic one. The later Asana wiring adds exactly one mapping layer at
the seam — the same place the Zoom mapping ultimately resolves — so no second
retryability definition survives into runtime. `plan_create_retry`'s `"failed"`
outcome is likewise a wiring-supplied classification: the wiring author maps a
CERTAIN-non-commitment response (4xx validation) to `"failed"` -> `SAFE_FRESH_CREATE`
and steers any 5xx/ambiguous create response to `"unknown"` -> `READBACK_REQUIRED`,
so an ambiguous 5xx never routes to a duplicate create.

## Field rules (`fields.py`)

- **GID is opaque.** A GID is kept as an exact string — never int-coerced,
  ordered, or zero-padded — because Asana GIDs look numeric but are opaque
  identifiers.
- **`due_on` and `due_at` are distinct fields, never inter-derived.** `due_on`
  is date-only (`YYYY-MM-DD`); `due_at` is a datetime. On the **write** path
  they are mutually exclusive (`_reject_both`) — a caller supplies at most one.
  On the **read** path, `DueDate.from_read`/`StartDate.from_read` accept Asana's
  own response shape, in which `due_on` is returned populated with the date
  component whenever `due_at` is set: the datetime wins and the derived date is
  dropped, so a legal response is never rejected and the date is never inferred
  back. `start_on`/`start_at` follow the identical rule.
- **A task belongs to many projects.** `projects` is a membership set; a task in
  zero projects is workspace-rooted, a distinct legal state.
- **Workspace is explicit, never inferred from projects.** `parse_task` reads a
  workspace only if the object states one.

## Pagination (`pagination.py`)

- **`offset`/`limit`.** `limit` is the page size, valid only in the inclusive
  range **1..100** — this bound is **Asana-documented**; `normalize_page_request`
  refuses an out-of-range limit at shaping time rather than as a discovered 400.
- **`offset` is an opaque cursor token, not a row index.** It comes from the
  prior page's `next_page.offset`; a numeric offset is a shaping error.
  `NextPage` carries only the `offset` — Asana's envelope also returns
  `path`/`uri` continuation references, but this connector drives pagination
  purely by the cursor and does not model them.
- **The offset token expires on an UNDOCUMENTED schedule.** Its TTL is marked
  **unknown**, and the HTTP status Asana uses for an expired offset is
  **unobserved** and deliberately not asserted; `classify_offset_rejection`
  builds the typed stale-offset signal from a rejection the caller determined.
- **The ~1000-object legacy non-paginated ceiling.** Asana **documents** that
  its non-paginated ("legacy") endpoints truncate at *approximately* 1000
  objects. `LEGACY_UNPAGINATED_TRUNCATION_LIMIT = 1000` encodes that documented
  approximate value; a result **at** the ceiling is treated as *possibly*
  truncated (not certainly complete), preserving Asana's own "around 1000"
  imprecision rather than asserting an exact cutoff.

## Error classification (`errors.py`)

`classify_error` maps an Asana HTTP status + JSON `errors[]` body onto a closed
`AsanaErrorCategory` set, reading the status first and consulting the body for
detail. This is Asana's own vendor-error taxonomy (W08 owns it), distinct from
the shared control-plane error boundary a later adapter maps onto.

- **Cross-workspace denial is ALWAYS explicit, never a silent fallback.** When a
  GID outside the caller's token workspace scope is rejected,
  `raise_cross_workspace_denial` **raises** `CrossWorkspaceDenied` — it never
  returns the exception as a value, never re-issues against another workspace,
  and never swallows the denial. Raising (not returning) is the enforcement:
  a returned exception could be kept and ignored, which is exactly the silent
  fallback the contract forbids.
- **403-vs-404 is not asserted.** Whether Asana returns 403 or 404 for a
  cross-workspace GID is **unobserved**; the denial records the observed status
  verbatim if the caller saw one but pins neither as canonical, and a bare
  403/404 is classified as `FORBIDDEN`/`NOT_FOUND`, never guessed as
  cross-workspace.

## Create semantics (`create.py`)

Asana provides **no** idempotency key, client dedupe token, or natural-key
uniqueness on any create, on both surfaces. Retry safety is the caller's
responsibility, modeled as attempt-scoped readback:

- `RetryDisposition` has no `exactly_once` value; `plan_create_retry` returns
  `READBACK_REQUIRED` for an ambiguous create, never "safe to retry".
- **`readback_matches` is attempt-scoped, not "already-exists".** It takes a
  **required** `pre_attempt_gids` set (the GIDs the caller observed for that
  name+context *before* the attempt) with no default: without that snapshot the
  readback cannot tell "my create succeeded" from "a distinct object of this
  name already existed", so the type system forbids omitting it. A GID is
  adopted only when its name matches, its workspace/project context equals
  `intent.context_gid`, **and** it is not in `pre_attempt_gids` — i.e. it
  appeared *after* the attempt. This closes the footgun of adopting a
  genuinely-distinct pre-existing same-name object (task names like "Deploy" are
  routinely reused) and silently dropping the intended create. It remains
  best-effort (it can still miss an un-propagated create), never exactly-once.
- **Batch partial failure is first-class.** Asana's MCP `create_tasks` /
  `update_tasks` batch up to **50** items per call — an **Asana-documented**
  count, not a schema. `parse_batch_result` preserves per-item
  `BatchItemOutcome` rows so a caller can retry only the failed subset (and only
  via readback). The batch **response row shape** it reads (per-row `gid` vs
  `status`/`errors`, a synthetic-400 fallback) is **provisional pending a live
  `tools/list` observation** — it is the connector's best current model of the
  MCP batch envelope, not a confirmed schema, and will be reconciled when the
  real envelope is observed at wiring time.

## Authorization surfaces (`auth.py`)

Two non-interchangeable surfaces, modeled as distinct concepts:

- **MCP** — Asana's V2 Streamable HTTP MCP server. No OAuth scopes: the only
  accepted scope value is `default` (a real scope is refused); bound to a single
  workspace at consent time; does **not** support dynamic client registration
  (a fixed vendor fact); its token does **not** work against REST.
- **NATIVE_REST** — a standard OAuth app using fine-grained `resource:action`
  scopes; **requires** at least one scope; not workspace-pinned; its token does
  **not** work against MCP.

`SurfaceToken.__post_init__` normalizes and validates **scopes per surface** (an
MCP token's scopes collapse to `default`; a scope-less REST token is refused),
so an invalid MCP scope or a scope-less REST token is a shaping fault, not a
silent pass. `assert_token_usable_on` refuses a token used against the surface
it was not minted for (`AuthSurfaceMismatch`) — the load-bearing "tokens are not
interchangeable" guard.

## Testing

`test/test_connectors_asana_*.py` cover each vendor rule with a negative path:
GID opacity; `due_on`/`due_at` write-path mutual exclusion and read-path
acceptance of Asana's both-populated response; illegal `limit` and numeric
offset; the expirable-cursor signal; the legacy truncation ceiling; explicit
cross-workspace **raise** (403/404 recorded but not asserted); attempt-scoped
readback (a pre-existing same-name object is not adopted, a newly-appeared one
is, and the pre-attempt snapshot is a required argument); batch partial failure
preserved per item; and per-surface scope validation. Every test is pure
data/logic with no side effects.
