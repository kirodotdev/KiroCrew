"""Folder scaffolding from a project tree — preview, then create.

Two endpoints sit on top of :mod:`kiro_crew.project_scan`:

* ``POST /api/project-scaffold/scan`` — dry-run preview. Runs the scanner and
  returns what a scaffold WOULD create, with nothing created.
* ``POST /api/project-scaffold/create`` — creates the confirmed selection by
  composing the existing folder create path.

The split is what makes the feature safe to point at an unfamiliar tree: a scan
is read-only, so the destructive-sounding half of "build me twenty folders" only
happens against a selection the user has seen.

Three properties belong to this module rather than to the scanner:

* **The scan root is validated by the folder API's own validator.** ``scan``
  refuses exactly what creating a folder by hand refuses — a relative path, a
  sensitive path, a path that is not a directory — because it calls the same
  :func:`~kiro_crew.dashboard.chat_folders._validate_project_dir`. One function,
  and the message the user reads is the one they would have read anyway.
* **Reconcile marking is an overlay, not a detection rule.** The scanner's
  output depends only on the filesystem and the passed configuration, which is
  what makes two scans of an unchanged tree compare equal. Which candidates
  already have folders is store state, so it is layered on here — a candidate is
  marked ``existing`` and drops out of the default selection, but it is still
  reported, because "already set up" is information the user wants.
* **The scanner owns its own bounds.** ``project_scan`` carries the depth bound
  as its own default rather than taking it from configuration, so the scan's
  result is a function of the filesystem and the root alone. A tunable belongs
  here, in the caller, if a need for one is ever filed.
* **A selection is re-derived, not trusted.** ``scaffold`` scans again and keeps
  only the selected paths the fresh scan offers, then creates folders on the
  scanner's own paths. A client can pick from what the server found; it cannot
  name the directory a folder is created on.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Iterable

from aiohttp import web

from kiro_crew.dashboard.chat_folders import (
    FolderCreateError,
    FolderOwnershipError,
    _audit_origin,
    _effective_request_app,
    _refuse_unattributable_caller,
    _validate_project_dir,
    create_folder_record,
)
from kiro_crew.dashboard.create_rate_limit import FOLDER_CREATE, allow_create
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.executors import discovery_executor
from kiro_crew.project_scan import Candidate, CandidateTree, Tier, scan
from kiro_crew.security import path_contains_sensitive
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

# Response status telling a preview with no candidates apart from one with some.
# Zero candidates is an answer — the tree holds no packages this scanner
# recognizes — so it is a 200 with a status a surface can branch on, not an
# error a surface has to render as a failure.
STATUS_OK = "ok"
STATUS_EMPTY = "empty"

# How many rejected paths a stale-selection refusal names. The list exists so a
# surface can say which entries went stale rather than only that something did;
# it is capped because the request body it comes from is caller-controlled and a
# response should not grow with it.
MAX_REPORTED_UNKNOWN = 20


class _BadRequest(ValueError):
    """A request field was missing or rejected before any work was done.

    Carries the ``error``/``code`` pair verbatim so both endpoints answer an
    unusable root identically — the message the user sees for a bad scan root is
    the one manual folder creation would have given them.
    """

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


def _resolve_root(body: object) -> str:
    """Return the validated, resolved scan root named by a request body.

    Resolution matters beyond validation: the returned path is what the scan is
    rooted at, so every candidate path is spelled the same way a folder's stored
    ``project_dir`` is (both come out of the same ``realpath``). That is what
    lets reconcile compare the two by equality rather than by re-resolving a
    stored path per candidate.

    Raises:
        _BadRequest: if the body is not an object, names no root, or names one
            the folder API would refuse.
    """

    if not isinstance(body, dict):
        raise _BadRequest("request body must be a JSON object", "invalid_json")
    raw = str(body.get("root") or "").strip()
    if not raw:
        # ``_validate_project_dir`` accepts "" — a folder is allowed to have no
        # project directory at all — so the empty case has to be caught here or a
        # rootless scan would fall through to scanning nothing.
        raise _BadRequest("root required", "folder_scan_root_required")
    resolved, err = _validate_project_dir(raw)
    if err:
        raise _BadRequest(err, "folder_scan_root_invalid")
    # The folder validator answers "is this path itself protected?" — the scan
    # needs the REVERSE direction too, because it sweeps everything BELOW the
    # root: a root that is an ancestor of a credential store (the home
    # directory itself, or a parent of ~/.ssh) would read declarations and
    # .gitignore files under it. Same rule the Notes vault applies to its bulk
    # `git add -A`, via the same list-based gate (no filesystem walk).
    if path_contains_sensitive(resolved):
        raise _BadRequest(
            "root contains a sensitive path — scan a narrower directory",
            "folder_scan_root_invalid",
        )
    return resolved


def _bad_request_response(exc: _BadRequest) -> web.Response:
    """Return the 400 for a request that was refused before any work started."""

    # ``code`` is the contract a client branches on; ``error`` is advisory prose
    # rendered into a localized UI.
    return web.json_response({"error": str(exc), "code": exc.code}, status=400)


async def _scan_off_loop(root: str) -> CandidateTree:
    """Run one scan of ``root`` off the loop thread.

    The walk is blocking filesystem work whose cost scales with a tree the user
    merely pointed at, so it may not run on the event loop: one unresponsive
    network mount would otherwise stall every chat, WS push, and heartbeat behind
    it. It goes to the discovery pool — the pool purpose-built for
    browser-triggerable, read-only filesystem discovery — not the subprocess pool
    (which must stay free for PTY teardown and wedge recovery) and not the
    maintenance pool (whose periodic sweeps must stay responsive).
    """

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(discovery_executor(), lambda: scan(Path(root)))


def scaffolded_project_dirs(folders: Iterable[Any]) -> set[str]:
    """Return the project directories folders are already bound to."""

    return set(folder_ids_by_project_dir(folders))


def folder_ids_by_project_dir(folders: Iterable[Any]) -> dict[str, str]:
    """Map each project directory a folder holds onto that folder's id.

    Reconcile needs the directories; scaffolding needs the ids too, because a
    candidate whose folder already exists is still the parent the candidates
    below it hang off. The first folder wins when two are bound to the same
    directory — store order, so the answer does not depend on which of them a
    caller happens to look at first — and an entry carrying a directory but no
    usable id maps to ``""``: the directory is taken (nothing may be created on
    it again) even though nothing can be parented to it.

    Skips entries without a usable ``project_dir``, and entries that are not
    dicts at all: the folder store is loaded without validation, so a
    hand-edited or legacy ``folders.json`` can hold either.
    """

    ids: dict[str, str] = {}
    for folder in folders:
        if not isinstance(folder, dict):
            continue
        project_dir = str(folder.get("project_dir") or "").strip()
        if project_dir and project_dir not in ids:
            ids[project_dir] = str(folder.get("id") or "")
    return ids


def default_selected(candidate: Candidate, *, existing: bool) -> bool:
    """Return whether a candidate starts ticked in the preview.

    An already-scaffolded candidate is never ticked whatever its tier: creation
    is additive, so the one thing a re-scan must not offer to do is duplicate a
    folder the user already has. Below that, the tier decides — ``AUTO`` is
    confident enough to tick, ``OFFERED`` is shown for the user to opt into.
    """

    return not existing and candidate.tier is Tier.AUTO


def _candidate_payload(candidate: Candidate, *, existing: bool) -> dict[str, Any]:
    """Render one candidate for a preview response."""

    return {
        # ``path`` is both the display target and the identifier the scaffold
        # request names a selection with, so it is spelled once, here.
        "path": candidate.path,
        "name": candidate.name,
        "parent_path": candidate.parent_path,
        "tier": candidate.tier.value,
        "signals": list(candidate.signals),
        "existing": existing,
        "selected": default_selected(candidate, existing=existing),
    }


def folder_display_name(path: str) -> str:
    """Return the folder name a directory scaffolds under.

    The basename, with a fallback to the whole path for the one directory that
    has no basename (a filesystem root): a folder must have a name, and the
    create path refuses an empty one.
    """

    return os.path.basename(path.rstrip(os.sep)) or path


def preview_payload(tree: CandidateTree, existing_dirs: set[str]) -> dict[str, Any]:
    """Return the scan response for ``tree``, overlaid with reconcile state.

    The overlay is an exact match against the project directories folders already
    hold: candidate paths and stored ``project_dir`` values are both resolved the
    same way, so equality is the whole comparison — no prefix or realpath
    guessing, which could mark a sibling directory as taken.
    """

    candidates = [
        _candidate_payload(candidate, existing=candidate.path in existing_dirs)
        for candidate in tree.candidates
    ]
    return {
        "root": tree.root,
        # The root's own folder is created by the scaffold step rather than being
        # a candidate, so the preview reports its reconcile state separately.
        "root_existing": tree.root in existing_dirs,
        "status": STATUS_EMPTY if not candidates else STATUS_OK,
        "candidates": candidates,
        "warnings": list(tree.warnings),
    }


async def api_chat_folders_scan(request: web.Request) -> web.Response:
    """POST /api/project-scaffold/scan — preview the folders a project would produce.

    Body: ``{"root": "<absolute path>"}``. Creates nothing.
    """

    state: DashboardState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    try:
        # Off the event loop for the same reason the walk is: resolution does
        # ``realpath``/``isdir`` syscalls against a path the user merely named,
        # and one stalled network mount must not stall every chat behind it.
        root = await asyncio.to_thread(_resolve_root, body)
    except _BadRequest as exc:
        # ``_validate_project_dir`` already SEL-logs a sensitive-path refusal;
        # the other rejections are ordinary caller error.
        return _bad_request_response(exc)

    tree = await _scan_off_loop(root)
    # Read the store AFTER the scan: the scan is the long part, and a folder
    # created while it ran should be reflected rather than offered again.
    payload = preview_payload(tree, scaffolded_project_dirs(state._folders))
    sel().log_api_access(
        caller=request.get("user", "dashboard"),
        operation="chat.folder_scan",
        outcome="allowed",
        source="dashboard",
        # The root is the resource; candidate paths are not enumerated into the
        # audit log, and a count is what makes the entry useful.
        resources=f"{root} candidates={len(payload['candidates'])}",
    )
    if payload["warnings"]:
        logger.info(
            "Folder scan of %s completed with %d warning(s)", root, len(payload["warnings"])
        )
    return web.json_response(payload)


def _selected_paths(body: dict[str, Any]) -> list[str]:
    """Return the paths a scaffold request selected.

    Raises:
        _BadRequest: if ``selected`` is present but is not a list of strings.
    """

    raw = body.get("selected")
    if raw is None:
        # An empty selection is legitimate: the scan root's own folder is created
        # regardless, so "just the root, none of the packages" is a real answer,
        # and an absent or null field is how a surface says it.
        return []
    if not isinstance(raw, list) or any(not isinstance(path, str) for path in raw):
        raise _BadRequest(
            "selected must be a list of candidate paths",
            "folder_scaffold_selection_invalid",
        )
    return list(raw)


def _nearest_folder_id(
    candidate: Candidate,
    *,
    by_path: dict[str, Candidate],
    folder_ids: dict[str, str],
    root: str,
) -> str:
    """Return the id of the folder ``candidate`` should hang off.

    Normally its parent candidate's folder. When that parent has none — it was
    left unselected, or its own creation failed — the chain is walked upward to
    the nearest ancestor that does have one, ending at the scan root's folder.
    Nesting under the nearest available ancestor keeps a partially selected tree
    shaped like the tree the user saw, where flattening everything onto the root
    would lose the structure that makes a sub-folder's ``project_dir`` meaningful.

    Termination is structural: each step moves to a strictly shorter ancestor
    path, and the chain ends at a candidate hanging off the root.
    """

    current = candidate.parent_path
    while current is not None:
        folder_id = folder_ids.get(current, "")
        if folder_id:
            return folder_id
        parent = by_path.get(current)
        current = parent.parent_path if parent else None
    # "" when the root folder could not be created or exists without a usable id:
    # a top-level folder is the honest placement, since there is nothing to nest
    # it under.
    return folder_ids.get(root, "")


def _failure(path: str, exc: Exception) -> dict[str, str]:
    """Render one folder that could not be created.

    A refusal carries the folder API's own code; anything else is the store write
    failing, which has no code of its own to report.
    """

    code = exc.code if isinstance(exc, FolderCreateError) else ""
    return {
        "path": path,
        "error": str(exc) or exc.__class__.__name__,
        "code": code or "folder_create_failed",
    }


def _scaffold_response(
    tree: CandidateTree,
    created: list[dict[str, str]],
    skipped_existing: list[str],
    failed: list[dict[str, str]],
) -> dict[str, Any]:
    """Render the scaffold outcome."""

    return {
        "root": tree.root,
        "created": created,
        "skipped_existing": skipped_existing,
        "failed": failed,
        "warnings": list(tree.warnings),
    }


async def _create_selection(
    state: DashboardState, tree: CandidateTree, selected: set[str], *, request_app: str
) -> dict[str, Any]:
    """Create the scan root's folder and the selected candidates beneath it.

    Every folder goes through the folder API's own create path, one call per
    folder, so each is validated and appended under the folders lock exactly as a
    hand-created folder is. That also makes each folder its own commit, which is
    what lets a failure partway through be reported rather than hidden: what was
    created stays created, and nothing is deleted to undo it. Rolling back would
    mean deleting folders that may already hold conversations by the time the
    next creation fails.

    ``request_app`` is the calling app's identity ("" for the person), passed
    to every ``create_folder_record`` so a scaffolded folder is stamped with
    its real owner and nesting under a folder another owner holds is refused —
    the same isolation a hand-created folder gets. An ownership refusal costs
    only its own path, like any other per-folder refusal, but is audited as a
    denial rather than reported as an ordinary failure: it is a security
    decision, and an audit trail that records it as an allow would be worse
    than useless.
    """

    existing = folder_ids_by_project_dir(state._folders)
    # Directory -> folder id for anything a child may hang off: folders already in
    # the store, plus the ones created below as they appear.
    folder_ids = dict(existing)
    by_path = {candidate.path: candidate for candidate in tree.candidates}
    created: list[dict[str, str]] = []
    skipped_existing: list[str] = []
    failed: list[dict[str, str]] = []

    if tree.root in existing:
        skipped_existing.append(tree.root)
    else:
        try:
            folder = await create_folder_record(
                state,
                name=folder_display_name(tree.root),
                project_dir=tree.root,
                request_app=request_app,
                unique_project_dir=True,
                require_resolved_project_dir=True,
            )
        except FolderOwnershipError as exc:
            # A security refusal, not a validation failure, so it is audited as a
            # denial. Caught ahead of FolderCreateError because it is a subclass:
            # left to the generic branch the whole request would answer 200 with
            # only allowed events, recording a denial as an allow.
            sel().log_api_access(
                caller=request_app or "dashboard",
                operation="chat.folder_scaffold",
                outcome="denied",
                source="dashboard",
                resources=tree.root,
                error=exc.code,
            )
            failed.append(_failure(tree.root, exc))
            return _scaffold_response(tree, created, skipped_existing, failed)
        except FolderCreateError as exc:
            if exc.code == "folder_project_dir_exists":
                # Lost a race with a concurrent scaffold of the same root: the
                # winner's folder is the one to hang children off, exactly as if
                # it had been in the pre-scan read.
                folder_ids.update(folder_ids_by_project_dir(state._folders))
                skipped_existing.append(tree.root)
            else:
                if exc.code == "folder_project_dir_moved":
                    # The root no longer names the directory the scan confirmed
                    # — the same substitution the walker refuses mid-scan,
                    # landed in the scan-to-create window instead. A security
                    # refusal, so it is audited as one.
                    sel().log_api_access(
                        caller=request_app or "dashboard",
                        operation="chat.folder_scaffold",
                        outcome="denied",
                        source="dashboard",
                        resources=tree.root,
                        error=exc.code,
                    )
                logger.warning("Folder scaffold of %s could not create the root folder", tree.root)
                failed.append(_failure(tree.root, exc))
                return _scaffold_response(tree, created, skipped_existing, failed)
        except OSError as exc:
            # Stop here rather than continue: with no root folder the whole
            # selection would be created as unrelated top-level folders, which is
            # a worse outcome to hand back than "nothing was created". An OSError
            # is the store write itself failing, so the next creation would fail
            # the same way.
            logger.warning("Folder scaffold of %s could not create the root folder", tree.root)
            failed.append(_failure(tree.root, exc))
            return _scaffold_response(tree, created, skipped_existing, failed)
        else:
            folder_ids[tree.root] = str(folder["id"])
            created.append(
                {"path": tree.root, "folder_id": str(folder["id"]), "name": folder["name"]}
            )

    # Path-sorted candidates put every ancestor before its descendants (an
    # ancestor's path is a prefix of theirs), which is the parent-before-child
    # order that lets each child name a parent id that already exists.
    for candidate in tree.candidates:
        if candidate.path not in selected:
            continue
        if candidate.path in existing:
            # Already scaffolded. Reported, not re-created — and still the parent
            # its own children hang off.
            skipped_existing.append(candidate.path)
            continue
        try:
            folder = await create_folder_record(
                state,
                name=candidate.name,
                parent_id=_nearest_folder_id(
                    candidate, by_path=by_path, folder_ids=folder_ids, root=tree.root
                ),
                project_dir=candidate.path,
                request_app=request_app,
                unique_project_dir=True,
                require_resolved_project_dir=True,
            )
        except FolderOwnershipError as exc:
            # Same reason as the root folder above: a foreign-owned parent is a
            # security refusal, so it is audited as denied rather than folded
            # into the generic per-folder failures.
            sel().log_api_access(
                caller=request_app or "dashboard",
                operation="chat.folder_scaffold",
                outcome="denied",
                source="dashboard",
                resources=candidate.path,
                error=exc.code,
            )
            failed.append(_failure(candidate.path, exc))
            continue
        except FolderCreateError as exc:
            if exc.code == "folder_project_dir_exists":
                # Lost a race with a concurrent scaffold: the winner's folder
                # stands, and it is still the parent this candidate's own
                # children hang off.
                folder_ids.update(folder_ids_by_project_dir(state._folders))
                skipped_existing.append(candidate.path)
                continue
            if exc.code == "folder_project_dir_moved":
                # Same substitution class as the root branch above: a security
                # refusal, audited as one, costing only this candidate.
                sel().log_api_access(
                    caller=request_app or "dashboard",
                    operation="chat.folder_scaffold",
                    outcome="denied",
                    source="dashboard",
                    resources=candidate.path,
                    error=exc.code,
                )
            failed.append(_failure(candidate.path, exc))
            continue
        except OSError as exc:
            # One refused or unwritable folder costs that folder. The rest of the
            # selection is still worth creating, and the caller is told which
            # ones were not.
            failed.append(_failure(candidate.path, exc))
            continue
        folder_ids[candidate.path] = str(folder["id"])
        created.append(
            {"path": candidate.path, "folder_id": str(folder["id"]), "name": folder["name"]}
        )
    return _scaffold_response(tree, created, skipped_existing, failed)


async def api_chat_folders_scaffold(request: web.Request) -> web.Response:
    """POST /api/project-scaffold/create — create the confirmed selection.

    Body: ``{"root": "<absolute path>", "selected": ["<candidate path>", ...]}``.

    The selection is re-derived server-side: the tree is scanned again here, and a
    selected path is only created if this scan offers it as a candidate. A client
    path is therefore never the reason a folder gets created on a directory — it
    only picks from what the server just found. That is what makes the difference
    between a stale preview (the user's tree changed under them) and a forged one
    (a path nobody was ever offered) irrelevant: both are refused by the same
    test, and the folder created for a candidate is created on the *scanner's*
    path, not the string the request carried.
    """

    state: DashboardState = request.app["state"]
    # A write to the shared folder tree: the same two guards the folder-create
    # route applies, for the same reasons. A caller naming a popped dashboard
    # slot cannot be attributed, so handing it the person's authority over the
    # person's folders is refused. And the rate budget is consumed ONCE per
    # scaffold call even though the call creates many folders — the limiter
    # exists to stop an automated loop on an auto-approved verb, and one
    # scaffold per goal is the legitimate shape; without this check the
    # scaffold route would be the loophole around the sibling route's limit.
    if (refusal := _refuse_unattributable_caller(state, request)) is not None:
        return refusal
    rl_source, rl_caller = _audit_origin(request)
    if rl_source != "dashboard" and not allow_create(FOLDER_CREATE, rl_caller):
        return web.json_response(
            {
                "error": "too many folders created recently; retry shortly",
                "code": "create_rate_limited",
            },
            status=429,
        )
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    try:
        # Same off-loop rule as the scan handler: resolution hits the
        # filesystem, and a stalled mount must not freeze the gateway.
        root = await asyncio.to_thread(_resolve_root, body)
        selected = _selected_paths(body)
    except _BadRequest as exc:
        return _bad_request_response(exc)

    # Never from the body: a caller that could name its own owner could name
    # someone else's (see chat_folders._folder_owner_app).
    request_app = _effective_request_app(state, request)

    tree = await _scan_off_loop(root)
    offered = {candidate.path for candidate in tree.candidates}
    unknown = sorted(set(selected) - offered)
    if unknown:
        # Security-relevant rather than merely a stale UI: the request asked for a
        # folder on a directory this server never offered, so the refusal is
        # audited even though the common cause is a preview the user left open
        # while the tree changed.
        sel().log_api_access(
            caller=request.get("user", "dashboard"),
            operation="chat.folder_scaffold",
            outcome="denied",
            source="dashboard",
            resources=f"{root} rejected={len(unknown)}",
            error="selection is not in the current scan",
        )
        return web.json_response(
            {
                "error": "selection is out of date — re-scan before creating folders",
                "code": "folder_scaffold_selection_stale",
                "unknown": unknown[:MAX_REPORTED_UNKNOWN],
            },
            status=400,
        )

    payload = await _create_selection(state, tree, set(selected), request_app=request_app)
    if payload["created"]:
        state.push_slots_update()
    sel().log_api_access(
        caller=request.get("user", "dashboard"),
        operation="chat.folder_scaffold",
        outcome="allowed",
        source="dashboard",
        # Counts rather than the paths themselves: the root is the resource, and
        # the audit entry answers "how much did this create" without copying the
        # user's directory layout into the log.
        resources=(
            f"{root} created={len(payload['created'])} "
            f"skipped={len(payload['skipped_existing'])} failed={len(payload['failed'])}"
        ),
    )
    if payload["failed"]:
        logger.warning(
            "Folder scaffold of %s created %d folder(s), %d failed",
            root,
            len(payload["created"]),
            len(payload["failed"]),
        )
    return web.json_response(payload)
