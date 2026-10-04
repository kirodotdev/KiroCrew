"""Workspace CRUD: ``/api/workspaces`` list, create, update and delete."""

from __future__ import annotations

import asyncio
import shutil
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.handlers.files import (
        KiroCrewConfig,
        WorkspaceConfig,
        WorkspaceDirUnusable,
        _sel,
        coerce_dict_section,
        data_home,
        drained_to_thread,
        is_sensitive_path,
        logger,
        materialize_workspace_dir,
        platform_compat,
        read_bounded_json,
        run_config_write,
        update_config_locked,
    )


# ── Workspace API ──
async def api_workspaces(request: web.Request) -> web.Response:
    """GET /api/workspaces — list configured workspaces."""
    cfg = KiroCrewConfig.load()
    default_ws = cfg.default_workspace
    result = []
    for name, ws in cfg.workspaces.items():
        result.append({"name": name, "path": ws.dir, "is_default": name == default_ws})
    if not result:
        result.append({"name": "default", "path": "workspace", "is_default": True})
    return web.json_response({"workspaces": result, "default": default_ws})


def _resolve_ws_dir(d: str) -> Path:
    """Resolve a workspace dir string the way collision checks compare them."""
    p = Path(d).expanduser()
    return p.resolve() if p.is_absolute() else (data_home() / d).resolve()


class _WorkspaceConflict(Exception):
    """A workspace precondition failed against FRESH state inside the lock.

    The handlers validate on a snapshot loaded before their awaits (fast 4xxs
    for the common case), but the decision that guards config integrity --
    name/directory collisions, default-workspace and agent references -- must
    be re-made against the state the mutation actually lands on, inside the
    run_config_write critical section, or two overlapping owner requests can
    both pass the stale check and persist a conflicting document. Carries the
    response payload the handler returns.
    """

    def __init__(self, status: int, error: str, code: str) -> None:
        super().__init__(error)
        self.status = status
        self.error = error
        self.code = code


