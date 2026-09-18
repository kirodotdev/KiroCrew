# Design Document

## Overview

Folder steering directories let a sidebar folder declare extra steering roots that every
chat under it inherits. The Folder_Store half (field, validation, accumulative resolver) and
the Folder_Config_Modal half already exist in the working tree and are kept as built. This
design replaces the *delivery* half.

The working tree today delivers folder steering through the harness layer: the chat slot
caches a `steering_dirs` list, `AcpRuntime` carries it onto `SpawnContext`, and the kiro
harness declares the documents as `native_context_documents`. That path is a no-op on every
provider — `native_context_documents` is a dedup *record* the Context_Builder uses to strip
bodies the host already loaded, not a transmission channel — and Codex, KAS and every future
config-authored harness never look at the field at all. The one path that would have
transmitted anything (`_load_steering_resources(extra_dirs)` in `build_session_context`) is
gated on `is_cc and not is_custom`.

The redesign moves delivery to the single layer every provider passes through: the
Context_Builder. The Chat_Runner resolves a slot's Effective_Steering_Dirs live from the
committed folder tree and hands them to `build_message`; the Context_Builder reads the
always-inclusion markdown once, dedups it, caps it, and places it in the session-start prompt
(non-member chats) or the member essentials envelope (member chats). Harnesses, `SpawnContext`
and `AcpRuntime` lose every folder-steering reference. Per-provider branching on this path is
gone by construction.

Key design decisions:

| Decision | Choice | Rationale |
|---|---|---|
| Delivery layer | `ContextBuilder.build_message` → `build_session_context` / `_build_v2_essentials` | The only code every provider, agent and mode passes through (Req 3, 4). The harness layer is enumerated-seam-based and explicitly warns against per-host checks ("one hole per host nobody named"). |
| Transport | Prompt text (session-start context), not native launch documents | `native_context_documents` is a dedup record, not a channel. Prompt text reaches kiro, CC, Codex, KAS and config-authored hosts identically. |
| Reader | One new pure module `kiro_crew/folder_steering.py` used by both the non-member and member paths | One reader means one admissibility check, one inclusion rule, one dedup, one skip rule (Req 3.5–3.9). `member_essential_context.py` returns to baseline, deleting the `extra_steering_dirs` / `folder_steering_documents` / `_is_within` additions. |
| Resolution time | Live, from `state.read_folders` via `slot.folder_id`, at Fresh_Session and Reinjection_Turn only | Editing a folder applies to existing chats at their next fresh session with no cache to go stale (Req 5). No `_ChatSlot.steering_dirs`. |
| Admissibility | `steering_target_admissible(resolved, base=<steering dir>)` per file | The existing steering gate, with the declared directory as the trust base, so a symlink can never read outside the root the operator pointed at (Req 3.5, 7.4). |
| Double-load guard | Skip any file whose realpath is under `<project>/.kiro/steering` or `~/.kiro/steering` | Every provider path already delivers project and global steering; re-sending them would double the tokens (Req 3.8). |
| Cap | Non-member: `caps.steering` when `skills.lazy_load`, else the shared ceiling, with the existing `[steering truncated]` marker. Member: envelope's `_MAX_DOCUMENTS` / `_MAX_SOURCE_BYTES` bounds | Mirrors the existing CC steering block and the existing member envelope exactly (Req 3.7). |
| Compaction | Re-inject under `[REINJECTED AFTER COMPACTION — folder steering]` with `_neutralize_structural_markers` on the payload | Same shape as the skills-index and response-preferences reinjection blocks; the fresh-session tail is already scrubbed, the reinjection path is not (Req 6). |
| Failure mode | Missing dir / unreadable file → debug log, skip. Resolver error → warning, no steering. Member bounds errors keep their fail-closed behaviour | Graceful for the operator's stale paths (Req 7); the member envelope's own bounds are unchanged by this feature (Req 3.7). |
| Trust posture of the bodies | Same as project steering: operator-authored, appended to the session-context tail | The tail already runs through `_neutralize_structural_markers` on the fresh-session path; the reinjection path applies it explicitly. |

