"""Settings > Developer: add or remove the dashboard and debug MCP sets on the default agent.

The default agent's spec (``kirocrew.json``) is the only state. There is no config
key and no ownership record: the panel shows what the spec on disk mounts, and the
two actions edit that spec directly.

``GET  /api/agent/default-mcp-grants`` -- per server, whether the spec on disk
mounts it (an ``mcpServers`` entry plus an ``@server`` ref in ``tools``), plus
``agent.session_control`` so the panel can say when the dashboard set's session
tools refuse calls.

``POST /api/agent/default-mcp-grants`` ``{"action": "add" | "remove"}`` -- owner
dashboard only. Runs a fixed list of steps and reports each one:

* ``write_spec`` -- under the agent-spec file lock, write the change. ``add`` sets
  both entries to the managed invocation with no ``autoApprove``, adds the refs to
  ``tools`` and drops every ``allowedTools`` ref naming either server, because the
  mount must never pre-approve a call. ``remove`` deletes both entries (including
  one added by hand) and every ``tools``/``allowedTools`` ref naming them.
* ``rebuild`` -- :func:`kiro_crew.agent.rebuild_agent_config_reporting`, so every
  derived spec (the worker, which never gets either set) is regenerated from it.
* ``verify`` -- re-read the spec on disk and check the intended end state.

Every step is idempotent: running an action twice leaves the spec as the first run
left it. The response names the first step that failed. New sessions load the
change; a running session keeps the tool list it started with.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiohttp import web

from kiro_crew.dashboard.chat_utils import run_to_completion
from kiro_crew.dashboard.handlers._shared import (
    read_bounded_json,
    require_owner_dashboard_request,
)
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

_OPERATION = "default_mcp_grant"

#: The two opt-in servers this route manages, and the ``mcp-*`` subcommand each
#: runs. A closed list: no request can name any other server.
SERVERS: dict[str, str] = {
    "kirocrew-dashboard": "mcp-dashboard",
    "kirocrew-debug": "mcp-debug",
}

ACTIONS = ("add", "remove")


class StepFailed(Exception):
    """One step of an action failed; ``step`` names it for the response."""

    def __init__(self, step: str, detail: str) -> None:
        super().__init__(detail)
        self.step = step
        self.detail = detail


def _names_server(ref: object, server: str) -> bool:
    return isinstance(ref, str) and (ref == f"@{server}" or ref.startswith(f"@{server}/"))


def _spec_path():  # type: ignore[no-untyped-def]
    from kiro_crew.agent import AGENT_FILENAME, kiro_agents_dir_path  # circular import

    return kiro_agents_dir_path() / AGENT_FILENAME


def _read_spec() -> dict[str, Any] | None:
    from kiro_crew.agent import _read_spec_capped  # circular import

    return _read_spec_capped(_spec_path())


def _parts(spec: dict[str, Any] | None) -> tuple[dict[str, Any], list[Any], list[Any]]:
    """The spec's ``mcpServers``, ``tools`` and ``allowedTools``, each defaulted."""
    spec = spec or {}
    raw_mcp, raw_tools, raw_allowed = (
        spec.get("mcpServers"),
        spec.get("tools"),
        spec.get("allowedTools"),
    )
    mcp: dict[str, Any] = raw_mcp if isinstance(raw_mcp, dict) else {}
    tools: list[Any] = raw_tools if isinstance(raw_tools, list) else []
    allowed: list[Any] = raw_allowed if isinstance(raw_allowed, list) else []
    return mcp, tools, allowed


def mounted_state(spec: dict[str, Any] | None) -> dict[str, bool]:
    """Per server: does *spec* mount it (entry present and an ``@server`` ref)."""
    mcp, tools, _allowed = _parts(spec)
    return {name: isinstance(mcp.get(name), dict) and f"@{name}" in tools for name in SERVERS}


def apply_to_spec(spec: dict[str, Any], action: str) -> list[str]:
    """Edit *spec* in place to the end state *action* names. Idempotent.

    Returns the approvals the edit dropped (``allowedTools`` refs, and
    ``<server>.autoApprove`` for an entry that carried one), so the caller can
    record them.
    """
    dropped: list[str] = []
    from kiro_crew.agent import _managed_opt_in_entry  # circular import

    mcp = spec.get("mcpServers")
    if not isinstance(mcp, dict):
        mcp = spec["mcpServers"] = {}
    tools = spec.get("tools")
    if not isinstance(tools, list):
        tools = spec["tools"] = []
    allowed = spec.get("allowedTools")
    for name, subcommand in SERVERS.items():
        if action == "add":
            # Keep the operator's own fields (``timeout``, ``disabledTools``, ...)
            # on an entry that already exists, re-pin the managed invocation, and
            # never carry an ``autoApprove``.
            existing = mcp.get(name)
            if isinstance(existing, dict) and "autoApprove" in existing:
                dropped.append(f"{name}.autoApprove")
            entry = (
                {k: v for k, v in existing.items() if k != "autoApprove"}
                if isinstance(existing, dict)
                else {}
            )
            entry.update(_managed_opt_in_entry(subcommand))
            mcp[name] = entry
            if f"@{name}" not in tools:
                tools.append(f"@{name}")
        else:
            mcp.pop(name, None)
            tools[:] = [r for r in tools if not _names_server(r, name)]
        # Both actions: the set is never pre-approved, so an allowedTools ref to
        # it is dropped when adding (it would go live) and when removing (it would
        # dangle).
        if isinstance(allowed, list):
            dropped.extend(r for r in allowed if _names_server(r, name))
            allowed[:] = [r for r in allowed if not _names_server(r, name)]
    return dropped


