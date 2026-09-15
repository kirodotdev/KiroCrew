# Microsoft Graph runtime base

The shared, network-free logic every Microsoft Graph-backed connector reuses:
how a request path is built from typed intent, how a request/response payload is
shaped, and how each of Graph's *actual* pagination variants is driven. This is
the `W05` runtime base in the connector campaign's DAG
([connector-capability-manifest](connector-capability-manifest.md)); the
SharePoint/Outlook (`W06`) and OneDrive/OneNote/Teams/Excel (`W07`) streams build
on it rather than re-deriving these conventions.

The code lives under `src/kiro_crew/connections/vendors/microsoft/graph/` and is
pure logic: no network call, no credential, no token cache. It sits inside
`kiro_crew.connections` (the shipped account-link subsystem —
[connections](connections.md)) as that subsystem's per-vendor Graph source, and
is distinct from `kiro_crew.teams`, which is the Bot Framework channel transport,
**not** the Graph Teams connector. The shared control-plane container at the
`vendors/` layer is owned by the connector campaign's `W01` stream; this base
depends on it by name and does not define or re-export any of its surface.

## What this base does NOT own

This base defines **no vendor-error taxonomy and no error envelope.** The
RUN-01 closed error set (`OperationError` / `ErrorClass`) and its typed envelope
are `W01`'s single source in `kiro_crew.connections.control_plane`; the mapping
from a Graph vendor error to that typed envelope is a separate campaign leaf that
consumes `W01`'s prepared boundary. This base consumes W01's shared vocabulary
where it needs one — the `CredentialMode` axis the locator reads — and defines
none of its own. The shaping types here raise plain `ValueError` subclasses
(`GraphLocatorError`, `GraphPayloadError`, `PagingError`) for a *shaping* fault —
a forbidden principal/credential combination, a malformed success envelope, a
paging precondition violation — never to classify a vendor error. A non-2xx Graph
response never reaches these types; the client layer routes it to `W01`'s
boundary first. Do not grow an error hierarchy in this tree.

## Resource locator

`locator.py` turns a typed `GraphLocator` into a root-relative Graph path via
`build_path`. Two axes are modelled separately and never collapsed:

- **The principal.** `Principal.ME` names the signed-in user; `Principal.USER`
  names an explicit directory object by id. A `USER` locator validates its
  `user_id` eagerly in `GraphLocator.__post_init__`, so it can never carry an
  empty id that would only surface when the path is assembled.
- **The credential mode**, supplied per dispatch to `build_path` as W01's
  shared `CredentialMode`
  (`kiro_crew.connections.control_plane.operation.CredentialMode`:
  `oauth_user` / `fine_grained_pat` / `service_to_service`). This base defines no
  second auth-mode vocabulary; `is_app_only` is its one Graph-specific reading of
  that shared axis.

**The load-bearing constraint: `build_path` refuses `/me` under an app-only
credential.** An app-only credential (`service_to_service`) has no signed-in user
for `/me` to resolve, so Microsoft Graph rejects it outright; a user-backed
credential (`oauth_user` or `fine_grained_pat`) resolves `/me`. `_principal_root`
raises `GraphLocatorError` for the app-only `/me` combination — enforced at
shaping time rather than discovered as a vendor 400. A top-level resource
(SharePoint, chats) is not principal-rooted, so it never touches the principal
and the refusal cannot apply to it; this is proven by
`test_graph_locator.py::TestTopLevelShapesIgnorePrincipalAndCredentialMode`.

`ResourceRef`'s classmethod constructors spell the four supported resource
shapes, and `ResourceRef.__post_init__` makes direct construction safe as well:
every id segment is validated (`_require_id` rejects an empty segment or one
carrying a path separator that would inject unmodelled structure), and an empty
segment tuple is rejected. The supported constructors are:

- `ResourceRef.sharepoint_list_items` — `/sites/{id}/lists/{id}/items[/{item}[/fields]]`.
  `fields` without an `item_id` is refused (Graph has no `/items/fields`
  collection).
- `ResourceRef.drive_item` — `/drives/{id}/items/{id}[/children | /workbook/...]`.
  `children` and a `workbook_tail` are mutually exclusive; a `workbook_tail` must
  begin with `workbook`.
- `ResourceRef.onenote` — `/{principal}/onenote/...`, principal-rooted.
- `ResourceRef.chat_messages` — `/chats/{id}/messages`, top-level.

The path `build_path` returns is root-relative (`/users/{id}/onenote/notebooks`);
it carries no API-version prefix (`/v1.0`), which the client layer prepends.

## Payload shaping

`payload.py` shapes both directions.

- `QuerySpec.to_query_params` serializes typed OData query intent
  (`$select`/`$filter`/`$top`/`$expand`/`$orderby`/`$search`/`$count`) into Graph's
  `$`-prefixed param map, in deterministic (option-name) order so a shaped request
  hashes stably for a later evidence receipt. `$top` must be a positive integer.
- `CalendarViewWindow` carries the **mandatory** `startDateTime`/`endDateTime`
  window a `calendarView` request needs. Graph expands recurring events across a
  range, so the window is required, not optional — omitting it is a vendor 400.
  This is enforced *structurally*: the window is a required constructor argument,
  so `shape_request` cannot assemble a calendarView request without one. A caller
  cannot forget a param that is not a param.
- `parse_collection` reads a success envelope into a `GraphPage`, surfacing the
  raw `@odata.nextLink` / `@odata.deltaLink` / `@odata.count` annotations. It
  raises `GraphPayloadError` for a structurally malformed success body — a
  missing or non-array `value`, or a page carrying **both** a `nextLink` and a
  `deltaLink` (contradictory paging state Graph never emits). It never interprets
  a vendor `error` object.