## Architecture

```
  folders.json ──► Folder_Store (chat_folders.py)
                     │  _validate_steering_dirs      (per entry: project_dir contract, 16 cap, dedup)
                     │  _resolve_folder_steering_dirs (root-first union, re-validate, cycle-guarded)
                     ▼
  Chat_Runner (chat_runner.py, dashboard turn)
     if (_context_is_new and not _provider_has_history) or _needs_reinjection:
        dirs = resolve from state.read_folders(...) via slot.folder_id     ← live, no slot cache
        on error: logger.warning(slot, error); dirs = ()
     build_message(..., steering_dirs=dirs)
                     │
                     ▼
  Context_Builder (context.py)                        ┌─ folder_steering.py ─────────────────┐
     build_message(steering_dirs)                     │ collect_folder_steering(dirs, project) │
       ├─ member:  _build_v2_essentials(steering_dirs)│   enumerate **/*.md per dir (sorted)   │
       │             documents += collect(...)   ◄────┤   skip under project/.kiro/steering,   │
       │             (envelope bounds apply)          │        ~/.kiro/steering                │
       ├─ fresh:   build_session_context(steering_dirs)   skip inclusion manual/auto/fileMatch  │
       │             parts += render(collect(...)) ◄──┤   admit: steering_target_admissible(   │
       │             (caps.steering / ceiling)        │          resolved, base=dir)           │
       └─ reinject: parts += "[REINJECTED … folder    │   realpath dedup across dirs           │
                    steering]" + neutralize(render)   │   missing dir / read error → debug,skip│
                     │                                └───────────────────────────────────────┘
                     ▼
  prompt text ──► provider client (kiro-cli | Claude Code | Codex | KAS | config-authored)

  Harness layer (acp/harness/*, acp/runtime.py, SpawnContext): NO folder-steering reference.
```

The Steering_Resolver and the Folder_Config_Modal are unchanged from the working tree. The
only Chat_Runner touch is the resolution + one keyword argument. Everything under
`acp/` returns to its `main` baseline.

## Components and Interfaces

### Folder_Store — `src/kiro_crew/dashboard/chat_folders.py` (kept as built)

- `MAX_FOLDER_STEERING_DIRS = 16`
- `_validate_steering_dirs(value: object) -> tuple[list[str], str | None]` — Req 1.1–1.7.
- `_resolve_folder_steering_dirs(folders, folder_id) -> tuple[list[str], str | None]` —
  Req 2.1–2.7. Walk contract matches `_resolve_folder_project_dir` (stop at cycle or
  missing folder, return what was collected).
- Create / update endpoints reject with `400` + `steering_dirs_invalid`; persist the resolved
  realpath spelling; omit the key when empty (Req 1.6, 1.7).

### Steering reader — `src/kiro_crew/folder_steering.py` (new)

```python
def collect_folder_steering(
    steering_dirs: Sequence[str],
    *,
    project: str | None,
    home: Path | None = None,
) -> list[tuple[str, str]]:
    """(source_path, body) for every always-inclusion *.md under the folder dirs."""

def render_folder_steering(documents: list[tuple[str, str]]) -> str:
    """Prompt section: header line, then '# <path>\\n<body>' per document; '' when empty."""
```

Behaviour of `collect_folder_steering`, in order, per directory:

1. `root = Path(dir).expanduser()`; `root_resolved = root.resolve()`. If `root_resolved` is
   not a directory → `logger.debug(...)`, skip the directory (Req 7.1).
2. Enumerate `sorted(root_resolved.glob("**/*.md"))`.
3. For each file: `resolved = p.resolve()`. Skip when `resolved` is under
   `Path(project).resolve() / ".kiro" / "steering"` (when `project` is set) or under
   `home / ".kiro" / "steering"` (Req 3.8). Skip when `resolved` already emitted (Req 3.9).
