# File Search Module

## Overview

This module owns three endpoints. `GET /api/file-search` finds files and
directories by NAME and backs the `@`-mention picker; `POST /api/file-grep` finds
them by CONTENT and backs the chat side panel's Files tab; `GET /api/path-complete`
lists ONE directory level and backs the composer's shell-style `./` completion.
They share `handlers/files.py`, the sensitive-path fence and the off-loop probe
discipline, and nothing else: separate roots, separate budgets, separate result
shapes.

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

### `GET /api/path-complete`

One directory level of a project, for the composer's `./` / `../` completion
(`FilePickerMenu` in `pathMode`). Same row shape as `/api/file-search`, so the
picker renders both unchanged.

| Param | Required | Description |
|---|---|---|
| `path` | yes | A project directory, matched against the gateway's own known-project allow-list (`_match_known_project_for`, the same one `api_project_git` and `api_project_tree` use). The matched SERVER-HELD value is what gets resolved. An unrecognised directory is 403 `unknown_project_dir`; an absent `path` is 400 `path_required`. |
| `dir` | no | The relative directory prefix the user has typed (`./`, `../src/`), joined onto that root. Never expanded, so an absolute or `~`-prefixed value fails containment rather than being honoured. |
| `q` | no | Partial entry name. Prefix match, case-insensitive, no minimum length — `./` alone is already an unambiguous request for a listing. Dot-prefixed entries are offered only when `q` itself starts with a dot, as a shell does. |

Response: `{"results", "root"}`, plus `outside: true` on the one answer whose cause
the caller cannot infer (see the two empty states below). Rows are
`{"path", "name", "kind", "size", "mtime"}`, directories first
then alphabetical, capped at `_PATH_COMPLETE_MAX_ENTRIES`; the scan is bounded
independently by `_PATH_COMPLETE_MAX_SCAN` entries examined, which is what keeps
a `node_modules`-sized directory cheap. Rows are NOT redacted, like the search
endpoint's and unlike the tree listing's: the name the picker inserts has to be
the real one for the path to resolve.

**Containment is the whole point of the separate endpoint.** `?project=` on
`/api/file-search` is any path on the host by design, which is exactly what a
`../` token must not become. Here the token is resolved LEXICALLY against the
allow-listed root — never by resolving a path, for the reason the next paragraph
gives — so a `..` run is judged on where it ENDS: it lists whenever it comes back
inside the project (`../<project-name>/`), and names nothing when it ends anywhere
else. That refusal is answered with the empty result set rather than an error,
because the user is still mid-token, and it is recorded in the SEL audit.
`test/test_path_complete.py` pins the parent-run refusal, the re-entering parent
run, the absolute and UNC-shaped `dir` refusals, and a link never being offered,
separately.

**Two empty states, because zero rows has two causes.** A token that resolved out
of the project lists nothing BY RULE, however full that directory is, so the
composer must not answer it with "No matching files" — that asserts something false
about a directory the user just named. The verdict travels in the payload as
`outside: true`, beside the rows it describes, because this endpoint reaches it to
serve the request at all; the picker reads it rather than re-deriving a second
spelling of the same rule that could disagree with the answer on screen.

**`~/` is deliberately absent.** Bare `$HOME` is not a search root anywhere in
this module (see the fallback-roots note under scope below), so the composer's
matcher does not recognise a `~/` token at all; `matchPathToken` in
`components/composerTokens.ts` pins that.

A NUL in either `dir` or `q` is screened at the boundary and answered as an empty
listing: no path can contain one, and the resolver raises `ValueError` rather than
`OSError` for it, which would otherwise be a 500 on caller input.

**Nothing caller-supplied is ever RESOLVED, and that is the security design.**
`os.path.realpath` on Windows opens the final path, so resolving a path whose link
target is `\\host\share` is itself an outbound SMB authentication that
authenticates as the gateway process — and a screen placed before the resolve only
narrows the window in which a same-UID writer (an agent working in that very
project) can swap a link into it. Three earlier rounds of this endpoint screened
one more thing before the resolve and each left a smaller window; the class ends by
removing the resolve.

