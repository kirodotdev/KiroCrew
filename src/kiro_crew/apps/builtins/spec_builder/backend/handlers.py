"""aiohttp request adapters for Spec Builder route families."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, NamedTuple

from aiohttp import web

from . import repository as _repository
from .decisions import (
    _CLAIM_ALIAS_CONFLICT,
    _CLAIM_FULL,
    _CLAIM_PENDING,
    _CLAIM_RECORDED,
    _CLAIM_TAKEN,
    _CLAIM_UNREADABLE,
    _CLAIM_WRITE_FAILED,
    _DECISIONS_LOCK,
    _aload_index_with_decision_alias_status,
    _apply_recorded_answers,
    _claim_decision,
    _current_decision,
    _decision_alias_conflict_locked,
    _decision_entries,
    _forget_decisions,
    _pending_decisions,
    _read_decisions,
)
from .parsers import (
    _APPROVABLE_PHASES,
    _MAX_FIELD,
    _PHASE_FILES,
    _SHA256_RE,
    _UNSCRUBBABLE,
    _VALID_TYPES,
    _clean_str,
    _decision_answer_prompt,
    _decision_fingerprint,
    _decision_key,
    _duplicate_prompt,
    _normalize_approvals,
    _normalize_spec_state,
    _numeric,
    _opted_in,
    _parse_tasks,
    _redact,
    _same_spec_dir,
    _seed_prompt,
    _sha256_text,
    _task_prompt,
    _usable_name,
)
from .repository import (
    _CAN_PUBLISH_DIR_NOREPLACE,
    _DELETING,
    _DUPLICATING,
    _MAX_MODEL_LEN,
    _PROCESS_ID,
    _aload_index,
    _aload_index_snapshot,
    _aload_index_with_slot_identity,
    _audit,
    _commit_delete_teardown,
    _create_duplicate_stage,
    _create_worktree,
    _derive_phase,
    _duplicate_stage_identity,
    _forget_deleted,
    _forget_observed_slot_identity,
    _load_index_with_discovery,
    _load_settings,
    _mark_deleting,
    _mutate_index,
    _new_slot_key,
    _pin_legacy_slot_identity,
    _prepare_handoff,
    _prepare_spec_dir,
    _read_recent_projects,
    _read_spec_files,
    _read_spec_text,
    _remember_deleted,
    _remove_duplicate_marker,
    _remove_worktree,
    _repo_info,
    _reservation_is_ours,
    _rollback_staged_docs,
    _rollback_worktree_if_ours,
    _safe_dir,
    _safe_dir_optional,
    _save_settings,
    _scan_subdirs,
    _slot_key,
    _touch_spec,
    _unmark_deleting,
    _write_and_publish_duplicate,
)
from .runtime import (
    _CLAIM_OK,
    _EXEC_MAX_CYCLES,
    _alias_slots,
    _alias_turn_snapshot,
    _autonudge_instance,
    _bind_execution_claim_to_turn,
    _bind_pending_dispatch_to_turn,
    _busy_alias,
    _claim_execution,
    _deliver_pending_decision,
    _dispatch_turn,
    _drop_execution_claim,
    _drop_execution_claim_if_owner,
    _effective_status,
    _ensure_worker_slot,
    _exec_loop_id,
    _exec_prompt,
    _execution_claim_is_current,
    _execution_stop_barrier,
    _final_alias_conflict,
    _halt_active_turn,
    _halt_execution,
    _pending_dispatch_is_current,
    _release_pending_dispatch_when_done,
    _remove_nudge_loop,
    _remove_nudge_loop_for_slot,
    _remove_orphaned_executions,
    _replay_pending_decision,
    _reserve_execution_claim,
    _reserve_pending_dispatch,
    _reserve_slot_turn,
    _serialize_messages,
    _teardown_worker_slot,
    _turn_key,
    _turn_lock,
    authorize_and_add_nudge,
)

logger = logging.getLogger("kirocrew.app.spec-builder")


def _collect_spec_documents(spec_dir: Path) -> tuple[str, dict, dict | None, dict]:
    """Gather everything the detail endpoint needs off the filesystem.

    BLOCKING -- call via ``asyncio.to_thread``. Bundled into one function so the
    detail handler makes a single thread hop instead of four, and so no future
    edit can reintroduce an inline read: derive the phase, read the three spec
    documents, read + normalize the agent-authored state file, and overlay this
    backend's recorded decisions onto it.

    The overlay belongs in THIS hop rather than in the handler. Reading the ledger
    separately put an await between the handler's fresh index read and the slot
    scoping that consumes it, so a delete-and-re-import in that window handed the
    replacement's slot a stale ``meta`` -- and the agent's next turn ran in the old
    project directory. The ledger is scoped by ``spec_dir``, and the fresh index
    read refuses outright when that no longer matches, so reading it here is either
    consistent with the response or the whole request is refused.
    """
    phase = _derive_phase(spec_dir)
    files, docs, tasks = _read_spec_files(spec_dir)
    state: dict | None = None
    raw_text = _read_spec_text(spec_dir, ".spec-state.json")
    if raw_text is not None:
        try:
            state = _normalize_spec_state(json.loads(raw_text))
        except json.JSONDecodeError:
            state = None
    with _DECISIONS_LOCK:
        store, _usable = _read_decisions()
        recorded = _decision_entries(store, str(spec_dir))
    state = _apply_recorded_answers(state, recorded)
    # The task list is parsed from the SAME raw tasks.md text already read for the
    # document response. _parse_tasks redacts only the label it returns, preserving
    # the raw identity hash without adding another filesystem read to each poll.
    meta = {
        "docs": docs,
        "tasks": tasks,
        "task_progress": {"done": sum(1 for t in tasks if t["done"]), "total": len(tasks)},
        # GET stays read-only. The SPA uses this bit to request recovery through
        # the CSRF-protected POST endpoint instead of letting a detail poll start
        # an agent turn.
        "decision_recovery_pending": any(
            entry.get("status") in ("pending", "relayed") for entry in recorded.values()
        ),
    }
    return phase, files, state, meta


# ── validation / auth ────────────────────────────────────────────────────────


def _require_auth(request: web.Request) -> web.Response | None:
    """Trust only the middleware-set user; otherwise return a 401 response."""
    if request.get("user") is not None:
        return None
    return web.json_response({"code": "unauthorized", "error": "Unauthorized"}, status=401)


def _require_interactive_user(request: web.Request) -> web.Response | None:
    """Refuse app-token callers where the request becomes a human-authored turn."""
    if denied := _require_auth(request):
        return denied
    if request.get("app"):
        _audit("spec_interactive_user_denied", outcome="denied")
        return web.json_response(
            {
                "code": "interactive_user_required",
                "error": "an interactive user is required for this action",
            },
            status=403,
        )
    return None


async def _read_json(request: web.Request):
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"code": "invalid_json", "error": "invalid JSON body"}, status=400)
    if not isinstance(body, dict):
        return web.json_response(
            {"code": "body_not_object", "error": "body must be a JSON object"}, status=400
        )
    return body


# ── HTTP handlers ─────────────────────────────────────────────────────────────


async def _handle_repo_info(request: web.Request) -> web.Response:
    if denied := _require_auth(request):
        return denied
    path = (request.query.get("path") or "").strip()
    # Off-loop AND through the same chokepoint as every other caller-supplied
    # directory: the hand-rolled is_absolute()/is_dir() pair both ran a stat on
    # the event loop (an unavailable network path froze the gateway) and skipped
    # the sensitive-path denial that _safe_dir applies.
    safe = await asyncio.to_thread(_safe_dir, path) if path else None
    if safe is None:
        return web.json_response({"is_git": False})
    return web.json_response(await _repo_info(str(safe)))


async def _handle_browse(request: web.Request) -> web.Response:
    """GET /browse?path= — unified folder picker feed for the UI.

    Returns ``{path, parent, dirs, is_git, recents}``: subdirectories of
    ``path`` (default: $HOME), whether ``path`` is a git repo, and — on the
    initial empty-path call — the dashboard's recent projects list. Mirrors
    the host ``api_browse_dirs`` security model: realpath + sensitive-path
    denial (including symlink targets), hidden/build dirs skipped, SEL audit.
    """
    if denied := _require_auth(request):
        return denied
    raw = (request.query.get("path") or "").strip()
    initial = not raw
    # Same chokepoint as create/settings — one implementation, one guarantee.
    # Off-loop: _safe_dir expands, realpaths and stats a CALLER-SUPPLIED path
    # (plus the nearest existing ancestor), so an unresponsive mount would freeze
    # the gateway before the scan below ever got its own thread.
    safe = await asyncio.to_thread(_safe_dir, raw or str(Path.home()))
    if safe is None:
        _audit("spec_browse_denied", raw or "~")
        return web.json_response({"code": "access_denied", "error": "Access denied"}, status=403)
    base = str(safe)
    # The scan is genuinely blocking work: scandir + a full sort + a realpath and
    # sensitive-path test PER ENTRY. On a large directory that stalls the whole
    # aiohttp loop (chat streaming, heartbeats, every other app), so it runs in a
    # worker thread. Also bounded, so a pathological directory can't produce an
    # unbounded response.
    dirs = await asyncio.to_thread(_scan_subdirs, base)
    out: dict[str, Any] = {
        "path": base,
        "parent": os.path.dirname(base),
        "dirs": dirs,
        "is_git": (await _repo_info(base)).get("is_git", False),
    }
    if initial:
        # Off-loop: a file read, a JSON parse and an is_dir() per candidate — on
        # stalled home storage that froze the gateway inside the picker's very
        # first request.
        out["recents"] = await asyncio.to_thread(_read_recent_projects)
    _audit("spec_browse", base)
    return web.json_response(out)


async def _handle_get_settings(request: web.Request) -> web.Response:
    if denied := _require_auth(request):
        return denied
    s = await asyncio.to_thread(_load_settings)
    # _redact like every other stored value this module returns (see the list
    # endpoint's working_dir / spec_dir / spec_type). settings.json is
    # agent-writable -- _load_settings says so itself and validates only its
    # SHAPE -- so a credential parked in base_path would otherwise be rendered
    # verbatim in the dashboard.
    return web.json_response(
        {
            "base_path": _redact(str(s.get("base_path", ""))),
            "model": _redact(str(s.get("model", ""))),
        }
    )


async def _handle_put_settings(request: web.Request) -> web.Response:
    if denied := _require_auth(request):
        return denied
    body = await _read_json(request)
    if isinstance(body, web.Response):
        return body
    base = str(body.get("base_path", "")).strip()
    # Same contract as the Research app's per-campaign pick: a non-string or
    # over-length model is a 400 that names the problem (a sliced id is a
    # different string that is never served, so truncating would trade the 400
    # for a silent fallback). '' = inherit. Unknown names are KEPT — availability
    # is only decidable in a live session, where the withhold path owns it.
    #
    # An OMITTED key preserves the stored value: settings.json predates this
    # field, so a legacy client PUTting only base_path must not silently erase
    # a configured model. Clearing requires an explicit "" — absence is not a
    # statement about the model.
    if "model" not in body:
        model = str((await asyncio.to_thread(_load_settings)).get("model", "") or "")
    else:
        raw_model = body.get("model")
        if not isinstance(raw_model, str):
            return web.json_response(
                {"code": "model_not_a_string", "error": "model must be a string"}, status=400
            )
        model = raw_model.strip()
        if len(model) > _MAX_MODEL_LEN:
            return web.json_response(
                {
                    "code": "model_too_long",
                    "error": f"model id too long (max {_MAX_MODEL_LEN} characters)",
                },
                status=400,
            )
        # GET serves this field through _redact, whose fail-closed branch returns a
        # literal placeholder when the security module is unavailable. A client that
        # round-trips that read back would otherwise persist the placeholder as the
        # app-wide default and stamp it onto every new spec slot. Checked
        # separately from the credential-shape test below: the placeholder is
        # ordinary prose that the redactor leaves unchanged.
        if model == _UNSCRUBBABLE:
            return web.json_response(
                {"code": "model_invalid", "error": "model must be a model id"}, status=400
            )
        # Reject any value the redactor would alter: a credential-shaped string
        # would otherwise be persisted and ride the slot stamp to the browser raw
        # (slot.model is an id, not prose -- no downstream sink scrubs it). Fails
        # closed with _redact when the security module is unavailable.
        if model and _redact(model) != model:
            return web.json_response(
                {"code": "model_invalid", "error": "model must be a model id"}, status=400
            )
    if base:
        if not Path(base).is_absolute():
            return web.json_response(
                {"code": "base_path_not_absolute", "error": "base_path must be an absolute path"},
                status=400,
            )
        # Same chokepoint as working_dir: without this, spec storage could be
        # repointed at a credential directory and every subsequent spec would
        # write into it.
        safe_base = await asyncio.to_thread(_safe_dir_optional, base)
        if safe_base is None:
            return web.json_response(
                {
                    "code": "base_path_not_a_directory",
                    "error": "base_path must be an existing, non-sensitive directory",
                },
                status=400,
            )
        base = str(safe_base)
    await asyncio.to_thread(_save_settings, {"base_path": base, "model": model})
    _audit(
        "settings_update",
        f"base_path={'set' if base else 'default'} model={'set' if model else 'default'}",
    )
    # Through _redact like the GET: the omitted-key branch echoes a value read
    # from disk, so a credential-looking string in the file would otherwise
    # reach the dashboard raw here even though the GET path scrubs it.
    return web.json_response({"ok": True, "base_path": _redact(base), "model": _redact(model)})


async def _handle_list(request: web.Request) -> web.Response:
    if denied := _require_auth(request):
        return denied
    index, phases = await asyncio.to_thread(_load_index_with_discovery)
    specs = []
    for name, meta in index.items():
        # A delete in flight keeps its entry so the name stays reserved (see
        # _mark_deleting); it is not a spec the user still has.
        if isinstance(meta, dict) and (meta.get(_DELETING) or meta.get(_DUPLICATING)):
            continue
        spec_dir = Path(meta.get("spec_dir", ""))
        slot = state.get_slot(_slot_key(name)) if (state := request.app.get("state")) else None
        specs.append(
            {
                "name": name,
                # index.json is AGENT-WRITABLE: the worker runs in the user's project
                # and can put anything in these fields, so every string that came out
                # of the index is scrubbed on the way to the browser -- the same
                # treatment transcript and file content already get.
                "working_dir": _redact(str(meta.get("working_dir", ""))),
                "spec_dir": _redact(str(spec_dir)),
                "spec_type": _redact(str(meta.get("spec_type", "feature"))),
                # Optional display label; the rail falls back to the name.
                "title": _clean_str(meta.get("title")),
                "archived": meta.get("archived") is True,
                # Reconciled, not raw: a capped nudge loop that ran out of cycles
                # leaves "executing" in the index forever (see _effective_status).
                "status": await _effective_status(name, meta, slot),
                "phase": phases.get(name, "new"),
                "running": bool(getattr(slot, "running", False)),
                # Validated, not passed through: see _numeric.
                "created_at": _numeric(meta.get("created_at")),
                "updated_at": _numeric(meta.get("updated_at")),
            }
        )
    # Timestamps are agent-writable too, so they are not necessarily numbers. Mixing a
    # str and a float in one sort key raises TypeError, which turned a single malformed
    # entry into a 500 on EVERY list request -- the whole app dark, with no way back
    # through the UI. Coerce per entry instead.

    def _sort_key(entry: dict) -> float:
        # The payload already carries validated floats (see _numeric), so this only
        # has to pick which one orders the list.
        return _numeric(entry.get("updated_at")) or _numeric(entry.get("created_at"))

    specs.sort(key=_sort_key, reverse=True)
    return web.json_response({"specs": specs, "default_base": ".kiro/specs"})


async def _handle_create(request: web.Request) -> web.Response:
    if denied := _require_auth(request):
        return denied
    body = await _read_json(request)
    if isinstance(body, web.Response):
        return body
    name = str(body.get("name", "")).strip()
    working_dir = str(body.get("working_dir", "")).strip()
    spec_type = str(body.get("spec_type", "feature")).strip().lower()
    description = str(body.get("description", ""))
    # _usable_name, not _valid_name: the loader admits an index key only when it
    # ALSO survives _redact unchanged, so accepting on the grammar alone created
    # specs that the very next _load_index discarded, orphaning the directory,
    # worktree and session this handler had already built. Credential-shaped
    # slugs reach here for real -- a description can slugify into one.
    if not _usable_name(name):
        return web.json_response(
            {
                "code": "invalid_name",
                "error": (
                    "name must be 1-64 chars: letters, digits, '-' or '_', "
                    "and must not look like a credential"
                ),
            },
            status=400,
        )
    if spec_type not in _VALID_TYPES:
        return web.json_response(
            {"code": "invalid_spec_type", "error": f"spec_type must be one of {_VALID_TYPES}"},
            status=400,
        )
    if not working_dir or not Path(working_dir).is_absolute():
        return web.json_response(
            {"code": "working_dir_not_absolute", "error": "working_dir must be an absolute path"},
            status=400,
        )
    safe_wd = await asyncio.to_thread(_safe_dir, working_dir)
    if safe_wd is None:
        # Covers "missing", "not a directory" and "sensitive location" with one
        # response so the endpoint can't be used to probe the filesystem.
        return web.json_response(
            {
                "code": "working_dir_not_a_directory",
                "error": "working_dir must be an existing, non-sensitive directory",
            },
            status=400,
        )
    working_dir = str(safe_wd)
    index, index_usable = await _aload_index_snapshot()
    if not index_usable:
        return web.json_response(
            {
                "code": "spec_index_unavailable",
                "error": "the spec index is unreadable; repair it before creating a spec",
            },
            status=503,
        )
    if name in index:
        return web.json_response(
            {"code": "spec_exists", "error": f"a spec named '{name}' already exists"}, status=409
        )

    # A hard exit can leave a durable Spec Builder loop after its final index
    # binding disappears. No Stop/Delete URL exists for that orphan, and normal
    # dispatch must stay closed while it can still edit files. Create is the one
    # recovery action available with an empty index, so remove authenticated
    # orphan loops before creating a worktree, directory, or index entry.
    try:
        removed_orphans = await _remove_orphaned_executions(request.app.get("state"))
    except Exception:
        logger.warning("could not remove orphaned Spec Builder execution", exc_info=True)
        return web.json_response(
            {
                "code": "orphaned_execution_cleanup_failed",
                "error": "could not stop an orphaned build; retry the create",
            },
            status=503,
        )
    if removed_orphans:
        _audit("spec_orphaned_execution_cleanup", str(len(removed_orphans)))

    # Optional: create a dedicated worktree + branch off the chosen repo and
    # use IT as the working dir (worktree-per-spec workflow). The spec files
    # then live inside the worktree's .kiro/specs/, traveling with the branch.
    worktree_branch = ""
    repo_root = ""
    created_worktree = ""
    if _opted_in(body, "use_worktree"):
        info = await _repo_info(working_dir)
        if not info.get("is_git"):
            return web.json_response(
                {
                    "code": "worktree_requires_git",
                    "error": "use_worktree requires a git repository",
                },
                status=400,
            )
        repo_root = info["root"]
        wt = await _create_worktree(repo_root, name)
        if isinstance(wt, str):
            return web.json_response(
                {"code": "worktree_creation_failed", "error": f"worktree creation failed: {wt}"},
                status=400,
            )
        working_dir, worktree_branch = wt
        created_worktree = working_dir
        _audit("spec_worktree_create", f"{name} -> {working_dir}")
        # The worktree is a SIBLING of the original checkout, so it becomes the
        # new containment root. Re-validate it through the same chokepoint —
        # without this, containment below is still measured against the original
        # checkout and every worktree-mode create fails.
        safe_wt = await asyncio.to_thread(_safe_dir, working_dir)
        if safe_wt is None:
            await _remove_worktree(repo_root, created_worktree, worktree_branch)
            return web.json_response(
                {
                    "code": "worktree_unusable",
                    "error": "created worktree is not a usable directory",
                },
                status=400,
            )
        safe_wd = safe_wt
        working_dir = str(safe_wd)

    # One thread hop for the rest of create's filesystem work: resolving the spec
    # dir (which reads settings), the containment check, the adopt-by-overwrite
    # probe and the mkdir. All of it stats caller-supplied paths, so none of it
    # may run on the event loop.
    import_existing = _opted_in(body, "import_existing")
    spec_dir, refusal = await asyncio.to_thread(
        _prepare_spec_dir, working_dir, safe_wd, name, import_existing
    )
    if refusal:
        kind, _, detail = refusal.partition(":")
        if created_worktree:
            await _remove_worktree(repo_root, created_worktree, worktree_branch)
        if kind == "escape":
            _audit("spec_path_escape_denied", f"{name} -> {spec_dir}")
            return web.json_response(
                {
                    "code": "spec_path_outside_root",
                    "error": "resolved spec path is outside its root",
                },
                status=400,
            )
        if kind == "existing":
            return web.json_response(
                {
                    "code": "spec_files_exist",
                    "error": (
                        f"'{name}' already has spec files ({detail}) at "
                        f"{spec_dir}. Re-send with import_existing to adopt them."
                    ),
                },
                status=409,
            )
        return web.json_response(
            {"code": "spec_dir_creation_failed", "error": f"cannot create spec dir: {detail}"},
            status=400,
        )

    # Creating this spec is an explicit decision that outranks an earlier delete of
    # the same directory, so the tombstone goes away — otherwise discovery would
    # keep skipping a spec the user just asked for.
    # Registration takes the directory turn lock, the same one the message, handoff,
    # stop and delete paths take. Delete removes its index entry and THEN scans for
    # other names still referencing the directory to decide whether to clear the
    # ledger; a registration that landed after that scan let the cleanup erase
    # answers the newly adopted spec owned, reopening decisions the user had already
    # settled. Holding the lock here makes "remove entry, then decide" atomic against
    # "register entry", so the scan cannot observe a half-registered directory.
    #
    # Every path that registers or removes an index entry for a directory holds
    # that directory's lock. Discovery does not need the lock because a delete
    # publishes a tombstone before teardown; the clear below stays inside the lock
    # for the same reason the delete-side write does.
    create_dir_key = _decision_key(str(spec_dir))
    async with _turn_lock(create_dir_key):
        # A crash can leave a protected ledger after its index entry disappears. If
        # this filesystem resolves the new spelling to that old key's directory, an
        # import would preserve the record under a key the new entry cannot read, while
        # a new-document create would clear only its own lexical key. Refuse before any
        # index mutation or seed dispatch; choosing or migrating the protected identity
        # from mutable filesystem state would make the irreversible key movable.
        _fresh_index, decision_alias_conflict, decision_store_usable = (
            await _aload_index_with_decision_alias_status(str(spec_dir))
        )
        if not decision_store_usable:
            if created_worktree:
                await _remove_worktree(repo_root, created_worktree, worktree_branch)
            return web.json_response(
                {
                    "code": "decision_record_unreadable",
                    "error": "recorded decisions could not be read; retry shortly",
                },
                status=503,
            )
        if decision_alias_conflict:
            if created_worktree:
                await _remove_worktree(repo_root, created_worktree, worktree_branch)
            return web.json_response(
                {
                    "code": "decision_directory_alias_conflict",
                    "error": "a recorded decision already belongs to this directory under another spelling",
                },
                status=409,
            )
        await asyncio.to_thread(_forget_deleted, str(spec_dir))
        # And for the same reason, any answers still recorded for this directory are
        # orphaned. A delete clears the ledger only AFTER the index entry is gone and
        # only best-effort, so a crash or a failed write in that window leaves a record
        # for a spec whose documents are gone. Without this, the next spec created at
        # the same path inherited them: its decision ids are agent-authored labels
        # ("transport", "storage") that recur across specs, so an unrelated question
        # rendered locked to an answer the user never gave for it, and answering was
        # refused -- the same false-answer outcome this ledger exists to prevent,
        # reached from the other side.
        #
        # Safe to clear HERE and nowhere else, because at this instant both halves of
        # "a different spec" are observable rather than assumed: _prepare_spec_dir just
        # refused the path if it held any phase file (so these documents are new), and
        # _forget_decisions re-reads the index under its lock and declines to clear a
        # directory another name still serves (so no live alias's settled answers can
        # be erased). That is what distinguishes a creation from an alias without
        # storing a witness in the record -- a witness the agent could rewrite to make
        # a record stop matching, which would unlock a settled decision and hand it the
        # reversal this design refuses.
        #
        # import_existing is deliberately excluded: adopting documents that already
        # exist is the case where the answers were given for THESE files, and clearing
        # them would reopen settled decisions -- the reversal direction. Discovery
        # (_discover_folder_specs) adopts existing documents too, and likewise does not
        # clear.
        if not import_existing:
            cleared, _still_referenced = await _forget_decisions(str(spec_dir))
            if not cleared:
                # The clear did not take, so this spec cannot be given a guaranteed-clean
                # slate -- and proceeding would hand it whatever the previous spec at this
                # path recorded. Housekeeping was allowed to fail on the DELETE path
                # because the spec was already gone; here the spec does not exist yet, so
                # refusing costs the user a retry instead of a spec whose cards are locked
                # to answers they never gave.
                #
                # Only the clear result proves the ledger is clean. A read-back probe
                # could fail transiently while leaving the old record intact, then
                # recover and overlay that answer onto the new spec. The decision ledger
                # is a trust root, so creation fails closed until it can be repaired.
                _audit("spec_decision_record_stale", name, outcome="denied")
                logger.warning(
                    "spec %s: refusing to create -- an orphaned decision record at this path "
                    "could not be cleared",
                    name,
                )
                if created_worktree:
                    await _remove_worktree(repo_root, created_worktree, worktree_branch)
                return web.json_response(
                    {
                        "code": "decision_record_not_cleared",
                        "error": (
                            "a previous spec's recorded answers are still stored for this "
                            "path and could not be cleared; retry the create"
                        ),
                    },
                    status=503,
                )
        # A fresh key per creation, so a name reused after a delete never appends to
        # the previous spec's transcript. Registered in the resolver map immediately:
        # the slot is acquired below, before the next index read repopulates it.
        slot_key = _new_slot_key(name)
        _repository._SLOT_KEYS[name] = slot_key
        now = time.time()
        entry = {
            "working_dir": working_dir,
            "spec_dir": str(spec_dir),
            "spec_type": spec_type,
            "status": "planning",
            "slot_key": slot_key,
            "worktree_branch": worktree_branch,
            "repo_root": repo_root,
            "created_at": now,
            "updated_at": now,
        }

        # Re-reading commit: create awaits git subprocesses and the request body, so
        # the duplicate-name check at the top is stale by now. Insert from a FRESH
        # read (and refuse if the name was taken meanwhile) so two concurrent creates
        # cannot silently overwrite each other, and so writing back the pre-await
        # snapshot cannot resurrect a spec deleted in the window.
        insert_refusal = ""

        def _insert(index: dict) -> bool:
            nonlocal insert_refusal
            if name in index:
                insert_refusal = "name"
                return False
            if any(
                _same_spec_dir(str(meta.get("spec_dir", "")), str(spec_dir))
                for meta in index.values()
            ):
                insert_refusal = "directory"
                return False
            index[name] = entry
            return True

        if not await _mutate_index(_insert):
            if created_worktree:
                await _remove_worktree(repo_root, created_worktree, worktree_branch)
            if insert_refusal == "directory":
                return web.json_response(
                    {
                        "code": "spec_dir_in_use",
                        "error": "another spec already uses this directory",
                    },
                    status=409,
                )
            return web.json_response(
                {"code": "spec_exists", "error": f"a spec named '{name}' already exists"},
                status=409,
            )

        # Everything below stays INSIDE the directory turn lock, through slot setup,
        # the final validation and the seed dispatch. Releasing at the insert left the
        # spec visible to a list poll while this request was still awaiting slot setup,
        # so a concurrent message could take the lock and start the FIRST turn -- the
        # seed then queued second and the persisted conversation began with something
        # other than the prompt that defines the spec. A registered spec whose seed has
        # not been dispatched is not yet ready to receive anything else.
        # The slot is acquired and configured ONLY AFTER the index arbitration above
        # decides this create won. get_or_create_slot keys off the name, so two
        # concurrent same-name creates share ONE slot: configuring it before
        # arbitration meant the LOSER stamped its own working_dir onto the shared
        # slot, and the winner's agent then ran in the rejected directory. The loser
        # now returns 409 having touched no slot state.
        state = request.app["state"]

        async def _unwind_create() -> None:
            """Drop what this create inserted -- identity-pinned. The pop keys off the
            NAME, so an unpinned unwind would delete the index entry of a same-name
            spec created while we were validating, leaving the user's new spec's files
            and slot behind with no record of them.

            Pinned on the per-creation slot key as well as the directory: a delete
            followed by a re-import at the same name AND path leaves spec_dir
            identical, so the directory alone cannot tell our insert from the
            replacement's."""
            ours = str(spec_dir)

            def _pop_if_ours(idx: dict) -> bool:
                meta = idx.get(name)
                if meta is None or str(meta.get("spec_dir", "")) != ours:
                    return False
                if str(meta.get("slot_key", "")) != slot_key:
                    return False
                del idx[name]
                return True

            was_ours = await _mutate_index(
                _pop_if_ours,
                on_commit=lambda: _forget_observed_slot_identity(name, slot_key),
            )
            # Gated on that SAME identity check -- see _rollback_worktree_if_ours for
            # why an ungated force-removal could destroy a replacement spec's work.
            await _rollback_worktree_if_ours(
                name,
                was_ours=was_ours,
                repo_root=repo_root,
                created_worktree=created_worktree,
                worktree_branch=worktree_branch,
            )

        creation_dispatch_claim = _reserve_pending_dispatch(str(spec_dir), slot_key, name)
        if not creation_dispatch_claim:
            await _unwind_create()
            return web.json_response(
                {
                    "code": "execution_stopping",
                    "error": "this spec was stopped before its first turn; retry the create",
                },
                status=409,
            )
        _release_pending_dispatch_when_done(creation_dispatch_claim)

        # adopt_closed=False: this spec is being CREATED. A delete leaves the old
        # spec's archived transcript on disk under a key derived from the NAME, so
        # adopting closed history here would hand the fresh agent the deleted
        # conversation. Only already-indexed specs may adopt a closed transcript.
        slot = await _ensure_worker_slot(state, name, entry, adopt_closed=False)
        if slot is None:
            # Another app owns this slot key, or the working dir no longer validates.
            await _unwind_create()
            return web.json_response(
                {
                    "code": "slot_owned_by_another_app",
                    "error": f"a chat session named '{name}' is owned by another app",
                },
                status=409,
            )
        # Slot setup AWAITS (the working-dir chokepoint runs off-loop), so a concurrent
        # delete-and-recreate can land in that window. Confirm this is still OUR spec
        # before dispatching a seed prompt that names our spec_dir -- otherwise the
        # turn would drive the replacement spec's agent with our plan.
        current = await _aload_index()
        live = current.get(name) or {}
        # Both fields, because a re-import at the same name AND path keeps spec_dir
        # while being a different creation with a different conversation -- and the
        # seed prompt below would then drive the replacement's agent.
        if (
            str(live.get("spec_dir", "")) != str(spec_dir)
            or str(live.get("slot_key", "")) != slot_key
        ):
            await _unwind_create()
            _audit("spec_create_aborted", f"{name}: deleted or recreated during slot setup")
            return web.json_response(
                {
                    "code": "spec_changed_during_create",
                    "error": "spec was deleted or recreated while being created; retry",
                },
                status=409,
            )
        # Do not auto-grant trust. The embedded chat exposes Approve / Trust / Reject,
        # while a backend TTL cannot be enforced once the page closes. Trust therefore
        # stays an explicit, auditable user choice in core's own mechanism.
        try:
            slot.title = f"Spec: {name}"
            slot._titled = True
            if hasattr(state, "push_slot_title"):
                state.push_slot_title(slot.key, slot.title)
        except Exception:
            logger.debug("title set failed", exc_info=True)

        if not _pending_dispatch_is_current(creation_dispatch_claim):
            await _unwind_create()
            return web.json_response(
                {
                    "code": "execution_stopped_during_start",
                    "error": "this spec was stopped before its first turn; retry the create",
                },
                status=409,
            )
        seed_turn = _dispatch_turn(
            state,
            slot,
            _seed_prompt(spec_type, name, spec_dir, working_dir, description),
        )
        _bind_pending_dispatch_to_turn(creation_dispatch_claim, slot, seed_turn)
        _audit("spec_create", name)
        return web.json_response(
            {
                "name": name,
                "spec_dir": str(spec_dir),
                "spec_type": spec_type,
                "status": "planning",
                "working_dir": working_dir,
                "worktree_branch": worktree_branch,
            },
            status=201,
        )


