# Implementation Plan: Folder Steering Directories

## Overview

Replace the harness-side folder-steering delivery in the working tree with a single
Context_Builder seam, resolved live from the folder tree by the Chat_Runner, so every
provider (kiro-cli, Claude Code, Codex, KAS, config-authored) receives folder steering
identically. The Folder_Store (field, validation, resolver) and the Folder_Config_Modal are
already built and are kept; this plan adds the reader, wires the Context_Builder and
Chat_Runner, deletes the dead plumbing, rewrites the system specs, and proves the result
with unit and property tests.

All Python gates run with an explicit `-n 4 --dist loadgroup` (or lower); the repository's
`-n auto` default MUST NOT be used on this host.

## Tasks

- [x] 1. Steering reader
  - [x] 1.1 Create `src/kiro_crew/folder_steering.py` with `collect_folder_steering` and `render_folder_steering`, and its tests
    - Implement `collect_folder_steering(steering_dirs, *, project, home=None) -> list[tuple[str, str]]` exactly per design: expanduser + resolve each root, skip a non-directory root with a debug log, `sorted(root.glob("**/*.md"))`, skip realpaths under `<project>/.kiro/steering` and `<home>/.kiro/steering`, realpath dedup across roots, admit each file with `steering_target_admissible(resolved, base=root_resolved)`, read via `safe_read_file` bounded to `member_essential_context._MAX_SOURCE_BYTES`, skip `PermissionError`/`OSError`/`UnicodeDecodeError` with a debug log, `split_frontmatter(body, STEERING_LOADER)`, skip `inclusion` in {manual, auto, filematch} (case-folded), emit `(str(resolved), body_without_frontmatter)`.
    - Implement `render_folder_steering(documents) -> str` producing the `[FOLDER STEERING — …]` header, `# <path>` + body per document, `[END FOLDER STEERING]`; return `""` for no documents.
    - Write `test/test_folder_steering.py`: always/absent inclusion emitted with frontmatter stripped; manual/auto/fileMatch skipped; nested files found; missing directory skipped and debug-logged (caplog); unreadable file skipped and debug-logged; symlink resolving outside its root refused; realpath dedup across two roots; files under a fake project `.kiro/steering` and fake home `.kiro/steering` skipped; renderer empty for no documents.
    - Add Property 1 (resolver root-first / dedup / cycle-safe, driving `_resolve_folder_steering_dirs` with hypothesis-generated folder trees over real tmp directories) and Property 2 (collector dedup / skip rules / root containment / order stability) as hypothesis tests in the same file with `max_examples` ≤ 50.
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.7, 3.5, 3.6, 3.8, 3.9, 7.1, 7.2, 7.4_

- [x] 2. Delete harness-side plumbing
  - [x] 2.1 Remove every folder-steering reference from the harness, runtime, slot and member-essentials layers, and assert the invariant
    - `src/kiro_crew/acp/harness/base.py`: remove `SpawnContext.steering_dirs`.
    - `src/kiro_crew/acp/harness/kiro.py`: remove `extra_steering_dirs=ctx.steering_dirs` and the `elif ctx.steering_dirs:` branch (restore the `main` shape of `resolve_spawn`).
    - `src/kiro_crew/acp/runtime.py`: remove the `steering_dirs` constructor parameter, `self._steering_dirs`, and the `SpawnContext(steering_dirs=...)` pass-through.
    - `src/kiro_crew/member_essential_context.py`: revert to `main` (`git diff` must be empty for this file): drop `_is_within`, `documents_for_member(extra_steering_dirs=)`, `folder_steering_documents`, `kiro_launch_documents(extra_steering_dirs=)`.
    - `src/kiro_crew/dashboard/state.py`: remove `steering_dirs` from `_ChatSlot.__slots__` and `__init__`.
    - `src/kiro_crew/dashboard/chat_handlers.py`: revert `api_chat_slot_create` to the `main` folder-project block (no steering resolution, no `steering_dirs_invalid` response, no `slot.steering_dirs` writes) and remove the steering re-resolution block from `api_chat_slot_agent`; drop the `_resolve_folder_steering_dirs` import.
    - Delete `test/test_folder_steering_documents.py`.
    - Write `test/test_harness_steering_invariant.py`: `SpawnContext` has no `steering_dirs` field (`dataclasses.fields`); `AcpRuntime.__init__` has no `steering_dirs` parameter (`inspect.signature`); `inspect.getsource(KiroHarness.resolve_spawn)` contains no `steering`; `kiro_launch_documents` signature has no `extra_steering_dirs`; `_ChatSlot.__slots__` has no `steering_dirs`.
    - Run `test/test_acp_harness*.py`, `test/test_member_essential_context*.py`, `test/test_chat_folder_steering_dirs.py` and the new file with `-n 4`.
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 5.2_

