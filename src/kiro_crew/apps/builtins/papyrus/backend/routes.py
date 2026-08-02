"""Papyrus — backend routes.

Registered on the gateway's own aiohttp application at startup (via the app's
``backend.routes`` manifest field, ``"backend.routes:register_routes"``), the same
shape as ``issue_radar`` and ``code_review_sage``. There is NO separate server
process and no port of its own: the upstream app ran a stdlib
``ThreadingHTTPServer``, which cannot participate in the gateway's auth, CSP, or
audit, and whose blocking ``subprocess.run`` calls would have to be re-plumbed
anyway.

Routes (browser-facing, same-origin authed):

  GET    /api/apps/papyrus/health                    -> {"status","compiler","git","managed"}
  POST   /api/apps/papyrus/compiler/provision        -> 202, then poll /health
  GET    /api/apps/papyrus/projects                  -> {"projects": [...]}
  POST   /api/apps/papyrus/projects                  {"name", "template"?}
  POST   /api/apps/papyrus/projects/clone            {"url", "name"?}
  GET    /api/apps/papyrus/project?name=<n>          -> {"name","main_file","files","has_pdf"}
  DELETE /api/apps/papyrus/project?name=<n>          -> {"ok": true}
  GET    /api/apps/papyrus/files?name=<n>            -> {"files": [...]}
  GET    /api/apps/papyrus/file?name=<n>&path=<p>    -> {"path", "content"}
  PUT    /api/apps/papyrus/file                      {"name","path","content"}
  POST   /api/apps/papyrus/file                      {"name","path","content"?}  (create)
  DELETE /api/apps/papyrus/file?name=<n>&path=<p>    -> {"ok": true}
  PUT    /api/apps/papyrus/main                      {"name","path"}
  POST   /api/apps/papyrus/compile                   {"name"} -> CompileResult
  GET    /api/apps/papyrus/pdf?name=<n>              -> application/pdf
  GET    /api/apps/papyrus/git?name=<n>              -> GitStatus
  POST   /api/apps/papyrus/git/commit                {"name","message"?}
  POST   /api/apps/papyrus/git/push                  {"name"}
  POST   /api/apps/papyrus/git/pull                  {"name"}

Authorization is deny-by-default at three layers, in this order:

1. ``_require_enabled`` — the app is opt-in (``defaultEnabled: false``) but routes
   are registered once at startup, so every handler refuses with 403 while the app
   is disabled.
2. ``_project`` — the project name must be a single validated slug that resolves
   inside the app's own data dir (``store.safe_project_dir``), and the directory
   must exist. A name that fails either is a 400/404, never a filesystem touch.
3. ``store.safe_child`` — every relative path (read, write, create, delete, main
   selection) is re-validated against the resolved project root, which is what
   rejects traversal and symlink escapes.

Every blocking call — filesystem walks, file reads/writes, and the compiler and
git subprocesses — runs off the event loop. See the module docstrings in
``latex.py`` and ``gitops.py``.

**Path validation is itself blocking, and is offloaded too.** ``_project`` /
``_project_for_create`` / ``_safe_relative`` are the containment gate above, and
each one calls ``Path.resolve()`` plus a ``stat``-family probe — real syscalls,
which on a stalled network filesystem (a ``KIROCREW_HOME`` on NFS/SMB) block for
as long as the mount takes to answer. The gateway runs every session, cron job and
the liveness heartbeat on ONE asyncio loop, so a validation call left inline would
freeze all of them until the watchdog killed the process. Each handler therefore
groups its validation together with the filesystem work it authorizes into ONE
sync closure behind a single ``asyncio.to_thread`` hop.

Grouping is what keeps the ordering honest: validation still runs FIRST, and no
``await`` sits between the check and the use it authorizes, so the check/use
window is *narrower* than it was when the gate ran on the loop. The three
handlers that cannot group — ``clone``, ``compile`` and the four ``git`` routes,
whose next step is an ``await`` on a subprocess that needs the validated path —
take one hop for validation alone. ``web.HTTPException`` raised inside a worker
thread propagates through the ``await`` unchanged (and subclasses none of
``ValueError``/``OSError``/``FileExistsError``, so the handlers' ``except``
ladders cannot swallow it), which is what lets the gate keep answering 400/404/409
from off the loop. Pinned by ``test_papyrus_routes.py``
``::TestNoBlockingCallsOnTheLoop``.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from functools import wraps
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiohttp import web

from kiro_crew.apps.builtins.papyrus.backend import gitops, latex, store, tectonic
from kiro_crew.apps.manager import is_app_enabled

try:
    from kiro_crew.security import redact

    _HAS_SECURITY = True
except ImportError:  # pragma: no cover - security module is always present
    _HAS_SECURITY = False

logger = logging.getLogger("kirocrew.app.papyrus")

#: Base path for every route. One constant so the registration block and the
#: frontend's API base cannot drift apart silently.
API_BASE = "/api/apps/papyrus"

#: Starter document for a new project. Deliberately minimal and package-standard
#: (amsmath/graphicx/hyperref/natbib/booktabs are what a paper actually uses).
DEFAULT_TEMPLATE = r"""\documentclass[11pt]{article}
\usepackage[utf8]{inputenc}
\usepackage{amsmath,amssymb}
\usepackage{graphicx}
\usepackage{hyperref}
\usepackage{natbib}
\usepackage{booktabs}

