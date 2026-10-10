"""Settings > Developer: add or remove the dashboard and debug MCP sets on the default agent.

The default agent's spec (``kirocrew.json``) is the only state. There is no config
key and no ownership record: the panel shows what the spec on disk mounts, and the
two actions edit that spec directly.

``GET  /api/agent/default-mcp-grants`` -- per server, whether the spec on disk
mounts it (an ``mcpServers`` entry plus an ``@server`` ref in ``tools``), plus
``agent.session_control`` so the panel can say when the dashboard set's session
tools refuse calls, plus the backends whose sessions never load the spec's MCP
servers.

``POST /api/agent/default-mcp-grants`` ``{"action": "add" | "remove"}`` -- owner
dashboard only. Runs a fixed list of steps and reports each one:

* ``write_spec`` -- under the agent-spec file lock, write the change. ``add`` pins
  both entries to the managed invocation and adds the refs to ``tools``. The
  managed emission itself pre-approves nothing; an ``autoApprove`` list or an
  ``allowedTools`` ref the owner already wrote is kept, as
  ``docs/request-for-change/rfc-owner-written-mcp-auto-approve.md`` decided.
  ``remove`` deletes both entries (including one added by hand) and every
  ``tools``/``allowedTools`` ref naming them.
* ``rebuild`` -- :func:`kiro_crew.agent.rebuild_agent_config_reporting`, so every
  derived spec (the worker, which never gets either set) is regenerated from it.
* ``verify`` -- re-read the spec on disk and check the intended end state.

Every step is idempotent: running an action twice leaves the spec as the first run
left it. The response names the first step that failed. New sessions on a backend
that loads the spec's MCP servers get the change; a running session keeps the tool
list it started with.
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
from kiro_crew.mcp_cleanup import _ref_server
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


def apply_to_spec(spec: dict[str, Any], action: str) -> None:
    """Edit *spec* in place to the end state *action* names. Idempotent."""
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
            # Keep every field the owner set on an existing entry (``timeout``,
            # ``disabledTools``, an ``autoApprove`` they wrote) and re-pin the
            # managed invocation, which carries no ``autoApprove`` of its own.
            existing = mcp.get(name)
            entry = dict(existing) if isinstance(existing, dict) else {}
            managed = _managed_opt_in_entry(subcommand)
            # Merge ``env`` rather than replace it: a relocated home makes the
            # managed map non-empty, and the owner's own variables stay.
            owner_env = entry.get("env")
            managed_env = managed.get("env")
            if isinstance(owner_env, dict) and isinstance(managed_env, dict):
                managed["env"] = {**owner_env, **managed_env}
            entry.update(managed)
            mcp[name] = entry
            if f"@{name}" not in tools:
                tools.append(f"@{name}")
        else:
            mcp.pop(name, None)
            tools[:] = [
                r
                for r in tools
                if not (isinstance(r, str) and r.startswith("@") and _ref_server(r) == name)
            ]
            if isinstance(allowed, list):
                allowed[:] = [
                    r
                    for r in allowed
                    if not (isinstance(r, str) and r.startswith("@") and _ref_server(r) == name)
                ]


def _end_state_holds(spec: dict[str, Any] | None, action: str) -> bool:
    mcp, tools, allowed = _parts(spec)
    for name in SERVERS:
        if action == "add":
            if not isinstance(mcp.get(name), dict) or f"@{name}" not in tools:
                return False
        elif (
            name in mcp
            or any(
                (isinstance(r, str) and r.startswith("@") and _ref_server(r) == name) for r in tools
            )
            or any(
                (isinstance(r, str) and r.startswith("@") and _ref_server(r) == name)
                for r in allowed
            )
        ):
            return False
    return True


def run_action(action: str) -> list[str]:
    """Run *action*'s steps (BLOCKING). Returns the completed steps; raises StepFailed."""
    from kiro_crew.agent import (  # circular import
        _decline_shared_agent_home,
        rebuild_agent_config_reporting,
    )
    from kiro_crew.apps.bridges import _mcp_lock  # circular import
    from kiro_crew.config.loader import write_config_atomically  # circular import

    done: list[str] = []
    path = _spec_path()
    try:
        with _mcp_lock(target=path):
            # The rebuild refuses a shared agent home this instance must not own;
            # refuse before writing too, or this instance's launcher and data home
            # land in the shared spec ahead of that refusal.
            if _decline_shared_agent_home(audit=False) is not None:
                raise StepFailed("write_spec", "this instance does not own the shared agent home")
            spec = _read_spec()
            if spec is None:
                raise StepFailed("write_spec", "the default agent spec could not be read")
            apply_to_spec(spec, action)
            write_config_atomically(path, spec)
    except StepFailed:
        raise
    except Exception as exc:  # noqa: BLE001 -- reported as the failed step
        logger.warning("default-mcp-grants write_spec failed", exc_info=True)
        raise StepFailed("write_spec", str(exc) or type(exc).__name__) from exc
    done.append("write_spec")

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
        "unreached_backends": unreached_backends(),
    }


def unreached_backends() -> list[str]:
    """Backends whose sessions never receive the spec's MCP servers.

    Read from the per-backend projection declarations, so the panel can say where
    a mounted set reaches no session instead of claiming every new session.
    """
    # Through the agent_sdk facade, not providers.mirrors: application code must
    # not reach the backend layer directly (scripts/check_agent_sdk_boundary.py).
    from kiro_crew.agent_sdk.backend_mcp_ability import ability_for
    from kiro_crew.agent_sdk.backends import selectable_backends

    gone = ("no-channel", "broker-only")
    return sorted(b for b in selectable_backends() if ability_for(b).projection in gone)


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
                await asyncio.to_thread(run_action, action)
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
                        # Named, not spread: a spread hides `code` from the
                        # error-code contract scan.
                        "servers": payload["servers"],
                        "session_control": payload["session_control"],
                        "unreached_backends": payload["unreached_backends"],
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
        return web.json_response(payload)

    return await run_to_completion(_run())