def _end_state_holds(spec: dict[str, Any] | None, action: str) -> bool:
    mcp, tools, allowed = _parts(spec)
    for name in SERVERS:
        if any(_names_server(r, name) for r in allowed):
            return False
        if action == "add":
            entry = mcp.get(name)
            if not isinstance(entry, dict) or "autoApprove" in entry:
                return False
            if f"@{name}" not in tools:
                return False
        elif name in mcp or any(_names_server(r, name) for r in tools):
            return False
    return True


def run_action(action: str, caller: str = "dashboard") -> list[str]:
    """Run *action*'s steps (BLOCKING). Returns the completed steps; raises StepFailed."""
    from kiro_crew.agent import rebuild_agent_config_reporting  # circular import
    from kiro_crew.apps.bridges import _mcp_lock  # circular import
    from kiro_crew.config.loader import write_config_atomically  # circular import

    done: list[str] = []
    path = _spec_path()
    try:
        with _mcp_lock(target=path):
            spec = _read_spec()
            if spec is None:
                raise StepFailed("write_spec", "the default agent spec could not be read")
            dropped = apply_to_spec(spec, action)
            write_config_atomically(path, spec)
    except StepFailed:
        raise
    except Exception as exc:  # noqa: BLE001 -- reported as the failed step
        logger.warning("default-mcp-grants write_spec failed", exc_info=True)
        raise StepFailed("write_spec", str(exc) or type(exc).__name__) from exc
    done.append("write_spec")
    if dropped:
        # The operator wrote these approvals; record what the action took out.
        sel().log_api_access(
            caller=caller,
            operation=_OPERATION,
            outcome="ok",
            source="dashboard",
            resources=f"{action} dropped approvals: {', '.join(dropped)}",
        )

    try:
        _path, wrote = rebuild_agent_config_reporting()
    except Exception as exc:  # noqa: BLE001 -- reported as the failed step
        logger.warning("default-mcp-grants rebuild failed", exc_info=True)
        raise StepFailed("rebuild", str(exc) or type(exc).__name__) from exc
    if not wrote:
        raise StepFailed("rebuild", "the agent spec rebuild was refused and wrote nothing")
    done.append("rebuild")

    if not _end_state_holds(_read_spec(), action):
        raise StepFailed("verify", "the agent spec on disk does not show the change")
    done.append("verify")
    return done


def _status_payload() -> dict[str, Any]:
    """The GET body (BLOCKING)."""
    from kiro_crew.config.loader import KiroCrewConfig  # circular import

    try:
        session_control = bool(KiroCrewConfig.load().agent.session_control)
    except Exception:  # noqa: BLE001 -- a status read degrades, it does not raise
        logger.debug("config load failed for default-mcp-grants status", exc_info=True)
        session_control = True
    state = mounted_state(_read_spec())
    return {
        "servers": [{"name": name, "mounted": state[name]} for name in SERVERS],
        "session_control": session_control,
    }


async def api_default_mcp_grants_get(request: web.Request) -> web.Response:
    """GET /api/agent/default-mcp-grants."""
    return web.json_response(await asyncio.to_thread(_status_payload))


async def api_default_mcp_grants_set(request: web.Request) -> web.Response:
    """POST /api/agent/default-mcp-grants -- add or remove both sets."""
    denied = await require_owner_dashboard_request(request, _OPERATION)
    if denied is not None:
        return denied

    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    action = body.get("action")
    if action not in ACTIONS:
        return web.json_response(
            {"error": "action must be 'add' or 'remove'", "code": "action_invalid"},
            status=400,
        )

    from kiro_crew.dashboard.handlers.mcp import _get_apply_lock  # circular import

    caller = request.get("user", "dashboard")

    async def _run() -> web.Response:
        # Under the MCP apply lock, so this rebuild cannot interleave with
        # /api/mcp/apply's.
        async with _get_apply_lock():
            try:
                steps = await asyncio.to_thread(run_action, action, caller)
            except StepFailed as exc:
                sel().log_api_access(
                    caller=caller,
                    operation=_OPERATION,
                    outcome="error",
                    source="dashboard",
                    resources=f"{action} failed at {exc.step}",
                )
                payload = await asyncio.to_thread(_status_payload)
                return web.json_response(
                    {
                        "error": f"{action} failed at step {exc.step}: {exc.detail}",
                        "code": "step_failed",
                        "failed_step": exc.step,
                        "servers": payload["servers"],
                        "session_control": payload["session_control"],
                    },
                    status=500,
                )
        sel().log_api_access(
            caller=caller,
            operation=_OPERATION,
            outcome="ok",
            source="dashboard",
            resources=f"{action} {', '.join(SERVERS)}",
        )
        payload = await asyncio.to_thread(_status_payload)
        return web.json_response({"ok": True, "steps": steps, **payload})

    return await run_to_completion(_run())