async def _handle_get(request: web.Request) -> web.Response:
    if denied := _require_auth(request):
        return denied
    name = request.match_info["name"]
    index = await _aload_index()
    meta = index.get(name)
    if not meta or meta.get(_DELETING) or meta.get(_DUPLICATING):
        return web.json_response({"code": "not_found", "error": "not found"}, status=404)
    spec_dir = Path(meta["spec_dir"])
    # Captured BEFORE the awaits below so the freshness check can compare the whole
    # identity, not just the directory (see that check for why).
    original_slot_key = str(meta.get("slot_key", ""))

    state = request.app.get("state")

    # Structured state maintained by the agent (decisions/blocking/context).
    # LLM-authored -> read symlink-safely, then project onto the documented
    # schema (types enforced, keys AND values redacted, lists capped) rather
    # than forwarding whatever shape the model happened to write.
    #
    # ALL of the detail handler's filesystem work happens in ONE worker-thread
    # hop: stat-ing the three phase files, reading up to three 1 MiB documents,
    # reading .spec-state.json, deriving task/document metadata, and overlaying the
    # recorded decisions. The UI polls
    # this endpoint every 2.5s while a build runs, so doing it inline froze the
    # gateway's event loop — chat streaming and heartbeats included — for the
    # duration of every poll. It is also the only place the ledger may be read from
    # here: a separate await would sit between the fresh index read below and the
    # slot scoping that consumes it.
    phase, files, spec_state, doc_meta = await asyncio.to_thread(_collect_spec_documents, spec_dir)

    # Live context counters from the worker slot's transcript. The slot is
    # CREATED here if it does not exist yet (see _ensure_worker_slot): a spec
    # discovered on disk has no slot, and if the embedded chat's /api/chat made
    # the first one it came up unscoped -- no _app, no project -- so approved
    # tools ran from the gateway's working directory, not the user's project.
    # Re-read the index before scoping the slot: the document collection above
    # awaits, so the spec can be deleted and RECREATED (elsewhere) in that
    # window. Scoping from the pre-await snapshot would repoint the new worker's
    # project at the OLD directory, and its agent would edit the old project.
    #
    # The identity check is the other half: an entry under the same NAME is not
    # the same spec. Without it this response would pair documents read from the
    # old directory with the new metadata.
    fresh, decision_alias_conflict, _decision_store_usable = (
        await _aload_index_with_decision_alias_status(str(spec_dir))
    )
    meta = fresh.get(name)
    if not meta:
        return web.json_response({"code": "not_found", "error": "not found"}, status=404)
    # BOTH halves of the identity, not just the directory. A delete leaves the
    # documents on disk, so a re-import at the same name AND path is a DIFFERENT
    # creation with its own conversation -- and a spec_dir-only check would pair the
    # replacement's metadata with documents and a decision record read for the spec
    # that is gone, serving the deleted spec's locked answers on the new one.
    if (
        str(meta.get("spec_dir", "")) != str(spec_dir)
        or str(meta.get("slot_key", "")) != original_slot_key
    ):
        return web.json_response(
            {
                "code": "spec_changed_during_read",
                "error": "spec was recreated while loading; retry",
            },
            status=409,
        )
    if decision_alias_conflict:
        return web.json_response(
            {
                "code": "decision_directory_alias_conflict",
                "error": "multiple spec names resolve to this directory; repair the spec index before continuing",
            },
            status=409,
        )
    turns = tool_calls = 0
    slot = await _ensure_worker_slot(state, name, meta)
    if slot is None and state is not None:
        # A foreign or unscoped slot holds this key (see _ensure_worker_slot).
        # Returning 200 anyway meant ChatEmbed mounted against that unrelated
        # session -- the user could read it, message into it and approve its tool
        # calls from this app. Refuse the whole detail read instead.
        return web.json_response(
            {
                "code": "slot_owned_by_another_app",
                "error": "this spec's chat session is owned by another app",
            },
            status=409,
        )
    if slot is not None and getattr(slot, "messages", None):
        for m in slot.messages:
            role = m.get("role", "") if isinstance(m, dict) else getattr(m, "role", "")
            if role == "user":
                turns += 1
            elif role == "tool":
                tool_calls += 1

    return web.json_response(
        {
            "name": name,
            # Agent-writable index fields; see the note in _handle_list.
            "working_dir": _redact(str(meta.get("working_dir", ""))),
            "spec_dir": _redact(str(spec_dir)),
            "spec_type": _redact(str(meta.get("spec_type", "feature"))),
            # The chat slot this spec's conversation lives in. The SPA must NOT
            # derive it from the name: keys are per-creation now, so a reused name
            # would mount the embed against the previous spec's transcript. Taken
            # from the live slot when there is one, otherwise resolved from the
            # index, so the value always names the session the app itself scoped.
            "slot_key": getattr(slot, "key", None) or _slot_key(name),
            "status": await _effective_status(name, meta, slot),
            # The selected-spec indicator and fast poll consume the same live flag
            # as the list endpoint.
            "running": bool(getattr(slot, "running", False)) if slot is not None else False,
            "phase": phase,
            "files": files,
            # Per-document raw hash, used to bind approval to the exact stored
            # revision even when the rendered text required redaction.
            "docs": doc_meta["docs"],
            # tasks.md's checklist, enumerated and individually addressable, plus
            # derived progress. Both come from re-parsing the markdown -- there is
            # no separate task store to drift out of sync with the file the IDE and
            # CLI also read.
            "tasks": doc_meta["tasks"],
            "task_progress": doc_meta["task_progress"],
            "decision_recovery_pending": doc_meta["decision_recovery_pending"],
            # A recorded human review per phase, stale when the document moved
            # after sign-off.
            "approvals": _normalize_approvals(meta.get("approvals"), doc_meta["docs"]),
            # Display label. The NAME stays the immutable identity (directory, git
            # branch, slot key); this is the only part a rename may touch.
            "title": _clean_str(meta.get("title")),
            "archived": meta.get("archived") is True,
            # Duplicate's crash-safe transaction needs descriptor-relative
            # filesystem operations. Keep an unsupported platform honest in the
            # UI instead of presenting an action the route must fail closed.
            "duplicate_supported": _CAN_PUBLISH_DIR_NOREPLACE,
            "state": spec_state,
            "context": {
                "worktree_branch": _redact(str(meta.get("worktree_branch", ""))),
                "turns": turns,
                "tool_calls": tool_calls,
            },
        }
    )


