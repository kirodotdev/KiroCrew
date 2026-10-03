"""Notes' in-gateway route: the state-file publish.

Why this ONE write runs in the GATEWAY process and not in the Notes backend
===========================================================================

The three Notes state files -- ``pat``, ``vaults.json`` and ``settings.json``
under ``workspace/md-notebook/`` -- are each bind-masked from every sandboxed
process and carved back to this app's own backend so it can READ them (see
``sandbox._MD_NOTEBOOK_STATE_LEAVES`` and ``app_backend_visible_targets``). The
backend is a spawned, sandboxed process, so the carve-out gives it the three
leaves and their write-staging directory ``md-notebook-staging``.

A WRITE cannot complete in the backend, though. Each carved-back leaf is bound
over its own name individually, and ``md-notebook-staging`` is a separate bind,
so the staging directory and a target leaf are DIFFERENT mount points even
though they share one filesystem. ``os.replace`` -- and ``os.link`` -- refuse to
cross a mount point (``EXDEV``), so the staging-then-rename publish raised
``EXDEV`` inside the backend's namespace. Staging the temp beside the target
instead put the real PAT bytes at an unmasked name a same-uid sandboxed agent
could read, which is the leak this route removes.

The gateway process has no such namespace: it sees the real crew data home with
no bind masks, so the staging directory and the targets are one filesystem AND
one mount, and the ordinary staged-rename publish is a plain atomic rename. So
the publish moves here, and the backend posts its content to this route rather
than writing the files itself. The staging bytes never exist anywhere a
sandboxed process can read: the gateway owns the whole write.

Auth is this app's own App Kit token (``request["app"] == "md-notebook"``), the
same credential the backend already holds to call back to the gateway. Writing
through this route exposes NOTHING readable to the sandbox -- it returns only an
ok/error -- so, unlike Dev Fleet's owner-only cutover, an app-token write is
safe here: a build child that stole the token could write content the user's own
Notes UI could already write, and could learn nothing by doing so. The route
validates that the named leaf is one of the three state files and refuses
anything else.

Registered at gateway startup by the ``BUILTIN_NAMES`` loop in
``dashboard/routes/system.py`` (``_mod.register_routes(app)`` on this package;
the hook is re-exported from ``__init__.py`` as in ``dev_fleet``).
"""

from __future__ import annotations

import asyncio
import importlib
import logging

from aiohttp import web

from kiro_crew.apps.manager import is_app_enabled
from kiro_crew.dashboard.handlers._shared import read_bounded_json
from kiro_crew.sandbox import MD_NOTEBOOK_APP_NAME
from kiro_crew.sel import sel

logger = logging.getLogger("kirocrew.app.md-notebook.gateway")

APP_NAME = MD_NOTEBOOK_APP_NAME
API_PREFIX = f"/api/apps/{APP_NAME}"

#: The content a single publish carries, capped. The real documents are tiny (a
#: PAT, a short vault list, a settings object); 256 KiB is generous headroom
#: while bounding a request before it is read into the gateway.
_MAX_STATE_BYTES = 256 * 1024


def _caller(request: web.Request) -> str:
    return str(request.get("app") or request.get("user") or request.remote or "unknown")


def _deny(
    request: web.Request, operation: str, error: str, *, status: int, code: str = "forbidden"
) -> web.Response:
    sel().log_api_access(
        caller=_caller(request),
        operation=operation,
        outcome="denied",
        source="md_notebook_gateway",
        resources=request.path,
        error=error,
    )
    return web.json_response({"ok": False, "code": code, "error": error}, status=status)


def _target_for_leaf(server, leaf: str):
    """The resolved target Path for a named state leaf, or ``None`` if unknown.

    An allowlist by name, never a path join: the only writable targets are the
    three state files, resolved through ``server``'s own path helpers so the
    gateway writes exactly the files the backend reads. Any other value -- a
    traversal, an absolute path, a fourth name -- resolves to ``None`` and is
    refused. ``server`` is passed in (imported lazily by the handler) so this
    module imports NOTHING that parses the environment at gateway startup.
    """
    if leaf == "pat":
        return server._pat_file()
    if leaf == "vaults.json":
        return server._vaults_json()
    if leaf == "settings.json":
        return server._settings_json()
    return None


#: The leaves whose write fsyncs the file before publishing: the PAT and the
#: settings (which carries the autoSync authorization bit). Matches the
#: ``fsync_file=True`` the backend's own writers pass, so routing the write
#: through the gateway does not weaken the durability the direct write had.
_FSYNC_LEAVES = frozenset({"pat", "settings.json"})