\title{Untitled Paper}
\author{Author Name}
\date{\today}

\begin{document}
\maketitle

\begin{abstract}
Your abstract here.
\end{abstract}

\section{Introduction}

Start writing here.

\bibliographystyle{plainnat}
\bibliography{references}

\end{document}
"""

#: Cap on an accepted JSON request body. The editor's saves are the large ones;
#: a thesis chapter is comfortably inside this, and the cap stops a client from
#: making the gateway buffer an arbitrary amount.
MAX_BODY_BYTES = 16 * 1024 * 1024

_Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


def _require_enabled(handler: _Handler) -> _Handler:
    """Deny every request while Papyrus is disabled (deny-by-default).

    Routes are registered once at gateway startup, so a default-disabled app
    would otherwise stay callable. ``is_app_enabled`` is a synchronous
    ``installed.json`` read, so it runs off the event loop.
    """

    @wraps(handler)
    async def _wrapped(request: web.Request) -> web.StreamResponse:
        if not await asyncio.to_thread(is_app_enabled, store.APP_NAME):
            return web.json_response({"error": "papyrus is disabled", "code": "app_disabled"}, status=403)
        return await handler(request)

    return _wrapped


def _clean(text: str) -> str:
    """Redact credentials + exfiltration URLs from tool output bound for the UI.

    Applies to the two surfaces that echo raw subprocess output: the LaTeX
    compile log and ``git``'s stderr. Both carry content this app does not
    author — a ``.tex`` is written by the agent or arrives wholesale from
    ``POST /projects/clone`` and can emit anything through ``\\message{}`` or an
    error path, and a failed push makes ``git`` echo the remote URL, which is
    where a personal access token embedded in a remote lives. ``redact()`` runs
    both required passes.

    Fails CLOSED: if the security module cannot be imported, the text is dropped
    rather than forwarded unscanned.

    Deliberately NOT applied to a file's ``content`` (``GET /file``): that value
    populates the editor and is written straight back by a save, so redacting it
    would overwrite the user's own source with ``[REDACTED]`` markers. This is the
    same carve-out ``security_posture`` records for the steering-file handler.
    """
    if not text:
        return text
    if not _HAS_SECURITY:
        return "[output withheld: redaction unavailable]"
    return redact(text)


async def _json_body(request: web.Request) -> dict[str, Any]:
    """Read a JSON object body, or raise a 400 ``HTTPException``."""
    if request.content_length is not None and request.content_length > MAX_BODY_BYTES:
        raise web.HTTPRequestEntityTooLarge(
            max_size=MAX_BODY_BYTES, actual_size=request.content_length
        )
    try:
        body = await request.json()
    except Exception as exc:
        raise web.HTTPBadRequest(reason="request body must be JSON") from exc
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(reason="request body must be a JSON object")
    return body


def _str_field(source: dict[str, Any], key: str) -> str:
    """A trimmed string field, or ``""`` for anything that is not a string.

    ``(source.get(k) or "").strip()`` raises ``AttributeError`` on a truthy
    non-string (``{"name": 1}``), which surfaces as a 500 for a plainly malformed
    request. Callers treat ``""`` as missing and answer 400.
    """
    value = source.get(key)
    return value.strip() if isinstance(value, str) else ""


def _project(name: str) -> Path:
    """Resolve *name* to an existing project directory, or raise an HTTP error.

    This is the app's authorization gate for a project: the name must be one
    validated slug segment resolving inside the app's own data dir, and the
    directory must already exist. Both failures are explicit HTTP responses, so
    no unvalidated string reaches the filesystem.

    BLOCKING — ``safe_project_dir`` calls ``Path.resolve()`` and ``is_dir()``
    ``stat``s. Call it from inside a worker-thread closure, never on the loop.
    """
    if not name:
        raise web.HTTPBadRequest(reason="missing 'name'")
    try:
        project = store.safe_project_dir(name)
    except store.PathRejected as exc:
        raise web.HTTPBadRequest(reason=str(exc)) from exc
    if not project.is_dir():
        raise web.HTTPNotFound(reason="project not found")
    return project


def _project_for_create(name: str) -> Path:
    """Resolve a project directory that must NOT yet exist.

    BLOCKING — ``Path.resolve()`` plus an ``exists()`` ``stat``. Worker thread only.
    """
    if not name:
        raise web.HTTPBadRequest(reason="missing 'name'")
    try:
        project = store.safe_project_dir(name)
    except store.PathRejected as exc:
        raise web.HTTPBadRequest(reason=str(exc)) from exc
    if project.exists():
        raise web.HTTPConflict(reason="project already exists")
    return project


def _safe_relative(project: Path, relative: str) -> str:
    """Validate a caller-supplied relative path, returning it unchanged.

    The resolved path is discarded on purpose: the store functions re-resolve it
    themselves, so there is exactly one place that decides what a path means. This
    call is here to turn a rejection into a 400 before any store call runs.

    BLOCKING — ``safe_child`` calls ``Path.resolve()``. Worker thread only.
    """
    if not relative:
        raise web.HTTPBadRequest(reason="missing 'path'")
    try:
        store.safe_child(project, relative)
    except store.PathRejected as exc:
        raise web.HTTPBadRequest(reason=str(exc)) from exc
    return relative


# ── health ──────────────────────────────────────────────────────────────────


async def _handle_health(request: web.Request) -> web.StreamResponse:
    """Report whether a compiler and git are available on this host.

    The UI uses this to explain "press Cmd+S does nothing" before the user has a
    compiler installed, rather than surfacing it as a failed compile. It also
    carries the ``managed`` block — whether this platform has a pinned Tectonic
    build, whether one is installed, and the provisioning job's live state — so
    the same poll drives both the warning banner and the install progress.
    """
    compiler = await latex.find_compiler()
    has_git = await asyncio.to_thread(gitops.git_available)
    managed = await asyncio.to_thread(tectonic.managed_status)
    return web.json_response(
        {
            "status": "ok",
            "compiler": Path(compiler).name if compiler else "",
            "git": has_git,
            "managed": managed,
        }
    )


# ── managed compiler provisioning ───────────────────────────────────────────


async def _handle_provision_compiler(request: web.Request) -> web.StreamResponse:
    """POST /compiler/provision — install the app's managed Tectonic compiler.

    Answers **202 immediately** and the UI polls ``GET /health``: the transfer is
    ~10-22MB, far longer than a request should hold open. The work runs on a
    daemon thread inside :func:`tectonic.provision_in_background`, so no
    filesystem or network call touches the event loop.

    **This is not the system-package install that ``pptx-maker`` deliberately
    refuses.** That decision — "installing a system package is a host-level action
    a web request must not take", pinned there by the absence of
    ``POST /deps/install`` — stands. This endpoint invokes no package manager and
    no privilege: it downloads ONE sha256-pinned, self-contained binary into this
    app's OWN data dir (``…/apps/papyrus/data/vendor/tectonic/``), never a system
    prefix, never ``PATH``, and it is undone by deleting that directory. See the
    module docstring of :mod:`.tectonic` and the spec's "Managed compiler"
    section.

    Explicitly user-triggered rather than automatic on enable, and never at
    startup: a 22MB fetch must be a decision, not a side effect of opening a page.
    Idempotent — an already-installed compiler answers 200 instead of downloading
    again.
    """
    if await asyncio.to_thread(tectonic.binary_installed):
        return web.json_response({"ok": True, "state": tectonic.STATE_DONE})
    if not await asyncio.to_thread(tectonic.platform_supported):
        # A 32-bit Linux box, a BSD, or Windows-on-ARM: there is no pinned build,
        # so say so plainly and leave the manual install path intact.
        return web.json_response(
            {
                "error": "no pinned Tectonic build for this platform",
                "code": "compiler_unsupported_platform",
            },
            status=422,
        )
    started = await asyncio.to_thread(tectonic.provision_in_background)
    if not started:
        # A job is already in flight; the UI is polling /health for it either way.
        return web.json_response({"ok": True, "state": tectonic.STATE_DOWNLOADING}, status=202)
    logger.info("papyrus: started managed Tectonic provisioning (%s)", tectonic.TECTONIC_RELEASE_TAG)
    return web.json_response({"ok": True, "state": tectonic.STATE_DOWNLOADING}, status=202)


# ── projects ────────────────────────────────────────────────────────────────


async def _handle_list_projects(request: web.Request) -> web.StreamResponse:
    projects = await asyncio.to_thread(store.list_projects)
    return web.json_response({"projects": [p.to_dict() for p in projects]})


async def _handle_create_project(request: web.Request) -> web.StreamResponse:
    body = await _json_body(request)
    name = store.normalize_project_name(_str_field(body, "name"))
    template = body.get("template")
    content = template if isinstance(template, str) and template.strip() else DEFAULT_TEMPLATE

    def _create() -> None:
        """BLOCKING — validate, then create, in ONE hop.

        The 409-on-existing check and the ``mkdir`` that depends on it stay in the
        same closure with no ``await`` between them, so offloading the gate does not
        widen the window in which a concurrent create could win the name.
        """
        # Size FIRST, before anything is created. `write_file` raises for an oversized
        # body, but by then `mkdir` has already taken the name — so a 9 MiB template
        # answered 500 and left an empty project directory behind, making every later
        # create of that name answer 409. The user could neither use the name nor see
        # why. Validating up front means the failure leaves no trace.
        if len(content.encode("utf-8")) > store.MAX_FILE_BYTES:
            raise web.HTTPRequestEntityTooLarge(
                max_size=store.MAX_FILE_BYTES,
                actual_size=len(content.encode("utf-8")),
            )
        project = _project_for_create(name)
        try:
            # `exist_ok=False` (the default) on purpose: the FileExistsError is the
            # authoritative answer to "did someone else take this name", and it is the
            # only one free of a check/use window. `_project_for_create`'s `exists()`
            # probe above narrows the window but cannot close it — two worker threads
            # can both pass it — so the loser used to raise an unhandled
            # FileExistsError and the request 500'd on what is really a 409.
            #
            # Same conflict, same status, as the probe reports; the user sees "project
            # already exists" either way rather than a server error for a name clash
            # they can fix by picking another.
            project.mkdir(parents=True)
        except FileExistsError as exc:
            raise web.HTTPConflict(reason="project already exists") from exc
        store.write_file(project, store.DEFAULT_MAIN_FILE, content)
        store.write_file(project, "references.bib", "")

    await asyncio.to_thread(_create)
    logger.info("papyrus: created project %s", name)
    return web.json_response({"name": name, "main_file": store.DEFAULT_MAIN_FILE}, status=201)


async def _handle_clone_project(request: web.Request) -> web.StreamResponse:
    body = await _json_body(request)
    url = _str_field(body, "url")
    if not url:
        raise web.HTTPBadRequest(reason="missing 'url'")
    requested = _str_field(body, "name") or gitops.derive_project_name(url)
    name = store.normalize_project_name(requested)
    # Validation alone in this hop: the very next step is an `await` on a git
    # subprocess that needs the resolved path, so there is nothing to group it
    # with. The check/use window is unchanged — it was already an `await` wide,
    # and `git clone` refuses a non-empty destination at the syscall regardless.
    project = await asyncio.to_thread(_project_for_create, name)

    try:
        await gitops.clone(url, project)
    except gitops.GitError as exc:
        return web.json_response(
            {"error": _clean(str(exc)), "output": _clean(exc.output), "code": "git_failed"},
            status=422,
        )

    main_file = await asyncio.to_thread(store.resolve_main_file, project)
    if main_file is None:
        # A repo with no .tex is not a paper. Remove the clone so the name is
        # free again and the user is not left with an unopenable project.
        await asyncio.to_thread(shutil.rmtree, project, True)
        return web.json_response({"error": "no .tex file found in repository", "code": "no_tex_in_repository"}, status=422)
    logger.info("papyrus: cloned project %s", name)
    return web.json_response({"name": name, "main_file": main_file}, status=201)


async def _handle_get_project(request: web.Request) -> web.StreamResponse:
    name = request.query.get("name", "").strip()

    def _read() -> tuple[Path, str | None, list[str], bool]:
        """BLOCKING — authorize the name, then walk the project, in ONE hop."""
        project = _project(name)
        main_file = store.resolve_main_file(project)
        if main_file is None:
            return project, None, [], False
        return (
            project,
            main_file,
            store.list_files(project),
            bool((pdf := store.pdf_path(project, main_file)) and pdf.is_file()),
        )

    project, main_file, files, has_pdf = await asyncio.to_thread(_read)
    if main_file is None:
        raise web.HTTPNotFound(reason="no .tex file found in project")
    return web.json_response(
        {"name": project.name, "main_file": main_file, "files": files, "has_pdf": has_pdf}
    )


async def _handle_delete_project(request: web.Request) -> web.StreamResponse:
    name = request.query.get("name", "").strip()

    def _delete() -> Path:
        """BLOCKING — authorize the name, then remove the tree, in ONE hop."""
        project = _project(name)
        shutil.rmtree(project, True)
        return project

    project = await asyncio.to_thread(_delete)
    logger.info("papyrus: deleted project %s", project.name)
    return web.json_response({"ok": True})


# ── files ───────────────────────────────────────────────────────────────────


async def _handle_list_files(request: web.Request) -> web.StreamResponse:
    name = request.query.get("name", "").strip()

    def _list() -> list[str]:
        """BLOCKING — authorize the name, then walk the tree, in ONE hop."""
        return store.list_files(_project(name))

    files = await asyncio.to_thread(_list)
    return web.json_response({"files": files})


async def _handle_read_file(request: web.Request) -> web.StreamResponse:
    name = request.query.get("name", "").strip()
    relative = request.query.get("path", "").strip()

    def _read() -> str:
        """BLOCKING — authorize name + path, then read, in ONE hop."""
        project = _project(name)
        return store.read_text_file(project, _safe_relative(project, relative))

    try:
        content = await asyncio.to_thread(_read)
    except store.PathRejected as exc:
        raise web.HTTPBadRequest(reason=str(exc)) from exc
    except FileNotFoundError as exc:
        raise web.HTTPNotFound(reason="file not found") from exc
    except ValueError as exc:
        raise web.HTTPBadRequest(reason=str(exc)) from exc
    return web.json_response({"path": relative, "content": content})


async def _handle_save_file(request: web.Request) -> web.StreamResponse:
    body = await _json_body(request)
    name = _str_field(body, "name")
    relative = _str_field(body, "path")
    content = body.get("content")
    if not isinstance(content, str):
        raise web.HTTPBadRequest(reason="'content' must be a string")

    def _write() -> None:
        """BLOCKING — authorize name + path, then write, in ONE hop."""
        project = _project(name)
        store.write_file(project, _safe_relative(project, relative), content)

    try:
        await asyncio.to_thread(_write)
    except store.PathRejected as exc:
        raise web.HTTPBadRequest(reason=str(exc)) from exc
    except ValueError as exc:
        raise web.HTTPBadRequest(reason=str(exc)) from exc
    return web.json_response({"ok": True, "path": relative})


async def _handle_create_file(request: web.Request) -> web.StreamResponse:
    body = await _json_body(request)
    name = _str_field(body, "name")
    relative = _str_field(body, "path")
    raw = body.get("content")
    content = raw if isinstance(raw, str) else ""

    def _create() -> None:
        """BLOCKING — authorize name + path, then create, in ONE hop."""
        project = _project(name)
        store.create_file(project, _safe_relative(project, relative), content)

    try:
        await asyncio.to_thread(_create)
    except store.PathRejected as exc:
        raise web.HTTPBadRequest(reason=str(exc)) from exc
    except FileExistsError as exc:
        raise web.HTTPConflict(reason="file already exists") from exc
    except ValueError as exc:
        raise web.HTTPBadRequest(reason=str(exc)) from exc
    return web.json_response({"ok": True, "path": relative}, status=201)


async def _handle_delete_file(request: web.Request) -> web.StreamResponse:
    name = request.query.get("name", "").strip()
    relative = request.query.get("path", "").strip()

    def _delete() -> None:
        """BLOCKING — authorize name + path, then delete, in ONE hop."""
        project = _project(name)
        store.delete_file(project, _safe_relative(project, relative))

    try:
        await asyncio.to_thread(_delete)
    except store.PathRejected as exc:
        raise web.HTTPBadRequest(reason=str(exc)) from exc
    except FileNotFoundError as exc:
        raise web.HTTPNotFound(reason="file not found") from exc
    except ValueError as exc:
        raise web.HTTPBadRequest(reason=str(exc)) from exc
    return web.json_response({"ok": True})


async def _handle_set_main(request: web.Request) -> web.StreamResponse:
    body = await _json_body(request)
    name = _str_field(body, "name")
    relative = _str_field(body, "path")

    def _apply() -> None:
        """BLOCKING — authorize name + path, then set the main file, in ONE hop."""
        project = _project(name)
        _safe_relative(project, relative)
        if not store.safe_child(project, relative).is_file():
            raise FileNotFoundError(relative)
        store.set_main_file(project, relative)

    try:
        await asyncio.to_thread(_apply)
    except store.PathRejected as exc:
        raise web.HTTPBadRequest(reason=str(exc)) from exc
    except FileNotFoundError as exc:
        raise web.HTTPNotFound(reason="file not found") from exc
    return web.json_response({"ok": True, "main_file": relative})


# ── compile + pdf ───────────────────────────────────────────────────────────


async def _handle_compile(request: web.Request) -> web.StreamResponse:
    body = await _json_body(request)
    name = _str_field(body, "name")

    def _resolve() -> tuple[Path, str | None]:
        """BLOCKING — authorize the name, then resolve the main doc, in ONE hop."""
        project = _project(name)
        return project, store.resolve_main_file(project)

    project, main_file = await asyncio.to_thread(_resolve)
    if main_file is None:
        raise web.HTTPNotFound(reason="no .tex file found in project")
    result = await latex.compile_project(project, main_file)
    payload = result.to_dict()
    # The log is the compiler's raw stdout+stderr tail and each diagnostic message
    # is a line lifted out of it, so both are redacted before they reach the UI.
    payload["log"] = _clean(str(payload.get("log") or ""))
    for diag in payload.get("errors") or []:
        diag["message"] = _clean(str(diag.get("message") or ""))
        # `file` too. It is parsed out of the SAME compiler line as `message` — a
        # `\input{...}` names any path the document chose, and TeX echoes it back —
        # so redacting one field and not its sibling left the identical leak one key
        # over. `None` is preserved rather than becoming `""`: the frontend renders
        # the file only when it is present.
        if diag.get("file"):
            diag["file"] = _clean(str(diag["file"]))
    if result.ok:
        return web.json_response(payload, status=200)
    # The failure body is spelled out as a dict LITERAL against a LITERAL status.
    # A computed status reads to the error-code scanner as `dynamic_status`, and a
    # bare variable or a `**spread` reads as `opaque_body` — in both cases it cannot
    # see the `code`, so only this form proves the contract holds.
    return web.json_response(
        {
            "ok": False,
            "log": payload["log"],
            "errors": payload.get("errors") or [],
            "duration_ms": payload.get("duration_ms", 0),
            "code": "compile_failed",
        },
        status=422,
    )


async def _handle_pdf(request: web.Request) -> web.StreamResponse:
    """Serve the compiled PDF.

    ``Content-Disposition: inline`` plus a restrictive per-response CSP: the
    document is rendered in the browser's own PDF viewer, and it is content the
    agent (or a cloned repo) produced, so it must not be able to run script in the
    dashboard's origin.
    """
    name = request.query.get("name", "").strip()

    def _resolve() -> tuple[Path, Path | None]:
        """BLOCKING — authorize and resolve the PDF path in ONE hop.

        Returns the PATH, not the bytes. Reading them here put the whole file in
        gateway memory: a PDF's size is decided by the document being compiled (and
        by a cloned repo, which can simply ship a large ``main.pdf``), so a single
        open of the viewer could exhaust the process. ``web.FileResponse`` streams
        from disk instead, so the response costs a buffer rather than the file.

        The check/use window widens from "none" to "the file could be deleted between
        `is_file()` and the send". That trade is deliberate and small: the failure mode
        is a 404 or a truncated download of a file the user just deleted, against an
        OOM that takes the whole gateway — every chat session, cron and the heartbeat
        — with it.
        """
        project = _project(name)
        main_file = store.resolve_main_file(project)
        if main_file is None:
            return project, None
        # `None` when the derived name is not contained — a cloned repo shipping
        # `main.pdf -> ~/.kiro/crew/.local_secret` would otherwise have that file
        # streamed straight to the browser by the `FileResponse` below.
        pdf = store.pdf_path(project, main_file)
        if pdf is None or not pdf.is_file():
            return project, None
        return project, pdf

    project, pdf_path = await asyncio.to_thread(_resolve)
    if pdf_path is None:
        raise web.HTTPNotFound(reason="no PDF yet — compile first")
    # Every header the buffered response carried is preserved. The CSP and `nosniff`
    # are load-bearing, not decoration: this is agent- or repo-produced content
    # rendered inline by the browser's own PDF viewer, so it must not be able to run
    # script in the dashboard's origin.
    return web.FileResponse(
        path=pdf_path,
        headers={
            "Content-Type": "application/pdf",
            "Content-Disposition": f'inline; filename="{project.name}.pdf"',
            "Content-Security-Policy": "sandbox; default-src 'none'; object-src 'none'",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )


# ── git ─────────────────────────────────────────────────────────────────────
#
# Each of these four validates the project name in ONE thread hop and then awaits
# a git subprocess with the resolved path. There is nothing to group the check
# with — the use is the `await` itself — and grouping is impossible anyway, since
# `gitops` spawns through the event loop's own subprocess transport. The
# check/use window is therefore exactly what it was before the offload.


async def _handle_git_status(request: web.Request) -> web.StreamResponse:
    project = await asyncio.to_thread(_project, request.query.get("name", "").strip())
    try:
        result = await gitops.status(project)
    except gitops.GitError as exc:
        return web.json_response({"is_git": False, "error": _clean(str(exc))})
    payload = result.to_dict()
    # Commit subjects and `git status` lines are author-supplied text from the
    # repository, which may be one this app cloned rather than one it created.
    for key in ("changes", "recent_commits"):
        if isinstance(payload.get(key), list):
            payload[key] = [_clean(str(item)) for item in payload[key]]
    # `branch` is the same class of value and was the one string field here that
    # skipped the pass: a ref name is chosen by whoever authored the repository, it is
    # rendered in the toolbar, and a branch can legally be named almost anything. Same
    # omission shape as the diagnostic `file` above — redact the list fields, miss the
    # scalar beside them.
    if payload.get("branch"):
        payload["branch"] = _clean(str(payload["branch"]))
    return web.json_response(payload)


async def _handle_git_commit(request: web.Request) -> web.StreamResponse:
    body = await _json_body(request)
    project = await asyncio.to_thread(_project, _str_field(body, "name"))
    message = _str_field(body, "message") or gitops.DEFAULT_COMMIT_MESSAGE
    try:
        output = await gitops.commit(project, message)
    except gitops.GitError as exc:
        return web.json_response(
            {"error": _clean(str(exc)), "output": _clean(exc.output), "code": "git_failed"},
            status=422,
        )
    return web.json_response({"ok": True, "output": _clean(output)})


async def _handle_git_push(request: web.Request) -> web.StreamResponse:
    body = await _json_body(request)
    project = await asyncio.to_thread(_project, _str_field(body, "name"))
    try:
        output = await gitops.push(project)
    except gitops.GitError as exc:
        # Two literal-status returns rather than `status=401 if ... else 422`: the
        # error-code scanner reads a computed status as `dynamic_status` and cannot
        # tell the response is an error at all.
        if exc.auth:
            return web.json_response(
                {
                    "error": _clean(str(exc)),
                    "output": _clean(exc.output),
                    "code": "git_auth_failed",
                },
                status=401,
            )
        return web.json_response(
            {"error": _clean(str(exc)), "output": _clean(exc.output), "code": "git_failed"},
            status=422,
        )
    return web.json_response({"ok": True, "output": _clean(output)})


async def _handle_git_pull(request: web.Request) -> web.StreamResponse:
    body = await _json_body(request)
    project = await asyncio.to_thread(_project, _str_field(body, "name"))
    try:
        output, stashed = await gitops.pull(project)
    except gitops.GitConflict as exc:
        return web.json_response(
            {"error": _clean(str(exc)), "output": _clean(exc.output), "code": "git_conflict"},
            status=409,
        )
    except gitops.GitError as exc:
        return web.json_response(
            {"error": _clean(str(exc)), "output": _clean(exc.output), "code": "git_failed"},
            status=422,
        )
    return web.json_response({"ok": True, "output": _clean(output), "stashed": stashed})


# ── registration ────────────────────────────────────────────────────────────


def register_routes(app: web.Application) -> None:
    """Register this app's routes on the gateway's aiohttp Application.

    Signature matches every other builtin app (``_mod.register_routes(app)``,
    single argument, hardcoded paths) — see ``dashboard/server.py``'s startup
    registration loop.
    """
    app.router.add_get(f"{API_BASE}/health", _require_enabled(_handle_health))
    app.router.add_post(
        f"{API_BASE}/compiler/provision", _require_enabled(_handle_provision_compiler)
    )

    app.router.add_get(f"{API_BASE}/projects", _require_enabled(_handle_list_projects))
    app.router.add_post(f"{API_BASE}/projects", _require_enabled(_handle_create_project))
    app.router.add_post(f"{API_BASE}/projects/clone", _require_enabled(_handle_clone_project))
    app.router.add_get(f"{API_BASE}/project", _require_enabled(_handle_get_project))
    app.router.add_delete(f"{API_BASE}/project", _require_enabled(_handle_delete_project))

    app.router.add_get(f"{API_BASE}/files", _require_enabled(_handle_list_files))
    app.router.add_get(f"{API_BASE}/file", _require_enabled(_handle_read_file))
    app.router.add_put(f"{API_BASE}/file", _require_enabled(_handle_save_file))
    app.router.add_post(f"{API_BASE}/file", _require_enabled(_handle_create_file))
    app.router.add_delete(f"{API_BASE}/file", _require_enabled(_handle_delete_file))
    app.router.add_put(f"{API_BASE}/main", _require_enabled(_handle_set_main))

    app.router.add_post(f"{API_BASE}/compile", _require_enabled(_handle_compile))
    app.router.add_get(f"{API_BASE}/pdf", _require_enabled(_handle_pdf))

    app.router.add_get(f"{API_BASE}/git", _require_enabled(_handle_git_status))
    app.router.add_post(f"{API_BASE}/git/commit", _require_enabled(_handle_git_commit))
    app.router.add_post(f"{API_BASE}/git/push", _require_enabled(_handle_git_push))
    app.router.add_post(f"{API_BASE}/git/pull", _require_enabled(_handle_git_pull))