async def _handle_messages(request: web.Request) -> web.Response:
    if denied := _require_auth(request):
        return denied
    name = request.match_info["name"]
    index = await _aload_index()
    if name not in index:
        return web.json_response({"code": "not_found", "error": "not found"}, status=404)
    state = request.app["state"]
    # Same reason as the detail handler: whichever endpoint touches a spec's slot
    # first must be the one that scopes it, or /api/chat wins the race unscoped.
    slot = await _ensure_worker_slot(state, name, index[name])
    if slot is None and state is not None:
        # Foreign or unscoped slot under our key (see _ensure_worker_slot). The
        # transcript belongs to that session, so serving it here would leak
        # somebody else's conversation into this app -- same refusal the detail
        # endpoint makes.
        return web.json_response(
            {
                "code": "slot_owned_by_another_app",
                "error": "this spec's chat session is owned by another app",
            },
            status=409,
        )
    return web.json_response(
        {
            "messages": await _serialize_messages(state, _slot_key(name)),
            "running": bool(getattr(slot, "running", False)) if slot else False,
        }
    )


async def _handle_recover_decision(request: web.Request) -> web.Response:
    """POST crash-recovery relay; never dispatch from the detail GET."""
    if denied := _require_interactive_user(request):
        return denied
    name = request.match_info["name"]
    body = await _read_json(request)
    if isinstance(body, web.Response):
        return body
    claimed_dir = str(body.get("spec_dir", "") or "").strip()
    claimed_key = str(body.get("slot_key", "") or "").strip()
    fresh = await _touch_spec(
        name,
        expect_spec_dir=claimed_dir or None,
        expect_slot_key=claimed_key or None,
    )
    if fresh is None:
        return web.json_response(
            {"code": "stale_client", "error": "spec was deleted or recreated; reload and retry"},
            status=409,
        )
    state = request.app["state"]
    slot = await _ensure_worker_slot(state, name, fresh)
    if slot is None:
        return web.json_response(
            {
                "code": "slot_owned_by_another_app",
                "error": "this spec's chat session is owned by another app",
            },
            status=409,
        )
    recovered = await _replay_pending_decision(state, slot, name, fresh)
    return web.json_response({"ok": recovered})