4. Admit with `steering_target_admissible(resolved, base=root_resolved)`; skip on failure
   (Req 3.5, 7.4). The trust base is the declared directory, never `$HOME`.
5. Read with `safe_read_file(str(resolved))`, bounded to `_MAX_SOURCE_BYTES` characters;
   `PermissionError` / `OSError` / `UnicodeDecodeError` → `logger.debug(...)`, skip
   (Req 7.2).
6. `split_frontmatter(body, STEERING_LOADER)`; when `inclusion` (case-folded) is one of
   `manual`, `auto`, `filematch` → skip (Req 3.6). Any other value, including absent, is
   treated as `always` and the body **without frontmatter** is emitted.
7. Append `(str(resolved), body)`.

The function is pure with respect to the process (no config load, no state), which is what
lets it serve the member path and the non-member path identically and be property-tested.

`render_folder_steering` produces:

```
[FOLDER STEERING — standards inherited from this chat's folder. Follow these as you would project steering.]
# /abs/path/one.md
<body>

# /abs/path/two.md
<body>
[END FOLDER STEERING]
```

### Context_Builder — `src/kiro_crew/context.py`

Signatures (the `steering_dirs` keyword already exists on `build_session_context` and
`build_message`; it is added to `_build_v2_essentials`):

```python
def _build_v2_essentials(self, memory_store, *, member="", project=None, workspace=None,
                         blocks_reads=False, context_groups=None, profile_overrides=None,
                         native_documents=None, native_envelope_out=None,
                         execution_template="", conditional_index=False, trigger_text="",
                         steering_dirs: tuple[str, ...] = ()) -> str

def build_session_context(self, ..., member="", steering_dirs: tuple[str, ...] = (),
                          _v2_essentials=None) -> str

def build_message(self, text, is_new_session, session_key=None, ..., needs_reinjection=False,
                  ..., steering_dirs: tuple[str, ...] = ()) -> tuple[str, HookResult]
```

Behaviour:

- `_load_steering_resources()` reverts to its `main` signature and body (agent-resource
  loader only). The CC-only block that calls it is untouched and no longer receives
  `steering_dirs`.
- `_build_v2_essentials`: when `steering_dirs` is non-empty and the project group is
  included and reads are not blocked, append `collect_folder_steering(steering_dirs,
  project=project)` to `documents` **after** `documents_for_member(...)` (so global and
  project steering keep precedence) and **before** the memory files. Apply the envelope's
  `_MAX_DOCUMENTS` check by raising `MemberEssentialContextError` exactly as
  `documents_for_member.add` does (Req 3.4, 3.7). Because `kiro_launch_documents` no longer
  sees the folder dirs, none of these sources match `native_documents`, so the native
  envelope keeps their bodies (Req 4.4).
- `build_session_context`: after the existing CC steering block and before thread history,
  when `steering_dirs` is non-empty, `not essentials` (member chats carry it in the envelope),
  and the project group is included: `folder_ctx = render_folder_steering(collect_folder_steering(...))`;
  if `lazy_skills and len(folder_ctx) > caps.steering` truncate at the cap and append
  `"\n...[steering truncated]\n"`; `parts.append(folder_ctx)`. No `is_cc`, no `is_custom`
  gate (Req 3.1–3.3, 3.7). The section sits inside the session-context tail that the
  existing code already passes through `_neutralize_structural_markers`.
- `build_message`: thread `steering_dirs` into both `_build_v2_essentials` calls and the
  `build_session_context` call (Req 4.5). In the `not is_new_session and needs_reinjection`
  block, after the response-preferences reinjection and when `steering_dirs` is non-empty and
  `not _essentials`: build the same `folder_ctx`, apply the same cap, and append
  `"[REINJECTED AFTER COMPACTION — folder steering]\n" + _neutralize_structural_markers(folder_ctx) + "\n[END REINJECTED]\n\n"`
  (Req 6.1, 6.2). Member chats already re-receive the envelope on every non-fresh turn
  (`if _essentials and not is_new_session: parts.append(_essentials)`), so their folder docs
  ride that existing lifecycle and need no separate reinjection block.
