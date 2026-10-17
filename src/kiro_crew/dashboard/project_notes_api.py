"""The dashboard API for learned project notes.

Three routes. Listing and DELETING a note reuse the existing generic item routes
(``GET /api/knowledge/items?namespace=…&type=project_note`` and
``DELETE /api/knowledge/items/{id}``) -- a note IS a Knowledge Library item, so a
second pair of routes here would duplicate them. What those generic routes cannot
do is turn a FOLDER into its project namespace, or cross a note from data into an
instruction; those two are what this module owns:

* ``POST /api/knowledge/project-notes/record`` -- the agent write path, reached
  only by the ``project_note_add`` MCP tool over the internal secret. The project
  is resolved SERVER-SIDE from the verified ``X-Session-Key`` (the same resolver
  item B uses), never from the body, so a session can only write its OWN project.
* ``GET /api/knowledge/project-notes?folder_id=<id>`` -- the general, read-only
  "notes for a folder" read. It is the one thing the generic item routes cannot
  do: resolve a folder id to its project namespace and return that project's
  notes (newest first), plus the namespace the caller then scopes an item delete
  to. App-grantable and documented in ``docs/app-kit/api-reference.md``; read-only
  for every principal.
* ``POST /api/knowledge/project-notes/{note_id}/promote`` -- person only, and an
  agent/app/crew-member principal is refused exactly like a ``steering_dirs``
  write (``chat_folders._refuse_principal_steering_dirs``), because promoting a
  note turns reference DATA into a standing INSTRUCTION, and only the person may
  do that. The note's text is appended to a Markdown file inside a steering
  directory the PERSON picks from the project's own declared steering dirs.

The project a ``folder_id`` belongs to is its root folder's project namespace
(``folder_ownership.project_root_folder_id`` ->
``knowledge/project_scope.project_namespace``), identical to the record path's
session-key derivation, so a list/promote names the same project a note was
written under.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime

from aiohttp import web

from kiro_crew.dashboard.chat_folders import _resolve_folder_steering_dirs
from kiro_crew.dashboard.folder_ownership import project_root_folder_id
from kiro_crew.dashboard.handlers._shared import (
    read_bounded_json,
    require_owner_dashboard_request,
)
from kiro_crew.dashboard.project_knowledge import resolve_session_project_namespace
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.hook_runtime.safe_reads import validate_file_path
from kiro_crew.knowledge import project_notes
from kiro_crew.knowledge.project_scope import project_namespace
from kiro_crew.security import is_sensitive_path, redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

#: Longest promote target filename accepted. A note is appended to one Markdown
#: file inside a chosen steering dir; the person names the file (default below),
#: and the name is a single path component, never a path.
_MAX_PROMOTE_FILENAME = 128

#: The file a promote appends to when the person names none. A conventional,
#: discoverable home for promoted learnings inside a steering directory.
_DEFAULT_PROMOTE_FILENAME = "project-notes.md"


async def _require_knowledge_owner(request: web.Request, operation: str) -> web.Response | None:
    """Owner gate for the record route, matching ``handlers/knowledge._require_knowledge_owner``.

    Replicated here rather than imported to avoid a circular import
    (``handlers/knowledge`` imports THIS module to register the routes). The rule
    is identical: only a dashboard-user request (``app == ""``) is gated, so the
    internal-secret caller this route exists for (no ``app`` key) and an app
    token (confined by its manifest scope) pass through, and a non-owner
    dashboard user is refused ``owner_only``.
    """
    if request.get("app") != "":
        return None
    return await require_owner_dashboard_request(request, operation)


def _store(request: web.Request):
    return request.app["state"].knowledge_store


def _folders(state: DashboardState) -> list[dict]:
    folders = getattr(state, "_folders", None)
    return folders if isinstance(folders, list) else []


def _namespace_for_folder(state: DashboardState, folder_id: str) -> str:
    """The project namespace a *folder_id* belongs to, or ``""`` for no project."""
    folder_id = (folder_id or "").strip()
    if not folder_id:
        return ""
    root_id = project_root_folder_id(_folders(state), folder_id)
    return project_namespace(root_id)


async def record_project_note_route(request: web.Request) -> web.Response:
    """POST /api/knowledge/project-notes/record -- the agent write path.

    Internal/MCP only (STRICT internal secret, see ``server._STRICT_INTERNAL_API_PATHS``).
    The namespace is derived from the verified ``X-Session-Key``, never the body,
    so a session writes only its own project. A session filed in no project is
    refused with a clear message and nothing is written -- the same refusal the
    store raises, surfaced as a 400 the tool returns verbatim.

    The owner gate runs first, identical to the sibling ``agent-document`` route:
    it is a no-op for the internal-secret caller this route exists for (which
    carries no ``app`` key) and for an app token (confined elsewhere by scope),
    and it refuses a dashboard-user request that is not the owner. Defense in
    depth behind the strict-internal middleware, and the invariant
    ``test_knowledge_owner_gate`` pins for every mutating knowledge route.
    """
    denied = await _require_knowledge_owner(request, "knowledge.project_note_record")
    if denied is not None:
        return denied
    state: DashboardState = request.app["state"]
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None
    session_key = request.headers.get("X-Session-Key", "").strip()
    namespace = await resolve_session_project_namespace(state, session_key)
    store = _store(request)
    try:
        note = await _run(
            store,
            lambda: project_notes.add_note(
                store,
                namespace=namespace,
                text=str(body.get("text") or ""),
                tags=body.get("tags"),
                source_session=session_key,
            ),
        )
    except project_notes.ProjectNoteError as exc:
        return web.json_response({"error": str(exc), "code": "project_note_refused"}, status=400)
    sel().log_tool_invocation(
        session_key=session_key or "mcp",
        source="mcp",
        agent="project-notes",
        tool_name="project_note_add",
        outcome="recorded",
        metadata={"namespace": namespace, "note_id": note.get("id", "")},
    )
    return web.json_response({"note": note})


async def list_project_notes_route(request: web.Request) -> web.Response:
    """GET /api/knowledge/project-notes?folder_id=<id> -- a folder's project notes.

    The general "notes for a folder" read: it resolves *folder_id* to its project
    namespace and returns that project's notes, newest first, plus the namespace
    itself so a caller can scope a generic item delete
    (``DELETE /api/knowledge/items/{id}``) or a project-first search to it. This
    is the one read the generic ``/api/knowledge/items`` routes cannot serve,
    because they take a namespace, not a folder — nothing else here maps a folder
    to its project. Read-only and app-grantable (documented in
    ``docs/app-kit/api-reference.md``); an empty or non-project folder answers an
    empty list, never an error.
    """
    state: DashboardState = request.app["state"]
    folder_id = request.query.get("folder_id", "").strip()
    namespace = _namespace_for_folder(state, folder_id)
    store = _store(request)
    notes = await _run(store, lambda: project_notes.list_notes(store, namespace))
    return web.json_response({"notes": notes, "namespace": namespace})


async def promote_project_note_route(request: web.Request) -> web.Response:
    """POST /api/knowledge/project-notes/{note_id}/promote -- person only.

    Turning a note into a standing instruction is a host-file write into a
    steering directory, which only the person may authorize -- folder permission
    is not host-file permission, exactly the boundary
    ``_refuse_principal_steering_dirs`` draws for declaring a ``steering_dirs``
    list. So an app/agent/crew-member principal is refused here the same way: a
    non-empty ``app`` request key is 403'd, and the owner gate below handles the
    person-vs-not distinction for a browser caller.

    Body: ``folder_id`` (the project, required), ``steering_dir`` (which of the
    project's own declared steering directories to write into, required), and an
    optional ``filename`` (a single Markdown component, default
    ``project-notes.md``). The chosen directory MUST be one the project already
    declares -- a person cannot aim a promote at an arbitrary directory through
    this route; the file write is confined to a dir the folder tree already
    steers from.
    """
    # An app/agent/member principal carries a non-empty ``app`` request key
    # (``token_auth``); the person's own dashboard call carries ``""``. Refuse the
    # former before any folder or file work, mirroring the steering_dirs write
    # gate, and audit the denial.
    request_app = str(request.get("app") or "")
    if request_app:
        sel().log_api_access(
            caller=request_app,
            operation="knowledge.project_note_promote",
            outcome="denied",
            source="app_isolation",
            resources=f"note={request.match_info.get('note_id', '-')}",
            error="promoting a note to an instruction is reserved to the person",
        )
        return web.json_response(
            {
                "error": (
                    "Promoting a project note to a steering instruction may be done "
                    "only from the person's own session: folder permission does not "
                    "grant host-file writes."
                ),
                "code": "steering_dirs_forbidden",
            },
            status=403,
        )
    denied = await require_owner_dashboard_request(request, "knowledge.project_note_promote")
    if denied is not None:
        return denied

    state: DashboardState = request.app["state"]
    note_id = request.match_info.get("note_id", "").strip()
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None
    folder_id = str(body.get("folder_id") or "").strip()
    chosen_dir = str(body.get("steering_dir") or "").strip()
    filename = str(body.get("filename") or "").strip() or _DEFAULT_PROMOTE_FILENAME

    namespace = _namespace_for_folder(state, folder_id)
    store = _store(request)
    note = await _run(store, lambda: project_notes.get_note(store, namespace, note_id))
    if note is None:
        return web.json_response({"error": "note not found", "code": "not_found"}, status=404)

    # The chosen directory must be one the PROJECT already declares. Resolve the
    # person's own (slot_app="") steering dirs for the folder chain and require an
    # exact canonical match, so this route can never aim a write at a directory
    # the folder tree does not already steer from.
    allowed, err = _resolve_folder_steering_dirs(_folders(state), folder_id, slot_app="")
    if err:
        return web.json_response(
            {
                "error": f"could not resolve the project's steering directories: {err}",
                "code": "steering_unresolved",
            },
            status=400,
        )
    canonical_choice = validate_file_path(chosen_dir) if chosen_dir else None
    if not canonical_choice or canonical_choice not in allowed:
        return web.json_response(
            {
                "error": (
                    "steering_dir must be one of the project's declared steering "
                    "directories. Declare it on the folder first, then promote."
                ),
                "code": "steering_dir_not_declared",
                "allowed": allowed,
            },
            status=400,
        )

    target_err = _validate_promote_filename(filename)
    if target_err is not None:
        return web.json_response({"error": target_err, "code": "invalid_filename"}, status=400)

    target_path = os.path.join(canonical_choice, filename)
    canonical_target = validate_file_path(target_path)
    if canonical_target is None or is_sensitive_path(canonical_target):
        return web.json_response(
            {
                "error": "the promote target is not a writable, non-sensitive path",
                "code": "invalid_target",
            },
            status=400,
        )
    # Defense in depth: the target must stay INSIDE the chosen (canonical)
    # directory after its own canonicalization, so a filename that resolved
    # through a link out of the dir is refused.
    if os.path.commonpath([canonical_choice, canonical_target]) != canonical_choice:
        return web.json_response(
            {
                "error": "the promote target resolves outside the chosen steering directory",
                "code": "invalid_target",
            },
            status=400,
        )

    try:
        await _run(store, lambda: _append_note_markdown(canonical_target, note))
    except OSError as exc:
        logger.warning("project-note promote write failed", exc_info=True)
        return web.json_response(
            {"error": f"could not write the steering file: {exc}", "code": "write_failed"},
            status=500,
        )
    sel().log_api_access(
        caller="dashboard",
        operation="knowledge.project_note_promote",
        outcome="completed",
        source="dashboard",
        resources=f"note={note_id} -> {canonical_target}",
    )
    return web.json_response({"promoted": note_id, "path": canonical_target})


def _validate_promote_filename(filename: str) -> str | None:
    """Return an error string if *filename* is not a safe Markdown component.

    A single path component (no separators, no ``..``), length-bounded, ending
    ``.md`` so a promote only ever appends to a Markdown steering file. The path
    checks the directory-join and canonicalization re-verify below; this keeps
    the obviously-wrong shapes out with a clear message.
    """
    if len(filename) > _MAX_PROMOTE_FILENAME:
        return f"filename must be at most {_MAX_PROMOTE_FILENAME} characters"
    if os.sep in filename or (os.altsep and os.altsep in filename) or "/" in filename:
        return "filename must be a single file name, not a path"
    if filename in (".", "..") or filename.startswith("."):
        return "filename must be a plain Markdown file name"
    if not filename.lower().endswith(".md"):
        return "filename must end in .md (a steering file is Markdown)"
    return None


def _append_note_markdown(path: str, note: dict) -> None:
    """Append one note as a Markdown section to *path*, creating the file if absent.

    The note text is re-redacted on the way out (it was redacted on store, so
    this is defense in depth for a legacy row), and a provenance line records when
    it was promoted. Opened in append mode so an existing steering file keeps its
    contents and the new learning lands at the end.
    """
    text = note.get("text") or ""
    text, _ = redact_credentials(text)
    text, _ = redact_exfiltration_urls(text)
    stamp = datetime.now().strftime("%Y-%m-%d")
    block = f"\n## Project note (promoted {stamp})\n\n{text.strip()}\n"
    existed = os.path.exists(path)
    with open(path, "a", encoding="utf-8") as fh:
        if not existed:
            fh.write("# Project notes\n")
        fh.write(block)


async def _run(store, fn):
    """Run a blocking store call off the event loop.

    ``store.db`` is thread-local, so a worker thread gets its own connection; the
    note reads/writes are small but they take the guarded knowledge connection,
    which must never block the gateway loop.
    """
    import asyncio

    return await asyncio.to_thread(fn)


def setup_project_notes_routes(app: web.Application) -> None:
    """Register the three project-notes routes on *app*.

    List and delete of a note itself are NOT here: a note is a Knowledge Library
    item, reached through the generic ``GET /api/knowledge/items`` (scoped with
    ``namespace`` + ``type=project_note``) and ``DELETE /api/knowledge/items/{id}``
    routes. These three own only what those cannot: the agent write, the
    folder->project read, and the person-only promote.
    """
    app.router.add_post("/api/knowledge/project-notes/record", record_project_note_route)
    app.router.add_get("/api/knowledge/project-notes", list_project_notes_route)
    app.router.add_post(
        "/api/knowledge/project-notes/{note_id}/promote", promote_project_note_route
    )