async def handle_state_write(request: web.Request) -> web.Response:
    """POST /api/apps/md-notebook/state -- publish one state leaf's content.

    Body ``{"leaf": "pat"|"vaults.json"|"settings.json", "content": str}``. The
    publish runs in THIS (gateway) process, where the staging dir and the target
    share one mount, so the staged rename never raises ``EXDEV`` and the content
    never lands at an unmasked name a sandboxed process could read.
    """
    operation = "md_notebook_state_write"
    if not await asyncio.to_thread(is_app_enabled, APP_NAME):
        return _deny(request, operation, "app not enabled", status=404, code="app_not_enabled")
    if request.get("app") != APP_NAME:
        return _deny(
            request,
            operation,
            "only the Notes backend may publish state",
            status=403,
        )
    body, err = await read_bounded_json(request, _MAX_STATE_BYTES)
    if err is not None:
        return err
    assert body is not None
    leaf = body.get("leaf")
    content = body.get("content")
    if not isinstance(leaf, str) or not isinstance(content, str):
        return _deny(
            request,
            operation,
            "'leaf' and 'content' must both be strings",
            status=400,
            code="invalid_request",
        )
    # Load the backend module HERE, not at module scope: this route is registered
    # into the gateway at startup, and ``server`` (via ``git_ops``) parses the
    # environment at import (``PORT``, ``MDNB_GIT_TIMEOUT_SEC``), so a module-scope
    # import would let a bad env value abort gateway startup before the socket
    # binds (the registration loop swallows only ModuleNotFoundError). Loaded per
    # request via ``importlib`` (a top-level import) after the body is validated,
    # so any such failure is a 500 on this one call rather than a dead gateway.
    # Run the import OFF the event loop (same ``asyncio.to_thread`` idiom as the
    # ``is_app_enabled`` gate above): the FIRST call executes ``server``'s module
    # body, which imports ``git_ops``/``notes`` and does synchronous work, and
    # that would block the gateway loop and its heartbeat. ``importlib`` caches
    # the module, so every later call is a cheap dict lookup on the worker thread.
    server = await asyncio.to_thread(
        importlib.import_module, "kiro_crew.apps.builtins.md_notebook.server"
    )

    target = _target_for_leaf(server, leaf)
    if target is None:
        # Audited like every other refusal on this route: an authenticated token
        # asking to write a name outside the three-leaf allowlist is a denied
        # access, and SEL is where that is recorded. The leaf value is kept out
        # of the response and the audit line (it is attacker-supplied).
        return _deny(
            request,
            operation,
            "not a state file",
            status=400,
            code="unknown_leaf",
        )
    # Re-apply the vault-registry sensitive-path refusal that otherwise lives only
    # in the backend's own vault-attach handler. The gateway route is a SECOND
    # write path onto the vault registry, so a body that points a vault at a
    # protected location (~/.ssh) would otherwise let the unattended sync loop
    # ``git add -A`` and push a credential store. Run OFF the event loop: for
    # ``vaults.json`` the check resolves each content root (``Path.resolve()``,
    # synchronous filesystem I/O), which on a stalled network mount would freeze
    # the whole gateway loop and its heartbeat — the same reason ``vault_path``
    # exists. The reason string is constant and caller-safe (it never echoes the
    # attacker-supplied body).
    try:
        unsafe = await asyncio.to_thread(server.refuse_unsafe_state_write, leaf, content)
    except Exception:
        # The validator is designed to fail CLOSED — ``_sensitive_vault_root``
        # returns True for a malformed/traversing entry — so a raised exception
        # here means an unforeseen shape the validator could not classify. Treat
        # it as a refusal, not a bare 500: audit it through ``_deny`` (the designed
        # ``denied`` outcome) rather than letting it escape unaudited. The
        # attacker-supplied body is never echoed.
        return _deny(
            request,
            operation,
            "a vault content root could not be validated",
            status=403,
            code="sensitive_state",
        )
    if unsafe is not None:
        return _deny(request, operation, unsafe, status=403, code="sensitive_state")
    try:
        await asyncio.to_thread(
            server._write_state_in_process_sync,
            target,
            content,
            fsync_file=leaf in _FSYNC_LEAVES,
        )
    except OSError as exc:
        sel().log_api_access(
            caller=_caller(request),
            operation=operation,
            outcome="failed",
            source="md_notebook_gateway",
            resources=leaf,
            error=type(exc).__name__,
        )
        return web.json_response(
            {"ok": False, "code": "write_failed", "error": "could not publish state"},
            status=500,
        )
    sel().log_api_access(
        caller=_caller(request),
        operation=operation,
        outcome="success",
        source="md_notebook_gateway",
        resources=leaf,
    )
    return web.json_response({"ok": True})


def register_routes(app: web.Application) -> None:
    """Register this app's in-gateway route (``_mod.register_routes(app)`` contract)."""
    app.router.add_post(f"{API_PREFIX}/state", handle_state_write)


__all__ = ("API_PREFIX", "APP_NAME", "handle_state_write", "register_routes")