- On warm turns that are neither fresh nor reinjection, nothing on the non-member path
  reads or appends folder steering (Req 3.10). The member envelope's per-turn delivery with
  provider-side acknowledged-snapshot suppression is pre-existing behaviour and is not
  changed by this feature.

### Chat_Runner — `src/kiro_crew/dashboard/chat_runner.py`

Immediately after `_needs_reinjection = state.sessions.consume_needs_reinjection(session_key)`
and before `run_in_embed_pool(state.context_builder.build_message, ...)`:

```python
_folder_steering_dirs: tuple[str, ...] = ()
if slot.folder_id and ((_context_is_new and not _provider_has_history) or _needs_reinjection):
    _folder_snapshot = await state.read_folders(lambda folders: [dict(f) for f in folders])
    _resolved_dirs, _steering_err = await asyncio.to_thread(
        _resolve_folder_steering_dirs, _folder_snapshot, slot.folder_id
    )
    if _steering_err:
        logger.warning("Folder steering unavailable for slot %s: %s", slot.name, _steering_err)
    else:
        _folder_steering_dirs = tuple(_resolved_dirs)
```

and `steering_dirs=_folder_steering_dirs` is passed to `build_message` (Req 4.5, 5.1–5.4,
7.3). A slot with no `folder_id` passes `()`. The resolution reads the committed tree each
time, so a folder edit or a slot re-file is picked up at the next Fresh_Session with no
cache to invalidate.

### Removed plumbing (Req 4, 5.2)

| File | Change |
|---|---|
| `acp/harness/base.py` | Remove `SpawnContext.steering_dirs` |
| `acp/harness/kiro.py` | Remove `extra_steering_dirs=ctx.steering_dirs` kwarg and the `elif ctx.steering_dirs:` branch |
| `acp/runtime.py` | Remove the `steering_dirs` constructor parameter, `self._steering_dirs`, and the `SpawnContext(steering_dirs=...)` pass-through |
| `member_essential_context.py` | Revert to `main`: drop `_is_within`, `documents_for_member(extra_steering_dirs=)`, `folder_steering_documents`, `kiro_launch_documents(extra_steering_dirs=)` |
| `dashboard/state.py` | Remove `steering_dirs` from `_ChatSlot.__slots__` and `__init__` |
| `dashboard/chat_handlers.py` | Revert `api_chat_slot_create` (no steering resolution, no `steering_dirs_invalid` 400, no `slot.steering_dirs` assignment) and `api_chat_slot_agent` (no steering re-resolution); drop the `_resolve_folder_steering_dirs` import |
| `context.py` | Revert `_load_steering_resources` to its `main` signature/body |
| `error-code-baseline.json` | Regenerate after the slot-create 400 is removed |

After these, every harness produces the same `SpawnPlan` for a folder chat and a non-folder
chat because no harness input differs (Req 4.1–4.3).

### Folder_Config_Modal — `website/src/components/FolderConfigModal.tsx` (kept as built)

Already implements Req 8.1–8.7: the "Additional steering" section, add via the existing
directory picker (cancel leaves the list unchanged), remove, read-only inherited list from
ancestors via `resolveFolderSteeringDirs` in `utils/folderAgent.ts`, `steering_dirs` sent as
`string[]`, server error text surfaced in the modal error area with the generic fallback,
and keys in all 13 locale catalogs. Tests: `FolderConfigModal.test.tsx`,
`folderSteeringDirs.test.ts`.

### System specs — `docs/system-specs/modules/`

- `config.md`: keep the field/validation/inheritance paragraphs; replace the "Slot binding"
  bullet with the live-resolution contract (resolved by the Chat_Runner from `folder_id` at
  Fresh_Session and Reinjection_Turn; no slot field) and point delivery at the
  Context_Builder (Req 9.1).