async def _handle_message(request: web.Request) -> web.Response:
    if denied := _require_interactive_user(request):
        return denied
    name = request.match_info["name"]
    index = await _aload_index()
    if name not in index:
        return web.json_response({"code": "not_found", "error": "not found"}, status=404)
    body = await _read_json(request)
    if isinstance(body, web.Response):
        return body
    text = str(body.get("text", "")).strip()
    if not text:
        return web.json_response({"code": "text_required", "error": "text required"}, status=400)
    state = request.app["state"]
    # Re-reading commit BEFORE dispatch: the body read above awaits, so a
    # concurrent DELETE can land in that window. Stamping through the mutator
    # both refuses to resurrect a deleted spec and hands back the FRESH entry to
    # scope the slot from, instead of the pre-await snapshot.
    # Identity-pinned against the CLIENT'S captured spec_dir, not against the
    # index we just read: comparing the index to itself always matches, so the
    # check was vacuous. The SPA sends the spec_dir it rendered (from the detail
    # payload), which is what makes a stale tab detectable -- if the spec was
    # deleted and recreated elsewhere under the same name, that value no longer
    # matches and the instruction must not reach the replacement's agent. A caller
    # that sends no spec_dir cannot be pinned; it is then treated as unpinned
    # rather than refused, so an older client keeps working.
    # The slot key rides along because a directory does NOT identify a creation:
    # delete leaves the documents on disk, so a re-import at the same name AND
    # path passes a spec_dir check while being a different spec with a different
    # conversation -- and this instruction would land in the replacement's chat.
    claimed_dir = str(body.get("spec_dir", "") or "").strip()
    claimed_key = str(body.get("slot_key", "") or "").strip()
    # Present only when this message is a decision card's answer.
    #
    # Both values go through _clean_str -- the SAME projection _normalize_spec_state
    # applies -- and through nothing else. Two reasons, and both were defects:
    #
    #  * the id becomes the ledger KEY, and the overlay matches it against the id
    #    the detail read serves. A different normalization here (a strip, a shorter
    #    cap) makes the two disagree for whitespace-bearing or long ids, and a
    #    disagreement is invisible: the answer is recorded, no card is ever locked,
    #    and the decision stays re-answerable.
    #  * the OPTION is what gets recorded and later rendered as the answer. The
    #    composed prompt ("Decision — <title>: <option>", localized) must not be:
    #    the card would show the whole sentence back instead of the choice.
    decision_id = _clean_str(body.get("decision_id"))
    decision_option = _clean_str(body.get("decision_option"))
    if decision_id and not decision_option:
        return web.json_response(
            {
                "code": "decision_option_required",
                "error": "decision_option required with decision_id",
            },
            status=400,
        )
    fresh = await _touch_spec(
        name, expect_spec_dir=claimed_dir or None, expect_slot_key=claimed_key or None
    )
    if fresh is None:
        return web.json_response(
            {"code": "stale_client", "error": "spec was deleted or recreated; reload and retry"},
            status=409,
        )
    fresh = await _pin_legacy_slot_identity(name, fresh)
    if fresh is None:
        return web.json_response(
            {"code": "stale_client", "error": "spec was deleted or recreated; reload and retry"},
            status=409,
        )
    slot = await _ensure_worker_slot(state, name, fresh)
    if slot is None:
        # Another app owns this slot key (see _ensure_worker_slot). Refuse rather
        # than dispatching a turn into a session we do not own.
        return web.json_response(
            {
                "code": "slot_owned_by_another_app",
                "error": "this spec's chat session is owned by another app",
            },
            status=409,
        )
    # Pure lexical work on the loop. Alias discovery below reads the index off-loop,
    # but must happen only after this request owns the directory lock.
    dir_key = _decision_key(str(fresh.get("spec_dir", "")))
    current_decision: dict[str, Any] | None = None
    # The turn lock spans the running-check, the claim and the dispatch, so no other
    # handler can start a turn on this spec in between -- see _TURN_LOCKS. Acquired
    # BEFORE the re-pin so the last await before the dispatch is still a pinning one.
    async with _turn_lock(dir_key):
        expected_slot_key = str(fresh.get("slot_key", ""))
        dispatch_claim = _reserve_pending_dispatch(dir_key, expected_slot_key, name)
        if not dispatch_claim:
            return web.json_response(
                {
                    "code": "spec_busy_elsewhere",
                    "error": (
                        "another request is starting or stopping work on these files; "
                        "retry shortly"
                    ),
                },
                status=409,
            )
        _release_pending_dispatch_when_done(dispatch_claim)
        # Every OTHER name on this directory, read only after entering the lock. The
        # index is agent-writable, so an alias can be added while this request waits;
        # scanning before the wait would miss a newly-busy alias and admit a second
        # agent over the same files. The filesystem work stays off the event loop.
        aliases = await _alias_slots(
            dir_key,
            own_slot_key=expected_slot_key or str(getattr(slot, "key", "")),
        )
        # An alias mid-turn is a SECOND session over these documents, so its turn is a
        # concurrent editor no matter what this request carries -- a decision answer, an
        # ordinary message, anything. Refused for all of them.
        #
        # Our OWN slot is excluded from `aliases`, which is what preserves same-slot
        # queuing: a message to the session that is running is queued by _dispatch_turn
        # (the established behaviour), while a decision answer to it is refused below --
        # a queued answer may never be delivered, and the ledger would claim it was.
        if busy_under := _busy_alias(state, aliases):
            _audit("spec_busy_elsewhere", f"{name}: {busy_under}", outcome="denied")
            return web.json_response(
                {
                    "code": "spec_busy_elsewhere",
                    "error": (
                        f"another view of this spec ({busy_under}) has an agent working on "
                        "these files; wait for it to finish"
                    ),
                },
                status=409,
            )
        alias_snapshot = _alias_turn_snapshot(state, aliases)
        # Re-pin after slot acquisition. _ensure_worker_slot awaits (it revalidates the
        # working dir off the event loop), so a delete can start AND finish between the
        # check above and this line -- handing the turn to a slot whose spec is gone.
        #
        # BOTH pins come from `fresh` -- the entry this request already verified -- not
        # from the client body. `slot_key` is optional on the wire (an older client that
        # sends none is treated as unpinned rather than refused), so reusing the CLAIMED
        # value here meant a request without one had no creation pin on the second check:
        # a delete plus a same-path recreate passed it, because spec_dir still matched,
        # and the stale slot wrote into the replacement's files. The captured value is
        # server-side data, so pinning to it is strictly stronger AND still lets an older
        # client through the first check.
        if (
            await _touch_spec(
                name,
                expect_spec_dir=fresh.get("spec_dir"),
                expect_slot_key=str(fresh.get("slot_key", "")) or None,
            )
            is None
        ):
            return web.json_response(
                {
                    "code": "stale_client",
                    "error": "spec was deleted or recreated; reload and retry",
                },
                status=409,
            )
        # A decision answer is claimed before it is dispatched, and a decision that is
        # already recorded is refused outright -- the agent has that answer and is
        # acting on it, so a second one would silently reverse a settled decision. The
        # claim is atomic (see _claim_decision), so two concurrent clicks on the same
        # card resolve to exactly one dispatched answer rather than two turns.
        #
        # A RUNNING slot is refused rather than queued. _dispatch_turn queues into a turn
        # that is already in flight, and a Pause (or Stop, or Delete) clears that queue by
        # design -- ending a turn must not let the agent keep working. So a queued answer
        # is an answer that may never be delivered, while the ledger would go on claiming
        # it was.
        #
        # The check is trustworthy for every Spec Builder entry point because the turn
        # lock is held through delivery. The claim itself is pending until relay, so a
        # process exit in that window is replayed rather than treated as final.
        if decision_id and getattr(slot, "running", False):
            return web.json_response(
                {
                    "code": "decision_agent_busy",
                    "error": "the agent is working on this spec; answer the decision once it stops",
                    "decision_id": decision_id,
                },
                status=409,
            )
        turn_reservation: asyncio.Task[Any] | None = None
        if decision_id:
            # Publish the claim-in-progress through the slot's ordinary ``running``
            # surface before the first validation await. Dashboard chat can start the
            # same app-owned slot without this module's directory lock; it must queue
            # behind the answer rather than replace the question between validation
            # and the durable claim.
            turn_reservation = _reserve_slot_turn(state, slot)
            if turn_reservation is None:
                return web.json_response(
                    {
                        "code": "decision_agent_busy",
                        "error": (
                            "the agent is working on this spec; answer the decision once "
                            "it stops"
                        ),
                        "decision_id": decision_id,
                    },
                    status=409,
                )
            # A card is a snapshot. Validate it only after serialization and the
            # final identity/idle checks: while this request waited for the lock, the
            # preceding agent turn could replace or remove the question. Reading it
            # before the wait would claim and deliver an answer for stale state.
            current_decision, decision_state_usable = await asyncio.to_thread(
                _current_decision, Path(str(fresh.get("spec_dir", ""))), decision_id
            )
            if not decision_state_usable:
                _audit(
                    "spec_decision_state_unreadable",
                    f"{name}: {decision_id}",
                    outcome="denied",
                )
                return web.json_response(
                    {
                        "code": "decision_state_unreadable",
                        "error": "this decision could not be verified; reload and retry",
                        "decision_id": decision_id,
                    },
                    status=503,
                )
            if current_decision is None:
                _audit("spec_decision_not_found", f"{name}: {decision_id}", outcome="denied")
                return web.json_response(
                    {
                        "code": "decision_not_found",
                        "error": "this decision is no longer present; reload before answering",
                        "decision_id": decision_id,
                    },
                    status=409,
                )
            offered_options = list(current_decision.get("options") or [])
            if offered_options and decision_option not in offered_options:
                _audit("spec_decision_option_stale", f"{name}: {decision_id}", outcome="denied")
                return web.json_response(
                    {
                        "code": "decision_option_not_offered",
                        "error": (
                            "this decision's options have changed; reload and choose from "
                            "the current options"
                        ),
                        "decision_id": decision_id,
                    },
                    status=409,
                )
            fingerprint = _decision_fingerprint(current_decision or {})
            delivery_id = uuid.uuid4().hex
            outcome, held = await _claim_decision(
                name,
                decision_id,
                decision_option,
                expect_spec_dir=str(fresh.get("spec_dir", "")),
                expect_slot_key=str(fresh.get("slot_key", "")),
                fingerprint=fingerprint,
                message=_decision_answer_prompt(current_decision, decision_option),
                delivery_id=delivery_id,
            )
            if outcome == _CLAIM_TAKEN:
                return web.json_response(
                    {
                        "code": "decision_already_answered",
                        "error": "this decision was already sent to the agent and cannot be changed",
                        "decision_id": decision_id,
                        "answer": _clean_str(held),
                    },
                    status=409,
                )
            if outcome == _CLAIM_FULL:
                _audit("spec_decision_ledger_full", name, outcome="denied")
                return web.json_response(
                    {
                        "code": "decision_ledger_full",
                        "error": "too many recorded decisions for this spec",
                    },
                    status=409,
                )
            if outcome == _CLAIM_ALIAS_CONFLICT:
                _audit("spec_decision_directory_alias_conflict", name, outcome="denied")
                return web.json_response(
                    {
                        "code": "decision_directory_alias_conflict",
                        "error": "multiple spec names resolve to this directory; repair the spec index before continuing",
                    },
                    status=409,
                )
            if outcome == _CLAIM_UNREADABLE:
                # The record exists but could not be read. Writing would erase every
                # answer in it, so nothing is recorded and nothing is dispatched.
                _audit("spec_decision_record_unreadable", name, outcome="denied")
                return web.json_response(
                    {
                        "code": "decision_record_unreadable",
                        "error": "this spec's recorded decisions could not be read; retry shortly",
                    },
                    status=503,
                )
            if outcome == _CLAIM_WRITE_FAILED:
                # The record could not be written (a full or unwritable data home), so
                # nothing was recorded and nothing is dispatched.
                _audit("spec_decision_record_write_failed", name, outcome="denied")
                return web.json_response(
                    {
                        "code": "decision_record_write_failed",
                        "error": "this spec's recorded decisions could not be written; retry shortly",
                    },
                    status=503,
                )
            if outcome != _CLAIM_RECORDED:
                if outcome != _CLAIM_PENDING:
                    return web.json_response(
                        {
                            "code": "stale_client",
                            "error": "spec was deleted or recreated; reload and retry",
                        },
                        status=409,
                    )
            pending = next(
                (
                    entry
                    for entry in await _pending_decisions(str(fresh.get("spec_dir", "")))
                    if entry.get("decision_id") == decision_id
                    and entry.get("fingerprint") == fingerprint
                ),
                None,
            )
            active_delivery_id = (
                pending.get("delivery_id", "") if pending is not None else delivery_id
            )
            delivered = pending is not None and await _deliver_pending_decision(
                state,
                slot,
                str(fresh.get("spec_dir", "")),
                pending,
                turn_reservation=turn_reservation,
                initial_aliases=aliases,
                alias_snapshot=alias_snapshot,
                own_name=name,
                expected_slot_key=expected_slot_key,
                dispatch_claim=dispatch_claim,
            )
            if not delivered:
                exact_still_pending = any(
                    entry.get("decision_id") == decision_id
                    and entry.get("fingerprint") == fingerprint
                    and entry.get("delivery_id") == active_delivery_id
                    for entry in await _pending_decisions(str(fresh.get("spec_dir", "")))
                )
                if not exact_still_pending:
                    _audit(
                        "spec_decision_changed_before_delivery",
                        f"{name}: {decision_id}",
                        outcome="denied",
                    )
                    return web.json_response(
                        {
                            "code": "decision_changed_before_delivery",
                            "error": (
                                "this decision changed before the answer reached the "
                                "agent; reload and answer the current question"
                            ),
                            "decision_id": decision_id,
                        },
                        status=409,
                    )
                _audit(
                    "spec_decision_delivery_pending",
                    f"{name}: {decision_id}",
                    outcome="denied",
                )
                return web.json_response(
                    {
                        "code": "decision_delivery_pending",
                        "error": "the answer is saved and will be delivered when the agent is available",
                        "decision_id": decision_id,
                    },
                    status=503,
                )
            _audit("spec_decision_answered", f"{name}: {decision_id}")
        else:
            # The ordinary message path also awaited the identity re-pin above.
            # Dashboard chat can run a different alias during that hop, including
            # a complete turn whose task has already returned to None. Re-scan after
            # the last await and publish this task synchronously if still uncontested.
            if busy_under := await _final_alias_conflict(
                state,
                dir_key,
                expected_slot_key or str(getattr(slot, "key", "")),
                aliases,
                alias_snapshot,
                own_name=name,
            ):
                _audit("spec_busy_elsewhere", f"{name}: {busy_under}", outcome="denied")
                return web.json_response(
                    {
                        "code": "spec_busy_elsewhere",
                        "error": (
                            f"another view of this spec ({busy_under}) has an agent "
                            "working on these files; wait for it to finish"
                        ),
                    },
                    status=409,
                )
            if not _pending_dispatch_is_current(dispatch_claim):
                return web.json_response(
                    {
                        "code": "execution_stopped_during_start",
                        "error": "the message was stopped before it reached the agent",
                    },
                    status=409,
                )
            turn = _dispatch_turn(
                state,
                slot,
                text,
                directive_user_origin=True,
            )
            _bind_pending_dispatch_to_turn(dispatch_claim, slot, turn)
        _audit("spec_message", name)
        return web.json_response({"ok": True})