- [x] 3. Chat_Runner live resolution
  - [x] 3.1 Resolve Effective_Steering_Dirs from the committed folder tree at build time and pass them to `build_message`
    - In `src/kiro_crew/dashboard/chat_runner.py`, directly after `_needs_reinjection = state.sessions.consume_needs_reinjection(session_key)`: when `slot.folder_id` and (`(_context_is_new and not _provider_has_history)` or `_needs_reinjection`), read `state.read_folders(...)` and call `_resolve_folder_steering_dirs` off-loop via `asyncio.to_thread`; on error `logger.warning("Folder steering unavailable for slot %s: %s", ...)` and use `()`; otherwise `tuple(resolved)`.
    - Pass `steering_dirs=_folder_steering_dirs` to the `build_message` call; pass `()` for a slot with no `folder_id`; do not touch the folder tree on warm turns.
    - Write `test/test_chat_runner_folder_steering.py` (patch `build_message` and the folder tree): fresh session passes the resolved tuple; reinjection turn passes it; no `folder_id` passes `()`; resolver error passes `()` and logs a warning naming the slot (caplog); a folder's `steering_dirs` edit is picked up at the next fresh session with no slot mutation; a warm turn does not read folders.
    - _Requirements: 4.5, 5.1, 5.3, 5.4, 7.3_

- [x] 4. Context_Builder delivery seam
  - [x] 4.1 Wire folder steering into `build_message`, `build_session_context` and `_build_v2_essentials`, add compaction reinjection, and test across providers
    - `src/kiro_crew/context.py`: revert `_load_steering_resources` to its `main` signature and body; the CC-only block calls it with no arguments.
    - Add `steering_dirs: tuple[str, ...] = ()` to `_build_v2_essentials`; when non-empty, reads not blocked and the project group included, append `collect_folder_steering(steering_dirs, project=project)` to `documents` after `documents_for_member(...)` and before the memory files, raising `MemberEssentialContextError("… too many documents")` past `_MAX_DOCUMENTS`.
    - In `build_session_context`, after the CC steering block: when `steering_dirs` and `not essentials` and the project group is included, `folder_ctx = render_folder_steering(collect_folder_steering(steering_dirs, project=project))`; if `lazy_skills and len(folder_ctx) > caps.steering` truncate and append `"\n...[steering truncated]\n"`; `parts.append(folder_ctx)`. No `is_cc` or `is_custom` gate.
    - In `build_message`: pass `steering_dirs` to both `_build_v2_essentials` calls and to `build_session_context`; in the `not is_new_session and needs_reinjection` block, after response preferences, when `steering_dirs` and `not _essentials`, append `"[REINJECTED AFTER COMPACTION — folder steering]\n" + _neutralize_structural_markers(folder_ctx) + "\n[END REINJECTED]\n\n"` with the same cap.
    - Write `test/test_context_folder_steering.py`: fresh session includes the section for `provider_type` kiro, claude-code, codex, kas and a config-authored name with identical section text; included for `agent="kirocrew"` and a custom agent; member chat carries bodies inside the essentials envelope and no `[FOLDER STEERING` outside it; warm turn has no section; reinjection turn has the block and a forged `[CURRENT USER REQUEST` inside a body is neutralized; lazy_load cap appends `[steering truncated]`; empty `steering_dirs` yields output byte-identical to the call without the argument.
    - Run `test/test_context*.py`, `test/test_steering*.py` and the new file with `-n 4`.
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.7, 3.10, 4.4, 4.5, 6.1, 6.2_

