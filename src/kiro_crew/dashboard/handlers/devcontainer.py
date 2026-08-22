"""Dashboard endpoints for Dev Container support (VS Code parity).

Routes (registered in server.py):
  GET    /api/devcontainer/status?project=...  — config presence, trust, container state
  GET    /api/devcontainer/config?project=...  — raw config + digest for the trust prompt
  POST   /api/devcontainer/trust               — {project}: grant trust for the CURRENT config
  DELETE /api/devcontainer/trust               — {project}: revoke
  POST   /api/devcontainer/rebuild             — {project}: rebuild the container

Input trust model: `project` is only accepted when it realpath-matches an
existing chat slot's project (the same barrier idea as worktree.py's
_allowed_repo_roots) — the trust decision is only meaningful for a directory
a session is actually scoped to, and this prevents an arbitrary caller from
probing or trusting paths sessions never touch.

Trust mutations are dashboard-caller-only and SEL-audited: granting trust
authorizes arbitrary container builds (image pulls, lifecycle hooks) for that
project, which is exactly the decision VS Code gates behind Workspace Trust.
"""

from __future__ import annotations

import asyncio
import logging
import os

from aiohttp import web

from kiro_crew.dashboard.chat_handlers import deny_non_dashboard_caller
from kiro_crew.devcontainer import (
    DevcontainerConfigChanged,
    DevcontainerError,
    config_preview,
    devcontainers_enabled,
    find_devcontainer_config,
    get_manager,
    grant_trust,
    revoke_trust,
)
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)


def _object_body(body: object) -> dict | None:
    """The request body as a mapping, or None when the JSON is not an object.

    ``request.json()`` succeeds for any valid JSON, so a well-formed non-object
    (``[1]``, ``"x"``, ``5``, ``true``) reaches the handlers as a list, str, int
    or bool. ``body.get(...)`` then raises AttributeError and the owner
    gets a 500 for what is really a malformed request. Note that the falsy
    non-objects (``[]``, ``""``, ``0``, ``null``) take the ``or {}`` branch and
    never crashed -- only the truthy ones do, which is why the guard tests the
    TYPE rather than the truthiness.
    """
    return body if isinstance(body, dict) else None


def _deny_non_owner(request: web.Request, operation: str) -> web.Response | None:
    """403 unless this is the dashboard owner's own request, else None.

    Stricter than ``deny_non_dashboard_caller`` in exactly one way, and the
    difference is the whole point: that helper permits a request carrying
    ``internal_auth`` (a valid ``X-Internal-Secret`` from loopback), because it
    also guards ``suggest_followup``, where the agent legitimately raises a
    card. That exemption is the path every MCP call arrives on, so honoring it
    here would let the agent preview a digest and grant trust to its OWN
    devcontainer configuration -- self-approving the human decision this entire
    feature exists to require, and turning the trust prompt into a formality.

    Nothing inside the gateway reaches these routes: the session and runtime
    paths call ``kiro_crew.devcontainer`` directly, and the only HTTP client is
    the dashboard's own trust card. So the agent has no legitimate use for them,
    and refusing ``internal_auth`` costs nothing.
    """
    if request.get("internal_auth") is True:
        try:
            sel().log_api_access(
                caller=str(request.get("user") or "internal"),
                operation=operation,
                outcome="denied",
                source="dashboard",
                error="internal callers cannot approve their own devcontainer",
            )
        except Exception:  # pragma: no cover - audit is best-effort
            logger.debug("SEL audit failed for %s denial", operation, exc_info=True)
        return web.json_response(
            {"error": "forbidden", "code": "internal_caller_denied"}, status=403
        )
    return deny_non_dashboard_caller(request, operation)


def _slot_project_strings(state: object) -> list[str]:
    """Every chat slot's project string. **Event-loop only, and does no I/O.**

    Reads the private ``_slots`` dict, which is where DashboardState actually
    keeps them: there is no ``chat_slots`` attribute and no ``__getattr__``, so
    naming one yields {} and fails every admission check closed (all endpoints
    400, even for a live slot's own project). Same accessor as
    ``worktree.py:_allowed_repo_roots``.

    Split from the resolution below so this half runs where the dict's owner runs.
    The loop creates and deletes slots, so reading the dict from a worker thread
    reads mutable state concurrently with its only writer -- and the part that
    actually needs a thread (realpath, which can block on a network-backed home)
    does not need the dict at all, only the strings.
    """
    slots = getattr(state, "_slots", None) or {}
    out: list[str] = []
    for slot in list(getattr(slots, "values", list)()):
        project = getattr(slot, "project", None)
        if isinstance(project, str) and project.strip():
            out.append(project.strip())
    return out


def _realpath_set(paths: list[str]) -> set[str]:
    """Resolve *paths* to realpaths. **Worker-thread half** -- this is the I/O."""
    roots: set[str] = set()
    for path in paths:
        try:
            roots.add(os.path.realpath(path))
        except OSError:
            continue
    return roots