async def _handle_handoff(request: web.Request) -> web.Response:
    if denied := _require_auth(request):
        return denied
    name = request.match_info["name"]
    index = await _aload_index()
    meta = index.get(name)
    if not meta:
        return web.json_response({"code": "not_found", "error": "not found"}, status=404)
    meta = await _pin_legacy_slot_identity(name, meta)
    if meta is None:
        return web.json_response({"code": "stale_client", "error": _STALE_CLIENT_ERROR}, status=409)
    spec_dir = Path(meta["spec_dir"])
    working_dir = meta.get("working_dir", "")
    # Captured BEFORE the await below, so the reread can compare against the
    # identity this request started with rather than re-deriving one.
    started_slot_key = str(meta.get("slot_key", ""))
    # Parse and check the CLIENT's claim before the destructive call below, the
    # same ordering _handle_stop_execution documents. _prepare_handoff clears the
    # STOP sentinel, so a stale same-name execute that got this far would disarm a
    # replacement's Pause before any identity comparison had run.
    claimed = await _client_claim(request)
    if _client_identity_mismatch(claimed, spec_dir, started_slot_key):
        return web.json_response({"code": "stale_client", "error": _STALE_CLIENT_ERROR}, status=409)
    # One thread hop for every filesystem touch this handler needs: the identity
    # re-check, the tasks.md gate, clearing a stale STOP sentinel from a prior run
    # (symlink-safe), and resolving the sentinel path the autonudge arm requires.
    # name + started_slot_key make the CLEAR itself conditional on identity, which
    # is the half a claim comparison cannot cover for a claimless request.
    has_tasks, sentinel_path = await asyncio.to_thread(
        _prepare_handoff, spec_dir, name, started_slot_key
    )
    if not has_tasks:
        return web.json_response(
            {
                "code": "tasks_missing",
                "error": "tasks.md has no unchecked tasks yet — finish the Tasks phase first",
            },
            status=409,
        )
    # Reread AFTER the await as well: a delete+recreate can land during the thread
    # hop, and a stale request would then capture the REPLACEMENT's slot while its
    # own abort path -- correctly pinned to what it captured -- closed the new
    # session. This is what protects slot acquisition.
    current = await _aload_index()
    meta = current.get(name)
    # Pinned on the per-creation slot key as well as the directory. A delete +
    # re-import at the same name AND path leaves spec_dir identical, so the
    # directory alone cannot distinguish our spec from the replacement -- and the
    # slot_key check below only validates the CLIENT's claim, so a request that
    # carries no claim had no identity check at all.
    if (
        not meta
        or str(meta.get("spec_dir", "")) != str(spec_dir)
        or str(meta.get("slot_key", "")) != started_slot_key
    ):
        return web.json_response(
            {
                "code": "spec_changed_during_start",
                "error": "spec was deleted or recreated while starting; retry",
            },
            status=409,
        )
    working_dir = meta.get("working_dir", "")
    if _client_identity_mismatch(claimed, spec_dir, str(meta.get("slot_key", ""))):
        return web.json_response({"code": "stale_client", "error": _STALE_CLIENT_ERROR}, status=409)
    state = request.app["state"]
    # FAIL CLOSED. Falling through to a single turn would bypass the authorization
    # chokepoint, including slot ownership, message bounds, sentinel checks and SEL
    # audit. An unauthorized run is not a degraded run.
    svc = _autonudge_instance() if _autonudge_instance is not None else None
    if svc is None or authorize_and_add_nudge is None:
        _audit("spec_handoff_denied", f"{name}: autonudge unavailable", outcome="denied")
        return web.json_response(
            {
                "code": "autonudge_unavailable",
                "error": (
                    "autonomous execution is unavailable: the auto-nudge service is not "
                    "running, so the run cannot be authorized or bounded"
                ),
            },
            status=503,
        )

    # CLAIM the run before any side effect: one atomic compare-and-set that both
    # refuses a second handoff and records the execution state. Reading the status
    # here and committing it further down was not a guard at all -- two concurrent
    # requests both read "planning", both passed, and both dispatched, so Pause
    # cancelled one prompt while the other drained and kept editing the user's
    # files. The decision and the write are now the same index mutation.
    #
    # Recording BEFORE arming also matters on its own: the arm is shielded and
    # survives a restart, so arming first left a window where a shutdown persisted
    # a timer with no execution state -- and the restored timer ran something Pause
    # could not stop, because Pause keys off that state.
    captured_slot_key = str(meta.get("slot_key", ""))
    handoff_dir_key = _decision_key(str(spec_dir))
    execution_claim, reservation_refusal = _reserve_execution_claim(
        handoff_dir_key, captured_slot_key, name
    )
    if not execution_claim:
        stopping = reservation_refusal == "stopping"
        return web.json_response(
            {
                "code": "execution_stopping" if stopping else "already_executing",
                "error": (
                    "this spec is being stopped; wait for Stop to finish"
                    if stopping
                    else "this spec is already starting; wait for it to finish"
                ),
            },
            status=409,
        )

    # A cancelled HTTP request must not leave a process-owned claim behind. The
    # conditional drop cannot release a newer request's generation.
    handler_task = asyncio.current_task()
    if handler_task is not None:

        def _release_abandoned_claim(_done: asyncio.Task[Any]) -> None:
            _drop_execution_claim_if_owner(handoff_dir_key, execution_claim, _done)

        handler_task.add_done_callback(_release_abandoned_claim)

    # Serialize the durable claim itself with Stop. Stop publishes its barrier
    # before waiting for this lock, so a Stop that gets here first revokes the
    # token before any ``executing`` write. If this write gets here first, Stop
    # cannot report success until it has overwritten that exact state with
    # ``planning``. There is therefore no late claim write after a successful Stop.
    async with _turn_lock(handoff_dir_key):
        if not _execution_claim_is_current(handoff_dir_key, execution_claim):
            return web.json_response(
                {
                    "code": "execution_stopped_during_start",
                    "error": "execution was stopped before it started",
                },
                status=409,
            )
        live_slot = state.get_slot(_slot_key(name)) if state is not None else None
        try:
            claim, committed = await _claim_execution(
                name,
                expect_spec_dir=str(spec_dir),
                expect_slot_key=captured_slot_key,
                live_running=bool(getattr(live_slot, "running", False)),
            )
        except Exception:
            # Nothing has been created yet, so there is nothing to unwind -- but the
            # run must not proceed on an unrecorded state, because Pause keys off it.
            _drop_execution_claim(handoff_dir_key, execution_claim)
            logger.warning("could not claim execution for %s", name, exc_info=True)
            return web.json_response(
                {
                    "code": "exec_state_write_failed",
                    "error": "could not record execution state; the run was not started",
                },
                status=500,
            )
        if claim == _CLAIM_TAKEN:
            _drop_execution_claim(handoff_dir_key, execution_claim)
            return web.json_response(
                {
                    "code": "already_executing",
                    "error": "this spec is already building; pause it before starting again",
                },
                status=409,
            )
        if claim != _CLAIM_OK:
            _drop_execution_claim(handoff_dir_key, execution_claim)
            return web.json_response(
                {
                    "code": "spec_changed_during_start",
                    "error": "spec was deleted or recreated while starting; retry",
                },
                status=409,
            )
        meta = committed or meta
    # Did the slot ALREADY exist? The unwind path below must only close a slot
    # this request created: a pre-existing one carries the user's conversation
    # (and possibly a running turn), and destroying it because a later index
    # write failed loses work the handoff never owned.
    slot_pre_existed = live_slot is not None
    # Tool calls are NOT auto-approved: the user approves (or clicks Trust) from
    # the embedded chat's approval card. The run is bounded by the STOP SENTINEL,
    # the Stop button, and a capped nudge cycle count.
    slot = await _ensure_worker_slot(state, name, meta)
    if slot is None:
        # Another app owns this slot key (see _ensure_worker_slot). Refuse rather
        # than dispatching a turn into a session we do not own -- and give the
        # claim back, or the spec stays marked executing with nothing running.
        if _execution_claim_is_current(handoff_dir_key, execution_claim):
            await _touch_spec(
                name,
                expect_spec_dir=str(spec_dir),
                expect_slot_key=captured_slot_key or None,
                status="planning",
                exec_started_at=0.0,
                exec_arming_at=0.0,
            )
            _drop_execution_claim(handoff_dir_key, execution_claim)
        return web.json_response(
            {
                "code": "slot_owned_by_another_app",
                "error": "this spec's chat session is owned by another app",
            },
            status=409,
        )
    prompt = _exec_prompt(name, spec_dir, working_dir)
    # Arm the autonudge loop through the SHARED AUTHORIZATION CHOKEPOINT so this
    # app enforces the same slot-ownership checks, message limits, sensitive
    # stop_sentinel_path refusal and SEL audit as POST /api/autonudge. Calling
    # svc.add directly (as this did) bypassed all of it, and max_cycles=0 meant
    # an unbounded loop. Fails CLOSED: if authorization is refused we do not
    # dispatch the autonomous turn.

    async def _release(reason: str, *, loop_id: str | None = None) -> None:
        """Undo ONLY what this request created, in the reverse order it was created.

        Both the loop and the slot are looked up by name, so an unpinned abort
        would cancel the loop and destroy the slot of a same-name spec that
        replaced ours.
        """
        if loop_id:
            try:
                await _remove_nudge_loop_for_slot(
                    str(getattr(slot, "key", "")), only_loop_id=loop_id
                )
            except Exception:
                # Best-effort HERE only: this is already an abort path, and the
                # reason that brought us here is the story worth surfacing. Logged
                # loudly because a surviving loop can still nudge.
                logger.warning(
                    "spec %s: could not remove the armed loop while unwinding",
                    name,
                    exc_info=True,
                )
        # Put the recorded state back only while this request still owns the
        # process claim. Stop revokes the token before waiting for this lock, and
        # a stale unwind must not overwrite Stop or tear down a newer request's slot.
        owned = _execution_claim_is_current(handoff_dir_key, execution_claim)
        if owned:
            try:
                await _touch_spec(
                    name,
                    expect_spec_dir=str(spec_dir),
                    expect_slot_key=captured_slot_key or None,
                    status="planning",
                    exec_started_at=0.0,
                    exec_arming_at=0.0,
                )
            except Exception:
                logger.warning(
                    "spec %s: could not clear the execution state while unwinding",
                    name,
                    exc_info=True,
                )
            owned = _drop_execution_claim(handoff_dir_key, execution_claim)
        if owned and not slot_pre_existed:
            await _teardown_worker_slot(state, name, only_slot=slot)
        _audit("spec_handoff_aborted", f"{name}: {reason}", outcome="denied")

    # The turn lock is acquired BEFORE the loop is armed, and held through the FINAL
    # freshness check and the dispatch. Arming first meant a 120s idle timer was already
    # running while this handler waited for the lock: a long wait let the loop dispatch
    # the build on its own, so a decision answer recorded under the lock queued behind a
    # turn nobody here started, and Pause could discard it.
    #
    # The busy check precedes arming so a refusal cannot leave a timer that later
    # dispatches the build it denied.
    async with _turn_lock(handoff_dir_key):
        # The execution claim is recorded before this lock is acquired. Stop takes
        # the same lock, but can get there first while this request is materializing
        # its slot: it then commits ``planning`` and reports success. Re-read both the
        # creation and the process-owned claim inside the lock, before arming
        # anything. The index is agent-writable, so its status and timestamps may
        # fail closed but can never authenticate ownership of this request.
        handoff_index = await _aload_index()
        handoff_meta = handoff_index.get(name) or {}
        same_creation = bool(
            handoff_meta
            and str(handoff_meta.get("spec_dir", "")) == str(spec_dir)
            and str(handoff_meta.get("slot_key", "")) == captured_slot_key
        )
        same_claim = bool(
            same_creation
            and str(handoff_meta.get("status", "")) == "executing"
            and _execution_claim_is_current(handoff_dir_key, execution_claim)
        )
        if not same_claim:
            stopped = not _execution_claim_is_current(handoff_dir_key, execution_claim)
            stopped = stopped or (
                same_creation and str(handoff_meta.get("status", "")) == "planning"
            )
            reason = "stopped before dispatch" if stopped else "execution claim changed"
            await _release(reason)
            return web.json_response(
                {
                    "code": (
                        "execution_stopped_during_start" if stopped else "spec_changed_during_start"
                    ),
                    "error": (
                        "execution was stopped before it started"
                        if stopped
                        else "spec or execution changed while starting; retry"
                    ),
                },
                status=409,
            )
        # The index is agent-writable, so discover aliases only after entering the
        # directory lock. A pre-lock snapshot can miss an alias added while this
        # request waits, after that alias has started work under the shared lock.
        handoff_aliases = await _alias_slots(
            handoff_dir_key,
            own_slot_key=captured_slot_key or str(getattr(slot, "key", "")),
        )
        # A handoff starts an autonomous build. Another name on this directory that is
        # mid-turn is a second agent already editing these files, so the build waits --
        # the same refusal an ordinary message gets, for the same reason.
        #
        # Nothing is armed yet, so this refusal has no loop_id to release.
        if busy_under := _busy_alias(state, handoff_aliases):
            await _release(f"busy under {busy_under}")
            _audit("spec_handoff_denied", f"{name}: busy under {busy_under}", outcome="denied")
            return web.json_response(
                {
                    "code": "spec_busy_elsewhere",
                    "error": (
                        f"another view of this spec ({busy_under}) has an agent working on "
                        "these files; wait for it to finish"
                    ),
                },
                status=409,
            )
        handoff_alias_snapshot = _alias_turn_snapshot(state, handoff_aliases)
        try:
            armed_loop, authz_err, _status = await authorize_and_add_nudge(
                svc=svc,
                state=state,
                slot_key=slot.key,
                message=prompt,
                idle_secs=120,
                max_cycles=_EXEC_MAX_CYCLES,
                stop_sentinel_path=sentinel_path,
                source="app:spec-builder",
                caller=str(request.get("user") or ""),
            )
        except Exception:
            logger.warning("autonudge arm raised for %s — refusing handoff", name, exc_info=True)
            await _release("authorization raised")
            _audit("spec_handoff_denied", f"{name}: authorization raised", outcome="denied")
            return web.json_response(
                {
                    "code": "authorization_failed",
                    "error": "could not authorize autonomous execution",
                },
                status=503,
            )
        if authz_err:
            # No trust to revoke (we never granted any), and revoking here would undo
            # a trust decision the user made themselves. The recorded execution state
            # IS ours to revoke, and _release does that.
            await _release(f"authorization refused: {authz_err}")
            _audit("spec_handoff_denied", f"{name}: {authz_err}", outcome="denied")
            return web.json_response(
                {
                    "code": "authorization_refused",
                    "error": f"could not start autonomous execution: {authz_err}",
                },
                status=403,
            )
        # Stop publishes its barrier before it waits for this directory lock, so it
        # can revoke a handoff while authorization is awaiting audit or persistence.
        # The armed loop is ours and must be removed, but Stop owns the durable
        # transition to ``planning`` once it has revoked this token.
        if not _execution_claim_is_current(handoff_dir_key, execution_claim):
            await _release(
                "stopped during authorization",
                loop_id=getattr(armed_loop, "id", None),
            )
            return web.json_response(
                {
                    "code": "execution_stopped_during_start",
                    "error": "execution was stopped before it started",
                },
                status=409,
            )
        # Authorization awaits outside the slot's own dispatch machinery. A channel
        # message can therefore start this same slot while the request is suspended,
        # even though Spec Builder handlers share the directory lock. Dispatching now
        # would QUEUE the build, and Pause clears that queue while this endpoint reports
        # success. Recheck the live slot after the await and unwind the loop we armed.
        if getattr(slot, "running", False):
            await _release(
                "the spec agent became busy during authorization",
                loop_id=getattr(armed_loop, "id", None),
            )
            return web.json_response(
                {
                    "code": "spec_agent_busy",
                    "error": "the spec agent started another turn; wait for it to finish",
                },
                status=409,
            )
        # The same turn lock the message and delete paths take, held across the FINAL
        # freshness check AND the dispatch. Two orderings depend on that span: a decision
        # answer must not be queued behind a build starting here (Pause would drop it),
        # and a DELETE must not slip between this check and the dispatch -- holding the
        # lock only for the dispatch left exactly that window, so the turn started on a
        # spec the delete had already removed.
        # Arming awaits too, so re-verify the creation once more. A DELETE landing in
        # that window tears down the slot and the loops it can see BY NAME -- ours
        # arrives after, and would be left nudging a spec that no longer exists. The
        # old arm-then-commit order caught this at the commit; the reorder above has to
        # catch it here instead.
        refreshed = await _touch_spec(
            name,
            expect_spec_dir=str(spec_dir),
            expect_slot_key=captured_slot_key or None,
            # The loop is armed: the reconciler can see it now, so the pre-arm
            # exemption must end here rather than expire on the grace window.
            exec_arming_at=0.0,
        )
        if (
            refreshed is None
            or str(refreshed.get("status", "")) != "executing"
            or not _execution_claim_is_current(handoff_dir_key, execution_claim)
        ):
            stopped = not _execution_claim_is_current(handoff_dir_key, execution_claim)
            await _release(
                (
                    "stopped during final execution check"
                    if stopped
                    else "deleted or recreated during authorization"
                ),
                loop_id=getattr(armed_loop, "id", None),
            )
            return web.json_response(
                {
                    "code": (
                        "execution_stopped_during_start" if stopped else "spec_changed_during_start"
                    ),
                    "error": (
                        "execution was stopped before it started"
                        if stopped
                        else "spec was deleted or recreated while execution was starting"
                    ),
                },
                status=409,
            )
        # Authorization and the freshness write await while dashboard chat can run
        # another alias without this directory lock. Re-scan after those waits and
        # compare its monotonic turn history; normal teardown clearing task=None must
        # not erase the evidence. An armed loop belongs to this refused handoff, so
        # unwind it along with the recorded execution claim.
        if busy_under := await _final_alias_conflict(
            state,
            handoff_dir_key,
            captured_slot_key or str(getattr(slot, "key", "")),
            handoff_aliases,
            handoff_alias_snapshot,
            own_name=name,
        ):
            await _release(
                f"alias became busy during authorization: {busy_under}",
                loop_id=getattr(armed_loop, "id", None),
            )
            return web.json_response(
                {
                    "code": "spec_busy_elsewhere",
                    "error": (
                        f"another view of this spec ({busy_under}) worked on these "
                        "files while execution was starting; retry after it finishes"
                    ),
                },
                status=409,
            )
        # This is synchronous with the same-slot busy check and dispatch below.
        # A Stop or another handoff may revoke the token during the alias await,
        # but nothing can replace it between this check and task publication.
        if not _execution_claim_is_current(handoff_dir_key, execution_claim):
            await _release(
                "stopped during the final alias check",
                loop_id=getattr(armed_loop, "id", None),
            )
            return web.json_response(
                {
                    "code": "execution_stopped_during_start",
                    "error": "execution was stopped before it started",
                },
                status=409,
            )
        # The alias scan above is the final await before dispatch. Channel traffic can
        # also start this same slot while that scan is off-loop. Refuse synchronously;
        # otherwise _dispatch_turn queues the build behind the channel turn and a
        # later Pause can discard it after this endpoint reported success.
        if getattr(slot, "running", False):
            await _release(
                "the spec agent became busy during the final freshness check",
                loop_id=getattr(armed_loop, "id", None),
            )
            return web.json_response(
                {
                    "code": "spec_agent_busy",
                    "error": "the spec agent started another turn; wait for it to finish",
                },
                status=409,
            )
        turn = _dispatch_turn(state, slot, prompt)
        _bind_execution_claim_to_turn(handoff_dir_key, execution_claim, slot, turn)
    _audit("spec_handoff", name)
    return web.json_response({"ok": True, "status": "executing"})


#: Returned when the client's rendered spec identity no longer matches the index.
_STALE_CLIENT_ERROR = "spec was deleted or recreated; reload and retry"


class _ClientClaim(NamedTuple):
    """What the client believes it is acting on. Both fields are optional."""

    spec_dir: str
    slot_key: str


async def _client_claim(request: web.Request) -> _ClientClaim:
    """The identity the CLIENT rendered, from the JSON body or the query string.

    Carries the per-creation ``slot_key`` as well as ``spec_dir``, because a
    directory does NOT identify a creation: deleting a spec leaves its documents on
    disk by design, so re-importing under the same name AND path produces a
    different spec with the same spec_dir -- and a stale tab's Pause would then
    cancel the replacement's run. The slot key is minted per creation, so it is the
    field that actually distinguishes them.

    Optional by design: a control that sends nothing cannot be pinned (an older tab
    predates these fields), so callers treat "" as unpinned rather than refusing. A
    DELETE carries them as query parameters because it has no body.
    """
    dir_claim = str(request.query.get("spec_dir", "") or "").strip()
    key_claim = str(request.query.get("slot_key", "") or "").strip()
    if not (dir_claim and key_claim) and request.can_read_body:
        try:
            body = await request.json()
        except Exception:
            body = None
        if isinstance(body, dict):
            dir_claim = dir_claim or str(body.get("spec_dir", "") or "").strip()
            key_claim = key_claim or str(body.get("slot_key", "") or "").strip()
    return _ClientClaim(dir_claim, key_claim)


def _client_identity_mismatch(
    claim: _ClientClaim, actual_dir: Path | str, actual_slot_key: str = ""
) -> bool:
    """True when the client named a DIFFERENT spec than the one we resolved.

    Either field is enough to refuse, and the SLOT KEY is the decisive one: two
    specs can share a directory across a delete + re-import, but never a
    per-creation key. A field the client did not send is not compared, so an older
    tab keeps working (unpinned, as before).
    """
    if claim.spec_dir and claim.spec_dir != str(actual_dir):
        return True
    return bool(claim.slot_key) and bool(actual_slot_key) and claim.slot_key != actual_slot_key


async def _pinned_entry(request: web.Request, name: str, body: dict) -> dict | web.Response:
    """Resolve the spec FRESH, pinned to the identity the client rendered.

    The shared prologue for every mutation added below, factored out because the
    pinning argument is subtle and six copies of it would drift: the body read is
    an await, so the entry has to be re-read after it, and the client's captured
    ``spec_dir`` + ``slot_key`` are what make a stale tab detectable. These new
    lifecycle controls require both fields: treating an absent claim as unpinned
    would let a control rendered before detail loaded mutate whichever creation
    currently owns the same name.
    """
    claimed_dir = str(body.get("spec_dir", "") or "").strip()
    claimed_key = str(body.get("slot_key", "") or "").strip()
    if not claimed_dir or not claimed_key:
        return web.json_response({"code": "stale_client", "error": _STALE_CLIENT_ERROR}, status=409)
    fresh = await _touch_spec(name, expect_spec_dir=claimed_dir, expect_slot_key=claimed_key)
    if fresh is None:
        return web.json_response({"code": "stale_client", "error": _STALE_CLIENT_ERROR}, status=409)
    return fresh


