"""aiohttp adapters for Spec Builder's read projections and the route facade.

Every response that projects stored or agent-writable values through the app
redactor is built here: the list and detail views, settings, and the created
duplicate. The mutating route families are owned by ``orchestration`` modules and
re-exported unchanged, so the route composition keeps one import surface.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

from aiohttp import web

from .decisions import (
    _DECISIONS_LOCK,
    _aload_index_with_decision_alias_status,
    _apply_recorded_answers,
    _decision_entries,
    _read_decisions,
)
from .orchestration.controls import (  # noqa: F401 -- route entry points re-exported
    _handle_approve,
    _handle_archive,
    _handle_run_task,
    _handle_title,
)
from .orchestration.create import _handle_create  # noqa: F401 -- route entry point
from .orchestration.delete import _handle_delete  # noqa: F401 -- route entry point
from .orchestration.duplicate import _publish_duplicate
from .orchestration.execution import (  # noqa: F401 -- route entry points re-exported
    _handle_handoff,
    _handle_stop_execution,
)
from .orchestration.execution_state import _effective_status
from .orchestration.messages import (  # noqa: F401 -- route entry points re-exported
    _handle_message,
    _handle_recover_decision,
)
from .orchestration.request_identity import _pinned_entry, _read_json, _require_auth
from .orchestration.turn_guard import _agent_is_writing, _turn_key, _turn_lock
from .parsers import (
    _PHASE_FILES,
    _UNSCRUBBABLE,
    _clean_str,
    _duplicate_prompt,
    _normalize_approvals,
    _normalize_spec_state,
    _numeric,
    _redact,
    _usable_name,
)
from .repository import (
    _CAN_PUBLISH_DIR_NOREPLACE,
    _DELETING,
    _DUPLICATING,
    _MAX_MODEL_LEN,
    _aload_index,
    _audit,
    _derive_phase,
    _load_index_with_discovery,
    _load_settings,
    _prepare_spec_dir,
    _read_recent_projects,
    _read_spec_files,
    _read_spec_text,
    _repo_info,
    _safe_dir,
    _safe_dir_optional,
    _save_settings,
    _scan_subdirs,
    _slot_key,
)
from .runtime import _dispatch_turn, _ensure_worker_slot, _serialize_messages

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
    read refuses outright when that does not match, so reading it here is either
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
            # Per-document raw hash, which binds approval to the exact stored
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

    outcome, detail, target_dir, entry = await _publish_duplicate(
        fresh, new_name, safe_wd, target_dir, docs
    )

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