async def api_workspaces_create(request: web.Request) -> web.Response:
    """POST /api/workspaces — create a new workspace."""
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request
    from kiro_crew.validation import WORKSPACE_NAME_RE  # noqa: F811

    # Ahead of the body read: a workspace entry carries a caller-supplied
    # directory, so the traversal and sensitive-path guards below are defending
    # against input that only the owner may supply in the first place.
    owner_denied = await require_owner_dashboard_request(request, "workspace.create")
    if owner_denied is not None:
        return owner_denied

    # Default cap: the body is a workspace name plus optional dir/copy_from.
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    name = body.get("name", "").strip()
    if not name:
        return web.json_response({"error": "Workspace name is required"}, status=400)
    if not WORKSPACE_NAME_RE.match(name):
        return web.json_response(
            {"error": "Invalid workspace name (use alphanumeric, hyphens, underscores)"},
            status=400,
        )
    cfg = KiroCrewConfig.load()
    if name in cfg.workspaces:
        return web.json_response({"error": f"Workspace '{name}' already exists"}, status=409)
    copy_from = body.get("copy_from", "").strip()
    # Set only once ALL validation has passed (staging is the LAST pre-persist
    # step): the staged tree awaiting install, and the destination it installs
    # into once the in-lock checks pass. copy_pending records that the branch
    # wants a copy, deferred until after the shared path validation below.
    staged_path: Path | None = None
    install_dst: Path | None = None
    copy_pending = False
    if copy_from:
        if copy_from not in cfg.workspaces:
            return web.json_response(
                {"error": f"Source workspace '{copy_from}' not found"}, status=404
            )
        # New workspace gets its own directory, named after the workspace
        ws_dir = body.get("dir", f"workspace-{name}")
        # Check for directory collision with existing workspaces
        existing_dirs = {ws.dir for ws in cfg.workspaces.values()}
        if ws_dir in existing_dirs:
            return web.json_response(
                {"error": f"Directory '{ws_dir}' is already used by another workspace"},
                status=409,
            )
        # Recursively copy source workspace data to the new directory
        src_path = data_home() / cfg.workspaces[copy_from].dir
        dst_path = data_home() / ws_dir
        # Resolved once each; both checks below judge these same objects.
        src_resolved = src_path.resolve()
        dst_resolved = dst_path.resolve()
        # Guard against path traversal
        if not dst_resolved.is_relative_to(data_home().resolve()):
            _sel().log_api_access(
                caller=request.get("user", "dashboard"),
                operation="workspace.create",
                outcome="denied",
                source="dashboard",
                resources=name,
            )
            return web.json_response({"error": "Invalid directory path"}, status=400)
        if not src_resolved.is_relative_to(data_home().resolve()):
            _sel().log_api_access(
                caller=request.get("user", "dashboard"),
                operation="workspace.create",
                outcome="denied",
                source="dashboard",
                resources=name,
            )
            return web.json_response({"error": "Invalid source directory path"}, status=400)
        # Reject config root itself to avoid copying .env / config.json
        cfg_root = data_home().resolve()
        if src_resolved == cfg_root or dst_resolved == cfg_root:
            _sel().log_api_access(
                caller=request.get("user", "dashboard"),
                operation="workspace.create",
                outcome="denied",
                source="dashboard",
                resources=name,
            )
            return web.json_response(
                {"error": "Cannot use config root as workspace directory"}, status=400
            )
        if src_path.is_dir():
            copy_pending = True
    else:
        ws_dir = body.get("dir", f"workspace-{name}")
    # Guard against path traversal for relative paths; absolute paths are allowed
    _abs = Path(ws_dir).expanduser().is_absolute()
    # Path constructed for validation only (never opened/read/written); the
    # is_relative_to + is_sensitive_path guards below reject traversals before
    # the value is stored in config. CodeQL's taint tracker does not model the
    # containment guard as a barrier.
    # Resolved exactly ONCE. Every check below judges this object and the create
    # below receives this same object: a second resolve after the checks would
    # follow a parent swapped for a link in between, and the pinned create can
    # only refuse a swap that happens AFTER the path it is handed was resolved.
    unresolved_dir = Path(ws_dir).expanduser() if _abs else data_home() / ws_dir
    validated_dir = unresolved_dir.resolve()  # lgtm[py/path-injection]

    # Check for directory collision with existing workspaces (resolve both sides)
    existing_resolved = {_resolve_ws_dir(ws.dir) for ws in cfg.workspaces.values()}
    if validated_dir in existing_resolved:
        return web.json_response(
            {"error": f"Directory '{ws_dir}' is already used by another workspace"},
            status=409,
        )
    if is_sensitive_path(str(validated_dir)):
        _sel().log_api_access(
            caller=request.get("user", "dashboard"),
            operation="workspace.create",
            outcome="denied",
            source="dashboard",
            resources=name,
        )
        return web.json_response({"error": "Invalid directory path"}, status=400)
    if not _abs and not validated_dir.is_relative_to(data_home().resolve()):
        _sel().log_api_access(
            caller=request.get("user", "dashboard"),
            operation="workspace.create",
            outcome="denied",
            source="dashboard",
            resources=name,
        )
        return web.json_response({"error": "Invalid directory path"}, status=400)
    if validated_dir == data_home().resolve():
        _sel().log_api_access(
            caller=request.get("user", "dashboard"),
            operation="workspace.create",
            outcome="denied",
            source="dashboard",
            resources=name,
        )
        return web.json_response(
            {"error": "Cannot use config root as workspace directory"}, status=400
        )
    if copy_pending:
        # STAGE the copy_from tree only now, after EVERY validation above has
        # passed -- a stage before validation leaks the copied tree on any 4xx
        # It is INSTALLED into place inside the locked
        # persist below, so a losing create never mutates the destination.

        def _ignore_sensitive(directory: str, entries: list[str]) -> set[str]:
            # Module-level is_sensitive_path alias -- one binding for one guard.
            from pathlib import Path as _Path  # noqa: F811

            skip: set[str] = set()
            for entry in entries:
                full = str(_Path(directory, entry).resolve())
                if is_sensitive_path(full):
                    skip.add(entry)
            return skip

        staging = dst_path.parent / f".{dst_path.name}.staging-{uuid.uuid4().hex[:8]}"

        def _copy_staged() -> None:
            shutil.copytree(src_path, staging, symlinks=True, ignore=_ignore_sensitive)

        def _drop_staging() -> None:
            shutil.rmtree(staging, ignore_errors=True)

        try:
            # drained_to_thread, not bare to_thread: a cancellation at the
            # await would leave the copytree THREAD still writing while the
            # cleanup below rmtrees the same tree -- the race can strand
            # partial ``.staging-*`` residue. Draining
            # runs the copy to completion first, so the cleanup only ever
            # starts on a quiescent tree, and the cleanup itself is drained so
            # it cannot be abandoned mid-delete either.
            await drained_to_thread(_copy_staged)
        except BaseException:
            await drained_to_thread(_drop_staging)
            raise
        staged_path = staging
        install_dst = dst_path

    # Persist as ONE delta read-modify-write on the raw document, inside a
    # single hold of the sidecar flock (update_config_locked), dispatched off
    # the loop with both locks via run_config_write -- the transaction shape
    # run_config_write's own docstring prescribes. The
    # handler's `cfg` was loaded before awaits above (the copytree can run for
    # seconds), so the state-dependent preconditions are re-decided against
    # the document as read INSIDE the lock, and only the keys this create owns
    # are written -- a concurrent write to any other setting is untouchable.
    def _mutate_create(doc: dict) -> dict:
        workspaces = coerce_dict_section(doc, "workspaces")
        if name in workspaces:
            raise _WorkspaceConflict(409, f"Workspace '{name}' already exists", "workspace_exists")
        raw_dirs = {
            _resolve_ws_dir(str(ws.get("dir", "")))
            for ws in workspaces.values()
            if isinstance(ws, dict)
        }
        if validated_dir in raw_dirs:
            raise _WorkspaceConflict(
                409,
                f"Directory '{ws_dir}' is already used by another workspace",
                "workspace_dir_in_use",
            )
        # Checks passed: INSTALL the staged tree now (we are in a worker
        # thread, inside the flock hold), before the config write, so a
        # directory only ever appears at the destination for a create that is
        # actually being persisted. The install invariant that makes rollback
        # TOTAL: the destination must not exist AT ALL -- any pre-existing
        # directory (even empty: its inode and metadata are not ours to
        # replace) is refused. publish_dir_noreplace, not check-then-rename:
        # POSIX os.rename silently replaces an EMPTY destination, so a racer's
        # directory created between a check and the rename would be destroyed;
        # the no-replace rename closes that window in the filesystem itself.
        if staged_path is not None and install_dst is not None:
            if install_dst.exists():
                raise _WorkspaceConflict(
                    409,
                    f"Destination directory '{ws_dir}' already exists; choose "
                    "another dir or remove it first",
                    "workspace_dir_occupied",
                )
            try:
                platform_compat.publish_dir_noreplace(staged_path, install_dst)
            except (FileExistsError, OSError) as exc:
                # A filesystem racer created the destination between the check
                # and the rename; refuse rather than replace anything.
                raise _WorkspaceConflict(
                    409,
                    f"Destination directory '{ws_dir}' already exists; choose "
                    "another dir or remove it first",
                    "workspace_dir_occupied",
                ) from exc
            install_state["installed"] = True
        # A create with no copy source still needs its directory to EXIST: the
        # config entry alone is a latent fleet-wide outage for private members
        # (see materialize_workspace_dir). Created through the pinned parent,
        # adopting a directory already there; a non-directory or a missing parent
        # is refused. Deliberately NOT rolled back when the config write fails --
        # a concurrent create can already have adopted and registered it.
        else:
            try:
                materialize_workspace_dir(validated_dir, leaf=unresolved_dir, display=ws_dir)
            except WorkspaceDirUnusable as exc:
                raise _WorkspaceConflict(409, str(exc), exc.code) from exc
        workspaces[name] = asdict(WorkspaceConfig(dir=ws_dir))
        return doc

    install_state: dict = {"installed": False}
    try:
        await run_config_write(update_config_locked, mutate=_mutate_create)
    except _WorkspaceConflict as conflict:
        # The staged tree was never installed; drop it in a worker -- an
        # inline rmtree of a large copied workspace would stall the loop.
        if staged_path is not None:
            await asyncio.to_thread(shutil.rmtree, staged_path, ignore_errors=True)
        return web.json_response({"error": conflict.error, "code": conflict.code}, status=409)
    except asyncio.CancelledError:
        # run_config_write SHIELDS and DRAINS the worker: a CancelledError
        # surfacing here means the worker ran to completion -- the install
        # landed AND the config write registered the workspace (a worker
        # failure would surface as that failure, not as cancellation).
        # Rolling back would delete a directory config.json now points at.
        # Nothing to clean: the staged tree was consumed by the install.
        raise
    except BaseException:
        # The worker itself failed (unreadable config, a failed atomic write): the
        # workspace was NOT registered. An installed tree is left in place, by the
        # same rule as the plain-create directory: by the time this runs a
        # concurrent create can have adopted the directory and registered it
        # (EEXIST is accepted above), so deleting it is the unsafe option -- it
        # would leave THAT workspace declared with no directory. A full copied tree with nothing
        # pointing at it is indistinguishable from a leak, so say where it is.
        # An uninstalled staging tree is residue nothing can have adopted; drop it
        # off the loop.
        if install_state["installed"] and install_dst is not None:
            logger.warning(
                "workspace create failed after its copied tree was installed; %s is "
                "left in place and no workspace entry names it",
                install_dst,
            )
        elif staged_path is not None:
            await asyncio.to_thread(shutil.rmtree, staged_path, ignore_errors=True)
        raise
    _sel().log_api_access(
        caller=request.get("user", "dashboard"),
        operation="workspace.create",
        outcome="success",
        source="dashboard",
        resources=name,
    )
    return web.json_response({"ok": True, "name": name})