def _slot_is_writing(slot: Any) -> bool:
    """True once a slot has published an in-flight agent turn."""
    task = getattr(slot, "task", None)
    return bool(
        getattr(slot, "running", False)
        or getattr(slot, "_in_stage_execution", False)
        or (task is not None and not task.done())
    )


def _agent_is_writing(request: web.Request, name: str) -> bool:
    """True while this spec's agent turn is in flight.

    Both the editor and the per-task run refuse in that window. The agent writes
    the spec documents itself, so accepting a save mid-turn means one of the two
    writes silently wins -- and the compare-and-swap hash cannot help, because the
    editor's base hash was valid when the turn STARTED. Refusing is the honest
    answer: the user is told to wait rather than told the save succeeded.
    """
    state = request.app.get("state")
    if state is None:
        return False
    slot = state.get_slot(_slot_key(name))
    return _slot_is_writing(slot)


async def _handle_approve(request: web.Request) -> web.Response:
    """Serialize approval recording with every turn that can change the document."""
    if denied := _require_auth(request):
        return denied
    name = request.match_info["name"]
    index = await _aload_index()
    meta = index.get(name)
    if not isinstance(meta, dict) or meta.get(_DELETING):
        return await _approve_locked(request)
    async with _turn_lock(str(meta.get("spec_dir", ""))):
        return await _approve_locked(request)


async def _approve_locked(request: web.Request) -> web.Response:
    """Record a human approval of one phase, against the version approved.

    Records rather than enforces, and the distinction is deliberate. The agent
    writes through its own file tools, so this API cannot enforce phase order
    without owning that filesystem access. The record still preserves who approved
    which exact text.
    """
    if denied := _require_auth(request):
        return denied
    name = request.match_info["name"]
    body = await _read_json(request)
    if isinstance(body, web.Response):
        return body
    phase = str(body.get("phase", "")).strip()
    if phase not in _APPROVABLE_PHASES:
        return web.json_response(
            {"code": "invalid_phase", "error": f"phase must be one of {list(_APPROVABLE_PHASES)}"},
            status=400,
        )
    claimed_hash = str(body.get("hash", "") or "")
    if not _SHA256_RE.match(claimed_hash):
        return web.json_response(
            {"code": "invalid_hash", "error": "hash must be a sha256 hex digest"}, status=400
        )
    fresh = await _pinned_entry(request, name, body)
    if isinstance(fresh, web.Response):
        return fresh
    spec_dir = Path(str(fresh.get("spec_dir", "")))
    captured_slot_key = str(fresh.get("slot_key", ""))
    fname = phase + ".md"

    def _current_hash() -> str:
        text = _read_spec_text(spec_dir, fname)
        return _sha256_text(text) if text is not None else ""

    actual = await asyncio.to_thread(_current_hash)
    if actual != claimed_hash:
        # Approving a version you have not seen records nothing meaningful, so the
        # client is sent back to re-read rather than having its claim trusted.
        return web.json_response(
            {
                "code": "doc_changed",
                "error": f"{fname} changed since you reviewed it — reload before approving",
                "current_hash": actual,
            },
            status=409,
        )
    user = str(request.get("user") or "")
    record = {"hash": claimed_hash, "at": time.time(), "user": user[:_MAX_FIELD]}

    def _record(index: dict) -> bool:
        meta = index.get(name)
        if meta is None or meta.get(_DELETING):
            return False
        if str(meta.get("spec_dir", "")) != str(spec_dir):
            return False
        if captured_slot_key and str(meta.get("slot_key", "")) != captured_slot_key:
            return False
        # Merged INSIDE the lock rather than by reading the dict out, editing it and
        # stamping it back: the read-modify-write would drop a second phase's
        # approval that landed in between, and this is the one field where losing a
        # record silently defeats the point of having it.
        existing = meta.get("approvals")
        approvals = dict(existing) if isinstance(existing, dict) else {}
        approvals[phase] = record
        meta["approvals"] = approvals
        meta["updated_at"] = time.time()
        return True

    if not await _mutate_index(_record):
        return web.json_response({"code": "stale_client", "error": _STALE_CLIENT_ERROR}, status=409)
    _audit("spec_phase_approve", f"{name}/{phase}")
    return web.json_response({"ok": True, "phase": phase, "hash": claimed_hash})


async def _handle_run_task(request: web.Request) -> web.Response:
    """Run ONE task from tasks.md as a single turn.

    The whole-list handoff arms an autonudge loop over every unchecked task, which
    is the only granularity the app had: there was no way to run one task, and no
    way to see which task a run was on. This dispatches a single scoped turn and
    stops, and progress stays derived from the file's checkboxes.
    """
    if denied := _require_auth(request):
        return denied
    name = request.match_info["name"]
    body = await _read_json(request)
    if isinstance(body, web.Response):
        return body
    raw_index = body.get("index")
    if not isinstance(raw_index, int) or isinstance(raw_index, bool) or raw_index < 0:
        return web.json_response(
            {"code": "invalid_index", "error": "index must be a non-negative integer"}, status=400
        )
    claimed_hash = str(body.get("hash", "") or "")
    if not _SHA256_RE.match(claimed_hash):
        return web.json_response(
            {"code": "invalid_hash", "error": "hash must be a sha256 hex digest"}, status=400
        )
    fresh = await _pinned_entry(request, name, body)
    if isinstance(fresh, web.Response):
        return fresh
    state = request.app.get("state")
    # An autonudge loop already working the whole list would collide with a
    # single-task turn: both write the same files and both check boxes off.
    if (
        await _effective_status(name, fresh, state.get_slot(_slot_key(name)) if state else None)
        == "executing"
    ):
        return web.json_response(
            {
                "code": "already_executing",
                "error": "this spec is already building — pause it first",
            },
            status=409,
        )
    if _agent_is_writing(request, name):
        return web.json_response(
            {
                "code": "agent_running",
                "error": "the agent is busy right now — wait for the turn to finish",
            },
            status=409,
        )
    spec_dir = Path(str(fresh.get("spec_dir", "")))

    def _task_snapshot() -> tuple[dict | None, str]:
        tasks = _parse_tasks(_read_spec_text(spec_dir, "tasks.md") or "")
        if raw_index >= len(tasks):
            return None, "task_not_found"
        candidate = tasks[raw_index]
        # Position AND text must both still match. The agent rewrites tasks.md
        # between polls, so an index alone is a moving target and a click on
        # "task 3" could otherwise dispatch whatever ended up third.
        if candidate["hash"] != claimed_hash:
            return None, "task_changed"
        if candidate["done"]:
            return None, "task_done"
        return candidate, ""

    def _task_conflict(code: str) -> web.Response:
        errors = {
            "task_not_found": "that task is no longer in the list — reload",
            "task_changed": "that task changed since the list was rendered — reload and pick it again",
            "task_done": "that task is already checked off",
        }
        return web.json_response({"code": code, "error": errors[code]}, status=409)

    task, task_error = await asyncio.to_thread(_task_snapshot)
    if task_error:
        return _task_conflict(task_error)
    # Hold the same per-spec lock that Execute uses to claim execution and Delete
    # uses to reserve teardown BEFORE materializing the worker slot. If Delete
    # captured "no slot" while _ensure_worker_slot awaited and this request then
    # restored one, Delete's identity-pinned teardown would deliberately leave the
    # new slot behind as an orphan. Re-pin first under the lock; after that Delete
    # either already owns the entry and no slot is created, or waits until the task
    # publishes its slot/turn and can capture that exact runtime.
    async with _turn_lock(str(spec_dir)):
        before_slot = await _touch_spec(
            name,
            expect_spec_dir=str(spec_dir),
            expect_slot_key=str(fresh.get("slot_key", "")) or None,
        )
        if before_slot is None:
            return web.json_response(
                {"code": "stale_client", "error": _STALE_CLIENT_ERROR}, status=409
            )
        current_slot = state.get_slot(_slot_key(name)) if state else None
        if await _effective_status(name, before_slot, current_slot) == "executing":
            return web.json_response(
                {
                    "code": "already_executing",
                    "error": "this spec is already building — pause it first",
                },
                status=409,
            )
        if _agent_is_writing(request, name):
            return web.json_response(
                {
                    "code": "agent_running",
                    "error": "the agent is busy right now — wait for the turn to finish",
                },
                status=409,
            )
        slot = await _ensure_worker_slot(state, name, before_slot)
        if slot is None:
            return web.json_response(
                {
                    "code": "slot_owned_by_another_app",
                    "error": "this spec's chat session is owned by another app",
                },
                status=409,
            )
        # Slot setup awaits, so re-pin the creation before using the materialized
        # slot. Delete cannot cross the lock, while other identity mutations still
        # fail this check.
        final_fresh = await _touch_spec(
            name,
            expect_spec_dir=str(spec_dir),
            expect_slot_key=str(fresh.get("slot_key", "")) or None,
        )
        if final_fresh is None:
            return web.json_response(
                {"code": "stale_client", "error": _STALE_CLIENT_ERROR}, status=409
            )
        if await _effective_status(name, final_fresh, slot) == "executing":
            return web.json_response(
                {
                    "code": "already_executing",
                    "error": "this spec is already building — pause it first",
                },
                status=409,
            )
        if _agent_is_writing(request, name):
            return web.json_response(
                {
                    "code": "agent_running",
                    "error": "the agent is busy right now — wait for the turn to finish",
                },
                status=409,
            )
        # Slot setup and status reconciliation both await. The IDE can edit
        # tasks.md during either window, so the earlier snapshot is no longer safe
        # to dispatch. Execute and Delete cannot cross this final awaited reread,
        # and _dispatch_turn publishes slot.task synchronously before the lock is
        # released.
        task, task_error = await asyncio.to_thread(_task_snapshot)
        if task_error:
            return _task_conflict(task_error)
        assert task is not None
        if _agent_is_writing(request, name):
            return web.json_response(
                {
                    "code": "agent_running",
                    "error": "the agent is busy right now — wait for the turn to finish",
                },
                status=409,
            )
        _dispatch_turn(
            state,
            slot,
            _task_prompt(
                name,
                spec_dir,
                str(final_fresh.get("working_dir", "")),
                task["text"],
                task["index"],
            ),
        )
    _audit("spec_task_run", f"{name}#{raw_index}")
    return web.json_response({"ok": True, "index": raw_index})


async def _handle_title(request: web.Request) -> web.Response:
    """Set a spec's display label.

    A rename, but of the LABEL only -- and that limit is the design, not a
    shortcut. The name is simultaneously the on-disk directory under
    ``.kiro/specs/``, the ``spec/<name>`` git branch, and the chat slot key, and
    ``_owns_slot_key`` requires the key to ENCODE the indexed name. So renaming the
    identity would move a directory the IDE and CLI also read, rewrite a branch
    that may already have commits, and orphan the spec's transcript, which is the
    very thing delete-and-recreate loses. A label fixes what users actually hit --
    a spec misnamed at the New Spec screen -- and costs none of that.
    """
    if denied := _require_auth(request):
        return denied
    name = request.match_info["name"]
    body = await _read_json(request)
    if isinstance(body, web.Response):
        return body
    if "title" not in body:
        return web.json_response({"code": "title_required", "error": "title required"}, status=400)
    title = str(body.get("title") or "").strip()[:120]
    fresh = await _pinned_entry(request, name, body)
    if isinstance(fresh, web.Response):
        return fresh
    # "" clears the label and the UI falls back to the name, so an empty title is
    # a reset rather than an error.
    if (
        await _touch_spec(
            name,
            expect_spec_dir=str(fresh.get("spec_dir", "")),
            expect_slot_key=str(fresh.get("slot_key", "")) or None,
            title=title,
        )
        is None
    ):
        return web.json_response({"code": "stale_client", "error": _STALE_CLIENT_ERROR}, status=409)
    _audit("spec_title", name)
    return web.json_response({"ok": True, "title": title})


async def _handle_archive(request: web.Request) -> web.Response:
    """Move a spec out of the working set, or bring it back.

    The non-destructive counterpart to delete: documents, transcript and index
    entry all stay, so an archived spec is recoverable by definition. Delete was
    the only lifecycle operation besides create, which meant tidying up a finished
    spec and destroying it were the same act.
    """
    if denied := _require_auth(request):
        return denied
    name = request.match_info["name"]
    body = await _read_json(request)
    if isinstance(body, web.Response):
        return body
    archived = body.get("archived")
    if not isinstance(archived, bool):
        return web.json_response(
            {"code": "archived_required", "error": "archived must be a boolean"}, status=400
        )
    fresh = await _pinned_entry(request, name, body)
    if isinstance(fresh, web.Response):
        return fresh
    state = request.app.get("state")
    if (
        archived
        and await _effective_status(name, fresh, state.get_slot(_slot_key(name)) if state else None)
        == "executing"
    ):
        # Archiving a running spec would hide a loop that keeps editing files, so
        # the user would have no surface left to stop it from.
        return web.json_response(
            {"code": "spec_executing", "error": "pause this spec before archiving it"}, status=409
        )
    if (
        await _touch_spec(
            name,
            expect_spec_dir=str(fresh.get("spec_dir", "")),
            expect_slot_key=str(fresh.get("slot_key", "")) or None,
            archived=archived,
        )
        is None
    ):
        return web.json_response({"code": "stale_client", "error": _STALE_CLIENT_ERROR}, status=409)
    _audit("spec_archive" if archived else "spec_unarchive", name)
    return web.json_response({"ok": True, "archived": archived})


async def _handle_duplicate(request: web.Request) -> web.Response:
    """Serialize a copy with work on both its source and destination directories."""
    if denied := _require_auth(request):
        return denied
    name = request.match_info["name"]
    body = await _read_json(request)
    if isinstance(body, web.Response):
        return body
    new_name = str(body.get("new_name", "")).strip()
    index = await _aload_index()
    meta = index.get(name)
    if not isinstance(meta, dict) or meta.get(_DELETING) or not _usable_name(new_name):
        return await _duplicate_locked(request)
    safe_wd = await asyncio.to_thread(_safe_dir, str(meta.get("working_dir", "")))
    if safe_wd is None:
        return await _duplicate_locked(request)
    source_key = _turn_key(str(meta.get("spec_dir", "")))
    target_key = _turn_key(str(safe_wd / ".kiro" / "specs" / new_name))
    first_key, second_key = sorted((source_key, target_key))
    async with _turn_lock(first_key):
        if first_key == second_key:
            return await _duplicate_locked(request)
        async with _turn_lock(second_key):
            return await _duplicate_locked(request)


