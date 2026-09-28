# GitHub connector: instance data and wire parsing (W02)

The GitHub provider stream's first slice: the per-operation **instance data**
for every operation the campaign tracks on GitHub, plus the GitHub-specific
**wire parsing** that reads GitHub's own API surface. This is the owning doc for
`src/kiro_crew/connections/vendors/github/`.

Scope note: this slice is data and GitHub-specific parsing only. It builds no
request, holds no credential, and performs no I/O. Real authorization and
transport are a later stream, gated on the shared control plane's landed
interfaces; a test double is never a production-ready claim. What this slice
consumes from single sources it does not own:

- the shared control plane's `Effect` vocabulary and its RUN-01 `ErrorClass`
  taxonomy, from `kiro_crew.connections.control_plane` (the seam this stream
  stacks on); the manifest-entry field schema, from
  [connector-capability-manifest.md](connector-capability-manifest.md);
- the governance scope catalog, from [governance.md](governance.md) — this
  stream registers **no** new scope.

## What this stream owns, and what it consumes

| Owns (here) | Consumes (elsewhere, unchanged) |
|---|---|
| Per-operation GitHub instance data (endpoint, method, effect, auth modes, scopes, account types, surfaces, tool names, pagination contract, idempotency class, five-layer policy) | The manifest-entry schema those fields mirror |
| Reading GitHub's two pagination contracts, its rate-limit signals, and its status/body | The neutral error classes a failure maps onto; the retry/backoff strategy |
| Mapping a GitHub status/body onto a neutral error class; a capability signature onto an operation_id | The governance `SCOPE_CATALOG` a policy value must be a member of; the control plane's `Effect` and `ErrorClass` closed sets |

The stream does **not** define the error/result/context envelope, the policy
validator, the three-axis totality check, a generic backoff, or any second
auth/governance/runtime. Those are the shared control plane's, consumed here.
It stacks on the control-plane seam and imports its `Effect` and `ErrorClass`
types rather than restating either vocabulary.

## Operation descriptors

`descriptors.py` carries one `GithubOperationDescriptor` per operation, indexed
by `operation_id` in `DESCRIPTORS`. The field set mirrors the manifest-entry
schema; the values are the GitHub facts for that operation. It is named
`Github...` to stay distinct from the control plane's own minimal dispatch
`OperationDescriptor`, and its `effect` field is the control plane's `Effect`
type, not a local copy.

Two invariants are enforced at import time, fail-closed, the same shape the
governance engine uses for an unknown matcher:

- **Every `policy` value is `None` or an exact-string member of the live
  `kiro_crew.platform.governance.SCOPE_CATALOG`.** `None` means that layer
  imposes no additional scope; a non-member fails the import. This stream
  registers no scope.
- **A `read` effect carries the `NONE_READ` idempotency class, and a mutating
  effect never does.** The idempotency class and pagination contract are drawn
  from closed enums, so a typo cannot invent a class the conformance runner
  cannot switch on.

### The five-layer policy mapping

Each operation's `policy` names existing catalog scopes only:

| Layer | Value | Why |
|---|---|---|
| `platform_scope` | `network.egress` | Every operation egresses to GitHub's API host. |
| `workspace_scope` | `null` | A connector operation touches no local filesystem or folder. |
| `session_scope` | `approval_mode` for a mutating effect (write/delete/admin), else `null` | Per-effect governance on an existing ordinal scope, not a new one. |
| `connection_scope` | `null` | No connection-binding scope exists in the catalog; `null` is the legal, honest encoding. |
| `provider_scope` | `mcp`, or `null` for the non-MCP rows | An operation that dispatches through a github-mcp-server tool is gated by the `mcp` scope; the raw legacy branch-protection REST endpoints and the meta rate-limit endpoint are not wrapped by a distinct MCP tool, so they carry `null` rather than a false `mcp` claim their own notes contradict. |

Every `auth_modes` value is a member of the control plane's `CREDENTIAL_MODES`
closed set (`oauth_user` / `fine_grained_pat` / `service_to_service`), pinned at
import by `_validate` — a GitHub App installation token is the
`service_to_service` credential class, named with the shared vocabulary rather
than a vendor-specific string.

### Idempotency classes

Each mutating operation names the idempotency class it actually has, from the
manifest's closed vocabulary — never a generic exactly-once claim it lacks:

