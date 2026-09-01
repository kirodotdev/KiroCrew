# File Search Module

## Overview

File search backs the `@`-mention picker in the dashboard chat composer. A user types `@` followed by a query that meets the endpoint minimum, picks a result, and the composer inserts a token that serializes into the prompt as an attachment marker; `test_short_query_returns_empty` pins the minimum-query refusal.

Results cover both **files** and **directories**. A file is an attachment whose content reaches the agent. A directory is a **path reference only**: the agent receives the path and explores it with its own glob/grep/read tools. No directory listing or recursive content is inlined.

## API

### `GET /api/file-search`

| Param | Required | Description |
|---|---|---|
| `q` | yes | Query string. Queries shorter than the accepted minimum return an empty result set; `test_short_query_returns_empty` pins the boundary. |
| `project` | no | An existing, non-sensitive directory after `expanduser` and `realpath` canonicalization. It takes precedence over `workspace`; `api_file_search` enforces the root check and `test_project_scoping` pins its scope. |
| `workspace` | no | Workspace name resolved through `workspace_dir_for` only when `project` is absent. A missing workspace does not establish scope, so `api_file_search` uses its fallback roots. |
| `kinds` | no | `all` (default), `files`, or `dirs`. Unrecognized values fall back to `all`. |
| `limit` | no | Result page size. `api_file_search` normalizes it to a positive server ceiling; invalid input uses the default. The ceiling is load-bearing because candidate collection scales with the requested page size; `test_limit_clamped_at_server_ceiling`, `test_limit_non_integer_falls_back_to_default`, and `test_limit_negative_or_zero_clamped_to_floor` pin the contract. |

Response:

```json
{
  "results": [
    {"path": "/repo/src/pages", "name": "pages", "kind": "dir",  "size": 0,    "mtime": 1750000000},
    {"path": "/repo/src/app.ts", "name": "app.ts", "kind": "file", "size": 2048, "mtime": 1750000000}
  ],
  "root": "/repo"
}
```

- `kind` is `"file"` or `"dir"`. Directory entries always report `size: 0`.
- The endpoint returns at most the normalized `limit`; `test_max_results_capped` and `test_limit_param_honoured` pin the default and expansion behavior. The folder panel expands through its fixed tiers while callers that omit `limit` retain the default page.
- `root` echoes the sole scoped safe root. Unscoped fallback searches return an empty `root`, as `api_file_search` constructs the response.
- Ranking is by fuzzy score, then **files before directories** on an equal score, then shorter name, then recency. The file bias keeps directory entries from crowding out the file a user is most likely searching for; `FileIndex.search` and `api_file_search` apply the same ordering, pinned by `test_index_files_outrank_dirs_on_equal_score`.

### Result sourcing

Two paths produce results:

1. **In-memory index fast path.** Used when the request resolves to one safe scoped root and that root's `FileIndex` is ready and untruncated. `api_file_search` selects it and `FileIndex.search` applies the `kinds` filter and ranking.
2. **Per-request walk fallback.** Used otherwise. `api_file_search` gives files and directories independent scanned-entry and candidate budgets, scans files first at each level, and applies an independent directories-entered ceiling. The independent budgets prevent one kind from starving the other, while the traversal ceiling guarantees a narrow, deep tree terminates even after a kind's collector is done; `test_files_and_dirs_have_independent_scan_budgets`, `test_many_matching_dirs_do_not_starve_files`, and `test_walk_stops_at_overall_scan_ceiling` pin those invariants.

### Scope and containment

`api_file_search` treats a caller-supplied `project` as the search root after canonicalization; it is not constrained to a configured workspace root. A `workspace` resolves only to its configured workspace directory. When neither establishes a root, the endpoint searches an existing `KIROCREW_PROJECT_DIR` and the Kiro Crew workspace, but never treats bare home as an implicit fallback; `test_fallback_does_not_use_home` and `test_explicit_home_project_still_searched` pin that distinction.

The walk starts at each selected root and the endpoint accepts no descendant path parameter to resolve against it. This is not a general root-containment guard: `api_file_search` resolves each candidate only for the sensitive-path check, so a non-sensitive symlink target outside the selected root can remain a result. A sensitive symlink target is rejected on its canonical path; `test_index_file_symlink_resolved_before_sensitive_check` and `test_walk_fallback_file_symlink_resolved_before_sensitive_check` pin that refusal.

Both result paths exclude dot-prefixed **files**, directories named by shared `file_index._SKIP_DIRS`, and candidates whose resolved path is sensitive. Dot-prefixed directories that are not in `_SKIP_DIRS` remain candidates but are not descended into, preserving useful configuration-folder matches without recursively exposing their contents; `test_index_offers_dot_dirs_but_not_skip_dirs`, `test_index_does_not_descend_into_dot_dirs`, and `test_walk_fallback_offers_dot_dirs_but_not_skip_dirs` pin the behavior. On macOS, the index prunes TCC-gated directories when rooted at home; the unscoped fallback also prunes them, while an explicit scoped root does not. `FileIndex._walk` and `api_file_search` enforce this distinction so background and implicit search do not repeatedly trigger consent prompts.

## Security

Both file and directory candidates are resolved with `os.path.realpath` **before** the `is_sensitive_path` check, so a symlink pointing into a sensitive tree is rejected on its real path rather than its link path. `FileIndex._walk` and the fallback collector inside `api_file_search` must remain symmetric: a divergence would let a sensitive target be reachable as a file but not as a directory, or the reverse. Realpath here guards sensitive targets, not root containment; the scope rule above documents the deliberate boundary.