async def _resolve_project(request: web.Request, raw: object) -> str | None:
    """Validate a caller-supplied project path against live slot projects."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    probe = await asyncio.to_thread(os.path.realpath, raw.strip())
    # Snapshot on the loop, resolve off it: the strings are copied where their
    # owner runs, and only the blocking realpath goes to a worker.
    slot_projects = _slot_project_strings(request.app.get("state"))
    roots = await asyncio.to_thread(_realpath_set, slot_projects)
    return probe if probe in roots else None


def _deny_unknown_project(request: web.Request, operation: str, probe: object) -> web.Response:
    """Audit and return the 400 for a project no live chat slot owns.

    Admission is an access decision -- the caller named a project it is not
    entitled to act on -- so it belongs in the audit log next to the trust grants
    and refusals. Every endpoint returned this 400 silently.

    Shared rather than pasted five times: the same gap has appeared once per
    endpoint, and a helper is the only version of this fix that a sixth endpoint
    inherits instead of forgetting.

    The probe is recorded truncated. It is caller-supplied and unresolved, so it is
    the useful thing to see afterwards, and it is not trusted enough to log whole.
    """
    caller = str(request.get("user") or "dashboard")
    sel().log_api_access(
        caller=caller,
        operation=operation,
        outcome="denied",
        resources=f"project={str(probe)[:200]!r}",
        error="unknown project: not the project of any live chat slot",
    )
    return web.json_response({"error": "unknown project", "code": "unknown_project"}, status=400)


async def api_devcontainer_status(request: web.Request) -> web.Response:
    """GET /api/devcontainer/status?project=<path>

    Owner-only like the rest of the surface. The response reports whether a
    project's configuration is trusted and which container backs it, so leaving
    it open would let a caller that is refused everywhere else still read the
    state of the trust decision.
    """
    denied = _deny_non_owner(request, "devcontainer_status")
    if denied is not None:
        return denied
    probe = request.query.get("project")
    project = await _resolve_project(request, probe)
    if project is None:
        return _deny_unknown_project(request, "devcontainer_status", probe)
    status = await get_manager().status(project)
    return web.json_response(status)


async def api_devcontainer_config(request: web.Request) -> web.Response:
    """GET /api/devcontainer/config?project=<path> — for the trust prompt.

    Owner-only: the response carries raw file bytes from the project tree, which
    no app or internal caller has business reading through this surface. Pairs
    with the O_NOFOLLOW + containment + sensitive-path screens in
    _read_config_bytes, which bound WHICH bytes can be returned at all.
    """
    denied = _deny_non_owner(request, "devcontainer_config")
    if denied is not None:
        return denied
    probe = request.query.get("project")
    project = await _resolve_project(request, probe)
    if project is None:
        return _deny_unknown_project(request, "devcontainer_config", probe)
    try:
        preview = await asyncio.to_thread(config_preview, project)
    except DevcontainerError as exc:
        # The same distinction the trust endpoint draws: "no_devcontainer_config" is
        # true only when there is no config, and one that WAS screened and refused is
        # the opposite of absent. No caller branches on this today -- the trust card
        # collapses every failure to null -- so this is consistency between the two
        # sibling endpoints rather than a behaviour fix.
        has_cfg = bool(await asyncio.to_thread(find_devcontainer_config, project))
        return web.json_response(
            {
                "error": str(exc),
                "code": "devcontainer_config_refused" if has_cfg else "no_devcontainer_config",
            },
            status=404,
        )
    return web.json_response(preview)


async def api_devcontainer_trust(request: web.Request) -> web.Response:
    """POST /api/devcontainer/trust {project, digest} — grant for current config.

    ``digest`` is the fingerprint the dashboard showed in the trust prompt, and
    it is REQUIRED. Granting against whatever happens to be on disk would let
    the agent rewrite ``.devcontainer/`` between the preview and the click and
    get its own configuration authorized. A mismatch returns 409 so the UI can
    re-read and re-prompt with the new bytes.
    """
    caller = str(request.get("user") or "dashboard")
    denied = _deny_non_owner(request, "devcontainer_trust")
    if denied is not None:
        return denied
    try:
        raw_body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    body = _object_body(raw_body)
    if body is None:
        return web.json_response(
            {"error": "body must be a JSON object", "code": "invalid_json"}, status=400
        )
    probe = body.get("project")
    project = await _resolve_project(request, probe)
    if project is None:
        return _deny_unknown_project(request, "devcontainer_trust.grant", probe)
    reviewed = body.get("digest")
    if not isinstance(reviewed, str) or not reviewed.strip():
        return web.json_response(
            {
                "error": "digest of the reviewed configuration is required",
                "code": "digest_required",
            },
            status=400,
        )
    try:
        digest = await asyncio.to_thread(grant_trust, project, reviewed.strip())
    except DevcontainerConfigChanged as exc:
        sel().log_api_access(
            caller=caller,
            operation="devcontainer_trust.grant",
            outcome="denied",
            resources=f"project={project}",
            error="config changed between preview and grant",
        )
        return web.json_response(
            {"error": str(exc), "code": "devcontainer_config_changed"}, status=409
        )
    except DevcontainerError as exc:
        # Audited for the same reason the branch above is: refusing to grant trust
        # is a permission decision. This is the branch EVERY configuration refusal
        # takes -- features, privileged modes, a project directory the sandbox
        # withholds, a tree over the ceilings -- so returning 404 and recording
        # nothing left the denials the audit log most needs invisible.
        #
        # The code is resolved rather than assumed: "no_devcontainer_config" is true
        # only when there is no config, and reporting it for a config that WAS
        # screened and refused tells a client the opposite of what happened. The
        # lookup sits on an error path, so the normal case pays nothing for it.
        has_cfg = bool(await asyncio.to_thread(find_devcontainer_config, project))
        sel().log_api_access(
            caller=caller,
            operation="devcontainer_trust.grant",
            outcome="denied",
            resources=f"project={project}",
            error=f"{type(exc).__name__}: {exc}",
        )
        return web.json_response(
            {
                "error": str(exc),
                "code": "devcontainer_trust_refused" if has_cfg else "no_devcontainer_config",
            },
            status=404,
        )
    sel().log_api_access(
        caller=caller,
        operation="devcontainer_trust.grant",
        outcome="success",
        resources=f"project={project} digest={digest[:12]}",
    )
    return web.json_response({"trusted": True, "digest": digest})


async def api_devcontainer_untrust(request: web.Request) -> web.Response:
    """DELETE /api/devcontainer/trust {project}"""
    caller = str(request.get("user") or "dashboard")
    denied = _deny_non_owner(request, "devcontainer_trust")
    if denied is not None:
        return denied
    try:
        raw_body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    body = _object_body(raw_body)
    if body is None:
        return web.json_response(
            {"error": "body must be a JSON object", "code": "invalid_json"}, status=400
        )
    probe = body.get("project")
    project = await _resolve_project(request, probe)
    if project is None:
        return _deny_unknown_project(request, "devcontainer_trust.revoke", probe)
    removed = await asyncio.to_thread(revoke_trust, project)
    sel().log_api_access(
        caller=caller,
        operation="devcontainer_trust.revoke",
        outcome="success" if removed else "noop",
        resources=f"project={project}",
    )
    return web.json_response({"trusted": False, "removed": removed})


async def api_devcontainer_rebuild(request: web.Request) -> web.Response:
    """POST /api/devcontainer/rebuild {project} — trust-gated full rebuild."""
    caller = str(request.get("user") or "dashboard")
    denied = _deny_non_owner(request, "devcontainer_rebuild")
    if denied is not None:
        return denied
    try:
        raw_body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    body = _object_body(raw_body)
    if body is None:
        return web.json_response(
            {"error": "body must be a JSON object", "code": "invalid_json"}, status=400
        )
    probe = body.get("project")
    project = await _resolve_project(request, probe)
    if project is None:
        return _deny_unknown_project(request, "devcontainer_rebuild", probe)
    # The opt-in is enforced here, not only where a session resolves its host.
    # This is the one request that BUILDS AND STARTS a container, and a trust
    # grant outlives the mode being turned off -- so without this check an owner
    # could start a container while `agent.devcontainer` is off, which is the
    # state an operator sets precisely to stop that from happening. Read
    # off-loop: it reads config from disk.
    if not await asyncio.to_thread(devcontainers_enabled):
        # Audited for the same reason as the trust refusal below: this is a
        # permission decision, so it belongs in the log rather than only in the
        # caller's response.
        sel().log_api_access(
            caller=caller,
            operation="devcontainer_rebuild",
            outcome="denied",
            resources=f"project={project} reason=devcontainer_disabled",
        )
        return web.json_response(
            {"error": "Dev Containers are disabled", "code": "devcontainer_disabled"},
            status=409,
        )
    try:
        info = await get_manager().up(project, rebuild=True)
    except DevcontainerError as exc:
        # Covers DevcontainerNotTrusted too: rebuild of an untrusted config
        # must fail, not silently re-grant.
        #
        # Audited as a DENIAL, not merely returned. A revoked or changed grant
        # refusing a rebuild is a permission decision, and the audit log is where
        # those are answerable after the fact -- a 409 the caller sees and nothing
        # else records leaves no trace that the trust gate did its job.
        sel().log_api_access(
            caller=caller,
            operation="devcontainer_rebuild",
            outcome="denied",
            resources=f"project={project} reason={type(exc).__name__}",
        )
        return web.json_response({"error": str(exc), "code": "devcontainer_up_failed"}, status=409)
    sel().log_api_access(
        caller=caller,
        operation="devcontainer_rebuild",
        outcome="success",
        resources=f"project={project} container={info.container_id[:12]}",
    )
    return web.json_response(
        {
            "container_id": info.container_id,
            "remote_workspace_folder": info.remote_workspace_folder,
        }
    )