No `ResourceRef` constructor spells a `.../calendarView` path, so this base ships
no typed `calendarView` locator shape; that shape is added by the consuming stream
(`W06` Outlook). Until then, `shape_request`'s path-sniff (a final `calendarView`
segment requires the window) together with `paging.check_first_page_preconditions`
enforces the mandatory start/end window regardless of how the caller produces the
path, so the guarantee holds.

## Pagination / cursor protocol

`paging.py` models Graph's pagination as a closed `PagingMode` enum, **not** a
generic "follow `@odata.nextLink` until absent" loop — because that loop is wrong
for most of the catalog. Each value corresponds to an actual evidence variant:

| Mode | Contract |
|---|---|
| `NEXT_LINK` | Plain `@odata.nextLink`; follow the opaque link verbatim until absent. |
| `TOP_SERVER_DRIVEN` | `$top` is a first-request page-size hint; the continuation is still a `nextLink`, and `$top` is **never** re-appended to it (the link already encodes the page size). |
| `NEXT_LINK_THEN_DELTA` | Pages via `nextLink`, **terminates on** `@odata.deltaLink`; the delta link is the cursor for the next change-tracking round, surfaced, not discarded. This is a delta-terminated mode. |
| `CALENDAR_VIEW` | Pages via `nextLink`, but the mandatory start/end window is a precondition of paging at all. |
| `FILTER_REQUIRED` | The collection rejects a first request with no `$filter`. |
| `DELTA_RESYNC` | Follows the same successful-page termination contract and additionally supports an expired delta cursor; see the 410 handling below. This remains distinct from `NEXT_LINK_THEN_DELTA`. |
| `NONE` | The operation does not paginate. |
| `UNKNOWN` | Paging genuinely unknown from the evidence. |

The W05 evidence catalog contains no operation that combines a first-page
precondition (`CALENDAR_VIEW` or `FILTER_REQUIRED`) with delta termination
(`NEXT_LINK_THEN_DELTA` or `DELTA_RESYNC`), so the two guard sets are over
mutually exclusive modes by design; if a future operation composes both, the
model gains a precondition flag then as a W06+ concern.

`NEXT_LINK_THEN_DELTA` and `DELTA_RESYNC` both page via `nextLink` and terminate
on `deltaLink` identically in this pure-logic base; they differ only in declared
intent at the manifest layer: `DELTA_RESYNC` marks a collection whose delta
cursor is expected to expire (a 410 resync is anticipated and handled by the
mode-ungated `parse_resync`), while `NEXT_LINK_THEN_DELTA` does not declare that
expectation. Their behavior intentionally converges here; a later slice may
differentiate it.

**`NONE` and `UNKNOWN` both refuse to page, for different reasons a caller may
need to distinguish.** A large share of the catalog's operations are pagination
`n/a`; `assert_pageable` refuses `NONE` so the paging engine is never applied to
them by default. Exactly one collection in the evidence shows no explicit
`nextLink` in its fetched examples; its mode is preserved as `UNKNOWN` verbatim —
the engine does not assume it paginates and does not fabricate a `nextLink` loop
for it — and `assert_pageable` refuses it with a *distinct* reason from `NONE`
(a positive "does not paginate" versus "unresolved evidence"). This distinction
is pinned by
`test_graph_paging.py::TestPageabilityGate::test_unknown_mode_is_not_pageable_and_distinct_from_none`.

`check_first_page_preconditions` refuses a first-page request that omits a
mandatory precondition — `startDateTime`/`endDateTime` for `CALENDAR_VIEW`,
`$filter` for `FILTER_REQUIRED` — before any request would be dispatched.

`next_step` decides the next action from a fetched `GraphPage`: continue on a
present `nextLink` (returned verbatim as the cursor), else complete — carrying the
`deltaLink` as a resumable delta cursor when one is present. For either member of
`DELTA_TERMINATED_MODES` (`NEXT_LINK_THEN_DELTA` and `DELTA_RESYNC`), that terminal
`deltaLink` is required; completing without it would silently lose the
change-tracking cursor. Non-delta modes may complete with neither link. The opaque
link is the cursor; the caller re-sends it unchanged and never rebuilds query
params.

**410 Gone is a delta-resync signal, not an error.** A resync applies to **both**
delta-terminated modes (`NEXT_LINK_THEN_DELTA` and `DELTA_RESYNC`); the modes stay
distinct, but labeling must never route a 410 away from `parse_resync`, which is
intentionally mode-ungated. When a delta cursor expires, Graph answers `HTTP 410
Gone` carrying `resyncChangesApplyDifferences` or
`resyncChangesUploadDifferences` and a fresh link in the `Location` header. The
correct response is to re-enumerate the whole collection from that fresh link.
`parse_resync` recognizes this — returning a `ResyncSignal` whose `fresh_link` is
the restart cursor to re-enumerate the whole collection from — and only this. It returns
`None` for any non-410 status (those are `W01`'s boundary) and for a 410 with no
resync annotation (a plain gone, also `W01`'s boundary); it raises `PagingError`
only for a 410 that claims a resync but is missing its `Location` link, which must
not be silently treated as terminal. Header lookup is case-insensitive.

## Governance policy fields on a manifest entry

A manifest entry's `policy` object is owned by
[connector-capability-manifest](connector-capability-manifest.md) (its `policy`
row); this base adds no rules for it. This slice ships no manifest entry, so it
declares no `policy` value here.
