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
| `kind` | enum | `widget`, `html`, `markdown`, `svg`, `json`, `text` — inferred on save when the caller omits it (see [Kind inference](#kind-inference)) |
| `source` | enum | `chat` (default), `cron`, `subagent`, `manual`, `import` |
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
| `GET` | `/api/artifacts/{slug}/versions` | `{slug, versions: [int]}` |
| `GET` | `/api/artifacts/{slug}/versions/{n}` | Specific version content |
| `GET` | `/api/artifact-folders` | Folder tree with `item_count` + breadcrumb `path` |
| `POST` | `/api/artifact-folders` | Create folder `{name, parent?\|parent_id?, color?}`; spawns background emoji-icon task |
| `PATCH` | `/api/artifact-folders/{id}` | Rename / reparent / reorder / icon / color |
| `DELETE` | `/api/artifact-folders/{id}` | `?delete_contents=` picks keep (re-parent, default) vs cascade (delete subtree incl. artifacts) |
| `PATCH` | `/api/artifacts/{slug}/folder` | Move an artifact into a folder (`{folder}` id/path or `{folder_id}` id-only) |

POST/PATCH/DELETE require an unrestricted session. The body is capped at
2 MiB; the store enforces a per-content cap of 1 MiB.

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
`test_artifact_folder_handlers.py`.

### Dashboard pages

- `/artifacts` — list page (name / kind / tags / updated_at), tag filter,
  name substring search, click-through to detail
- `/artifacts/<slug>` — full-screen render of the current artifact in a
  sandboxed iframe (same security model as inline `<mcwidget>`), with a
  version dropdown

A small "Save as artifact" button is overlaid on every rendered `<mcwidget>`
in chat. Clicking prompts for a name and POSTs to `/api/artifacts`.

## Validation & Limits

| Field | Limit |
|---|---|
| `slug` | regex `^[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?$`, ≤ 80 chars |
| `name` | ≤ 200 chars, non-empty |
| `description` | ≤ 2,000 chars |
| `tags` | ≤ 16 tags; each ≤ 64 chars |
| `content` | ≤ 1 MiB |
| `kind` | one of `widget` / `html` / `markdown` / `svg` / `json` / `text` |
| `source` | one of `chat` / `cron` / `subagent` / `manual` / `import` |
| `MAX_VERSIONS` | 50 (oldest pruned beyond cap) |

## Security

- **Path traversal** — slugs are regex-validated; the store resolves every
  path and refuses any that escape the artifact root.
- **Sensitive paths** — every read and write goes through
  `security.is_sensitive_path()`; the store refuses to instantiate at any
  sensitive root.
- **Restricted sessions** — POST/PATCH/DELETE are denied when the dashboard
  classifies the session as restricted (`_is_restricted_session`).
- **SEL audit** — every mutation emits a `log_tool_invocation` event from the
  HTTP layer (`api/dashboard/handlers/artifacts.py`). Reads are not audited.
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