async def _duplicate_locked(request: web.Request) -> web.Response:
    """Copy a spec's documents into a new spec.

    The recovery path for the case rename cannot serve: a spec whose NAME is wrong
    after it already has a branch or history. The copy takes the documents and
    nothing else -- new name, new directory, new slot key, so a fresh conversation
    rather than a replayed one. No worktree either; that is an opt-in at create
    time and silently branching off someone's repo is not a copy operation.
    """
    if denied := _require_auth(request):
        return denied
    name = request.match_info["name"]
    body = await _read_json(request)
    if isinstance(body, web.Response):
        return body
    new_name = str(body.get("new_name", "")).strip()
    if not _usable_name(new_name):
        return web.json_response(
            {
                "code": "invalid_name",
                "error": (
                    "new_name must be 1-64 chars: letters, digits, '-' or '_', "
                    "and must not look like a credential"
                ),
            },
            status=400,
        )
    fresh = await _pinned_entry(request, name, body)
    if isinstance(fresh, web.Response):
        return fresh
    if new_name == name:
        return web.json_response(
            {"code": "spec_exists", "error": "that is the same name"}, status=409
        )
    if _agent_is_writing(request, name):
        return web.json_response(
            {
                "code": "agent_running",
                "error": "the agent is busy right now — wait for the turn to finish",
            },
            status=409,
        )
    working_dir = str(fresh.get("working_dir", ""))
    safe_wd = await asyncio.to_thread(_safe_dir, working_dir)
    if safe_wd is None:
        return web.json_response(
            {
                "code": "working_dir_not_a_directory",
                "error": "this spec's project folder is no longer usable",
            },
            status=400,
        )
    source_dir = Path(str(fresh.get("spec_dir", "")))

    def _source_snapshot() -> tuple[dict[str, str | None], list[str]]:
        """Read every phase file once, distinguishing absent from unsafe."""
        payload: dict[str, str | None] = {}
        unreadable: list[str] = []
        for _phase, fname in _PHASE_FILES:
            try:
                os.lstat(source_dir / fname)
                existed = True
            except FileNotFoundError:
                existed = False
            except OSError:
                payload[fname] = None
                unreadable.append(fname)
                continue
            text = _read_spec_text(source_dir, fname)
            payload[fname] = text
            if text is None and existed:
                unreadable.append(fname)
        return payload, unreadable

    def _copy() -> tuple[Path, str, dict[str, str | None], list[str]]:
        """Read the source documents, then validate the destination. ONE hop."""
        payload, unreadable = _source_snapshot()
        target, refusal = _prepare_spec_dir(str(safe_wd), safe_wd, new_name, False, create=False)
        return target, refusal, payload, unreadable

    target_dir, refusal, docs, unreadable = await asyncio.to_thread(_copy)
    if unreadable:
        return web.json_response(
            {
                "code": "spec_document_unreadable",
                "error": "one or more source documents could not be read safely",
            },
            status=409,
        )
    if refusal:
        kind = refusal.partition(":")[0]
        if kind == "existing":
            return web.json_response(
                {
                    "code": "spec_files_exist",
                    "error": f"'{new_name}' already has spec files on disk",
                },
                status=409,
            )
        if kind == "escape":
            _audit("spec_path_escape_denied", f"{new_name} -> {target_dir}")
            return web.json_response(
                {
                    "code": "spec_path_outside_root",
                    "error": "resolved spec path is outside its root",
                },
                status=400,
            )
        return web.json_response(
            {"code": "spec_dir_creation_failed", "error": "cannot create the copy's directory"},
            status=400,
        )
    if not any(text is not None for text in docs.values()):
        return web.json_response(
            {"code": "nothing_to_copy", "error": "this spec has no documents to copy yet"},
            status=409,
        )
    # One read per document is not a snapshot: the agent can finish writing
    # requirements after it was read and then write design before that file is
    # read. A second identical pass proves the payload formed one stable view,
    # while the slot checks reject the known writer on both sides of the awaits.
    if _agent_is_writing(request, name):
        return web.json_response(
            {
                "code": "agent_running",
                "error": "the agent is busy right now — wait for the turn to finish",
            },
            status=409,
        )
    confirmed_docs, confirmed_unreadable = await asyncio.to_thread(_source_snapshot)
    if confirmed_unreadable or confirmed_docs != docs:
        return web.json_response(
            {
                "code": "spec_changed_during_duplicate",
                "error": "the source documents changed while they were being copied — retry",
            },
            status=409,
        )
    if _agent_is_writing(request, name):
        return web.json_response(
            {
                "code": "agent_running",
                "error": "the agent is busy right now — wait for the turn to finish",
            },
            status=409,
        )

    slot_key = _new_slot_key(new_name)
    duplicate_token = uuid.uuid4().hex
    stage_dir = target_dir.parent / f".{new_name}.duplicate-{duplicate_token}"
    document_hashes = {
        fname: _sha256_text(text) for fname, text in docs.items() if text is not None
    }
    now = time.time()
    entry = {
        "working_dir": str(safe_wd),
        "spec_dir": str(target_dir),
        # Validated, not carried over blind: spec_type comes off the agent-writable
        # index, and an unknown value would flow into the copy's own payload.
        "spec_type": (
            st if (st := str(fresh.get("spec_type", "feature"))) in _VALID_TYPES else "feature"
        ),
        "status": "planning",
        "slot_key": slot_key,
        "worktree_branch": "",
        "repo_root": "",
        "title": _clean_str(fresh.get("title")),
        "created_at": now,
        "updated_at": now,
        _DUPLICATING: {
            "owner": _PROCESS_ID,
            "at": now,
            "token": duplicate_token,
            "stage_dir": str(stage_dir),
            "documents": document_hashes,
        },
    }

    def _insert(index: dict) -> bool:
        if new_name in index:
            return False
        index[new_name] = entry
        return True

    stage_failure = await asyncio.to_thread(_create_duplicate_stage, stage_dir, duplicate_token)
    if stage_failure:
        _audit("spec_duplicate_failed", f"{name} -> {new_name}", outcome="failure")
        if stage_failure == "unsupported_platform":
            return web.json_response(
                {
                    "code": "doc_write_unsupported",
                    "error": "duplicating is not available on this platform",
                },
                status=501,
            )
        return web.json_response(
            {"code": "doc_write_failed", "error": "could not write the copy"}, status=400
        )
    stage_identity = await asyncio.to_thread(_duplicate_stage_identity, stage_dir, duplicate_token)
    if stage_identity is None:
        await asyncio.to_thread(_remove_duplicate_marker, stage_dir, duplicate_token)
        _audit("spec_duplicate_failed", f"{name} -> {new_name}", outcome="failure")
        return web.json_response(
            {"code": "doc_write_failed", "error": "could not write the copy"}, status=400
        )
    held = entry[_DUPLICATING]
    assert isinstance(held, dict)
    held["stage_dev"], held["stage_ino"] = stage_identity

    async def _release_reservation() -> bool:
        def _pop(index: dict) -> bool:
            meta = index.get(new_name)
            if (
                meta is None
                or str(meta.get("slot_key", "")) != slot_key
                or not _reservation_is_ours(meta, _DUPLICATING)
            ):
                return False
            del index[new_name]
            return True

        return await _mutate_index(_pop)

    def _finish(index: dict) -> bool:
        meta = index.get(new_name)
        if (
            meta is None
            or str(meta.get("slot_key", "")) != slot_key
            or not _reservation_is_ours(meta, _DUPLICATING)
        ):
            return False
        meta.pop(_DUPLICATING, None)
        meta["updated_at"] = time.time()
        return True

    async def _complete_transaction() -> tuple[str, str, Path]:
        """Reach a durable terminal state after publishing transaction provenance."""
        if not await _mutate_index(_insert):
            # No reservation points at this empty, marker-only stage. A crash
            # before cleanup strands no copied document.
            await asyncio.to_thread(_remove_duplicate_marker, stage_dir, duplicate_token)
            return "exists", "", target_dir

        # The marked stage exists before the name is reserved, but it is not
        # populated yet. Re-run validation after arbitration so an external
        # writer that placed files in the meantime is refused, not overwritten.
        resolved_target, reserved_refusal = await asyncio.to_thread(
            _prepare_spec_dir,
            str(safe_wd),
            safe_wd,
            new_name,
            False,
            create=False,
            expected_dir=target_dir,
        )
        if reserved_refusal:
            if await _release_reservation():
                # The stage contains no documents. Removing the reservation
                # first leaves only an empty marker directory after a crash.
                await asyncio.to_thread(_remove_duplicate_marker, stage_dir, duplicate_token)
            return "refusal", reserved_refusal, resolved_target

        failure, created = await asyncio.to_thread(
            _write_and_publish_duplicate,
            stage_dir,
            resolved_target,
            docs,
            duplicate_token,
            stage_identity,
        )
        if failure:
            if failure == "identity_mismatch":
                # A competing directory won the publication name. It is not our
                # copy, so never leave this duplicate's index entry pointing at
                # it; the source documents remain available for a clean retry.
                await _release_reservation()
                return "write_failed", failure, resolved_target
            # Keep the marker while rolling back. If the process exits during
            # this step, recovery still has proof that the reservation and any
            # staged documents belong to this transaction. Release the index
            # only after every editable document is confirmed absent, then
            # remove the marker last.
            rolled_back = await asyncio.to_thread(_rollback_staged_docs, stage_dir, created)
            if rolled_back and await _release_reservation():
                await asyncio.to_thread(_remove_duplicate_marker, stage_dir, duplicate_token)
            return "write_failed", failure, resolved_target

        await asyncio.to_thread(_forget_deleted, str(resolved_target))
        try:
            finalized = await _mutate_index(_finish)
        except Exception:
            # Publication already committed. Keep its marker and reservation so
            # startup recovery can adopt the complete copy, while containing a
            # storage failure as the same recoverable response as a lost claim.
            logger.exception("could not finalize duplicate index entry for %s", new_name)
            return "finalization_failed", "", resolved_target
        if not finalized:
            return "finalization_failed", "", resolved_target
        await asyncio.to_thread(_remove_duplicate_marker, resolved_target, duplicate_token)
        return "success", "", resolved_target

    transaction = asyncio.create_task(_complete_transaction())
    try:
        # The thread performing publication cannot be stopped by task
        # cancellation. Shield reservation and finalization together, so the
        # request cannot abandon a same-process reservation that recovery skips.
        outcome, detail, target_dir = await asyncio.shield(transaction)
    except asyncio.CancelledError as cancelled:
        # Keep this handler as a strong owner of the transaction and do not
        # report cancellation until its index state is terminal. Repeated
        # cancellation (for example during server shutdown) cannot reopen the
        # same-process recovery gap.
        while not transaction.done():
            try:
                await asyncio.shield(transaction)
            except asyncio.CancelledError:
                continue
        transaction.result()
        raise cancelled

    if outcome == "exists":
        return web.json_response(
            {"code": "spec_exists", "error": f"a spec named '{new_name}' already exists"},
            status=409,
        )

    if outcome == "refusal":
        kind = detail.partition(":")[0]
        if kind == "moved":
            return web.json_response(
                {
                    "code": "spec_destination_changed",
                    "error": "the copy destination changed while it was being created; retry",
                },
                status=409,
            )
        if kind == "existing":
            return web.json_response(
                {
                    "code": "spec_files_exist",
                    "error": f"'{new_name}' already has spec files on disk",
                },
                status=409,
            )
        if kind == "escape":
            _audit("spec_path_escape_denied", f"{new_name} -> {target_dir}")
            return web.json_response(
                {
                    "code": "spec_path_outside_root",
                    "error": "resolved spec path is outside its root",
                },
                status=400,
            )
        return web.json_response(
            {"code": "spec_dir_creation_failed", "error": "cannot create the copy's directory"},
            status=400,
        )

    if outcome == "write_failed":
        _audit("spec_duplicate_failed", f"{name} -> {new_name}", outcome="failure")
        if detail == "unsupported_platform":
            return web.json_response(
                {
                    "code": "doc_write_unsupported",
                    "error": "duplicating is not available on this platform",
                },
                status=501,
            )
        return web.json_response(
            {"code": "doc_write_failed", "error": "could not write the copy"}, status=400
        )

    if outcome == "finalization_failed":
        # Publication is already atomic and visible. Preserve the complete,
        # marker-provenanced copy so a surviving reservation can recover it on
        # restart; deleting its contents would leave a destination name that no
        # future no-replace publication could win.
        return web.json_response(
            {
                "code": "spec_changed_during_create",
                "error": "the copy was published but its reservation changed; reopen or import the existing copy",
            },
            status=409,
        )
    entry.pop(_DUPLICATING, None)
    # adopt_closed=False for the same reason create passes it: a name reused after
    # a delete must not hand the fresh agent the deleted spec's transcript.
    slot = await _ensure_worker_slot(request.app.get("state"), new_name, entry, adopt_closed=False)
    if slot is None:
        # The index and documents are committed before session arbitration.
        # Retain both so the published copy stays discoverable and recoverable.
        return web.json_response(
            {
                "code": "slot_owned_by_another_app",
                "error": f"a chat session named '{new_name}' is owned by another app",
            },
            status=409,
        )
    try:
        slot.title = f"Spec: {new_name}"
        slot._titled = True
        if (state := request.app.get("state")) is not None and hasattr(state, "push_slot_title"):
            state.push_slot_title(slot.key, slot.title)
    except Exception:
        logger.debug("title set failed", exc_info=True)
    _dispatch_turn(request.app.get("state"), slot, _duplicate_prompt(new_name, name, target_dir))
    _audit("spec_duplicate", f"{name} -> {new_name}")
    return web.json_response({"name": new_name, "spec_dir": _redact(str(target_dir))}, status=201)