- [x] 5. System specs
  - [x] 5.1 Rewrite the folder-steering paragraphs in `docs/system-specs/modules/config.md` and `docs/system-specs/modules/providers.md`
    - `config.md`: keep field / validation / accumulative-inheritance bullets; replace the "Slot binding" bullet with live resolution by the Chat_Runner from `folder_id` at Fresh_Session and Reinjection_Turn, no slot field, delivery by the Context_Builder.
    - `providers.md`: replace the launch-document paragraph with: folder steering is read by `kiro_crew.folder_steering` and placed into session-start context by the Context_Builder for every provider (kiro-cli, Claude Code, Codex, KAS, config-authored); `SpawnContext`, `AcpRuntime` and harnesses carry no folder-steering field; project and global steering are skipped to avoid double delivery; compaction reinjection.
    - Run the docs lint used by CI (`make docs-lint` or the script it wraps) on both files.
    - _Requirements: 9.1, 9.2_

- [x] 6. Folder dialog verification
  - [x] 6.1 Confirm the built modal satisfies every Requirement 8 criterion and fill any test gap
    - Read `website/src/components/FolderConfigModal.tsx` and `website/src/test/FolderConfigModal.test.tsx`; add a test for picker-cancel leaving the list unchanged and for a server rejection without a message falling back to the generic save-failed string if either is missing.
    - Confirm `steering_dirs` is sent as `string[]` in the save payload test and inherited rows render read-only.
    - Run `npx tsc --noEmit`, `npx eslint` on the touched files, `npm run i18n:check`, and `npx vitest run src/test/FolderConfigModal.test.tsx src/test/folderSteeringDirs.test.ts`.
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 8.7_

- [x] 7. Integration gate
  - [x] 7.1 Run the full static and targeted test gates for the change set and regenerate the error-code baseline
    - `black --check`, `isort --check-only`, `flake8`, `mypy` on every touched Python file.
    - `pytest -n 4 --dist loadgroup` over `test/test_folder_steering.py`, `test/test_context_folder_steering.py`, `test/test_chat_runner_folder_steering.py`, `test/test_harness_steering_invariant.py`, `test/test_chat_folder_steering_dirs.py`, `test/test_context*.py`, `test/test_member_essential_context*.py`, `test/test_acp_harness*.py`, `test/test_chat_folders*.py`, `test/test_chat_handlers*slot*.py`.
    - Run the error-code contract gate and regenerate `error-code-baseline.json` so the removed slot-create response is reflected; confirm the gate passes.
    - Confirm `git diff --stat -- src/kiro_crew/acp src/kiro_crew/member_essential_context.py` is empty.
    - Fix any finding in place and re-run the failing gate.
    - _Requirements: 3.2, 4.1, 4.2, 4.3, 9.1, 9.2_

## Notes

- Tasks 1.1, 2.1, 3.1, 5.1 and 6.1 touch disjoint files and run in parallel; 4.1 depends on
  the reader from 1.1 and on 2.1 having removed the member-essentials signature it must not
  call; 7.1 runs last over the whole change set.
- Every pytest invocation MUST pass an explicit `-n 4 --dist loadgroup` (or lower). The
  repository default `-n auto` spawns one worker per core and has already crushed this
  shared host twice.
- No task commits. The spec and the change set are committed together once 7.1 is green.
- `website/` changes are already built; task 6.1 verifies, it does not redesign.

## Task Dependency Graph

```json
{"waves": [{"id": 0, "tasks": ["1.1", "2.1", "3.1", "5.1", "6.1"]}, {"id": 1, "tasks": ["4.1"]}, {"id": 2, "tasks": ["7.1"]}]}
```
