# Artifacts Module

Last Updated: 2026-05-21

## Overview

Artifacts give chat-rendered LLM-generated UI a persistent identity, version
history, and a stable handle the agent can iterate on across sessions.

A typical flow:

1. Agent emits an `<mcwidget>` in chat ("here's your CR queue")
2. Agent (or user) calls `artifact_save` — the widget is persisted under
   `~/.kirocrew/artifacts/<slug>/current.html`
3. Days later, in a fresh session, the user says "iterate on the cr-queue
   artifact and add an age column"
4. Agent calls `artifact_get("cr-queue")` to read the current HTML, modifies
   it, then `artifact_update("cr-queue", content=…)` to publish a new version
5. The previous version is preserved under `versions/v1.html` for rollback

The dashboard provides a `/artifacts` library page for browse/search and a
`/artifacts/<slug>` standalone view with a version dropdown.

## Storage Layout

```
~/.kirocrew/artifacts/
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
| `slug` | string | URL-safe handle, derived from `name` if not given |
| `name` | string | Human-readable display name |
| `kind` | enum | `widget`, `html`, `markdown`, `svg`, `json`, `text`, `webapp` — inferred on save when the caller omits it (see [Kind inference](#kind-inference)) |
| `source` | enum | `chat` (default), `cron`, `subagent`, `manual`, `import` |
| `pinned` | bool | "Starred" — user-curated keep flag (default `false`). Drives the Artifacts page **Starred** view. Metadata-only; toggling does NOT bump `version`. |
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
kirocrew artifact save --name N [--kind K] [--content C | --content-file F] [--tags A,B] [--description D]
kirocrew artifact update <slug> [--content C | --content-file F] [--name N] [--description D] [--tags A,B]
kirocrew artifact versions <slug>
kirocrew artifact delete <slug>
```

The CLI proxies through the gateway HTTP API (matches `kirocrew learn`).

### HTTP

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/artifacts` | `?tag&kind&q` filters + `?folder=` scoping (absent = all; empty = unfiled/root; id = that folder); returns `{artifacts: […]}` |
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

POST/PATCH/DELETE require an unrestricted session. The HTTP body envelope is
capped at 2 MiB; the store enforces a per-content cap of 25 MiB
(`artifacts.MAX_CONTENT_BYTES`), large enough for cloned/pulled rich artifacts
(HTML reports, CSVs). The MCP save/update field cap
(`validation.ARTIFACT_CONTENT_MAX`) imports that same constant so the tool and
store paths never disagree.

**Folders (Mesh-2720):** `Artifact.folder_id` (`""` = unfiled) is an opaque,
rename-safe membership id, tolerant-loaded for legacy meta.json.
`ArtifactStore.set_folder()` is a metadata-only move (NO version bump);
`list(folder=)` filters (None = all, `""` = unfiled, id = that folder).
`ArtifactFolderStore` keeps a flat `parent_id` tree in
`~/.kirocrew/artifact_folders.json` — create/rename/reparent (cycle- and
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
destinations:<provider>`) BEFORE dispatch — `overwrite-remote` on the resolved
`publication.provider`, and `clone` on the routed provider (a clone sets
`auto_sync=True`, arming every future snapshot push). Fork and the read-only
routes (browse, upstream-status, pull-latest) stay ungated ingress. All remote
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

A small "Save as artifact" button is overlaid on every rendered `<mcwidget>`
in chat. Clicking prompts for a name and POSTs to `/api/artifacts`.

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

Materialization is authorization-gated: the requested path must appear in the
recorded chat `file_changes` (never an arbitrary client path), and the read is
routed through the `hooks.safe_read_file_bytes` keystone. `source` is recorded
as `chat` for materialized documents.

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
  HTTP layer (`api/dashboard/handlers/artifacts.py`). Reads are not audited.
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
  iframe that powers `<mcwidget>`. No `dangerouslySetInnerHTML` without
  DOMPurify; no inline event handlers.

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

Content-bearing local artifacts (markdown/text documents) are automatically
ingested into the Knowledge Library so they become searchable, stay in sync as
the artifact changes, and are removed when the artifact is deleted. On by
default via `knowledge.auto_ingest_artifacts`; the eligible kinds are
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
- **First-enable backfill tied to source-row creation.** Because the feature is
  on by default, the store may already hold artifacts created before the
  listener existed. The one-time pass that ingests them is tied to the
  *creation of the aggregate source row*: when `ensure_artifact_source` actually
  inserts the row (its existence is the idempotency marker — no separate flag),
  a background backfill runs once. On every later boot the row already exists,
  so nothing re-runs. (Nothing writes the store while the gateway is down, and
  there is no out-of-process writer, so no recurring reconcile is needed.)
- **Security.** Ingested text *and* the LLM-originated artifact name (used as
  the source/item title) are passed through `redact_credentials()` and
  `redact_exfiltration_urls()` before landing in the Knowledge store (ARCC Bsc4
  — never persist secrets), consistent with the chat-ingest path. File-backed
  artifacts whose `source_path` resolves to a sensitive path are refused (with a
  SEL audit event), mirroring the folder-watcher file-read guard.
- **Dedup tie-in.** A file-backed artifact whose `source_path` is also inside a
  synced folder source is the same document under two sources (the aggregate
  `artifact` source and the folder's `local_file` source). The live Knowledge
  dedup sweep collapses the pair once both copies share a `content_hash` (the
  persistent folder copy wins over the artifact copy), so the overlap
  self-resolves rather than needing special-casing here.

## Companion Chat (Mesh-2772)

The artifact detail page can host a **companion chat panel**: the artifact
renders alongside a live agent session bound to it. This is the backend half.

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

## Roadmap

In scope for the foundation:

- ✅ data layer + CLI + MCP tools + HTTP + library page + standalone page
- ✅ "Save as artifact" affordance on rendered widgets
- ✅ system prompt context note documenting the iterate flow

Out of scope (separate tasks):

- **Whiteboard layout** — saved arrangements of (artifact_id, x, y, w, h) —
  parent task [Mesh-1437](https://taskei.amazon.dev/tasks/Mesh-1437).
- **Live refresh bindings** — cron / Python script / MCP-tool source types
  that auto-rewrite `current.html` on a schedule — task
  [Mesh-1565](https://taskei.amazon.dev/tasks/Mesh-1565). The hook will be a
  new `meta.json.refresh_binding` field consumed by a refresh service.
- **Right-panel inline render** — clicking an `<a>` to an artifact in chat
  opens the artifact in a side panel rather than the standalone page —
  related to [Mesh-1534](https://taskei.amazon.dev/tasks/Mesh-1534).
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