async def _handle_stop_execution(request: web.Request) -> web.Response:
    if denied := _require_auth(request):
        return denied
    name = request.match_info["name"]
    # Parse the body FIRST. Reading it is an await, so doing it after the index
    # read reopened the very window the capture below is meant to close: a
    # delete+recreate landing while a slow request body arrived left the index
    # snapshot (and the identity check against it) describing the OLD spec while
    # the loop id and slot captured afterwards belonged to the REPLACEMENT, whose
    # run this request would then cancel.
    claimed = await _client_claim(request)
    index = await _aload_index()
    meta = index.get(name)
    if not meta:
        return web.json_response({"code": "not_found", "error": "not found"}, status=404)
    spec_dir = Path(meta["spec_dir"])
    # Stop is destructive, so it takes the SAME directory turn lock the message,
    # handoff and delete paths take. Without it Stop was the one way to interleave
    # with a decision answer: that path records the answer and dispatches it under
    # this lock, and an unserialized Stop landing between those two steps cancelled
    # the dispatched turn while the recorded answer stood -- leaving a card locked
    # to an answer the agent never received. The record is deliberately never
    # rewritten (a rewrite is how a decision gets reversed), so the fix is to stop
    # the interleaving rather than to undo the write: with the lock there are two
    # orderings instead of three, and both are honest. Answer then Stop cancels a
    # turn that really was dispatched; Stop then answer refuses at the busy check.
    dir_key = _decision_key(str(spec_dir))
    # Keep both identities. The raw key pins the mutable index row across awaits;
    # the monotonic resolver key identifies the live worker and is what detail gave
    # the client. They legitimately differ after an agent rewrites index.json.
    original_index_slot_key = str(meta.get("slot_key", ""))
    original_slot_key = _slot_key(name)
    # A stale tab must be refused before it can publish a Stop barrier. There is
    # no await between this check and entering the creation-scoped barrier below.
    if _client_identity_mismatch(claimed, spec_dir, original_slot_key):
        return web.json_response({"code": "stale_client", "error": _STALE_CLIENT_ERROR}, status=409)
    # Publish the Stop before waiting for the directory lock. A handoff may be
    # suspended inside authorization while holding that lock; revoking its
    # process-owned generation makes it unwind the loop instead of dispatching,
    # and the barrier refuses any restart for this creation until Stop commits.
    async with (
        _execution_stop_barrier(dir_key, original_slot_key, name) as claimed_slot_keys,
        _turn_lock(dir_key),
    ):
        # Re-read INSIDE the lock. Acquiring it is an await, so the snapshot above can
        # describe a spec that was replaced while this request waited, and the identity
        # check has to judge the spec actually about to be halted.
        index = await _aload_index()
        meta = index.get(name)
        if not meta:
            return web.json_response({"code": "not_found", "error": "not found"}, status=404)
        spec_dir = Path(meta["spec_dir"])
        if str(meta.get("slot_key", "")) != original_index_slot_key:
            # A different creation now holds this name. Halting would write a STOP
            # sentinel for, and cancel the run of, a spec this request never verified.
            return web.json_response(
                {"code": "stale_client", "error": _STALE_CLIENT_ERROR}, status=409
            )
        if _decision_key(str(spec_dir)) != dir_key:
            # Kept alongside the slot-key check because it answers a different question:
            # the index is agent-writable, so an entry can be repointed at another
            # directory WITHOUT a recreate, leaving the slot key intact while the lock
            # held is no longer the one guarding these documents.
            # now would serialize against nothing that matters and could cancel the
            # replacement's run. Refuse and let the client retry against what exists.
            return web.json_response(
                {"code": "stale_client", "error": _STALE_CLIENT_ERROR}, status=409
            )
        # From here to the capture there is NO await: the halt writes a sentinel,
        # removes the nudge loop and cancels the running turn, and all three are
        # looked up by name.
        if _client_identity_mismatch(claimed, spec_dir, _slot_key(name)):
            return web.json_response(
                {"code": "stale_client", "error": _STALE_CLIENT_ERROR}, status=409
            )
        state = request.app.get("state")
        # The creation this request verified, carried to the commit below.
        captured_slot_key = _slot_key(name)
        captured_loop_id = _exec_loop_id(name)
        captured_slot = state.get_slot(_slot_key(name)) if state is not None else None
        stop_slots: list[Any] = []
        if state is not None:
            for claimed_slot_key in claimed_slot_keys:
                claimed_slot = state.get_slot(claimed_slot_key)
                if claimed_slot is not None and claimed_slot not in stop_slots:
                    stop_slots.append(claimed_slot)
        if captured_slot is not None and captured_slot not in stop_slots:
            stop_slots.append(captured_slot)
        primary_slot = stop_slots[0] if stop_slots else None
        try:
            await _halt_execution(
                state,
                name,
                spec_dir,
                reason="user stop",
                only_loop_id=captured_loop_id,
                only_slot=primary_slot,
                expect_slot_key=original_index_slot_key,
            )
            for claimed_slot_key, claimed_loop_id in claimed_slot_keys.items():
                if claimed_slot_key == captured_slot_key and claimed_loop_id == captured_loop_id:
                    continue
                await _remove_nudge_loop_for_slot(claimed_slot_key, only_loop_id=claimed_loop_id)
            for extra_slot in stop_slots[1:]:
                await _halt_active_turn(state, name, only_slot=extra_slot)
        except Exception:
            # A failed loop removal means the run can still nudge itself; saying
            # "stopped" would be false and the user would not retry.
            logger.warning("spec %s: halt failed", name, exc_info=True)
            _audit("spec_stop_failed", name, outcome="denied")
            return web.json_response(
                {
                    "code": "stop_failed",
                    "error": "could not stop the run; it may still be working — retry",
                },
                status=503,
            )
        # Re-reading commit: halting awaits, so a concurrent DELETE in that window
        # must not be undone by writing back the snapshot above. The halt itself is
        # idempotent, so nothing is lost by reporting the deletion instead.
        if (
            await _touch_spec(
                name,
                expect_spec_dir=str(spec_dir),
                expect_slot_key=original_index_slot_key or None,
                status="planning",
            )
            is None
        ):
            # Gone, or recreated elsewhere under the same name -- in which case the
            # STOP sentinel we just wrote belongs to the OLD spec and this request
            # must not mark the NEW one as stopped.
            return web.json_response({"code": "not_found", "error": "not found"}, status=404)
        claimed_slot_keys.commit()
    _audit("spec_stop_execution", name)
    return web.json_response({"ok": True, "status": "planning"})


async def _handle_delete(request: web.Request) -> web.Response:
    if denied := _require_auth(request):
        return denied
    name = request.match_info["name"]
    # Body first, then the index: see _handle_stop_execution. A body await
    # between the two would let a replacement spec be the thing torn down.
    claimed = await _client_claim(request)
    index, doomed_runtime_slot_key, doomed_observed_slot_key = (
        await _aload_index_with_slot_identity(name)
    )
    if name not in index:
        return web.json_response({"code": "not_found", "error": "not found"}, status=404)
    doomed_dir = str(index[name].get("spec_dir", ""))
    # The creation we verified, carried to the commit below so the entry that gets
    # dropped is the one this request checked.
    doomed_slot_key = str(index[name].get("slot_key", ""))
    # A tampered raw key can differ from the authenticated identity this process
    # has already observed. Successful deletion owns both spellings and must release
    # both, otherwise a same-name recreation remains pinned to the deleted worker.
    # A legacy row has no persisted key, but its name-derived runtime key is still
    # the creation identity captured by this request. Carry that fallback through
    # both index transactions so a same-path replacement cannot satisfy an empty pin.
    # Prefer a non-empty raw spelling so a deliberately malformed/tampered row remains
    # a reachable cleanup endpoint for the authenticated runtime identity.
    doomed_commit_slot_key = doomed_slot_key or doomed_runtime_slot_key
    if _client_identity_mismatch(claimed, doomed_dir, doomed_runtime_slot_key):
        return web.json_response({"code": "stale_client", "error": _STALE_CLIENT_ERROR}, status=409)
    # Hold the same directory lock as message and handoff across the destructive
    # sequence. Those handlers must observe either a live spec or no spec, never a
    # teardown window in which they can start a turn or record an answer.
    doomed_key = _decision_key(doomed_dir)
    # Publish the same creation-scoped revocation as Stop before waiting for the
    # mutable directory lock. If the agent repointed this name while a message's
    # final scan was off-thread, deleting through the new spelling must still
    # prevent that stale request from publishing onto the old slot afterwards.
    async with (
        _execution_stop_barrier(doomed_key, doomed_runtime_slot_key, name) as claimed_slot_keys,
        _turn_lock(doomed_key),
    ):
        _fresh_index, decision_alias_conflict, _decision_store_usable = (
            await _aload_index_with_decision_alias_status(doomed_dir)
        )
        if decision_alias_conflict:
            return web.json_response(
                {
                    "code": "decision_directory_alias_conflict",
                    "error": "multiple spec names resolve to this directory; repair the spec index before continuing",
                },
                status=409,
            )
        # Publish the tombstone before the entry becomes hidden; otherwise discovery
        # could re-adopt the documents during teardown. Every non-delete exit clears it.
        await asyncio.to_thread(_remember_deleted, doomed_dir)
        # Reserve rather than drop the name during teardown. This keeps same-name
        # creation out and lets rollback restore the original per-creation slot key.
        if not await _mark_deleting(
            name, expect_spec_dir=doomed_dir, expect_slot_key=doomed_commit_slot_key
        ):
            await asyncio.to_thread(_forget_deleted, doomed_dir)
            return web.json_response({"code": "not_found", "error": "not found"}, status=404)
        # RESERVED -- only now capture the runtime. Capturing before the reservation left
        # a window where a message could materialize a NEW slot (or arm a new loop) that
        # this capture had already passed: the teardown below then cancelled a stale
        # handle while the freshly-created session kept running the agent against files
        # the user had just deleted. With the marker set first, _touch_spec refuses that
        # message, so nothing new can appear between here and the teardown.
        state = request.app.get("state")
        doomed_loop_id = _exec_loop_id(name)
        doomed_slot = state.get_slot(_slot_key(name)) if state is not None else None
        doomed_slots: list[Any] = []
        if state is not None:
            for claimed_slot_key in claimed_slot_keys:
                claimed_slot = state.get_slot(claimed_slot_key)
                if claimed_slot is not None and claimed_slot not in doomed_slots:
                    doomed_slots.append(claimed_slot)
        if doomed_slot is not None and doomed_slot not in doomed_slots:
            doomed_slots.append(doomed_slot)
        if not doomed_slots:
            # Preserve the teardown boundary even when no runtime slot exists.
            # The helper treats a pinned None as a no-op, while callers still get
            # one archive/failure boundary before the final index transaction.
            doomed_slots.append(None)
        # Stop any execution loop; leave the .md files on disk (they are the user's
        # project files under .kiro/specs) — only drop app bookkeeping + the slot.
        try:
            await _remove_nudge_loop(name, only_loop_id=doomed_loop_id)
            for claimed_slot_key, claimed_loop_id in claimed_slot_keys.items():
                if (
                    claimed_slot_key == doomed_runtime_slot_key
                    and claimed_loop_id == doomed_loop_id
                ):
                    continue
                await _remove_nudge_loop_for_slot(claimed_slot_key, only_loop_id=claimed_loop_id)
        except Exception:
            # Fail the delete rather than report success: the entry is still in the
            # index, so a retry is meaningful, and the persisted loop cannot rearm
            # against a same-name spec re-imported later. Release the reservation and
            # the tombstone too -- both were taken above, and leaving either behind
            # would hide a spec the user still has from their own list.
            logger.warning("spec %s: loop removal failed — delete aborted", name, exc_info=True)
            await _unmark_deleting(name, expect_spec_dir=doomed_dir)
            await asyncio.to_thread(_forget_deleted, doomed_dir)
            _audit("spec_delete_aborted", name, outcome="denied")
            return web.json_response(
                {
                    "code": "loop_removal_failed",
                    "error": "could not stop this spec's background loop; nothing was deleted",
                },
                status=503,
            )
        # Tear down the worker as well as its nudge loop so an in-flight turn cannot
        # keep editing after deletion. The order mirrors gateway slot deletion: pop
        # from the registry, cancel and await the task, then persist as closed.
        #
        # require_archive: the conversation is user data, so deletion cannot report
        # success unless it is durably archived. Before any teardown, failure releases
        # the reservation; after one slot succeeds, the durable reservation remains so
        # a retry can finish the partial delete without claiming its queue is restorable.
        try:
            teardown_reserved = await _commit_delete_teardown(
                name,
                expect_spec_dir=doomed_dir,
                expect_slot_key=doomed_commit_slot_key,
            )
        except Exception:
            logger.warning(
                "spec %s: destructive delete boundary could not be saved",
                name,
                exc_info=True,
            )
            teardown_reserved = False
        if not teardown_reserved:
            await _unmark_deleting(name, expect_spec_dir=doomed_dir)
            await asyncio.to_thread(_forget_deleted, doomed_dir)
            _audit("spec_delete_reservation_failed", name, outcome="denied")
            return web.json_response(
                {
                    "code": "delete_reservation_failed",
                    "error": (
                        "could not reserve this spec's destructive cleanup; retry " "the delete"
                    ),
                },
                status=503,
            )
        archive_succeeded = True
        teardown_committed = False
        for slot_to_remove in doomed_slots:
            if not await _teardown_worker_slot(
                state, name, only_slot=slot_to_remove, require_archive=True
            ):
                archive_succeeded = False
                break
            teardown_committed = True
        if not archive_succeeded:
            if teardown_committed:
                # At least one slot has already been archived and had its queued
                # work discarded. Re-exposing the spec would claim that no delete
                # occurred even though that session cannot be restored. Keep the
                # reservation and tombstone so the next DELETE completes the
                # remaining idempotent teardown instead.
                claimed_slot_keys.commit()
                return web.json_response(
                    {
                        "code": "archive_failed",
                        "error": (
                            "part of this spec's conversation was archived; retry "
                            "the delete to finish cleanup"
                        ),
                    },
                    status=503,
                )
            released = await _unmark_deleting(name, expect_spec_dir=doomed_dir)
            # The spec lives again, so the tombstone must go: leaving it would suppress
            # the documents from discovery for a spec that was never deleted.
            await asyncio.to_thread(_forget_deleted, doomed_dir)
            detail = (
                "nothing was deleted"
                if released
                else "nothing was deleted; the spec may need a reload to reappear"
            )
            return web.json_response(
                {
                    "code": "archive_failed",
                    "error": f"could not archive this spec's conversation; {detail}",
                },
                status=503,
            )

        pop_refusal = ""

        def _pop_if_same(idx: dict) -> bool:
            nonlocal pop_refusal
            # Identity-pinned: a same-name spec cannot exist here (the name was reserved),
            # but the entry is still re-read under the lock, so pin it anyway rather than
            # trusting the snapshot this handler loaded before the awaits.
            meta = idx.get(name)
            if meta is None or str(meta.get("spec_dir", "")) != doomed_dir:
                return False
            actual_key = str(meta.get("slot_key", ""))
            if doomed_commit_slot_key and actual_key and actual_key != doomed_commit_slot_key:
                return False
            # The slot teardown above awaited while the agent could still write its
            # index. Refuse inside this final transaction if it minted a second lexical
            # ledger spelling in that window; popping now would strand the settled row
            # under the removed spelling and let the survivor create a conflicting one.
            if _decision_alias_conflict_locked(idx, doomed_dir):
                pop_refusal = "directory_alias"
                return False
            del idx[name]
            return True

        # A raised write failure and a False mutation both leave the entry reserved
        # after its transcript teardown; the ledger remains intact until pop succeeds.
        released_slot_keys = tuple(
            {
                doomed_slot_key,
                doomed_observed_slot_key,
                doomed_runtime_slot_key,
                *claimed_slot_keys.keys(),
            }
        )
        try:
            popped = await _mutate_index(
                _pop_if_same,
                on_commit=lambda: _forget_observed_slot_identity(name, *released_slot_keys),
            )
        except Exception:
            logger.warning("spec %s: the index entry could not be removed", name, exc_info=True)
            popped = False
        if not popped:
            if pop_refusal == "directory_alias":
                # Every captured slot is already archived. Keep the destructive
                # boundary visible rather than resurrecting a partially torn-down
                # spec; after the alias is repaired, retrying DELETE can finish the
                # idempotent index removal.
                claimed_slot_keys.commit()
                _audit("spec_decision_directory_alias_conflict", name, outcome="denied")
                return web.json_response(
                    {
                        "code": "decision_directory_alias_conflict",
                        "error": (
                            "multiple spec names resolve to this directory; repair "
                            "the spec index, then retry the delete"
                        ),
                    },
                    status=409,
                )
            # The conversation is ALREADY archived, so un-deleting would be the lie the
            # ordering above exists to prevent. The reservation stays, which keeps the
            # spec hidden and makes a retry idempotent: it re-runs a no-op teardown and
            # removes the entry.
            #
            # The recorded answers are untouched, which is why there is nothing to put
            # back: the ledger is only cleared once the entry is actually gone. The spec
            # still exists, so its settled decisions stay settled.
            logger.warning("spec %s: archived but the index entry could not be removed", name)
            return web.json_response(
                {
                    "code": "index_write_failed",
                    "error": (
                        "this spec's conversation was archived but its record could not be "
                        "removed; retry the delete"
                    ),
                },
                status=503,
            )
        # The spec is gone from the index. NOW the ledger entry can go: until this point
        # a failure had to leave the answers intact, because a spec that survives with
        # its answers erased is a decision silently reopened. From here a cleanup failure
        # is housekeeping -- logged, not fatal, and not worth failing a delete that has
        # already happened. It is not harmless on its own, though: a DIFFERENT spec can
        # later be created at this same path, and create closes that by clearing an
        # orphaned record before it registers one.
        forgot, still_referenced = await _forget_decisions(doomed_dir)
        if not forgot:
            _audit("spec_decision_record_stale", name, outcome="denied")
            logger.warning("spec %s: deleted, but its decision record could not be cleared", name)
        if not still_referenced:
            # The lock deliberately STAYS registered. Evicting it looked safe when no
            # other name referenced the directory, but "no reference" was read before
            # this line and cannot be relied on at it: a create can register the same
            # directory in that window, and -- worse -- a handler that called
            # _turn_lock() before the eviction is already waiting on the OLD object.
            # The next arrival would then be handed a BRAND-NEW lock and the two would
            # serialize against nothing, running concurrent turns over the same files:
            # exactly the hole the directory-keyed lock exists to close, reintroduced by
            # its own cleanup. There is no reference count that fixes this, because a
            # waiter holds the object rather than an index entry, so the eviction is
            # simply dropped. What remains is one small asyncio.Lock per directory that
            # ever had a turn -- a bounded, harmless residue next to a correctness hole.
            logger.debug("spec %s: keeping the turn lock registered for %s", name, doomed_key)
        claimed_slot_keys.commit()
        _audit("spec_delete", name)
        return web.json_response({"ok": True})
