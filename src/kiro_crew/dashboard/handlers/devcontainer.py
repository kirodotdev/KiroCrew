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
    get_manager,
    grant_trust,
    revoke_trust,
)
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)


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


def _slot_project_roots(state: object) -> set[str]:
    """Realpaths of every chat slot's project directory.

    Reads the private ``_slots`` dict, which is where DashboardState actually
    keeps them: there is no ``chat_slots`` attribute and no ``__getattr__``, so
    naming one yields {} and fails every admission check closed (all endpoints
    400, even for a live slot's own project). Same accessor as
    ``worktree.py:_allowed_repo_roots``.
    """
    roots: set[str] = set()
    slots = getattr(state, "_slots", None) or {}
    for slot in list(getattr(slots, "values", list)()):
        project = getattr(slot, "project", None)
        if isinstance(project, str) and project.strip():
            try:
                roots.add(os.path.realpath(project.strip()))
            except OSError:
                continue
    return roots


async def _resolve_project(request: web.Request, raw: object) -> str | None:
    """Validate a caller-supplied project path against live slot projects."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    probe = await asyncio.to_thread(os.path.realpath, raw.strip())
    roots = await asyncio.to_thread(_slot_project_roots, request.app.get("state"))
    return probe if probe in roots else None


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
    project = await _resolve_project(request, request.query.get("project"))
    if project is None:
        return web.json_response(
            {"error": "unknown project", "code": "unknown_project"}, status=400
        )
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
    project = await _resolve_project(request, request.query.get("project"))
    if project is None:
        return web.json_response(
            {"error": "unknown project", "code": "unknown_project"}, status=400
        )
    try:
        preview = await asyncio.to_thread(config_preview, project)
    except DevcontainerError as exc:
        return web.json_response({"error": str(exc), "code": "no_devcontainer_config"}, status=404)
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
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    project = await _resolve_project(request, (body or {}).get("project"))
    if project is None:
        return web.json_response(
            {"error": "unknown project", "code": "unknown_project"}, status=400
        )
    reviewed = (body or {}).get("digest")
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
        return web.json_response({"error": str(exc), "code": "no_devcontainer_config"}, status=404)
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
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    project = await _resolve_project(request, (body or {}).get("project"))
    if project is None:
        return web.json_response(
            {"error": "unknown project", "code": "unknown_project"}, status=400
        )
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
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    project = await _resolve_project(request, (body or {}).get("project"))
    if project is None:
        return web.json_response(
            {"error": "unknown project", "code": "unknown_project"}, status=400
        )
    try:
        info = await get_manager().up(project, rebuild=True)
    except DevcontainerError as exc:
        # Covers DevcontainerNotTrusted too: rebuild of an untrusted config
        # must fail, not silently re-grant.
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
