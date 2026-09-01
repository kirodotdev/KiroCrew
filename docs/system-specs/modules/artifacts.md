# Artifacts Module

## Overview

Artifacts give chat-rendered LLM-generated UI a persistent identity, version
history, and a stable handle the agent can iterate on across sessions.

A typical flow:

1. Agent emits an `<mcwidget>` in chat ("here's your CR queue")
2. Agent (or user) calls `artifact_save` — the widget is persisted under
   `~/.kiro/crew/artifacts/<slug>/current.html`
3. Days later, in a fresh session, the user says "iterate on the cr-queue
   artifact and add an age column"
4. Agent calls `artifact_get("cr-queue")` to read the current HTML, modifies
   it, then `artifact_update("cr-queue", content=…)` to publish a new version
5. The previous version is preserved under `versions/v1.html` for rollback

The dashboard provides a `/artifacts` library page for browse/search and a
`/artifacts/<slug>` standalone view with a version dropdown.

## Storage Layout

```
~/.kiro/crew/artifacts/
└── <slug>/
    ├── meta.json        canonical metadata (no content)
    ├── current.html     latest content
    └── versions/
        ├── v1.html
        ├── v2.html
        └── …
```

`meta.json` schema:

| Field | Type | Notes |
|---|---|---|
| `slug` | string | URL-safe handle. Derived from `name` when not given, resolving a collision by suffixing (`-2`, `-3`, …); an explicitly-passed slug is refused — never renamed — when it is already taken or malformed |
| `name` | string | Human-readable display name |
| `kind` | enum | `widget`, `html`, `markdown`, `svg`, `json`, `text`, `webapp`, `image` — inferred on save when the caller omits it (see [Kind inference](#kind-inference)) |
| `source` | enum | `chat` (default), `cron`, `subagent`, `manual`, `import` |
| `pinned` | bool | "Starred" — user-curated keep flag (default `false`). Drives the Artifacts page **Starred** view. Metadata-only; toggling does NOT bump `version`. |
| `auto_registered` | bool | `true` when the store created this record automatically from a chat-emitted `<mcwidget>` (see [Widget auto-registration](#widget-auto-registration)) rather than from an explicit save. Sweepable by the retention pass while unpinned; tolerant-loaded (pre-existing artifacts default `false`, so they are never swept). |
| `description` | string | Optional, ≤ 2,000 chars |
| `tags` | string[] | ≤ 16 tags, alphanumeric / `_`, `:`, `.`, `-` |
| `version` | int | Latest version number; bumps on every content change |
| `created_at` / `updated_at` | string | ISO 8601 UTC microseconds |

## Public API

### Python (`kiro_crew.artifacts`)

```python
from kiro_crew.artifacts import ArtifactStore, get_default_store

store = get_default_store()
art = store.create(name="CR Queue", content="<table>…</table>", tags=["ops"])
art = store.get(art.slug)
art = store.update(art.slug, content="<table>… age column …</table>")
versions = store.list_versions(art.slug)
items = store.list(tag="ops")
store.delete(art.slug)

# Reconcile a provider's authoritative comments into the local mirror
# (fetch-on-view). Returns the merged list; leaves origin=="local" untouched.
store.merge_remote_comments(art.slug, "artifactory", remote_comments)
```

The store is thread-safe. A module-level singleton is available via
`get_default_store()`; pass an explicit `root` to `ArtifactStore(root=...)`
for isolated test instances.

### Kind inference

`store.create()` (and every path that funnels through it — the HTTP create
route, the `artifact_save` MCP tool, the `kirocrew artifact save` CLI) infers
`kind` when the caller omits it (`kind=None`), via `_infer_kind(content,
source_path, explicit)`:

1. **Explicit wins** — a non-empty `kind` argument is used as-is (back-compat).
2. **Extension** — for file-backed artifacts (`source_path` set): `.md` /
   `.markdown` → `markdown`, `.html` / `.htm` → `html`, `.svg` → `svg`,
   `.json` → `json`, `.txt` → `text`, any other extension → `text`.
3. **Content sniff** — for inline content with no `source_path`: HTML-ish
   markup (`<div`, `<span`, `<style`, `<table`, `<mcwidget`, `<html`,
   `<!doctype html`) → `widget`; a leading markdown heading (`#`…`######`) or
   content with **no** `<` at all → `markdown`; otherwise the legacy `widget`
   default (ambiguous blobs keep prior behavior).

Only `widget` and `markdown` are inferred from inline content; the richer
kinds need the extension signal. This is the safety prerequisite that lets
agents save markdown deliverables without the mis-save footgun (a markdown
doc stored as `widget` renders as raw inner HTML).

### MCP tools (`@kirocrew-core/*`)

| Tool | Purpose |
|---|---|
| `artifact_save` | Create a new artifact, returns slug; optional `folder` (id or `/`-separated human path, mkdir -p) files it in one call |
| `artifact_get` | Read content + metadata (optionally a specific version) |
| `artifact_update` | Modify content/name/description/tags; bumps version on content change |
| `artifact_list` | List artifacts (filter by `tag`, `kind`, name `q`) |
| `artifact_versions` | List version numbers for a slug |
| `artifact_delete` | Permanent delete (artifact + all versions) |
| `artifact_folder_list` | List the folder tree (id, name, parent_id, path, item_count) |
| `artifact_folder_create` | Create a folder; `parent` = id or path (mkdir -p) |
| `artifact_folder_rename` | Rename a folder (id or path) |
| `artifact_folder_move` | Reparent a folder; cycle-guarded |
| `artifact_folder_delete` | Delete a folder; default keeps contents (re-parent), `delete_contents=true` cascades |
| `artifact_move` | Move an artifact into a folder / unfile it (metadata-only, no version bump) |
| `artifact_get_comments` | Read all comments on an artifact (local + provider-synced) |
| `artifact_post_comment` | Post a comment; agent comments carry the structured `is_agent` flag (no emoji stamped into the body — dashboard renders a lucide `Bot` icon, CLI prefixes a plain-text `[agent]` marker) + SEL-audited; `scope='shared'` syncs to the provider |
| `artifact_mark_review` | Advance a comment thread to REVIEW status (agent can mark_review but NEVER resolve) |
| `artifact_delete_comment` | Delete a fully-applied comment thread (root cascades to replies); provider-synced comments refused; SEL-audited with a `reason` |

Schemas live in `validation.py` (`ARTIFACT_*_SCHEMA`) and are registered in
`MCP_CORE_SCHEMAS`. The MCP tool layer always proxies through the HTTP API so
SEL audit, restricted-session enforcement, and any future authorization
middleware live in one place.

### CLI (`kirocrew artifact`)

```
kirocrew artifact list [--tag T] [--kind K] [--q SUBSTR]
kirocrew artifact show <slug> [--version N] [--meta]
kirocrew artifact save --name N [--slug S] [--kind K] [--content C | --content-file F] [--tags A,B] [--description D]
kirocrew artifact update <slug> [--content C | --content-file F] [--name N] [--description D] [--tags A,B]
kirocrew artifact versions <slug>
kirocrew artifact delete <slug>
```

The CLI proxies through the gateway HTTP API (matches `kirocrew learn`).

### HTTP

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/artifacts` | `?tag&kind&q` filters + `?folder=` scoping (absent = all; empty = unfiled/root; id = that folder) + `?session=` scoping (same absent/empty distinction; validated like `origin_session_key`) + `?pinned=` (tri-state — unrecognized values don't scope); returns `{artifacts: […]}` |
| `POST` | `/api/artifacts` | JSON body — creates, returns full artifact + content; optional `folder` key (id or human path, mkdir -p) |
| `GET` | `/api/artifacts/{slug}` | Returns full artifact + content |
| `PATCH` | `/api/artifacts/{slug}` | Partial update; `content` bumps version; optional `folder` key (metadata-only) |
| `DELETE` | `/api/artifacts/{slug}` | Permanent delete |
| `PATCH` | `/api/artifacts/{slug}/pin` | Star/unstar — body `{pinned: bool}` (strictly boolean; non-booleans rejected). Metadata-only, no version bump |
| `GET` | `/api/artifacts/session-docs` | Virtual, read-only list of non-code documents produced across chat sessions (the "All" firehose). `?session=<slot>` scopes to one session. Creates nothing; each entry carries `saved` (pinned) + `slug`. Registered before the `/{slug}` dynamic route |
| `POST` | `/api/artifacts/materialize` | Turn a recorded chat document into a real, pinned file-backed artifact — body `{path}`. The path MUST be a document recorded in chat `file_changes` (authorization allowlist); the read goes through `hooks.safe_read_file_bytes` (is_sensitive_path + `O_NOFOLLOW` + `MAX_FILE_BYTES` cap). Idempotent by `source_path` |
| `GET` | `/api/artifacts/{slug}/versions` | `{slug, versions: [int]}` |
| `GET` | `/api/artifacts/{slug}/versions/{n}` | Specific version content |
| `GET` | `/api/artifact-folders` | Folder tree with `item_count` + breadcrumb `path` |
| `POST` | `/api/artifact-folders` | Create folder `{name, parent?\|parent_id?, color?}`; spawns background emoji-icon task |
| `PATCH` | `/api/artifact-folders/{id}` | Rename / reparent / reorder / icon / color |
| `DELETE` | `/api/artifact-folders/{id}` | `?delete_contents=` picks keep (re-parent, default) vs cascade (delete subtree incl. artifacts) |
| `PATCH` | `/api/artifacts/{slug}/folder` | Move an artifact into a folder (`{folder}` id/path or `{folder_id}` id-only) |
| `POST` | `/api/artifacts/{slug}/pull-latest` | Pull the tracked upstream (`?source=publication\|origin\|auto`) into a NEW local snapshot via `publish_sync.pull_upstream`; ungated ingress |
| `GET` | `/api/artifacts/{slug}/upstream-status` | Cheap metadata-only drift check (`publish_sync.upstream_status`); best-effort, never blocks on the network |
| `POST` | `/api/artifacts/{slug}/overwrite-remote` | Force-push local content over an upstream-ahead remote (`publish_sync.overwrite_upstream`); **egress — gated by `_publish_governance_denied` on the resolved `publication.provider`** |
| `GET` | `/api/remote-artifacts/{provider}/browse` | Provider-routed discovery: `?q=` → `search_remote`, else `list_remote(?scope=mine\|shared\|public)`; rows annotated with `local_slug`; unregistered provider → 503 (matches clone/fork) |
| `POST` | `/api/remote-artifacts/{provider}/clone` | Bidirectional clone (`publish_sync.clone_from_remote`, sets `auto_sync=True` → arms future pushes); **gated by `_publish_governance_denied` on the routed provider**; empty registry → 503. Body: `{ "external_id": ... }` (provider-native ids can contain `/`, which a path segment can't carry) |
| `POST` | `/api/remote-artifacts/{provider}/fork` | Independent copy with pull-only `fork_metadata` lineage (`publish_sync.fork_from_remote`); ungated ingress; empty registry → 503. Body: `{ "external_id": ... }` |
| `GET` | `/api/remote-artifacts/{provider}/{external_id}` | Read-only detail fetch (metadata + content) for a provider-hosted artifact the user has no local copy of — content source for the remote-detail viewer; ungated ingress; passes `_redact_remote_response`; empty registry → 503 |
| `GET` | `/api/remote-artifacts/{provider}/{external_id}/comments` | List comments on a provider-hosted artifact (`fetch_comments`, `COMMENTS_READ`); TTL-cached in memory; provider failure surfaces as `remote_sync_error`, not a 500; ungated ingress; anchor/body redacted per comment |
| `POST` | `/api/remote-artifacts/{provider}/{external_id}/comments` | Post a top-level comment straight through to the provider (`post_comment`, `COMMENTS_WRITE`, scope=shared); **egress — gated by `_publish_governance_denied` on the routed provider** |
| `POST` | `/api/remote-artifacts/{provider}/{external_id}/comments/{comment_id}/reply` | Reply to a provider thread (`reply_comment`); **egress — gated by `_publish_governance_denied`** |
| `POST` | `/api/remote-artifacts/{provider}/{external_id}/comments/{comment_id}/review` | Advance a provider thread to REVIEW (`mark_review`); **egress — gated by `_publish_governance_denied`** |
| `DELETE` | `/api/remote-artifacts/{provider}/{external_id}/comments/{comment_id}` | Delete a provider comment (`delete_comment`); **egress — gated by `_publish_governance_denied`** |

`external_id` (and `comment_id`) travel as percent-encoded path segments on
these routes; aiohttp's `path_safe` matching (3.9.2+) preserves `%2F`, so a
provider-native id containing `/` round-trips correctly (browse-listing ids are
slash-free in practice). Clone/fork keep the id in the JSON body instead.

POST/PATCH/DELETE require an unrestricted session. The HTTP body envelope is
capped at 2 MiB; the store enforces a per-content cap of 25 MiB
(`artifacts.MAX_CONTENT_BYTES`), large enough for cloned/pulled rich artifacts
(HTML reports, CSVs). The MCP save/update field cap
(`validation.ARTIFACT_CONTENT_MAX`) imports that same constant so the tool and
store paths never disagree.

**Folders:** `Artifact.folder_id` (`""` = unfiled) is an opaque,
rename-safe membership id, tolerant-loaded for legacy meta.json.
`ArtifactStore.set_folder()` is a metadata-only move (NO version bump);
`list(folder=)` filters (None = all, `""` = unfiled, id = that folder).
`ArtifactFolderStore` keeps a flat `parent_id` tree in
`~/.kiro/crew/artifact_folders.json` — create/rename/reparent (cycle- and
depth-guarded, `MAX_FOLDER_DEPTH` 20)/reorder/delete, breadcrumb, item counts,
and id-or-path resolution with mkdir -p semantics (`resolve_path`, all-or-nothing
rollback). Folder delete is an explicit choice: keep (re-parent direct children
to the parent) vs cascade (permanently delete the whole subtree, incl.
descendant artifacts) — never silent.

**Auth note (fork adaptation):** `"/api/artifact-folders"` is registered in
`token_auth`'s `mixed_internal_paths` in `server.py` — the 5 folder MCP tools
authenticate via `X-Internal-Secret`, and the prefix matcher
(`path == p or path.startswith(p + "/")`) does NOT cover the hyphenated path
via the `"/api/artifacts"` entry. Guarded by a regression test in
`test_artifact_folder_handlers.py`. `"/api/remote-artifacts"` is registered the
same way (same non-coverage reason; the prefix covers every
`/api/remote-artifacts/{provider}/...` sub-route) so `--slack-only` auth stays
at parity with the dashboard — guarded in `test_remote_artifacts.py`.

**Remote artifacts (provider-routed browse / clone / fork — G4).** The
`/api/remote-artifacts/{provider}/...` trio + the upstream sync trio
(`pull-latest` / `upstream-status` / `overwrite-remote`) wire `publish_sync`'s
provider-agnostic orchestration (`pull_upstream` / `clone_from_remote` /
`fork_from_remote` / `upstream_status` / `overwrite_upstream`) to HTTP. The
surface is **inert in the public edition**: the provider registry is empty, so
`get_provider()` raises `PublishUnavailableError` → browse / clone / fork all
503, and the frontend gates the entire remote section + `UpstreamSyncBanner` on
a non-empty `GET /api/artifacts/publish-providers` result (zero remote pixels /
requests with no provider). A companion registers providers via the CPP publish
seam. The picker includes a provider whenever `available() or installable()`
(`PublishProvider.installable()` defaults `False`; a companion provider whose
`ensure_ready()` self-installs on first publish overrides it to `True`), and
each row carries an `available` flag so the FE can hint install-on-first-use for
a not-yet-installed but installable destination. Governance: `publish_sync` has NO internal gate and `push_version` is
ungated, so the two egress-arming routes go through
`_publish_governance_denied` (fail-closed `capabilities.publish ∩
destinations:<provider>`, a module-local alias for the shared
`publish_governance.publish_denied_reason` — the same decision the public-web
deploy path uses, see `governance.md`) BEFORE dispatch — `overwrite-remote` on the resolved
`publication.provider`, and `clone` on the routed provider (a clone sets
`auto_sync=True`, arming every future snapshot push). The four remote comment
WRITE routes (post / reply / review / delete) are outbound egress too, so each
goes through the same `_publish_governance_denied` gate BEFORE the provider call
— a denial is an audited 403 and no bytes leave the box (there is no local
mirror to fall back to, unlike the local shared-comment path). Fork and the
read-only routes (browse, upstream-status, pull-latest, remote-artifact detail,
remote comment list) stay ungated ingress. All remote
payloads pass `_redact_remote_response` (recursive credential/exfil-URL
redaction, depth-capped, `localPath` stripped). Browse rows are annotated with
`local_slug` BEFORE redaction (so a credential-shaped `external_id` isn't
rewritten out of the local-match lookup) using a single off-loop
`ArtifactStore.index_by_artifact_id` scan (not a per-row `find_by_artifact_id`
scan on the event loop) so the UI dedups already-local copies. Browse is
paginated: the response carries the provider's `next_page_token`, the client
forwards it as `?pageToken=`, and `RemoteBrowseSection` drives a
`useInfiniteQuery` with a "Load more" control — so remote artifacts past the
provider's first page are reachable rather than silently truncated.

### Dashboard pages

- `/artifacts` — list page (name / kind / tags / updated_at), tag filter,
  name substring search, click-through to detail
- `/artifacts/<slug>` — full-screen render of the current artifact in a
  sandboxed iframe (same security model as inline `<mcwidget>`), with a
  version dropdown

### Publish panel (`PublishHub`) — reading a publish outcome

`PublishHub` posts to the row's declared `endpoint` and must recognize **two**
response shapes, because two different routes answer that POST:

- `{url}` / `{public_url}` — the deploy shape (`POST /api/deploy/deploy`).
- a serialized artifact carrying a `publication` block — what `POST
  /api/artifacts/{slug}/publish` returns, which is where an app provider lands
  when it hands the confirmed publish to the core route (the supported way to
  reuse the core's single publish authorization + audit trail rather than
  growing a second one). The link, when the destination exposes one, is
  `publication.view_url`.

`readPublishOutcome` is that reader, and it returns an *outcome*, not a url:

- success is signalled by the return SHAPE, never inferred from a non-empty url
  — a destination may publish successfully and expose no browsable link, and
  conflating the two rendered a succeeded publish as the error branch with an
  undefined message (a bare red icon and no text);
- an `error` field wins over anything else in the same body;
- `publication: null` (an unpublished artifact) is not success;
- anything unrecognized is reported as a NAMED error (`unexpected_response`)
  rather than an empty one.

**HTTP 200 is not success on the artifact shape.** `publish_sync.publish()`
treats the version push as best-effort: its re-publish branch runs
`push_version(force=True)`, reads `refreshed.publication.last_error`, persists it
and returns normally — so the route answers 200 with a publication whose remote
content is stale. A non-empty (non-whitespace) `last_error` is therefore an error
outcome carrying the provider's own already-redacted message; whitespace-only
stays success, because the core writes `""` to clear the field.

The public-exposure warning and the blocking `PublicPublishAckModal` are
unchanged and unconditional — every destination gets both, on the clean path and
on a scan override.

## Widget auto-registration

**Every `<mcwidget>` the agent emits becomes an artifact automatically** — no
user gesture required. Registration happens on the backend when the assistant
segment is finalized (`chat_runner._flush_segment` →
`widget_artifacts.register_widgets_off_loop`), and the record is created
**unpinned**: it is a *record*, not a library entry. The star on a rendered
widget is therefore a pure `pinned` flip, not a create.

Why the backend and not `WidgetFrame` on mount: the chat list virtualizes, so a
message never scrolled into view never mounts its widgets. Frontend registration
would make an artifact's existence depend on whether a human happened to look at
it. Finalize-time registration covers every emitted widget exactly once and gets
the originating `session_key` for free — which is what lets the in-session
Artifacts tab list widgets at all (a widget's HTML is inline in the message and
never written to disk, so the file-backed session-docs scan cannot see it).

**Identity — a two-language contract.** The slug is derived from
`(message_ts, widget_index)`:

- `src/kiro_crew/widget_slug.py` → `derive_widget_slug`
- `website/src/lib/widgetSlug.ts` → `deriveWidgetSlug`

Both MUST produce identical output (two FNV-1a passes, 32-bit prime, 16 hex
chars); the frontend uses it to find the artifact the backend wrote, with no id
exchanged. Likewise `widget_parse.parse_widgets` mirrors the frontend's
`parseBlocks` widget detection, because a disagreement about *which* spans are
widgets shifts `widget_index` and mis-keys every subsequent artifact. Parity is
pinned by shared vectors/fixtures in `test/test_widget_slug.py`,
`test/test_widget_parse.py`, and `website/src/test/widgetSlug.test.ts` — a change
to one side fails all three.

Registration is **idempotent and non-destructive**: an existing slug is left
untouched (a replayed or rehydrated message never duplicates or clobbers content
the user has since iterated on), and a widget carrying an explicit `slug=`
attribute is skipped entirely, since re-emission names an existing artifact
rather than authoring a new one. Failures are logged, never raised — a lost
registration must not break the chat turn that produced the widget.

**Restricted sessions never register.** Incognito and temporary slots
(`slot.is_restricted`, i.e. `memory_mode != "persistent"`) are denied every
artifact write at the HTTP gate (`_is_restricted_session`), so
`_schedule_widget_registration` returns early for them. Without that check the
chat path would be a back door around the ceiling: widget HTML from a session the
user expected to leave no trace would persist under `artifacts/<slug>/` and appear
in the library. Both paths key off the same `slot.is_restricted` signal, so they
cannot drift apart.

**Retention.** Unclaimed auto-registered artifacts are pruned oldest-first past
`MAX_AUTO_WIDGET_ARTIFACTS` (200) by `ArtifactStore.prune_auto_widgets`, which
runs after each registration. Without it, a chat-heavy user accumulates one
three-file artifact directory per throwaway widget forever and every library
listing is an O(N) scan over them.

Because the sweep **deletes user-visible data**, eligibility
(`_is_sweepable_auto_widget`) is deliberately conservative: any sign of human or
agent investment exempts the record permanently. A record is swept only if it is
`auto_registered` **and** all of the following hold — not `pinned`, no
`folder_id` (never filed), no `publication` (never shared — a live URL points at
it), no `fork_metadata`, no `description`/`tags`, `updated_at == created_at`
(never edited), and no `comments.json` sidecar (never commented on). Explicit
saves are never swept at all. Merely *rendering* a widget is not a claim; touching
it in any of those ways is.

The comments check is a separate file stat because `add_comment` writes only the
sidecar — it never touches `meta.json`, so a commented artifact still looks
pristine to every metadata signal above, and the sweep would otherwise delete the
user's comments along with it.

The edit test is `updated_at == created_at`, **not** `version == 1`: `update()`
bumps `version` only when `snapshot=True`, so a plain content save — the common
agent-iteration path — leaves the version at 1 while rewriting the body. Keying on
the version would let the sweep delete freshly-iterated widgets. Conversely
`set_pinned` / `set_folder` deliberately don't touch `updated_at`, which is why
they are separate signals.

Ordering is newest-first, re-sorted on `(updated_at, slug)` inside the sweep:
`list()`'s `updated_at`-only sort is not a total order, so widgets registered in
the same microsecond would otherwise tie-break by directory scan order and make
*which* one gets deleted nondeterministic. The candidate snapshot is taken
unlocked, so eligibility is **re-checked and the directory removed in a single
lock acquisition** — otherwise a star landing mid-sweep would lose to a stale
verdict and silently delete an artifact the user had just claimed. Note the sweep
deliberately does NOT delegate to `delete()`: re-checking under the lock and then
calling a method that re-acquires it reopens the same window between the two
acquisitions, so the removal is inlined.

**Star semantics in `WidgetFrame`.** `exists` and `pinned` are separate states:
`{exists: true, pinned: false}` is the normal steady state, so the star renders
hollow (offering to save) while the title still links to `/artifacts/<slug>`.
Starring pins; if the artifact is absent (a pre-feature widget, a failed
registration, or one reclaimed by the sweep) it falls back to create + pin, and
tolerates 409 as "already there". Un-starring unpins only — the record and its
version history survive.

## Starred & Session Documents

The Artifacts page is a single unified, searchable table with two conceptual
inputs, distinguished by the leading **star** column:

- **Starred artifacts** — real, saved artifacts with `pinned=true`. The
  **Starred** view shows only these; the star toggles `pinned` via
  `PATCH /api/artifacts/{slug}/pin` (metadata-only, no version bump).
- **Session documents** — a *virtual* firehose of non-code documents the agent
  produced across chats (from message `file_changes`), surfaced only in the
  **All** view via `GET /api/artifacts/session-docs`. Nothing is written to disk
  for these until the user stars one, which **materializes** it into a real,
  pinned, file-backed artifact via `POST /api/artifacts/materialize`
  ("Virtual All + materialize-on-save"). Search matches name/source (incl. the
  originating session title); the file-type filter applies to both inputs.

The page opens on the **All** view by default. The Starred/All selection is
persisted per-browser (`localStorage['mc-artifacts-pinned-only']`), so a user
who last chose **Starred** resumes there on their next visit.

The firehose reads lightweight history projections: each JSONL file is streamed
as bytes, lines without the serialized `"file_changes"` key are skipped without
JSON parsing, and only `ts` plus `meta.file_changes` are retained in a bounded
file-stamp cache. It never routes through the full parsed-message cache, so a
dashboard refetch cannot pin or repeatedly decode the complete transcript
corpus while looking for document paths.

Materialization is authorization-gated: the requested path must appear in the
recorded chat `file_changes` (never an arbitrary client path), and the read is
routed through the `hooks.safe_read_file_bytes` keystone. `source` is recorded
as `chat` for materialized documents.

### In-session Artifacts tab

The chat side panel's **Artifacts** tab (`SessionArtifactsTab`) shows everything
one session produced, merging the same two inputs scoped to that session:

1. **Real artifacts** via `GET /api/artifacts?session=<slot>` — including every
   auto-registered widget. Rows open `/artifacts/<slug>`.
2. **Session documents** via `GET /api/artifacts/session-docs?session=<slot>` —
   file-backed, and the only input with a path, so those rows open the file.

A materialized document is both, so artifact rows whose slug already appears in
the document list are dropped in favor of the path-aware row. The star means
"keep in library" for either: a document with no slug materializes, everything
else is a `pinned` flip.

`?session=` is validated through the same grammar as a save's
`origin_session_key`, but a validation **miss keeps the raw value** instead of
collapsing to `""`. Collapsing is correct for a *write* (attributing a save to no
session is safe) and wrong for a *read* filter: `""` is the real no-origin bucket,
so an unvalidatable key would return some **other** session's artifacts — notably
every `artifact_save` from the MCP path, which stores `session_key=""`. Since
`store.list` compares exactly, the raw value matches zero records: an honestly
empty tab rather than a foreign one. This is not only a hostile-input case — a slot
key can legitimately exceed the grammar's 128-char cap, because the artifact
companion-chat flow names a slot `Artifact: <name>` and names run to
`MAX_NAME_LEN` (200).

Like `?folder=`, absent means "don't scope" while present-but-empty means "only
unattributed" — the handler reads the raw key to keep the two distinct. `?pinned=`
is tri-state for the same reason: an unrecognized value does not scope rather than
being read as `false`.

**The session key is the BARE slot key** (`chat-<N>-<ts>`), never a decorated
`dashboard:<key>` form. `ArtifactStore.list` compares `session_key` exactly — it
does no prefix folding, unlike `_collect_session_docs`, which accepts either form.
All three writers must therefore agree on the bare key: widget auto-registration
(`_schedule_widget_registration`), `WidgetFrame`'s fallback create
(`origin_session_key: slotKey`), and materialization. A decorated key on any one
of them silently partitions artifacts into a bucket the tab never queries, with
every write-side unit test still green — so test the round-trip
(`list(session_key=slot.key)` finds it), not the stored string.

## Validation & Limits

| Field | Limit |
|---|---|
| `slug` | regex `^[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?$`, ≤ 80 chars |
| `name` | ≤ 200 chars, non-empty |
| `description` | ≤ 2,000 chars |
| `tags` | ≤ 16 tags; each ≤ 64 chars |
| `content` | ≤ 25 MiB (`MAX_CONTENT_BYTES`) |
| `kind` | one of `widget` / `html` / `markdown` / `svg` / `json` / `text` / `webapp` |
| `source` | one of `chat` / `cron` / `subagent` / `manual` / `import` |
| `MAX_VERSIONS` | 50 (oldest pruned beyond cap) |
| `MAX_AUTO_WIDGET_ARTIFACTS` | 200 (oldest **unpinned auto-registered** widgets pruned beyond cap) |

## Security

- **Path traversal** — slugs are regex-validated; the store resolves every
  path and refuses any that escape the artifact root.
- **Sensitive paths** — every read and write goes through
  `security.is_sensitive_path()`; the store refuses to instantiate at any
  sensitive root.
- **Relocate root confinement** — `PATCH /relocate` (and the `artifact_move`
  MCP tool) point a file-backed artifact at a `source_path`; a later GET reads
  that file, so an unconfined relocate would be an agent-reachable
  arbitrary-local-file read primitive. The target is therefore confined to the
  user's home dir by default (an operator can widen to additional absolute roots
  via `publish.relocate_roots`); the resolved path must be `is_relative_to` an
  allowed root (a `..` guard runs first, and the `is_sensitive_path` denylist
  still applies inside every root). The `is_relative_to` barrier is also the
  sanitizer CodeQL's path-injection tracker requires.
- **Restricted sessions** — POST/PATCH/DELETE are denied when the dashboard
  classifies the session as restricted (`_is_restricted_session`).
- **SEL audit** — every mutation emits a `log_tool_invocation` event from the
  HTTP layer (`dashboard/handlers/artifacts.py`). Reads are not audited.
  `_audit` redacts caller-supplied text before it reaches the SEL writer (which
  signs bytes as-written and does NOT redact): the `error` string and every
  string leaf of `extra` metadata pass through `redact_via_context`, so an
  upstream provider exception carrying a credential/signed URL — or a
  provider-controlled `external_id` echoed into `extra` on the remote
  browse/clone/fork/pull/overwrite error paths — cannot leak into the audit log.
  Routing through the platform-seam shim (not the bare `_redact_text`) means a
  loaded companion's extra credential/cookie regexes apply to the audit trail.
- **Atomic writes** — `_write_text()` writes to a `.tmp` sibling and renames,
  so a crash mid-write cannot corrupt `current.html` or `meta.json`.
- **Tolerant load** — `_read_meta_file()` ignores unknown keys and supplies
  defaults for missing keys, so future schema additions don't break existing
  files.
- **Frontend rendering** — artifact bodies are rendered in the same sandboxed
  iframe that powers `<mcwidget>`, and that frame loads a **real document** from
  `GET /sandbox-doc/{doc_id}/{tok}` rather than a browser-built `blob:` URL. A
  blob URL is refused outright by some WebKit-based in-app browsers (which can
  take the whole page down with it) and a sandboxed `srcdoc` frame blank-renders
  on WebKit, so a plain document URL is the only form that loads everywhere. The
  authed `POST /api/sandbox-doc` stashes the html the caller already holds and
  returns the URL; the path credential is client-bound with a short TTL, and the
  response pins `Content-Security-Policy: sandbox` so the document keeps an
  opaque origin even when opened top-level. Flags match what the embedding frame
  already grants, so nothing is newly permitted or denied. The same response's
  `frame-ancestors` is **`'self'` plus the ancestors `'self'` cannot express**
  (`origin.frame_ancestors_value`), and both halves are load-bearing. `'self'` is
  resolved by the BROWSER against the frame's real URL, so it stays correct behind
  a TLS-terminating tunnel that rewrites `Host` and may not forward
  `X-Forwarded-Proto` — deriving the origin server-side instead names
  `http://localhost:<port>` while the browser is on `https://<tunnel-host>` and
  blanks every frame on that path. `'self'` alone is not enough because the
  directive is matched against EVERY ancestor: the Instances embed nests a remote
  dashboard inside the local one, so a widget sits three levels down (local
  dashboard → embedded dashboard → widget) with a grandparent on a different
  origin, and the browser refuses the embed while the `GET` still returns 200.
  Those extra ancestors come from `server._extra_frame_ancestors` (the embedding
  parent's port, from a validly-signed token claim) and each is re-validated
  against a strict origin form, so a malformed or inexpressible source — a
  bracketed IPv6 literal is not a valid CSP host-source — cannot reach the header.
  See `dashboard/handlers/sandbox_doc.py`. No `dangerouslySetInnerHTML` without
  DOMPurify; no inline event handlers.
- **The detail frame is sized to its document, and promoted to its own
  compositing layer.** Both are corrections to shapes that only misbehave on iOS
  WebKit, so both are invisible in a desktop dev loop and both are pinned by
  tests in `website/src/test/ArtifactBody.iframeBlob.test.tsx`.
  - The frame takes the height its document reports (`includeHeightReporter`, the
    same `mc-widget-height` protocol the chat frame uses, in its own measured-height
    key space) and carries **no minimum**. It previously stood in a fixed
    `calc(100vh - 240px)` box with `minHeight: 480`, which on a phone put a short
    artifact in a frame hundreds of pixels taller than itself and made the reader
    scroll a pane inside a scrolling page. A floor reintroduced above the reported
    height brings both back.
  - `transform: translateZ(0)` is on the frame because iOS WebKit was measured
    **skipping the document's first paint**: it loaded, its injected scripts ran,
    it reported a correct layout height, and it sat in a correctly sized visible
    frame while painting nothing. Four unrelated post-load invalidations each
    made it appear (a 1px resize, a transform toggle, an opacity flip, a display
    toggle), so promotion is the one remedy that needs no timing — anything
    scheduled off the `load` event is a race on a slow connection. Content height
    only ever *correlated* with the symptom because tall content happened to
    trigger one of those invalidations.
- **A frame showing something that is not ours offers a retry.** The document URL
  is single-use, so a navigation the ENGINE starts on its own (memory pressure, a
  back/forward cache eviction) re-requests a spent URL and lands the frame on a
  404 page — which fires `load` like any other navigation and leaves a silent
  empty box, while the failure notice stays hidden because the mint itself
  succeeded. Every document this surface builds carries the height reporter, so
  silence past a grace window is the signal to surface the existing retry (which
  re-mints; re-pointing at the spent URL recovers nothing). Deliberately not an
  automatic re-mint: a second `load` also happens when a link inside an artifact
  navigates the frame, and silently pulling the reader back would fight them.
  `/sandbox-doc/` is on the service worker's skip list for the same single-use
  reason, and because an iframe navigation has `mode === 'navigate'`, so the
  worker's offline fallback would otherwise serve the SPA shell INTO the widget
  frame (`website/src/test/serviceWorkerSkipRules.test.ts` runs the real worker).

## Versioning

Each `create()` writes the initial content to `current.html` and snapshots
it as `versions/v1.html`. Each subsequent `update(slug, content=…)` that
changes the content bumps the version number, writes the new content as
both `current.html` and `versions/v{N}.html`. Older versions remain in
`versions/` untouched until the prune cap is reached, so any prior version
can be re-read via `get(slug, version=N)` or rolled back into `current.html`
via a follow-up `update()`.

`list_versions(slug)` returns the sorted set of stored version numbers.
`get(slug, version=N)` reads a specific version. After pruning, lower-numbered
versions may be unavailable; callers must handle `ArtifactNotFoundError` for
out-of-range versions.

## Comments & Lifecycle

Comments live in a per-artifact `comments.json` sidecar (`ArtifactComment`
dataclass; threads are one level deep — replies carry the root's id as
`thread_id`). `status` is `open | review | resolved`; `sync_state` tracks
provider push status (`local_only | pending_push | synced | push_failed`).
Provider push/reconcile itself is companion-edition-only behavior behind the
CPP publish seam — the open-source core carries the `sync_state` field and
enforces the provider-origin guards, but ships no remote reconcile loop.

**Inbound comment sync (fetch-on-view).** `GET /api/artifacts/{slug}/comments`
opportunistically pulls the provider's comments (`fetch_comments`, when the
publication provider advertises `COMMENTS_READ`) and reconciles them into the
local mirror via `ArtifactStore.merge_remote_comments(slug, provider, comments)`
before returning. Each merged mirror carries `target_provider`/`target_external_id`
(the publication's provider + artifact id) so a later local edit/review/delete of
that comment routes back to the source — the write handlers gate on
`target_external_id` before calling the provider, so without it those mutations
would silently stay local and be resurrected on the next fetch. The provider is
authoritative for its own comments; the merge
drops mirrors that came back tombstoned (cascade-dropping a whole thread when its
ROOT is deleted upstream), syncs mutable fields (status/body/author) of changed
provider comments, adds newly-seen ones, and leaves `origin == "local"` comments
untouched — while keeping provider comments merely absent from one fetch (a
transient/paginated empty is not a delete). The fetch is network IO and the merge
is blocking filesystem IO, so both run off the event loop; any failure is
best-effort and surfaces as `remote_sync_error` rather than failing the list.
Every awaited remote publish-provider network call is bounded by
`_REMOTE_PROVIDER_TIMEOUT_S` (15s) via `asyncio.wait_for` (CWE-400): a timeout on
the primary read path (`remote_artifact_fetch`) maps to a **504**, while the
best-effort comment-sync paths degrade like any other provider failure
(`remote_sync_error`, local write still succeeds).
With no provider registered (the public default) `get_provider` raises and the
endpoint degrades to local-only comments. Comment `body`, `author`, **and** the
anchor `quote`/`prefix`/`suffix` are run through `_redact_text` (credential +
exfil-URL redaction) at every read boundary — the local list endpoint and the
remote-detail serializer (`_serialize_remote_comment`) — because
provider-controlled comments are merged into the mirror raw, so redaction cannot
live only on the local POST path.

**Agent disposition contract** (owner decision 2026-07-13; rubric ships in
the builtin `artifacts` skill):

- `artifact_delete_comment` (MCP) — for comments that were unambiguous
  directives, fully applied. Requires a `reason` (≤ 500 chars) recorded in
  the SEL audit and the activity feed. Root deletes cascade to replies.
- `artifact_mark_review` — for comments addressed with judgment; human
  verifies and resolves.
- Resolution stays human-only: the resolve endpoint returns 403 for any
  MCP-originated request (actor inferred from the `X-Internal-Secret`
  header, never from a body flag).
- Agents may not delete provider-synced comments (403) — provider
  reconciliation (companion edition) would resurrect or desync them; mark
  REVIEW instead.

**Orphaned anchors** — every content write through `update()` (agent
iterations, dashboard saves, reverts, upstream pulls) rescans open anchored
comments with a plain-substring check (`anchor_quote in content` — the same
exactness contract as the frontend highlighter). Threads whose quote is
gone get `anchor_orphaned=true` (a dedicated field, deliberately not a
`sync_state` value so push status is never clobbered); the flag clears if
the text returns (e.g. a revert). The UI shows a warning and de-emphasizes
orphaned threads.

**Activity feed** — comment lifecycle changes append a `comment` event
(`ALLOWED_EVENT_TYPES`) to the artifact's audit log with
`metadata.action ∈ deleted | reviewed | resolved`, a ≤ 100-char
`comment_snippet`, and the agent's `reason` on deletes, so a deleted
comment never disappears without a trace.

## Knowledge Library Auto-Ingest

Content-bearing local artifacts (markdown/text documents) can be automatically
ingested into the Knowledge Library so they become searchable, stay in sync as
the artifact changes, and are removed when the artifact is deleted. Off by
default, opt in with `knowledge.auto_ingest_artifacts`; the eligible kinds are
`knowledge.auto_ingest_artifact_kinds` (default `["markdown", "text", "html",
"json"]`). `widget` is excluded (widgets/dashboards are UI, not documents — and
a remote widget round-trips back to `kind="widget"` on clone) and `svg` is
excluded (the file reader has no `.svg` support).

The feature plugs into the existing Knowledge **source framework** rather than
adding a parallel watcher (see `kiro_crew.knowledge.artifact_ingest`):

- **One aggregate "Artifacts" source.** A single `sources` row of
  `source_type="artifact"` (uri `artifact://`) appears in the dashboard Sources
  UI alongside the user's folder/upload sources. Items are grouped per-artifact
  in a dedicated `artifact_item_state` table (keyed by `source_id` + `slug`,
  with the artifact's display `name` stored as the group label) — the same
  item-group pattern a folder source uses per file, so one artifact's items can
  be replaced on edit or removed on delete without touching the rest. A per-slug
  `content_hash` makes an unchanged artifact a cheap no-op. The dashboard
  sub-groups this source per-artifact (one row per artifact, labelled by name)
  the same way folder sources sub-group per file: `_attach_file_paths` supplies
  the label and the frontend gates sub-grouping on `source_type` in
  (`local_folder`, `obsidian_vault`, `artifact`).
- **One ingestion path (via the file reader).** Ingestion routes through the
  same `IngestionPipeline.ingest_file` → `FileReader` path as folders/uploads,
  not a parallel raw-text path: the (redacted) artifact content is written to a
  temp file with the kind's real extension (`markdown→.md`, `text→.txt`,
  `html→.html`, `json→.json`) and read back through the reader, so `html`
  artifacts get `_read_html` prose extraction instead of raw markup.
- **Event-driven, no polling.** The gateway is the only process that writes the
  artifact store (the agent's MCP tools, the CLI, the dashboard, and bookmarks
  all HTTP-proxy to the gateway's `/api/artifacts` routes; Artifactory
  pull/clone also funnel through the store). So a single in-process
  change-listener registered via `ArtifactStore.set_change_listener` observes
  every write path. `ArtifactKnowledgeSync.on_change` schedules the work on the
  gateway loop: `upsert` → ingest/replace the artifact's item group; `delete` →
  remove it. The store stays dependency-free — it knows nothing about the
  Knowledge package; it only fires `(action, slug)` after a
  content-affecting mutation (create, content-changing update, delete). A
  metadata-only rename fires a separate `rename` signal that refreshes the
  stored group label without re-ingesting (no chunk churn).
- **Reconcile on every start, not a creation-gated backfill.** The feature is
  opt-in, and while it is off the change-listener is not registered, so writes in
  that window never reach the Library. Tying the catch-up pass to *creation of
  the aggregate source row* cannot repair that: the row outlives the feature
  being switched off, so on any install that ever had it on a later opt-in gets
  `created=False` and the pass never runs — the gap is permanent and silent.
  `ArtifactKnowledgeSync.start()` therefore runs `reconcile_artifacts`
  **unconditionally**, comparing the artifact store against
  `artifact_item_state`: ingest what is missing or changed, drop state for
  artifacts that no longer exist. `created` is now reported for logging only.
  - **Converged is free.** `ingest_artifact` already skips unchanged content, so
    the steady state spends no extraction calls and logs at debug.
  - **Removals are judged against every artifact, not the eligible kinds.**
    Narrowing `auto_ingest_artifact_kinds` makes an artifact ineligible, not
    absent; reaping on that basis would delete content the user never deleted.
    A reap also requires two signals, not one: `get()` raising
    `ArtifactNotFoundError` *and* the artifact's directory being gone. That error
    is also what a missing `meta.json` raises, which a partially-restored
    directory hits while its content is still present, and deletion removes the
    whole directory — so demanding both costs a recoverable stale group instead
    of unrecoverable deleted items.
  - **An emptied artifact is dropped unbudgeted.** Its body is blank, so there is
    nothing to extract and the drop costs no LLM calls. Keeping it behind the
    budget would let obsolete text stay searchable for as many restarts as a
    backlog of newer changes takes to drain. Only tracked artifacts are read, and
    the pass stands down on `source_missing` for the same reason
    `ingest_artifact` does.
  - **Off-window metadata drift is repaired unbudgeted.** A tracked artifact
    whose kind differs from the kind recorded at ingest (`artifact_item_state.kind`)
    had its group produced by the previous kind's reader, so that group is
    reaped exactly as the live `upsert` path does, and the ingest loop rebuilds
    it if the new kind is eligible. The decision reads the *recorded* kind, not
    the current allowlist: "the artifact changed" and "the user narrowed
    `auto_ingest_artifact_kinds`" are indistinguishable from the current kind
    alone, and reaping on the latter would delete content the user never
    touched. A row predating the column carries `NULL`, so its ingested kind is
    unknown; that is resolved by which repair is safe. An eligible artifact is
    re-ingested once (in budget, no deletion), which repairs any undetectable
    drift and backfills the column so it never repeats; an ineligible one is
    left alone, because deletion is the only repair available there and drift
    was never proven. Where the
    removal happens depends on whether anything will rebuild the group: a drift
    into an *ineligible* kind is reaped in the unbudgeted pre-pass (removal is
    the whole repair), while a drift between two *eligible* kinds is replaced
    inside the budgeted loop, so a backlog past the budget keeps its stale group
    and defers rather than being deleted now and restored several starts later.
    An eligible replacement clears only the recorded content hash, never the
    group: that defeats the unchanged-content short-circuit (a byte-identical
    body under a new kind would otherwise keep the previous reader's chunks)
    while leaving `ingest_file` to do its normal atomic replace, so a failed
    extraction keeps the old items and the artifact stays searchable.
    Every tracked artifact's stored group label is also refreshed to its current
    redacted name, so a rename during the off-window stops showing the old
    label. Neither pass touches content hashes — a converged store still spends
    nothing.
- **A dead source pointer neither deletes nor rewrites an index.**
  `ingest_artifact` returns early whenever `get()` reported `source_missing`
  (live file moved / unreadable, so the content is a snapshot fallback). That
  snapshot is not evidence about the live file in either direction: blank does
  not mean the artifact was emptied, and non-blank does not mean it is current,
  so acting on it would either destroy a valid index or replace newer indexed
  text with older. Same rule as the reconcile reap — only provable state acts.
  - **Ingests are bounded per run** by `RECONCILE_INGEST_BUDGET` (a module
    constant, not a config key). `ArtifactStore.list` is newest-first, so a
    backlog from a long off-window drains across successive starts with the most
    recent artifacts first, instead of arriving as one unbounded burst of billed
    extraction calls. Unchanged artifacts do not consume budget.
- **Security.** Ingested text *and* the LLM-originated artifact name (used as
  the source/item title) are passed through `redact_credentials()` and
  `redact_exfiltration_urls()` before landing in the Knowledge store (per
  input-validation guidance — never persist secrets), consistent with the
  chat-ingest path. File-backed
  artifacts whose `source_path` resolves to a sensitive path are refused (with a
  SEL audit event), mirroring the folder-watcher file-read guard.
- **Dedup tie-in.** A file-backed artifact whose `source_path` is also inside a
  synced folder source is the same document under two sources (the aggregate
  `artifact` source and the folder's `local_file` source). The aggregate
  `artifact` source is **excluded from dedup entirely** (`enumerate_docs` and
  `_build_doc_for` skip `_AGGREGATE_SOURCE_TYPES`, and `_delete_doc` refuses a
  cascade on them): treating the whole aggregate as one dedup unit keyed on a
  single item's hash both misidentified it and — when it lost a pair — cascade-
  deleted the user's entire artifact library. The artifact↔folder overlap
  therefore persists (both copies remain retrievable) until per-artifact-slug
  dedup is built; that is a recorded, intentional trade against silent data
  loss on the hard-delete path.

## Companion Chat

The artifact detail page hosts a **companion chat panel**: the artifact renders
on the left and a live agent session bound to it runs on the right, so an
iteration loop never leaves the page.

**Binding** — a chat slot may carry an `artifact` field (a validated artifact
slug) set at slot create (`POST /api/chat/slots` body key `artifact`,
validated against the slug grammar; invalid values are silently dropped).
The field is serialized in `to_dict()` — flowing into `GET /api/chat/slots`
and the WS `slots` snapshot, which is how the frontend resolves the active
bound session with zero extra endpoints — and persisted in the history meta
line so the binding survives gateway restarts and History-page resumes
(resuming a bound session re-establishes it as the artifact's active
companion).

**Tamper gate** — the binding is validated against a single shared slug
grammar (`validation.ARTIFACT_SLUG_RE`, `\Z`-anchored) at EVERY boundary it
crosses: slot create (`chat_handlers`) AND history-metadata restore on both
paths (`chat_persistence` rehydrate + bulk restore) — a tampered history
JSONL cannot inject an arbitrary string that flows into `to_dict()`/WS
broadcasts.

**Invariant** — at most one *active* (non-archived) bound session per slug,
maintained by the frontend flow. The backend accepts any valid slug and does
not enforce uniqueness.

**Live refresh** — the artifact mutation funnel broadcasts a typed
`artifact_update {slug, version, deleted}` WS event
(`DashboardState.push_artifact_update`, called via the handlers'
`_notify_artifact_update` helper) from: create (both the genuine-create and
source_path dedup-bump paths), content-carrying PATCH (Save / Snapshot /
MCP update / revert — metadata-only PATCHes do NOT emit), delete
(`deleted: true`), relocate, and pull-latest (when the pull actually landed a
new snapshot). Fire-and-forget; react-query's 30s staleness window remains
the safety net.

**Panel (frontend)** — the comments sidebar and the chat panel are mutually
exclusive flex siblings of the artifact body, icon-toggled from the toolbar
(sparkle = chat, speech bubble = comments); neither overlays the artifact. The
comment-count auto-reveal never switches away from an open chat panel, since the
chat panel opens only on explicit action.

**Session resolution (frontend)** — the active bound session is resolved from
the Redux slots snapshot (`slot.artifact === slug`), so no extra endpoint exists:
the WS `slots` event already carries the binding. The flow keeps it to at most
one *active* bound session per slug by archiving before creating; the resolver
tolerates more by picking the most recently active, so a race or a History-page
resume degrades gracefully rather than erroring.

**Create is one round trip** — the `POST /api/chat/slots` response carries the
binding, so it is dispatched straight into the slots list (`addSlotOptimistic`)
and the panel becomes interactive immediately. The silent context entry POST and
the `fetchSlots` reconciliation run in the background: the context entry is
consumed on the *next* user message, so it always lands before a human can type
and send.

**Chat parity** — the panel embeds the same `ChatPage` component as `/chat`
(`embedded` + `embedMode="chat"` for the single-session chrome), so follow-up
option chips, question cards, steer-send, tool groups and regenerate are
identical by construction. A `noUrlSync` prop gates
`useChatPageSessionController.ts`'s one URL-write effect: the host route
`/artifacts/:slug` owns the URL, and an in-place
`navigate` would swap the host route out from under the panel.

**Composer staging** — "Ask agent to address" routes into the bound session and
*stages* (never auto-sends) its message through the existing `writePrefill`
sessionStorage channel `useChatPageComposerController.tsx` already consumes on
slot activation.

## Roadmap

In scope for the foundation:

- ✅ data layer + CLI + MCP tools + HTTP + library page + standalone page
- ✅ "Save as artifact" affordance on rendered widgets
- ✅ widgets are artifacts **by default** — auto-registered unpinned on emission,
  listed in the in-session Artifacts tab, retention-swept while unstarred
- ✅ system prompt context note documenting the iterate flow

Out of scope (separate tasks):

- **Whiteboard layout** — saved arrangements of (artifact_id, x, y, w, h) —
  tracked as follow-on work.
- **Live refresh bindings** — cron / Python script / MCP-tool source types
  that auto-rewrite `current.html` on a schedule — tracked as follow-on work.
  The hook will be a new `meta.json.refresh_binding` field consumed by a
  refresh service.
- **Right-panel inline render** — clicking an `<a>` to an artifact in chat
  opens the artifact in a side panel rather than the standalone page —
  related follow-on work.
- **Cross-user sharing**, **embeddings/full-text search**, **install from
  URL/community widget store** — future expansions.

## WebApp Artifacts (`kind="webapp"`)

A `webapp` artifact represents a *deployed application*. It carries structured
`webapp_metadata` (deploy target, architecture, lifecycle/TTL, cost estimate,
teardown handle, local app tree) and the dashboard renders it as a
browser-framed app card: a live preview of the app plus deploy state, cost,
and TTL panels.

**Preview rendering (local-first fallback chain).** The card and the gallery
thumbnail try, in order: (1) the **local preview channel** — the gateway
serves the app's local copy (`webapp_metadata.app_dir`) through a token-gated
static route, working for every lifecycle state including expired and
not-yet-deployed; (2) a sandboxed iframe of the **live CloudFront deployment**
(`framablePreviewUrl` gate: https + `<dist-id>.cloudfront.net` host shape
only, mirrored by the server CSP `frame-src https://*.cloudfront.net`);
(3) a status hero.

### WebApp Metadata Schema

| Field | Type | Description |
|---|---|---|
| `deploy_target.provider` | string | `"aws"` (default) |
| `deploy_target.account` | string | AWS account ID |
| `deploy_target.region` | string | AWS region |
| `deploy_target.public_url` | string | The live HTTPS URL |
| `deploy_target.profile` | string | Named AWS CLI profile used |
| `app_dir` | string | Absolute path of the local app tree that was/would be deployed. Set by the artifact author (the deploy API never sees the artifact and the directory together, so it cannot back-fill this). LLM-influenceable — re-validated against the allow-listed local roots at serve time. |
| `architecture.tier` | enum | `"static"`, `"api"`, `"stateful"` |
| `architecture.resources` | list | `[{type, id}]` — infrastructure resources |
| `lifecycle.created_at` | string | ISO 8601 creation time |
| `lifecycle.expires_at` | string? | ISO 8601 expiry (null = persistent) |
| `lifecycle.persistent` | bool | Whether the deploy has no TTL |
| `lifecycle.ttl_hours` | int | Original TTL in hours |
| `lifecycle.status` | enum | `"draft"`, `"deploying"`, `"live"`, `"error"`, `"expired"` |
| `teardown.method` | string | `"reaper-lambda"` |
| `teardown.handle` | string | Reaper target handle |

### Local Preview Channel

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `GET` | `/api/artifacts/{slug}/app-preview` | standard dashboard auth | Validate the artifact + `app_dir` and mint a short-lived (15 min) HMAC path token. Returns `{available, base}`; `{available: false}` for every miss (no oracle). |
| `GET` | `/artifact-app/{slug}/{token}/{path}` | HMAC path token (auth-middleware bypass) | Serve one static file from the app's web root — **`app_dir/public` is mandatory** (deploy-contract layout); an app_dir without a contained `public/` directory reports the preview unavailable, it is never served directly. Sandboxed preview iframes carry no cookies, so the token IS the auth. |

Serve-time security (fail-closed 404 for every rejection): allow-listed local
roots (same list as the deploy publish path); `public` symlink must resolve
inside the validated `app_dir`; full-resolution containment check per file
(traversal + symlink escape); dotfile components never served; sensitive
paths rejected (`is_sensitive_path`); reads go through the inode-pinned
`safe_read_file_bytes_nolink(within_root=webroot)` helper; token HMAC binds
`slug + webroot + exp` with a per-process secret; responses carry
`Content-Security-Policy: sandbox allow-scripts` (opaque origin even outside
the iframe) plus `nosniff` and `no-store`. All filesystem work runs off the
event loop via `asyncio.to_thread`.

### Deploy Routes (`/api/deploy/*`)

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/deploy/config` | Read deploy config (default profile) |
| `PUT` | `/api/deploy/config` | Update deploy config |
| `GET` | `/api/deploy/profiles` | List registered AWS profiles |
| `POST` | `/api/deploy/profiles` | Add a new profile |
| `PUT` | `/api/deploy/profiles/{name}` | Update a profile |
| `DELETE` | `/api/deploy/profiles/{name}` | Delete a profile |
| `GET` | `/api/deploy/iam-policy` | Get the required IAM policy document |
| `POST` | `/api/deploy/verify` | Verify credentials for a profile |
| `POST` | `/api/deploy/deploy` | Deploy a site (confirm-gated) |
| `POST` | `/api/deploy/recall` | Recall (soft teardown) a site (confirm-gated) |
| `POST` | `/api/deploy/destroy` | Full teardown of infrastructure (confirm-gated) |
| `GET` | `/api/deploy/list` | List deployed sites |
| `POST` | `/api/deploy/teardown/{slug}` | Human-triggered artifact teardown |
| `GET` | `/api/deploy/pending` | List pending (unconfirmed) deploy previews |
| `POST` | `/api/deploy/pending/{id}/confirm` | Execute a pending deploy (cookie/token only; internal-secret denied) |
| `POST` | `/api/deploy/pending/{id}/dismiss` | Dismiss/cancel a pending deploy (cookie/token only; internal-secret denied) |

Mutating routes fall into two categories:
- **Confirm-gated** (two-step preview+confirm): deploy, recall, destroy.
- **Auth-gated CRUD** (cookie/token auth, no confirm step): profile and config
  creation/update/deletion.
- **Pending-confirmation** (cookie/token only; internal-secret sessions are
  explicitly denied): `GET /api/deploy/pending`, `POST .../confirm`,
  `POST .../dismiss`. These routes support the two-step preview→confirm
  deploy flow — the gateway generates a pending entry at preview time and
  the dashboard UI confirms or dismisses it.

All mutating routes require an unrestricted (non-restricted) session.

### Teardown Semantics

Teardown of a `webapp` artifact follows a **tombstone + manifest-expiry + reaper**
model:

1. **Tombstone:** `mark_webapp_expired(slug)` sets `lifecycle.status="expired"`
   in the artifact metadata. The artifact is kept as deploy history.

2. **Manifest expiry (best-effort):** The teardown handler rewrites the S3
   deploy manifest (`.kirocrew-deploy.json`) with `expires_at=now`,
   `persistent=false`. This is a non-destructive S3 PUT using the deployment's
   recorded profile. If credentials are unavailable or the bucket is unreachable,
   the tombstone still stands.

3. **Reaper sweep:** The in-account reaper (`scripts/reaper.sh` or the reaper
   Lambda via EventBridge) scans deploy manifests on a schedule. Manifests with
   `expires_at` in the past are reaped: backend stack deleted, S3 prefix removed,
   CloudFront invalidated. The manifest removal commits the reap.

The gateway's `/api/deploy/destroy` endpoint (confirm-gated) calls
`engine.destroy` under cookie/token auth + confirm + audit to initiate
infrastructure teardown. This is the **direct teardown path** — it performs
destructive AWS calls (DeleteStack, bucket deletion, distribution teardown)
synchronously under the user's own credentials during the request.

Separately, the **reaper path** (the in-account reaper Lambda or
`scripts/reaper.sh` via EventBridge schedule) sweeps for expired manifests
and performs the same cleanup on a schedule. The reaper runs with the user's
own credentials in-account and handles the case where the gateway is unreachable
or the user did not explicitly destroy before TTL expiry.

## Image Artifacts (`kind="image"`)

An `image` artifact is a **raster picture**, not text. Its bytes live in a binary
sidecar beside `meta.json` and are served by a dedicated endpoint; the textual
`current.html` exists but stays **empty**, and `content` in every API response is
`""`. Consumers must therefore never render an image artifact through the text or
widget paths — `ArtifactBodyImage` handles it, bypassing both Monaco and the
sandboxed iframe.

SVG is deliberately **not** an image artifact: it is markup, stored as
`kind="svg"` text and rendered through the sanitizing `SvgViewer`. Serving
agent-authored SVG as an image would reintroduce a same-origin script vector.

### Storage layout

```
~/.kiro/crew/artifacts/
└── <slug>/
    ├── meta.json        includes the `image` block below
    ├── current.html     present but EMPTY (bytes are not text)
    ├── asset.<ext>      the raster bytes (png|jpg|webp|gif|bmp)
    └── versions/
        └── v1.html      empty, mirroring current.html
```

The sidecar's extension is derived **from the allowlisted mime**, never from the
stored `ext` field, on every read (see [Security](#security)). `delete` removes
the whole artifact directory, so the sidecar needs no separate cleanup.

### `image` metadata schema

Tolerant-loaded: every field is optional so an older or hand-edited record still
opens, and each consumer degrades gracefully.

| Field | Type | Description |
|---|---|---|
| `mime` | string | One of `image/png`, `image/jpeg`, `image/webp`, `image/gif`, `image/bmp`. Anything else is rejected on create **and** refused on read. |
| `ext` | string | Sidecar extension as written. Informational only — reads re-derive it from `mime`. |
| `size_bytes` | int | Byte length of the sidecar. |
| `width` / `height` | int | Natural pixel size, sniffed from the file header with the stdlib only (no Pillow). `null` when unmeasurable — dimensions are a rendering nicety, not a gate. |
| `sha256` | string | Hex digest of the bytes. |
| `original_filename` | string | Basename of the source file; names the download. **LLM-derived** → redacted in `_serialize`. |
| `alt` | string | Accessible description, from the markdown alt text. **LLM-derived** → redacted in `_serialize`. |

### Asset endpoint

`GET /api/artifacts/{slug}/asset` — returns the raw bytes with the sniffed
`Content-Type`, so an `<img src=…>` can point straight at it and the artifact
JSON never carries base64.

- **Authenticated** like every other artifact route; unauthenticated requests get
  403. No restricted-session gate applies (it is a read) and no `referenced`
  breadcrumb is recorded (an asset fetch is a sub-resource of a detail view that
  was already counted).
- `Cache-Control: private, max-age=31536000, immutable` — `private` because the
  bytes are behind token auth and a shared proxy must never serve a cached copy
  to an unauthenticated requester.
- 404 when the slug does not resolve, is not an image artifact, its sidecar is
  missing, or its mime is not in the allowlist.
- The read is offloaded with `asyncio.to_thread`: the sidecar may be up to
  `MAX_CONTENT_BYTES` and a synchronous read would stall every other gateway
  task, the liveness heartbeat included.

### Auto-registration from chat (`kiro_crew.image_artifacts`)

Finalized assistant messages are scanned for **local** markdown image references
and each one is registered, copying the bytes immediately so temp-file cleanup
cannot strip them.

- **Identity.** Slugs are derived deterministically from `(message_ts, index)`
  via the widget-slug contract, where `index` counts **every** image match in the
  message including skipped ones — so an image's ordinal is stable regardless of
  which siblings were skipped. A replayed message is therefore idempotent and
  never clobbers an artifact the user has since edited.
- **Destination parsing.** Balanced-paren walk, so `screenshot(1).png` survives;
  `<...>` destinations are unwrapped so a path containing spaces survives;
  backslashes are treated as escapes **only** before markdown-significant
  characters, so a native Windows path (`C:\Users\me\shot.png`) survives; alt
  text accepts escaped brackets (`![Revenue \[Q1\]](…)`) and is unescaped for
  display.
- **Skipped:** remote/`data:`/protocol-relative URLs (never fetched), relative
  paths, unsupported extensions, sensitive paths, and restricted/incognito
  sessions.
- **Budgets (per message):** at most `MAX_IMAGES_PER_MESSAGE` (12) images and
  `MAX_IMAGE_BYTES_PER_MESSAGE` (64 MiB) of copied bytes. Counted over
  *eligible* images rather than successful writes, so a replay cannot walk past
  the cap one batch at a time. Pruning runs after the loop, so without these a
  single message could fill the disk.
- **Retention.** Auto-registered images are `auto_registered=True` and unpinned,
  so they ride the **same** count-based sweep as auto-registered widgets
  (`prune_auto_widgets(keep=MAX_AUTO_WIDGET_ARTIFACTS)`) — the predicate is
  kind-agnostic. Images and widgets therefore share one budget; pinning
  ("Save permanently"), filing, tagging, or commenting exempts a record.
- **Never raises.** A failure to register a chat image is a lost convenience, not
  a reason to fail the turn that produced it; per-image failures are logged and
  skipped individually. Dispatch uses `asyncio.to_thread` rather than the shared
  subprocess pool, so a wedged teardown worker cannot hold registration until
  after the source file is gone.