async def api_workspaces_update(request: web.Request) -> web.Response:
    """PUT /api/workspaces/{name} — update a workspace."""
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    # Ahead of the 404: whether a workspace exists is not a non-owner's to learn.
    owner_denied = await require_owner_dashboard_request(request, "workspace.update")
    if owner_denied is not None:
        return owner_denied

    name = request.match_info["name"]
    cfg = KiroCrewConfig.load()
    if name not in cfg.workspaces:
        return web.json_response({"error": f"Workspace '{name}' not found"}, status=404)
    # Default cap: the body is a single directory field.
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    if "dir" in body:
        new_dir = body["dir"]
        _abs = Path(new_dir).expanduser().is_absolute()
        # Resolved for validation only; is_relative_to + is_sensitive_path guard
        # below reject traversals before the value is stored in config.
        resolved = (  # lgtm[py/path-injection]
            Path(new_dir).expanduser().resolve() if _abs else (data_home() / new_dir).resolve()
        )
        if is_sensitive_path(str(resolved)):
            _sel().log_api_access(
                caller=request.get("user", "dashboard"),
                operation="workspace.update",
                outcome="denied",
                source="dashboard",
                resources=name,
            )
            return web.json_response({"error": "Invalid directory path"}, status=400)
        if not _abs and not resolved.is_relative_to(data_home().resolve()):
            _sel().log_api_access(
                caller=request.get("user", "dashboard"),
                operation="workspace.update",
                outcome="denied",
                source="dashboard",
                resources=name,
            )
            return web.json_response({"error": "Invalid directory path"}, status=400)
        if resolved == data_home().resolve():
            _sel().log_api_access(
                caller=request.get("user", "dashboard"),
                operation="workspace.update",
                outcome="denied",
                source="dashboard",
                resources=name,
            )
            return web.json_response(
                {"error": "Cannot use config root as workspace directory"}, status=400
            )
        existing_dirs = {
            (
                (data_home() / ws.dir).resolve()
                if not Path(ws.dir).expanduser().is_absolute()
                else Path(ws.dir).expanduser().resolve()
            )
            for n, ws in cfg.workspaces.items()
            if n != name
        }
        if resolved in existing_dirs:
            return web.json_response(
                {"error": f"Directory '{new_dir}' is already used by another workspace"},
                status=409,
            )

    # Persist as ONE delta RMW on the raw document inside the flock hold (see
    # workspace.create): the mutation AND its state-dependent precondition
    # (dir collision) are re-decided against the document as read inside the
    # lock, and only this workspace's entry is written.
    def _mutate_update(doc: dict) -> dict | None:
        workspaces = coerce_dict_section(doc, "workspaces")
        ws = workspaces.get(name)
        if not isinstance(ws, dict):
            # A concurrent delete won the race after our 404 check; recreating
            # the workspace from this handler's older view would undo it.
            raise _WorkspaceConflict(404, f"Workspace '{name}' not found", "workspace_not_found")
        if "dir" in body:
            # `resolved` is the ONE path screened above (is_sensitive_path,
            # containment, config root); re-resolving the string here would let a
            # parent swapped for a link between the screen and this check pass a
            # different directory. Only the OTHER entries resolve fresh -- they are
            # the state re-decided inside the lock.
            others = {
                _resolve_ws_dir(str(w.get("dir", "")))
                for n2, w in workspaces.items()
                if n2 != name and isinstance(w, dict)
            }
            if resolved in others:
                raise _WorkspaceConflict(
                    409,
                    f"Directory '{body['dir']}' is already used by another workspace",
                    "workspace_dir_in_use",
                )
            # Publish only a usable workspace directory. An update names a
            # destination the owner already chose; creating it belongs to the
            # create path, which owns that directory's lifecycle.
            if not resolved.is_dir():
                raise _WorkspaceConflict(
                    409,
                    f"Directory '{body['dir']}' does not exist or is not a directory; "
                    "create it first",
                    "workspace_dir_unusable",
                )
            ws["dir"] = body["dir"]
            return doc
        return None  # nothing to change -- skip the write

    try:
        await run_config_write(update_config_locked, mutate=_mutate_update)
    except _WorkspaceConflict as conflict:
        if conflict.status == 404:
            return web.json_response({"error": conflict.error, "code": conflict.code}, status=404)
        return web.json_response({"error": conflict.error, "code": conflict.code}, status=409)
    _sel().log_api_access(
        caller=request.get("user", "dashboard"),
        operation="workspace.update",
        outcome="success",
        source="dashboard",
        resources=name,
    )
    return web.json_response({"ok": True, "name": name})