- `providers.md`: replace the launch-document paragraph with: folder steering is read by the
  Context_Builder and placed in session-start context for every provider; harnesses and
  `SpawnContext` carry no folder-steering field (Req 9.2).

## Data Models

Folder record (`folders.json`), unchanged from the working tree:

```json
{
  "id": "f-…", "name": "Platform", "parent_id": "f-root",
  "project_dir": "/repos/platform", "default_agent": "kirocrew",
  "steering_dirs": ["/repos/org-standards/steering", "/repos/platform-standards"]
}
```

- `steering_dirs`: optional `list[str]` of resolved realpaths; absent when empty.
- `_ChatSlot`: **no** steering field. `folder_id` is the only link.
- `Effective_Steering_Dirs`: `tuple[str, ...]`, root-first, realpath-deduped, computed per
  build; never persisted.
- Steering document: `(source_path: str, body: str)` where `body` is the markdown with
  frontmatter removed.
- Prompt section: see `render_folder_steering` above; on reinjection wrapped by
  `[REINJECTED AFTER COMPACTION — folder steering]` … `[END REINJECTED]`.

## Correctness Properties

The three properties below are exercised with `hypothesis` (already a project dependency).

### Property 1: Resolver is root-first, deduplicated and cycle-safe

For any folder tree generated as a list of folder records with arbitrary `parent_id`
edges (including cycles and dangling parents) and arbitrary valid `steering_dirs` drawn
from a pool of real temporary directories, `_resolve_folder_steering_dirs(tree, id)`:

- returns no duplicates;
- returns entries in root-first order: for any two entries, the one contributed by a
  strictly-closer-to-root folder appears first, unless it was already contributed by an
  even closer folder;
- terminates and returns only directories from folders reachable before the first repeated
  or missing `parent_id`;
- returns `[]` when no folder on the walked chain lists any directory.

**Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.7**

### Property 2: Collector dedups, respects skip rules, and never escapes its roots

For any set of temporary steering directories populated with arbitrary `*.md` files
(arbitrary `inclusion` frontmatter, some nested, some duplicated by symlink, some placed
under a fake `<project>/.kiro/steering` or `<home>/.kiro/steering`),
`collect_folder_steering(dirs, project=project, home=home)`:

- emits each realpath at most once;
- emits no file whose realpath is under `<project>/.kiro/steering` or `<home>/.kiro/steering`;
- emits no file with `inclusion` in {`manual`, `auto`, `fileMatch`} (any case);
- emits only files whose realpath is under one of the declared roots (a symlink pointing
  outside is never emitted);
- is order-stable: the output for the same inputs is identical across two calls.

**Validates: Requirements 3.5, 3.6, 3.8, 3.9, 7.4**

### Property 3: Harness plans are steering-invariant

For every registered harness and any `SpawnContext`, there is no folder-steering input to
vary: `SpawnContext` has no `steering_dirs` field and `AcpRuntime.__init__` has no
`steering_dirs` parameter, so `resolve_spawn(ctx)` for a folder chat and a non-folder chat
receives byte-identical inputs. This is asserted structurally (`dataclasses.fields`,
`inspect.signature`) rather than by generation.

**Validates: Requirements 4.1, 4.2, 4.3**

## Error Handling

