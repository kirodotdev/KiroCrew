"""Creation of a new spec: validation, registration, and its seed turn.

Registration holds the directory turn lock from the protected-ledger checks
through the seed dispatch, so a registered spec is never visible to another
request before the prompt that defines it has been dispatched.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from aiohttp import web

from .. import repository as _repository
from ..decisions import _aload_index_with_decision_alias_status, _forget_decisions
from ..parsers import (
    _VALID_TYPES,
    _decision_key,
    _opted_in,
    _same_spec_dir,
    _seed_prompt,
    _usable_name,
)
from ..repository import (
    _aload_index,
    _aload_index_snapshot,
    _audit,
    _create_worktree,
    _forget_deleted,
    _forget_observed_slot_identity,
    _mutate_index,
    _new_slot_key,
    _prepare_spec_dir,
    _remove_worktree,
    _repo_info,
    _rollback_worktree_if_ours,
    _safe_dir,
)
from ..runtime import _dispatch_turn, _ensure_worker_slot
from .dispatch_claims import (
    _bind_pending_dispatch_to_turn,
    _pending_dispatch_is_current,
    _release_pending_dispatch_when_done,
    _reserve_pending_dispatch,
)
from .execution_state import _remove_orphaned_executions
from .request_identity import _read_json, _require_auth
from .turn_guard import _turn_lock

logger = logging.getLogger("kirocrew.app.spec-builder")


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
        # response so the endpoint cannot serve as a filesystem probe.
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
            # Another app owns this slot key, or the working dir does not validate.
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