So containment is decided LEXICALLY by `_completion_segments`, which walks the
typed prefix from the project root's own segments — an absolute, drive-absolute or
UNC-shaped `dir` (`hooks.is_unc_shape`), or a `..` run that ends up elsewhere,
names nothing under the root — and the directory is then reached by
`_open_completion_dir`, which opens ONE COMPONENT AT A TIME and refuses to follow a
link at any of them (`O_DIRECTORY | O_NOFOLLOW` relative to the parent descriptor
on POSIX, which is atomic; `platform_compat.pin_directory` per component on
Windows, where the refusal at each name carries the property instead). The target
is therefore under the root by construction rather than by a check, and a link
planted in any window is refused rather than followed. `test_path_complete.py` pins
the invariant directly: the resolver is replaced with a tripwire that fails if it
is ever handed a path below the project dir.

Two smaller rules carry the same idea, and both exist because a check and the OS
can disagree about one string: a Windows component is refused when trailing dots or
spaces would be stripped from it (`".. "` is not `".."` to the resolver but is to
Win32, and those names are unopenable on Windows anyway; the rule sees ordinary
names only, since `.` and `..` are handled before it), and the sensitive-path
fence uses `is_sensitive_resolved_path` rather than `is_sensitive_path` — the
latter canonicalises what it is handed, so the fence itself would have been the
outbound SMB call, running before the no-follow open that removes the window. Its
contract wants a canonical input and gets one: the root is `realpath`'d, the walk
proves every component is not a link, and a link entry is never offered. Entry
metadata is read with `follow_symlinks=False` for the same reason.

**The directory is identified by its DESCRIPTOR, not by the name it was opened
by.** A path string is not a single name: on Windows an 8.3 alias (`SSH~1`) is a
second name the filesystem keeps for the same directory, so no lexical fence can
see that `./SSH~1/` IS `.ssh`, and another string rule would only rename the
problem. After the walk, `pinned_fs.fd_real_path` gives the kernel's own answer for
the descriptor already held — the documented containment witness for exactly this
shape — and that name is what the containment and sensitive-path checks judge, and
what every entry path is built from. It fails CLOSED: a host that cannot answer
leaves nothing to validate, so the request is refused rather than served on the
caller's spelling.

**A link is never offered, because it can never be entered.** The walk refuses to
follow one, so completing into a link would fail on the next keystroke — offering
it would be offering a dead end. That single rule replaces every question about
where a link points (out of the project, at a `\\host\share`, or through a chain
into either) and answers all of them without resolving anything. With no link in
the walked path or at the entry's own name, the entry's path IS its canonical path,
so the sensitive-path fence is exact without a resolution too.

A refusal and "nothing is there" are ONE answer to the caller and TWO audit facts,
so the listing distinguishes them internally (`refused` vs `missing`) and each
writes its own SEL line — a link or non-directory at a component is a `denied`
record, an absent one an `allowed` record with zero results. Without the split, the
interesting case was the one that logged nothing.

Off-loop like its siblings: the listing runs through `_run_path_probe` on the
TRANSFER pool, because `dir` makes the resolved directory caller-influenced even
though the root is server-held.

### `POST /api/file-grep`

Content search under one directory, for the chat side panel's Files tab
(`FileBrowserRail`, Content mode). Answers "which files under this root CONTAIN
this text", one row per file.

| Param | Required | Description |
|---|---|---|
| `root` | yes | Directory to search, validated by `_grep_resolve_root` off the event loop. A sensitive path is 403 `sensitive_path`; a file rather than a directory is `not_a_directory`. |
| `q` | yes | Literal query. Outside `_GREP_MIN_QUERY_CHARS`..`_GREP_MAX_QUERY_CHARS`, or containing a line break, the endpoint answers the EMPTY payload rather than an error — for a line break that is also the true answer, since both engines are line-oriented. |

Response: `{"results", "truncated", "engine", "skipped_docs", "root"}`. Each result
is `{"file", "line", "preview", "label"}`; `line` is 0 for a document hit, which
has no line to reveal, and `label` names a place inside the document (`p 2`,
`slide 7`, `Costs · row 12`) or is empty where the format has none, as `.docx`
does not.