## FileIndex

`FileIndex` keeps an in-memory list of entries per canonical project root, rebuilds it on a background refresh loop, and shares instances across slots through `FileIndexRegistry`. `test_acquire_same_root_shares_index`, `test_release_stops_index_at_zero_refcount`, and `test_stop_cancels_refresh` pin lifecycle and ownership.

Each entry is a 6-tuple: `(path, name, relpath, size, mtime, kind)` where `kind` is `"file"` or `"dir"` and directory entries carry `size: 0`.

Directories are collected during the walk rather than derived from file paths,
so an **empty** directory is still indexed and searchable. Both files and
directories count toward the entry cap; once the cap is hit the index is marked
truncated and the fast path is disabled for that root, falling back to the
per-request walk.

`FileIndex.search(query, scorer, max_results, kinds)` applies the same
`kinds` filter and file-before-directory tie-break as the endpoint.

## Scope of this module

This document covers discovery (how the endpoint and index find files and
directories, and how the picker stages them in the composer) and the folder
reference lifecycle below.

## Folder references (composer -> wire -> render)

A folder reference is carried end to end by its composer token. The token is
the single source of truth; there is no side state.

**Composer.** An `@rel/` token (trailing slash, boundary-checked, no `@` or
whitespace in the body; URLs and slash-only bodies excluded) IS a staged
folder. The picker inserts one; a hand-typed token stages identically. Chips
in the preview strip derive from the tokens in the input
(`parseDirTokens`), so inserting a token stages the chip, deleting the token
by any means unstages it, and the per-slot text draft persists staged folders
across slot switches and reloads for free. The chip's remove control strips
exactly its token (boundary-checked, so a longer sibling token survives).
Picker-picked FILES record their inserted `@rel` token too, and the file
chip's remove strips it — the same remove contract for both chip kinds.
Uploaded/dropped files have no token and keep a state-only remove.

**Wire.** On send, each `@rel/` token is rewritten in the
LLM-facing text to `[attached_dir N] /abs/path` — absolute via the slot's
project root (`dirFullPath`; a rel that is already absolute passes through,
and with no project the rel path is used as-is). N is the 1-based appearance
order and indexes `meta.dirs[N-1]`, the ordered absolute paths persisted on
the message. The display text keeps the `@rel/` tokens — the same
fresh-vs-wire split files use with `[attached_file N]` + `meta.files`. The
server persists `meta` opaquely (`_redact_meta` filters values, not keys), so
no backend change is involved. Steer deliberately does NOT serialize: its
transport is text-only (no meta), so a marker would have no `meta.dirs` index
to replay against and a spaced path would truncate under the `\S+` fallback —
the raw `@rel/` token stays correct there.

**Render.** `resolveDirSegment` (in `utils/fileTokens.ts`, which owns the
attachment-marker wire format for files and folders alike) rewrites markers
back to `@label/` display
tokens — lossless for paths with spaces via the meta index — and maps fresh
`@rel/` tokens to their meta path. Labels are basename-first (separators
normalized, so Windows paths label by segment) and widen by parent segments
on collision (shared `buildFileLabels` rule, applied to the
staged preview strip as well). The bubble renders each token as an inline
chip: folder icon, label, full path in the tooltip. Clicking the chip opens
the directory in the side panel's file tree via the same folder-open handler
assistant-message directory chips use; shift-click reveals it in the OS file
manager. Folders
never render as block cards: a folder is a path reference, not an upload,
and its token is by construction present in the text. A message containing a
folder reference renders its text as inline spans, so block markdown in it
shows literally — the same trade-off inline file mentions make.

## Key Files

| File | Role |
|---|---|
| `src/kiro_crew/dashboard/handlers/files.py` | `api_file_search` endpoint, fuzzy scorer, walk fallback |
| `src/kiro_crew/dashboard/file_index.py` | `FileIndex`, `FileIndexRegistry` |
| `website/src/components/FilePickerMenu.tsx` | Picker UI, `kind` propagation, trailing-slash insertion |
| `website/src/components/ChatInput.tsx` | Composer wiring, pending file/folder preview strip |
| `website/src/utils/fileTokens.ts` | Attachment-marker owner: file AND dir token parse/serialize/resolve |
| `website/src/pages/chat/useChatPageComposerController.tsx` | Token-derived staging |
| `website/src/pages/chat/useChatPageActionsController.ts` | Send/steer serialization |
| `website/src/pages/chat/ChatPageMessageContent.tsx` | Bubble chips |

## Tests

| File | Coverage |
|---|---|
| `test/test_file_search.py` | Endpoint behaviour, scoring, exclusions |
| `test/test_file_index.py` | Index build, refresh, registry refcounting |
| `test/test_file_search_dirs.py` | Directory results, `kinds` filter, independent scan budgets, dirs-visited ceiling, symlink security |
| `website/src/test/FilePickerMenu.dirs.test.tsx` | Folder rows, selection payloads, trailing slash |
| `website/src/test/ChatInput.dirStripHeight.test.tsx` | Preview-strip height compensation for a folders-only strip |
| `website/src/test/fileTokens.dirs.test.ts` | Token parse/serialize/resolve units, label widening, lossless spaced paths |
| `website/src/test/ChatPage.dirStaging.test.tsx` | Token-derived staging, per-slot draft survival, remove parity, send serialization + `meta.dirs` |
| `website/src/test/renderUserContent.dirs.test.tsx` | Bubble chips: fresh, replay, mixed file+dir, paste-adjacent |
