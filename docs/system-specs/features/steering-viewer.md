# Steering Viewer

The Steering tab under Agent Capabilities lists, reads, creates, edits and deletes the Kiro steering files that are injected into every session.

Before this feature steering files were loaded but never surfaced: nothing in the dashboard showed which always-on conventions were in effect.

## Policy

Two locations are shown, with provenance, because two different mechanisms load them:

| Source | Location | Loaded by |
|--------|----------|-----------|
| `user` (Global) | `~/.kiro/steering/**/*.md` | kiro-cli for every session; also what the CC-backend injection in `context.py:_load_steering_resources()` reaches, since its `file://.kiro/steering/**/*.md` resource globs against `$HOME` |
| `workspace` (Workspace) | `<project>/.kiro/steering/**/*.md` | kiro-cli, because the session subprocess runs with the slot's project directory as cwd |

The active project comes from `_ChatSlot.project` via `handlers/_shared.py:active_project_dir()`, which resolves deterministically: the slot named by the request's `X-Session-Key` if it has a project, else the single project every slot agrees on, else nothing. That last case matters for mutations — with two chats open on different projects there is no defensible "active" project for a settings page, and picking the first-inserted slot would create, overwrite or delete files in the wrong project. With no project (or an ambiguous one), only the global root is offered and `POST /api/steering` with `source=workspace` is a 400.

Every steering filesystem transaction — the two-root scan, the single-file read, and each create / update / delete — runs as one complete blocking function on `discovery_executor()`. A project on a slow or network filesystem must never stall the event loop, and with it every chat and the heartbeat, for the duration of a write.

Path policy (`handlers/steering.py`):

- A file handle is `"<source>/<relpath>"`. `_split_key()` rejects a missing/unknown source, absolute paths, `~`, `..` segments, backslashes, NUL, and any suffix other than `.md`.
- Containment is anchored on the **trust base** (`$HOME` for `user`, the project dir for `workspace`), not on the steering root: `_contained()` resolves the deepest *existing* ancestor and requires it to sit at or under the resolved base. This is what rejects a `~/.kiro/steering` symlink pointing at `/etc` — comparing against the root itself would follow such a link.
- `is_sensitive_path()` gates every root, listed entry and resolved target; writes additionally gate on `is_sensitive_write_path()`.
- A symlink at the **leaf** is rejected outright and never listed — not merely one that escapes the base. `.kiro/steering/rules.md -> ../../README.md` satisfies base containment, so without this a `PUT` would truncate (and `DELETE` unlink) a file that is not a steering document.
- Update replaces the file (`atomic_write()`: unique temp file in the same directory, then `os.replace`) rather than truncating in place — on a nearly full filesystem a truncate followed by a failed write would destroy the user's document. `os.replace` swaps the directory entry rather than writing through it, so it cannot follow a symlink raced into place and needs no descriptor-identity check.
- Update preserves the target's existing permission bits (`mode=stat.S_IMODE(pre.st_mode)`) rather than forcing `0o600` — the old in-place write inherited them, and a project steering file checked out group-readable must not be silently tightened by a save. Create uses `0o600`, which is correct for a file this endpoint is bringing into existence.
- Both write paths pass `newline=""`. Text-mode writes translate `\n` to `\r\n` on Windows, so a document read back and saved again would accumulate carriage returns on every cycle (CRLF → CR CR LF → …). Content lands on disk exactly as the editor sent it. `atomic_write()` gained an opt-in `newline` parameter for this; its default behavior is unchanged.
- Create uses `os.open` with `O_NOFOLLOW` where the platform has it (absent on Windows, so the flag comes from `getattr(os, "O_NOFOLLOW", 0)` — referencing it directly would make every write a 500 there). Create adds `O_EXCL`, which refuses a pre-planted symlink on its own. Update opens without `O_TRUNC`, compares the opened inode (`fstat` `st_dev`/`st_ino`) against the pre-open `lstat`, and only then truncates — so the platforms without `O_NOFOLLOW` are covered by the identity check rather than by trusting the kernel.
- Create sanitizes the user-supplied name (`_safe_rel_name()`): disallowed characters become `-`, `.`/`..` segments are dropped, and a `.md` suffix is appended. A traversal attempt lands inside the steering root or is rejected — never outside it.
- Caps: `STEERING_MAX_FILES = 500` listed entries, `STEERING_FILE_MAX_BYTES = 256 KiB` per document (413 on read of a larger file, 413 on an oversize write).
- Read responses return content **verbatim** — no credential redaction. The response populates the editor and is written straight back on save, so redacting it would overwrite the user's own file with `[REDACTED]` markers: a data-loss bug traded for no confidentiality gain, since the recipient is the same local OS user who owns the file. `api_skill_detail` behaves the same way for the same reason. Listing **metadata** — the first-heading description, display paths, the project path — *is* run through `redact_credentials()` + `redact_exfiltration_urls()` via `_redact_meta()`, mirroring `_redact_prompt` in `handlers/prompts.py`; metadata never round-trips, so redacting it is free. Paths also collapse the real home to `~`.
- The single-file read is one offloaded transaction, `_resolve_and_read_blocking()`: it resolves, then reads through `hooks.safe_read_file_bytes_nolink()` with `within_root` set to the steering root. That helper opens with `O_NOFOLLOW`, `fstat`s the descriptor (rejecting hardlinks and non-regular files), and verifies the **opened** descriptor's real path resolves inside the root and is not sensitive — `O_NOFOLLOW` alone guards only the final path component, so an ancestor directory swapped for a symlink between resolution and open would otherwise escape. Resolution and read are not split, to keep the check-to-use window minimal. The pre-read `lstat` only supplies the size for the 413 message; a file that grows past the cap before the descriptor read makes the helper raise `FileTooLargeError`, which is caught and mapped to 413 rather than a 500.
- Every filesystem touch goes through `_offload()`, including `resolve_steering_file()` — resolution is itself `is_dir`/`lstat`/`resolve` metadata work, and on a network-backed project a stat storm alone is enough to stall the loop. Nothing in these handlers stats, reads, writes or unlinks on the event loop.
- Restricted (incognito / temporary / guest) sessions may read steering but never create, update or delete it (`_is_restricted_session` → 403). Every mutation emits `sel().log_api_access(operation="steering.create|update|delete")`; reads emit `log_tool_invocation(tool_kind="steering")`.