async def api_workspaces_delete(request: web.Request) -> web.Response:
    """DELETE /api/workspaces/{name} — delete a workspace."""
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    # Ahead of the 404/409 guards: those are referential, not authorization, and
    # this handler reaches `cfg.save()` with an entry removed.
    owner_denied = await require_owner_dashboard_request(request, "workspace.delete")
    if owner_denied is not None:
        return owner_denied

    name = request.match_info["name"]
    cfg = KiroCrewConfig.load()
    if name not in cfg.workspaces:
        return web.json_response({"error": f"Workspace '{name}' not found"}, status=404)
    if name == cfg.default_workspace:
        return web.json_response(
            {"error": f"Cannot delete default workspace '{name}'. Change default_workspace first."},
            status=409,
        )
    referencing = [a for a, ac in cfg.agents.items() if ac.workspace == name]
    if referencing:
        return web.json_response(
            {"error": f"Workspace '{name}' is referenced by agents: {', '.join(referencing)}"},
            status=409,
        )
    # Persist as ONE delta RMW on the raw document inside the flock hold (see
    # workspace.create); the referential guards (default workspace, agent
    # references) are re-run against the document as read inside the lock.

    def _mutate_delete(doc: dict) -> dict | None:
        workspaces = coerce_dict_section(doc, "workspaces")
        if name not in workspaces:
            return None  # already gone -- a concurrent delete landed first
        if name == doc.get("default_workspace", "default"):
            raise _WorkspaceConflict(
                409,
                f"Cannot delete default workspace '{name}'. Change default_workspace first.",
                "workspace_is_default",
            )
        fresh_refs = [
            a
            for a, ac in coerce_dict_section(doc, "agents").items()
            if isinstance(ac, dict) and ac.get("workspace") == name
        ]
        if fresh_refs:
            raise _WorkspaceConflict(
                409,
                f"Workspace '{name}' is referenced by agents: {', '.join(fresh_refs)}",
                "workspace_referenced",
            )
        del workspaces[name]
        return doc

    try:
        await run_config_write(update_config_locked, mutate=_mutate_delete)
    except _WorkspaceConflict as conflict:
        return web.json_response({"error": conflict.error, "code": conflict.code}, status=409)
    _sel().log_api_access(
        caller=request.get("user", "dashboard"),
        operation="workspace.delete",
        outcome="success",
        source="dashboard",
        resources=name,
    )
    return web.json_response({"ok": True})