**Two engines, one contract.** A text pass runs `rg --json` where the host has a
ripgrep this endpoint may run, and an equivalent python walk where it does not.
ripgrep is pinned to the fallback's semantics rather than its own defaults, and
each flag closes a divergence: `--fixed-strings` (both python passes match
`re.escape(query)`), `--ignore-case` rather than `--smart-case` (both fold case
unconditionally), `--no-ignore` (`os.walk` cannot honour ignore files),
`--max-filesize` and `--max-count 1` to match the fallback's own ceilings, and
`--no-config`, because `RIPGREP_CONFIG_PATH` can carry `--pre=<binary>` and the
child inherits this process's environment. Document containers are excluded with
`--iglob`, not `--glob`: the document pass claims a file by
`splitext(name)[1].lower()`, so a case-sensitive exclusion leaves `REPORT.DOCX` in
ripgrep's pass and the file is reported twice. Directory skips stay
case-sensitive, matching the walk's own exact-name screen — extensions are folded
on both sides, directory names on neither. Every glob is negated, which is
load-bearing: one non-negated glob flips ripgrep's whole set into allowlist mode.

**The pattern channel is stdin.** A child's arguments are readable by other
accounts through `/proc/<pid>/cmdline` and `ps`, and the query is secret-class
text — it is redacted before every SEL write, because "which file holds this key"
is an ordinary reason to type a credential into a search box. `--file -` hands the
pattern over stdin instead, so the argv carries the flags and the root only. That
is also why a query containing a line break is refused: `--file` is
line-delimited, so two lines would become two patterns OR-ed together while the
python pass matches one literal.

**The binary is vetted by the shared chokepoint.**
`github_runner.validate_provider_executable`, the same one `gh`, `glab`, `az` and
`aws` resolve through, so the trust policy stays single-sourced. `rg` takes the
default relaxed policy — it is handed no provider credentials — and any refusal
takes the python engine rather than failing the search.

**Sensitive exclusions are anchored under the root.** `is_sensitive_path` is
HOME-anchored, so `~/.npmrc` is a credential store and a project's own `.npmrc` is
an ordinary file the python walk searches. Each entry is therefore resolved to its
absolute home path and emitted only when it truly lies inside the tree being
searched, as `!/<relative>` — with the leading slash, because ripgrep follows
gitignore semantics under which a slash-less pattern matches a basename at any
depth. A root outside HOME emits no exclusions and loses nothing.

**One budget, and partial answers say so.** Both passes share a single wall-clock
deadline. `truncated` reports that the answer is short and `skipped_docs` is a
FLOOR of documents the budget did not reach, so "no matches" and "the search
stopped" stay different facts. ripgrep's records are read on a thread through a
bounded queue, because ripgrep prints nothing for a non-matching file and an
inline read could not observe the deadline until it chose to speak; a record over
`_GREP_RG_MAX_RECORD_BYTES` is skipped and marks the answer short rather than
being truncated into invalid JSON; and a dead reader with an empty queue ends the
search, since the end-of-stream sentinel is a non-blocking put that a full queue
drops.

**Document extraction is deadline-bounded and re-parsed per request.** A
document pass extracts `.docx`/`.pptx`/`.xlsx`. The character cap bounds TEXT, not
work: a workbook of empty rows produces none, so the worksheet row loop samples
the deadline every `_GREP_ROW_DEADLINE_STRIDE` rows. A parse that did not see all
of a document's text — deadline, mid-read failure, or the character cap — marks
the answer `truncated`. There is no extraction cache: the shared 2 s budget
already bounds what one keystroke can cost, and a cache keyed by content had to
carry the partial-parse flag with it to stay honest.