Behavior is pinned by `test/test_steering_api.py` and `website/src/test/SteeringTab.test.tsx`.

## Wiring

- `src/kiro_crew/dashboard/handlers/steering.py` — `steering_roots()`, `list_steering_blocking()`, `resolve_steering_file()`, the blocking transactions `_read_file_blocking` / `_create_file_blocking` / `_update_file_blocking` / `_delete_file_blocking` (all dispatched through `_offload()` onto `discovery_executor()`, the same pool as `/api/skills`), and the handlers `api_steering`, `api_steering_create`, `api_steering_detail`.
- `src/kiro_crew/dashboard/handlers/_shared.py:active_project_dir(state, session_key="")` — shared, deterministic project-dir resolution; `_resolve_skill_root()` and `api_skills` now route through it too (they previously read a non-existent `slot.project_dir`, so workspace-scoped skills never resolved).
- `src/kiro_crew/dashboard/server.py` — routes `GET/POST /api/steering` registered before the catch-all `GET/PUT/DELETE /api/steering/{key:.+}`.
- `website/src/pages/overview/SteeringTab.tsx` — list-detail layout (React Query keys `['steering']`, `['steering-file', key]`), `MarkdownRenderer` for view, textarea for edit, `Modal` for create with a scope selector.
- `website/src/pages/CapabilitiesPage.tsx` — the `steering` tab (Compass icon) between Skills and Hooks.
- `website/src/api/client.ts` — `steeringFiles`, `steeringFile`, `createSteering`, `updateSteering`, `deleteSteering`.

## Non-goals

- Editing the agent config's `resources` globs — that already exists at `GET/PUT /api/agent/config`; this tab shows the files those globs reach, not the globs.
- Showing the truncation state of the 10% `_STEERING_CAP` context budget, or a per-session "what was actually injected" trace.
- Steering files outside the two standard roots (arbitrary `file://` resources in an agent config are not browsable here).
- `AGENTS.md` / `CLAUDE.md` foundational files, which Kiro loads by a separate mechanism.
