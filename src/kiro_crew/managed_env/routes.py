"""The dashboard's ``/api/managed-environments/*`` routes.

Seven routes, each behind the core's owner check (a Shared Crew teammate is refused there:
``teammate_access.is_owner_only_for_teammates`` names this prefix), then the lane switch
(the ``crew_client`` preview, read on every request): off, every route answers 404.

* ``GET  /api/managed-environments/list``: every environment of the signed-in user,
  ``own`` true for the ones this Crew created (only those take an action), and a deleted
  own id missing from the List as ``GONE``;
* ``POST /api/managed-environments/{id}/stop`` / ``resume`` / ``delete``: own ids only.
  Delete sends DeleteSandbox and answers ``deleting``; the lane removes no Remote Crew row;
* ``POST /api/managed-environments/{id}/ssh-session``: one fresh session for an own
  environment, with the Region the Session Manager plugin starts in. Its answer is a
  secret: it is never logged. An environment the last List read as not RUNNING is read
  once more first, and refused with no session while it is still not RUNNING.
* ``GET  /api/managed-environments/ssh-config``: whether ``~/.ssh/config`` holds the lane's
  current ``crew-sbx-*`` block, and the block's text, so the dashboard shows what a write
  adds before anyone agrees. It writes nothing;
* ``POST /api/managed-environments/ssh-config``: write that block into ``~/.ssh/config``
  with this gateway's own Python (only Crew's marked block, atomic, 0600 for a new file),
  and answer the same shape.

The routes sit on the dashboard's own listener, which serves loopback unless the
operator configured otherwise; they add no listener.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Callable, Optional

from aiohttp import web

from kiro_crew.managed_env import settings as lane_settings
from kiro_crew.managed_env.client import GuardRefused, SandboxError
from kiro_crew.managed_env.engine import SandboxEngineError, SandboxLaunchEngine
from kiro_crew.managed_env.remote import ssh_host_for
from kiro_crew.managed_env.settings import SettingsMissing

logger = logging.getLogger(__name__)

PREFIX = "/api/managed-environments"
ACTIONS = ("stop", "resume", "delete")
_ERROR_MAX = 300


def _json(status: int, *, code: str, error: str) -> web.Response:
    return web.json_response({"code": code, "error": error}, status=status)


def _refusal(exc: Exception) -> web.Response:
    """A refused or failed call as one plain line and a stable ``code``. No token."""
    if isinstance(exc, SettingsMissing):
        return _json(409, code="not_configured", error=str(exc))
    if isinstance(exc, GuardRefused):
        return _json(409, code=exc.reason, error=exc.message[:_ERROR_MAX])
    if isinstance(exc, SandboxError):
        if exc.kind == "not_configured":
            return _json(409, code="not_configured", error=exc.message[:_ERROR_MAX])
        if exc.kind == "no_token":
            from kiro_crew.managed_env.core_signin import NOT_SIGNED_IN

            return _json(409, code="not_signed_in", error=NOT_SIGNED_IN)
        if exc.status == 409:
            return _json(409, code="not_ready", error=exc.message[:_ERROR_MAX])
        if exc.status == 404:
            return _json(404, code="gone", error="The environment is gone.")
        return _json(502, code="service", error=str(exc)[:_ERROR_MAX])
    if isinstance(exc, SandboxEngineError):
        return _json(502, code=exc.code, error=str(exc)[:_ERROR_MAX])
    raise exc


class SandboxRoutes:
    """The handlers. ``engine`` and ``lane_enabled`` are for tests."""

    def __init__(
        self,
        engine: Optional[Callable[[], SandboxLaunchEngine]] = None,
        *,
        lane_enabled: Optional[Callable[[], bool]] = None,
    ) -> None:
        self._engine = engine or SandboxLaunchEngine
        self._lane_enabled = lane_enabled or (lambda: lane_settings.lane_enabled())
        #: Ids the last List read as anything but RUNNING: no ssh session is asked for them.
        self._not_running: set[str] = set()
        self._lock = threading.Lock()

    async def _gate(self, request: web.Request, operation: str) -> Optional[web.Response]:
        """The core's owner check first (its answer is the route's answer); then off = 404."""
        from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

        refused = await require_owner_dashboard_request(request, operation)
        if refused is not None:
            return refused
        if not self._lane_enabled():
            return _json(404, code="not_found", error="not found")
        return None

    # -- list ------------------------------------------------------------------------

    def list_entries(self) -> dict:
        """The signed-in account's List answer, plus each deleted own id missing from it
        as ``GONE``. It never removes Remote Crew rows."""
        engine = self._engine()
        client = engine.client
        entries = client.list_sandboxes()
        # A deleted ledger id stays own while listed, so it can read "being deleted".
        own = client._guard.own_ids(deleted=True)
        deleted = own - client._guard.own_ids()
        out = []
        not_running = set()
        for e in entries:
            sid = e.get("execEnvironmentId")
            if not isinstance(sid, str) or not sid:
                continue
            status = str(e.get("status") or "")
            if status != "RUNNING":
                not_running.add(sid)
            item = {"sandbox_id": sid, "status": status, "own": sid in own}
            if isinstance(e.get("displayName"), str):
                item["display_name"] = e["displayName"]
            if sid in own:
                item["ssh_host"] = ssh_host_for(sid)
            out.append(item)
            deleted.discard(sid)
        # No ssh_host and no action: the person removes its crew with the core Remove.
        out += [{"sandbox_id": sid, "status": "GONE", "own": True} for sid in sorted(deleted)]
        with self._lock:
            self._not_running = not_running
        return {"sandboxes": out}

    async def list_handler(self, request: web.Request) -> web.Response:
        refused = await self._gate(request, "managed environments list")
        if refused is not None:
            return refused
        try:
            body = await asyncio.to_thread(self.list_entries)
            return web.json_response(body)
        except Exception as exc:  # noqa: BLE001 - _refusal re-raises what it does not map
            return _refusal(exc)

    # -- stop / resume / delete ------------------------------------------------------

    def act_sync(self, sid: str, action: str) -> dict:
        engine = self._engine()
        if action == "delete":
            # By ledger id, again on every press (a 404 reads as gone). It removes no row:
            # the person removes a gone environment's crew with the core Remove.
            engine.client.delete_sandbox(sid)
            return {"status": "deleting"}
        call = engine.client.stop_sandbox if action == "stop" else engine.client.resume_sandbox
        out = call(sid)
        with self._lock:
            if action == "stop":
                self._not_running.add(sid)
            else:
                # Starting: the next ssh session reads its state again rather than refusing.
                self._not_running.discard(sid)
        return {"status": str(out.get("status") or "")}

    async def act(self, request: web.Request) -> web.Response:
        action = request.match_info.get("action", "")
        sid = request.match_info.get("sandbox_id", "")
        refused = await self._gate(request, f"managed environment {action}")
        if refused is not None:
            return refused
        if action not in ACTIONS:
            return _json(404, code="not_found", error="not found")
        try:
            out = await asyncio.to_thread(self.act_sync, sid, action)
            return web.json_response(out)
        except Exception as exc:  # noqa: BLE001
            return _refusal(exc)

    # -- ssh-session -----------------------------------------------------------------

    def ssh_session_sync(self, sid: str) -> dict:
        engine = self._engine()
        region = engine._settings().ssh_region_or_raise()
        with self._lock:
            marked = sid in self._not_running
        if marked:
            # One read, not a hard gate: the mark may be older than the environment's start.
            if str(engine.client.get_sandbox(sid).get("status")) != "RUNNING":
                raise SandboxError(
                    "StartSshSession", "not_ready", 409, "The environment is not running."
                )
            with self._lock:
                self._not_running.discard(sid)
        session = engine.client.start_ssh_session(sid)
        return {
            "ssmSessionId": session.session_id,
            "streamUrl": session.stream_url,
            "tokenValue": session.token_value,
            "region": region,
        }

    async def ssh_session(self, request: web.Request) -> web.Response:
        sid = request.match_info.get("sandbox_id", "")
        refused = await self._gate(request, "managed environment ssh session")
        if refused is not None:
            return refused
        try:
            body = await asyncio.to_thread(self.ssh_session_sync, sid)
        except Exception as exc:  # noqa: BLE001
            return _refusal(exc)
        return web.json_response(body, headers={"Cache-Control": "no-store"})

    # -- ssh-config ------------------------------------------------------------------

    def ssh_config_sync(self, write: bool) -> dict:
        """The ssh block's state, written first when asked, with this install's own Python
        (``gateway_python``), so the ProxyCommand runs the install that serves this
        dashboard, a pipx one too."""
        from kiro_crew.managed_env import ssh_config

        path = ssh_config.default_config()
        python = ssh_config.gateway_python()
        if write:
            ssh_config.ensure_block(path, python)
        return {
            "present": ssh_config.block_current(path, python),
            "path": path,
            "block": ssh_config.render_block(python),
        }

    async def ssh_config_handler(self, request: web.Request) -> web.Response:
        refused = await self._gate(request, "managed environment ssh settings")
        if refused is not None:
            return refused
        from kiro_crew.managed_env.ssh_config import SshConfigError

        try:
            write = request.method == "POST"
            return web.json_response(await asyncio.to_thread(self.ssh_config_sync, write))
        except (SshConfigError, OSError) as exc:
            return _json(409, code="ssh_config_unwritable", error=str(exc)[:_ERROR_MAX])


def register_sandbox_routes(app: web.Application, handlers: SandboxRoutes) -> None:
    app.router.add_get(f"{PREFIX}/list", handlers.list_handler)
    app.router.add_get(f"{PREFIX}/ssh-config", handlers.ssh_config_handler)
    app.router.add_post(f"{PREFIX}/ssh-config", handlers.ssh_config_handler)
    app.router.add_post(f"{PREFIX}/{{sandbox_id}}/ssh-session", handlers.ssh_session)
    app.router.add_post(f"{PREFIX}/{{sandbox_id}}/{{action}}", handlers.act)