**`.pdf` is deliberately absent.** Extracting PDF text has no memory ceiling this
process can enforce: `pdfplumber` exposes no length limit, and the allocation is
the parsed character list itself, so any check runs after the memory is already
committed — a 25 MB input can decompress to orders of magnitude more text.
Without the extension a PDF is not a document to this pass, and both engines then
skip it as binary (ripgrep by its own detection, the python walk by its NUL
sniff); `test_a_pdf_is_not_searched_at_all` pins that, with a `.docx` beside it so
the assertion cannot pass by finding nothing. Restoring PDF needs a
resource-bounded extractor, which belongs with the identical exposure in
`knowledge/readers.py:_read_pdf` rather than in this module alone.

**Every string a row carries is redacted**, asserted as a rule over the row rather
than field by field: preview, label and path. The path uses the same
`redact_path_segments` the tree and git listings use, so a clean path stays
byte-for-byte openable.

Off-loop like its sibling: root validation and the search both run through
`_run_path_probe`, and the search takes a TRANSFER pool slot rather than a probe
slot because it holds its worker for the length of the search.

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

This document covers discovery -- how the two endpoints and the index find
files and directories by name and by content, and how the picker stages them in
the composer -- and the folder reference lifecycle below.

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
| `src/kiro_crew/dashboard/handlers/files.py` | `api_file_search` endpoint, fuzzy scorer, walk fallback; `api_file_grep` endpoint, rg argv + stdin pattern channel, python fallback, document pass |
| `website/src/pages/chat/FileBrowserRail.tsx` | Files tab: Name/Content toggle, result rows, status row |
| `website/src/api/fileGrep.ts` | `/api/file-grep` client and result types |
| `src/kiro_crew/dashboard/file_index.py` | `FileIndex`, `FileIndexRegistry` |
| `website/src/components/FilePickerMenu.tsx` | Picker UI, `kind` propagation, trailing-slash insertion, `pathMode` |
| `website/src/components/composerTokens.ts` | Caret-relative `@` / `$` / `./` token matchers and the shared token replace |
| `website/src/components/ChatInput.tsx` | Composer wiring, pending file/folder preview strip |
| `website/src/utils/fileTokens.ts` | Attachment-marker owner: file AND dir token parse/serialize/resolve |
| `website/src/pages/ChatPage.tsx` | Token-derived staging, send/steer serialization, bubble chips |

## Tests

| File | Coverage |
|---|---|
| `test/test_file_search.py` | Endpoint behaviour, scoring, exclusions |
| `test/test_path_complete.py` | Directory listing, prefix + dot-entry rules, cap, the containment refusals (`../` escape, absolute `dir`, symlink out, an entry pointing out), the re-entering `../` run, and the swap-after-validation race |
| `website/src/test/ChatInput.pathTrigger.test.tsx` | The `./` trigger: scoping per token, Tab/Enter accept, directory re-open, the debounce and placeholder windows (an accepted row is always rebuilt on the prefix that produced it), the out-of-project empty state, Escape, no `~/`, no menu without a project |
| `website/src/test/composerTokens.test.ts` | Token matchers and detection↔insertion span agreement |
| `test/test_file_grep.py` | Engine parity, the stdin pattern channel, anchored exclusions, deadline-bounded extraction, row redaction |
| `website/src/test/FileBrowserRail.test.tsx` | Toggle default and remount, request floor, status row, document note, project-switch invalidation |
| `website/src/test/fileGrep.test.ts` | The wire call: path, URL-encoded `root`/`q`, body returned as-is, transport errors surface |
| `test/test_file_index.py` | Index build, refresh, registry refcounting |
| `test/test_file_search_dirs.py` | Directory results, `kinds` filter, independent scan budgets, dirs-visited ceiling, symlink security |
| `website/src/test/FilePickerMenu.dirs.test.tsx` | Folder rows, selection payloads, trailing slash |
| `website/src/test/ChatInput.dirStripHeight.test.tsx` | Preview-strip height compensation for a folders-only strip |
| `website/src/test/fileTokens.dirs.test.ts` | Token parse/serialize/resolve units, label widening, lossless spaced paths |
| `website/src/test/ChatPage.dirStaging.test.tsx` | Token-derived staging, per-slot draft survival, remove parity, send serialization + `meta.dirs` |
| `website/src/test/renderUserContent.dirs.test.tsx` | Bubble chips: fresh, replay, mixed file+dir, paste-adjacent |