| Condition | Layer | Behaviour | Req |
|---|---|---|---|
| `steering_dirs` not a list of strings / relative / missing / sensitive / >16 / duplicate | Folder_Store | `400` `steering_dirs_invalid`; sensitive path also SEL-logged like `project_dir` | 1.1–1.5 |
| Stored value fails re-validation | Steering_Resolver | `([], error)` naming the directory | 2.6 |
| Resolver error at build time | Chat_Runner | `logger.warning` naming slot + error; `steering_dirs=()`; turn proceeds | 7.3 |
| Directory missing at build time | `collect_folder_steering` | `logger.debug`; directory contributes nothing | 7.1 |
| File unreadable / undecodable | `collect_folder_steering` | `logger.debug`; file skipped | 7.2 |
| File fails `steering_target_admissible` against its root | `collect_folder_steering` | skipped silently (defense in depth) | 3.5, 7.4 |
| Non-member section exceeds `caps.steering` (lazy_load on) | Context_Builder | truncate + `[steering truncated]` | 3.7 |
| Member envelope exceeds `_MAX_DOCUMENTS` | Context_Builder | `MemberEssentialContextError` (existing fail-closed member contract) | 3.7 |
| Server rejects folder save | Folder_Config_Modal | server message in error area, generic fallback | 8.5 |

No new error codes are introduced beyond `steering_dirs_invalid` on the folder endpoints;
the slot-create endpoint's `steering_dirs_invalid` branch is removed.

## Testing Strategy

Framework: `pytest` (backend, run with an explicit `-n 4 --dist loadgroup`, never the
repo's `-n auto` default), `hypothesis` for the three properties above, `vitest` for the
website.

Unit tests (backend):

- `test/test_chat_folder_steering_dirs.py` (existing, kept): validation and resolver cases.
- `test/test_folder_steering.py` (new): collector — always/absent inclusion emitted with
  frontmatter stripped; `manual`/`auto`/`fileMatch` skipped; nested files found; missing
  directory skipped with a debug log; unreadable file skipped with a debug log; symlink out
  of root refused; realpath dedup across two roots; project and global `.kiro/steering` files
  skipped; `render_folder_steering` empty for no documents; Properties 1 and 2 with
  `hypothesis`.
- `test/test_context_folder_steering.py` (new): `ContextBuilder.build_message` with a
  temporary steering dir —
  fresh session includes the section for `provider_type` in {kiro, claude-code, codex, kas}
  and for a config-authored provider type, with identical section text;
  included for `agent="kirocrew"` and for a custom agent;
  member chat (`_private_owner`) carries the documents inside the essentials envelope with
  bodies present and no `[FOLDER STEERING]` section outside it;
  warm turn (`is_new_session=False, needs_reinjection=False`) has no section;
  reinjection turn has the `[REINJECTED AFTER COMPACTION — folder steering]` block with a
  forged `[CURRENT USER REQUEST` marker in a body neutralized;
  lazy_load cap truncation appends `[steering truncated]`;
  empty `steering_dirs` produces byte-identical output to the pre-feature call.
- `test/test_chat_runner_folder_steering.py` (new): the runner passes the resolved tuple on a
  fresh session and on a reinjection turn, passes `()` for a slot with no `folder_id`, passes
  `()` and logs a warning when the resolver errors, re-resolves after the folder's
  `steering_dirs` change without a session reset, and does not read the folder tree on a
  warm turn.
- `test/test_harness_steering_invariant.py` (new): Property 3 — `SpawnContext` has no
  `steering_dirs` field; `AcpRuntime.__init__` has no `steering_dirs` parameter;
  `KiroHarness.resolve_spawn` source contains no `steering` reference;
  `kiro_launch_documents` signature has no `extra_steering_dirs`.
- `test/test_folder_steering_documents.py` (existing in tree): **deleted** — it tests the
  removed harness-side plumbing.

Unit tests (website, kept as built, verified present): `FolderConfigModal.test.tsx` covers
list render, add via picker, picker cancel leaves list unchanged, remove, inherited read-only
rows, `steering_dirs` in the save payload, server error surfaced with generic fallback;
`folderSteeringDirs.test.ts` covers the accumulative resolver.

Gates run before the change set is declared complete: `black`, `isort`, `flake8`, `mypy` on
touched Python; targeted pytest files above plus `test/test_context*.py`,
`test/test_member_essential_context*.py`, `test/test_acp_harness*.py`,
`test/test_chat_folders*.py`; the error-code baseline gate with baseline regeneration;
`tsc`, `eslint`, `i18n:check`, and the two website test files.