- `base_sha_guard` — an optimistic-concurrency base/head SHA (`create_or_update_file`, `merge_pull_request`, `update_pull_request_branch`).
- `external_id_upsert` — a natural duplicate guard: the vendor rejects a duplicate name/head-base or returns the existing resource (`create_repository`, `fork_repository`, `create_branch`, `create_pull_request`).
- `none_verify_by_readback` — no idempotency param; recover by re-reading current state.
- `none_read` — the read-only sentinel; reads carry no write-idempotency guarantee.

**Documented contradictions, carried as facts.** `create_or_update_file`
exposes an explicit base-SHA concurrency guard, but `push_files` and
`delete_file` document no equivalent expected-head-sha param — so they are
`none_verify_by_readback`, not `base_sha_guard`. The descriptor table records
this asymmetry rather than pretending the guard exists.

## Wire parsing

### Pagination (`pagination.py`)

GitHub carries two pagination contracts, and a single-method operation uses
exactly one:

- **REST `page`/`perPage`** with a `Link` response header. `parse_link_header`
  reads the `rel="next"` URL (tolerating a missing header on the last page and
  skipping a malformed entry rather than raising). `perPage` is clamped to
  GitHub's ceiling of 100 before the request, so the client's record of what it
  asked for matches what it gets.
- **A cursor `after`** for the GraphQL-backed tools (`list_issues`,
  `list_dependabot_alerts`, and `pull_request_read`'s `get_review_comments`).
  The cursor param name is kept **distinct** from the native REST param names:
  a cursor request never emits `page`. Collapsing the two into one "page token"
  field would lose the information that they advance differently — the
  contradiction the campaign evidence recorded.

`pull_request_read` is one operation spanning nine methods that do **not** share
one pagination contract (most page by `page`/`perPage`; `get_review_comments`
advances by cursor). The campaign evidence models it as a single operation with
a compound pagination fact, so its descriptor carries `Pagination.MIXED` rather
than a single scalar that would falsely claim one contract for every method.

### Rate limits (`rate_limit.py`)

`read_rate_limit` reads GitHub's `x-ratelimit-*` primary-limit headers, the
`retry-after` header, and the secondary-limit body phrase into a neutral
`RateLimitSnapshot`. The secondary limit is only observable once a request is
rejected — there is no proactive budget to inspect — so the reader treats a
`retry-after` header or a secondary-limit body phrase as the signal. This module
reports that a backoff is warranted; it does not perform the backoff (the
control plane owns that).

### Error mapping (`errors.py`)

`classify_github_failure` maps a GitHub HTTP status + body onto the control
plane's `ErrorClass` (the RUN-01 twelve-value closed set). GitHub-specific
readings it encodes:

- **404 is `not_found` for both a hidden private resource and a truly missing
  one** — indistinguishable on the wire by GitHub's own design, so the mapping
  does not fabricate a distinction.
- **403/429 with a rate-limit signal is `throttle`; a bare 403 is `forbidden`.**
  A secondary-limit signal (a `retry-after` header or the body phrase) and a
  primary-limit signal (`x-ratelimit-remaining: 0`) both route to `throttle`.
- **409 is `conflict`** (stale base/head SHA), **405 is `conflict`** (a
  protected-branch merge refusal — a required check or approval unmet, the head
  moved), **422 is `input`** (bad base branch, malformed ref, duplicate name),
  **401 is `auth`** (invalid/expired/revoked credential — distinct from a 403
  permission failure).

The class type and its closed set are imported from the control plane; this
stream classifies GitHub failures into that vocabulary and does not fork a
second taxonomy or define its own error class.

### Capability signatures (`signatures.py`)

`resolve_operation_id` maps a discovery capability signature (a tool name, or a
`tool:method` form for the multi-method tools) back to an `operation_id`. An
ambiguous bare tool name or an unknown signature resolves to `None` — a
discovery gap for a human to register, never an auto-write into the manifest.

## Testing

`test/test_connector_github_*.py` cover the descriptor table integrity
(including the documented contradictions), the error mapping on
vendor-documented failure shapes (404 hidden-private vs missing, 409 stale SHA,
422 bad base, 403 permission vs secondary/primary limit, 401 revoked), the
pagination parsing (malformed `Link` header, `perPage` over the ceiling, cursor
vs REST param separation), the rate-limit reading, and the signature
resolution. Every test is pure data/logic with no side effects.
