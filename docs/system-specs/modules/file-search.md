# File Search Module

## Overview

File search backs the `@`-mention picker in the dashboard chat composer. A user
types `@` followed by 2+ characters, picks a result, and the composer inserts a
token that serializes into the prompt as an attachment marker.

Results cover both **files** and **directories**. A file is an attachment whose
content reaches the agent. A directory is a **path reference only**: the agent
receives the path and explores it with its own glob/grep/read tools. No
directory listing or recursive content is inlined.

## API

### `GET /api/file-search`

| Param | Required | Description |
|---|---|---|
| `q` | yes | Query string. Fewer than 2 characters returns an empty result set. |
| `project` | no | Absolute project path to scope the search to. |
| `workspace` | no | Workspace name to scope the search to, when `project` is absent. |
| `kinds` | no | `all` (default), `files`, or `dirs`. Unrecognized values fall back to `all`. |

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
- At most 15 results are returned.
- Ranking is by fuzzy score, then **files before directories** on an equal
  score, then shorter name, then recency. The file bias keeps directory entries
  from crowding out the file a user is most likely searching for.

### Result sourcing

Two paths produce results:

1. **In-memory index fast path.** Used when the request is scoped to a single
   project and that project's `FileIndex` is ready and untruncated.
2. **Per-request walk fallback.** Used otherwise, bounded by a scan budget
   (50k entries scoped, 5k unscoped) and a collection cap. Files and directories
   are collected into separate candidate lists, each with its own cap, and files
   are scanned first at each level. A shared cap let a burst of matching
   directories fill it before the files in the same directory were examined, so
   the file-before-directory tie-break never got the chance to run.

   The per-kind budgets alone do **not** bound the walk, so an independent
   `_WALK_MAX_DIRS_VISITED` ceiling (20k directories entered) hard-stops the
   traversal. Making the per-kind budgets independent removed the single shared
   counter that used to terminate `os.walk`: on the default `kinds=all` request,
   the directory counter only advances per directory *name*, so a deep tree with
   few directories per level exhausts the file budget at the first level while
   the directory half of the done-check stays false forever. The walk then
   descends the entire tree doing no useful work — on every `@`-mention
   keystroke, in the shared executor, with an aborted request unable to stop its
   own thread.

   The ceiling counts directories **entered**, not entries scored (once a kind
   is done its collector returns immediately, so an entries-based counter stops
   advancing while traversal continues), and it is deliberately **not** derived
   from the per-kind budget: in a narrow-deep tree the directory-name count grows
   at the same rate as directories visited, so any multiple of the per-kind
   budget is unreachable in exactly the case that needs bounding.

Both paths apply the same exclusions: dot-prefixed names, a skip set
(`node_modules`, `__pycache__`, `dist`, `build`, `venv`, `env`, `out`,
`target`), and `is_sensitive_path`.

## Security

Both file and directory candidates are resolved with `os.path.realpath`
**before** the `is_sensitive_path` check, so a symlink pointing into a sensitive
tree is rejected on its real path rather than its link path. This matches the
existing precedent in `api_browse_dirs` / `api_browse_files`. The two branches
are deliberately symmetrical: a divergence would let a sensitive target be
reachable as a file but not as a directory, or the reverse.

## FileIndex

`FileIndex` keeps an in-memory list of entries per project root, rebuilt every
30 seconds on a background task, capped at 100,000 entries.

Each entry is a 6-tuple: `(path, name, relpath, size, mtime, kind)` where `kind`
is `"file"` or `"dir"` and directory entries carry `size: 0`.

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
| `website/src/pages/ChatPage.tsx` | Token-derived staging, send/steer serialization, bubble chips |

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
