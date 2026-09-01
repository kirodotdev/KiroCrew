"""Map ACP sessions to dashboard chat slots through the gateway HTTP API."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlparse

import aiohttp

from kiro_crew.acp.types import (
    CONFIG_CATEGORY_MODEL,
    CONFIG_OPTION_MODEL,
    CONFIG_OPTION_TYPE_SELECT,
    SESSION_MODE_DEFAULT_ID,
    SESSION_MODE_DEFAULT_NAME,
    STOP_REASON_CANCELLED,
    STOP_REASON_END_TURN,
)
from kiro_crew.acp_server.mcp_config import servers_to_acp_dicts
from kiro_crew.acp_server.mcp_supervisor import SessionMcpSupervisor
from kiro_crew.acp_server.server import (
    PromptHandler,
    PromptRequest,
    SelectorBusyError,
    SelectorState,
    SessionSink,
)
from kiro_crew.config import config_dir
from kiro_crew.dashboard.urls import is_loopback

if TYPE_CHECKING:
    from kiro_crew.acp_server.mcp_config import StdioMcpServer

logger = logging.getLogger(__name__)

# The slot name doubles as the ACP session id. Namespaced so an editor session
# is recognisable in the dashboard sidebar and never collides with a hand-made
# slot name.
SESSION_PREFIX = "acp"

# A turn can legitimately run for many minutes (long tool chains), so the SSE
# read has no total timeout; only connect/probe calls are bounded.
_PROBE_TIMEOUT = 10.0
_STREAM_TIMEOUT = aiohttp.ClientTimeout(total=None, sock_connect=_PROBE_TIMEOUT)

# Aggregate wall-clock ceiling for hosting/validating one session's whole MCP
# set. Bounds session/new setup even if a server hangs its handshake; the
# per-server initialize has its own shorter timeout inside the supervisor.
_MCP_SETUP_DEADLINE = 60.0


def build_mode_state(current_effort: str, effort_levels: list[str]) -> dict[str, Any] | None:
    """Build an ACP ``SessionModeState`` from a slot's effort + available levels.

    The provider-default effort (``""`` internally) is surfaced as the stable
    ``SESSION_MODE_DEFAULT_ID`` mode; the concrete levels are advertised verbatim
    from the runtime's own list (no invented fallbacks). ``currentModeId`` is the
    slot's current level, or the default id when the slot has no explicit level
    (or one no longer offered). Returns ``None`` when there are no concrete levels
    beyond the default — nothing meaningful to switch, so no mode selector.
    """
    levels = [lvl for lvl in effort_levels if isinstance(lvl, str) and lvl]
    if not levels:
        return None
    available: list[dict[str, Any]] = [
        {"id": SESSION_MODE_DEFAULT_ID, "name": SESSION_MODE_DEFAULT_NAME}
    ]
    for lvl in levels:
        available.append({"id": lvl, "name": lvl.capitalize()})
    current = current_effort if current_effort in levels else SESSION_MODE_DEFAULT_ID
    return {"currentModeId": current, "availableModes": available}


def build_model_config_option(
    current_model: str, models: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Build the model ``SessionConfigOption`` (select) from ``/api/models`` rows.

    Option values are the registry-backed canonical model ids — never arbitrary
    strings. The slot's current model resolves to itself when present in the set,
    else to the first option (the provider default; ``/api/models`` is
    default-first), which is also how an empty (auto/default) slot model is
    surfaced. Returns ``None`` when no models are available (degraded
    ``/api/models``), so no model selector is fabricated.
    """
    options: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in models:
        if not isinstance(row, dict):
            continue
        value = row.get("model_name")
        if not isinstance(value, str) or not value or value in seen:
            continue
        seen.add(value)
        opt: dict[str, Any] = {"value": value, "name": str(row.get("display_name") or value)}
        desc = row.get("description")
        if isinstance(desc, str) and desc:
            opt["description"] = desc
        options.append(opt)
    if not options:
        return None
    current_value = current_model if current_model in seen else options[0]["value"]
    return {
        "id": CONFIG_OPTION_MODEL,
        "name": "Model",
        "category": CONFIG_CATEGORY_MODEL,
        "type": CONFIG_OPTION_TYPE_SELECT,
        "currentValue": current_value,
        "options": options,
    }


def _client_safe_error(message: str) -> RuntimeError:
    """A RuntimeError whose message is safe to forward to the ACP client.

    The dispatch layer forwards any exception carrying ``acp_client_safe`` to the
    editor verbatim (see server._apply_session_mcp), so the message must name no
    secret — this one is a fixed phrase.
    """
    err = RuntimeError(message)
    err.acp_client_safe = True  # type: ignore[attr-defined]
    return err


def _project_paths_match(left: str, right: str) -> bool:
    """Return whether two project paths resolve to the same filesystem location.

    Keep the original path strings for storage and ACP responses. Canonicalization is
    comparison-only so logical paths such as ``/home/user`` remain user-facing while
    matching physical aliases such as ``/local/home/user``.
    """
    if left == right:
        return True
    if not left or not right:
        return False
    try:
        left_real = os.path.normcase(os.path.realpath(left))
        right_real = os.path.normcase(os.path.realpath(right))
    except (OSError, ValueError):
        return False
    return left_real == right_real


def _sanitize_locations(raw: Any) -> list[dict[str, Any]] | None:
    """Filter and normalize a locations list arriving from the gateway SSE stream.

    Discards anything that would violate the ACP ``ToolCallLocation`` schema:
    non-list values, non-dict entries, missing or non-string ``path``, and
    non-positive line numbers. Returns None (not [] ) when nothing survives,
    so ``send_tool_call`` drops the key entirely.
    """
    if not isinstance(raw, list):
        return None
    out: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        path = entry.get("path")
        if not isinstance(path, str) or not path:
            continue
        cleaned: dict[str, Any] = {"path": path}
        line = entry.get("line")
        if isinstance(line, int) and not isinstance(line, bool) and line > 0:
            cleaned["line"] = line
        out.append(cleaned)
    return out or None


def default_secret_path() -> Path:
    """Path to the gateway's owner-only internal IPC secret."""
    return config_dir() / ".local_secret"


def default_base_url() -> str:
    """Loopback dashboard URL, honoring the explicit KIROCREW_PORT override."""
    port = os.environ.get("KIROCREW_PORT", "5476")
    return f"http://127.0.0.1:{port}"


class HttpGatewayBackend:
    """A ``SessionBackend`` (+ ``PromptHandler``) proxying to the gateway.

    Construct, ``await open()``, hand ``prompt_handler()`` and this object to an
    ``AcpAgentServer``, and ``await close()`` on shutdown.
    """

    supports_load = True
    supports_list = True
    supports_resume = True

    def __init__(
        self,
        base_url: str,
        *,
        agent: str | None = None,
        secret_path: str | None = None,
        token: str | None = None,
        session_prefix: str = SESSION_PREFIX,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._agent = agent or ""
        self._secret_path = secret_path
        self._token = token
        self._prefix = session_prefix
        self._secret = ""
        self._session: Any = None  # aiohttp.ClientSession, created in open()
        # Owns the REAL, long-lived client MCP children: spawns each under
        # Kiro Crew's sandbox and exposes it to the provider through a trusted
        # per-session Unix-socket proxy. shutdown() in close() reaps them all.
        self._mcp = SessionMcpSupervisor()
        # Sessions on whose slot THIS adapter registered a non-empty MCP set.
        # Tracked so adapter EOF (close) clears only the slots it owns and never
        # a pre-existing slot it merely loaded/resumed.
        self._mcp_sessions: set[str] = set()
        self._tool_seq = 0

    # ── lifecycle ──

    async def open(self) -> None:
        """Load credentials and confirm the gateway is reachable."""
        if not self._token:
            host = urlparse(self._base_url).hostname or ""
            if not is_loopback(host):
                raise AcpGatewayError(
                    "non-loopback gateways require an explicit presigned token; "
                    "the local internal secret is never sent off-host"
                )
            path = Path(self._secret_path) if self._secret_path else default_secret_path()
            try:
                self._secret = path.read_text(encoding="utf-8").strip()
            except OSError:
                # Not fatal on its own: a presigned token may be configured. The
                # probe below then fails with a clear message if neither works.
                logger.info("no gateway internal secret at %s", path)
        self._session = aiohttp.ClientSession()
        async with self._session.get(
            self._url("/api/chat/slots"),
            headers=self._headers(),
            timeout=aiohttp.ClientTimeout(total=_PROBE_TIMEOUT),
        ) as resp:
            if resp.status == 403:
                raise AcpGatewayError(
                    "gateway refused the request (403): needs the internal secret "
                    "from $KIROCREW_HOME/.local_secret or a presigned token"
                )
            if resp.status >= 400:
                raise AcpGatewayError(
                    f"gateway at {self._base_url} answered {resp.status} — is it running?"
                )
        logger.info("gateway reachable at %s", self._base_url)

    async def close(self) -> None:
        # Adapter EOF cleanup. Clear the MCP config on every slot THIS adapter
        # registered servers on (owned configs) so a disconnected editor never
        # leaves client-supplied MCP servers registered on a shared dashboard
        # slot. Slots this adapter merely loaded/resumed without registering MCP
        # are NOT touched — they are pre-existing and not owned here. Do this
        # while the HTTP session is still open, then reap the adapter-owned
        # sandboxed children + proxy sockets (which EOFs any provider-spawned
        # proxy and stops it too).
        for session_id in list(self._mcp_sessions):
            with contextlib.suppress(Exception):
                await self._clear_slot_mcp(session_id)
        self._mcp_sessions.clear()
        await self._mcp.shutdown()
        if self._session is not None:
            await self._session.close()
            self._session = None

    # ── SessionBackend ──

    async def create_session(self, cwd: str) -> str:
        """Create a fresh dashboard slot and scope it to *cwd*. Returns its key."""
        name = f"{self._prefix}-{uuid.uuid4().hex[:12]}"
        body: dict[str, Any] = {"name": name}
        if self._agent:
            body["agent"] = self._agent
        data = await self._post_json("/api/chat/slots", body)
        session_id = str((data or {}).get("key") or (data or {}).get("name") or name)
        await self._set_project(session_id, cwd)
        return session_id

    async def load_session(self, session_id: str, cwd: str) -> list[dict[str, str]]:
        """Activate an existing slot and return its conversation for replay."""
        await self._activate_session(session_id, cwd)
        data = await self._get_json(f"/api/chat/slots/{quote(session_id, safe='')}")
        messages = data.get("messages", []) if isinstance(data, dict) else []
        return [
            {
                "role": str(message.get("role", "")),
                "content": str(message.get("content", "")),
            }
            for message in messages
            if isinstance(message, dict)
        ]

    async def resume_session(self, session_id: str, cwd: str) -> None:
        """Resume an existing slot without replaying its history."""
        await self._activate_session(session_id, cwd)

    async def delete_session(self, session_id: str) -> None:
        """Delete a slot this adapter created (failed session/new cleanup).

        Best-effort — a failed handshake must not leave an orphan slot, but a
        delete that itself fails should not mask the original error.
        """
        self._mcp_sessions.discard(session_id)
        with contextlib.suppress(Exception):
            await self._mcp.teardown(session_id)
        if self._session is None:
            return
        try:
            async with self._session.delete(
                self._url(f"/api/chat/slots/{quote(session_id, safe='')}"),
                headers=self._headers(),
            ) as resp:
                if resp.status >= 400:
                    logger.debug("delete slot %s -> %s", session_id, resp.status)
        except aiohttp.ClientError as exc:
            logger.debug("delete slot %s failed: %s", session_id, exc)

    async def _clear_slot_mcp(self, session_id: str) -> None:
        """Register an empty MCP set on a slot, clearing any config we stored."""
        await self._post_json(
            f"/api/chat/slots/{quote(session_id, safe='')}/mcp",
            {"servers": []},
            allow_fail=True,
        )

    async def cancel(self, session_id: str) -> None:
        """Stop the backing turn. The dashboard owns it; this is a soft stop."""
        await self._post_json(
            f"/api/chat/slots/{quote(session_id, safe='')}/stop", {}, allow_fail=True
        )

    async def get_available_commands(self, _session_id: str) -> list[dict[str, Any]] | None:
        """Return the gateway's provider-aware slash-command catalog."""
        data = await self._get_json("/api/slash-commands", allow_fail=True)
        if not isinstance(data, list):
            return None
        return [item for item in data if isinstance(item, dict)]

    async def configure_session_mcp(self, session_id: str, servers: "list[StdioMcpServer]") -> None:
        """Host this session's client MCP servers and register their proxies.

        The untrusted client command/env is NEVER handed to the model-side
        provider. Instead:

        1. **Host** — :class:`SessionMcpSupervisor` spawns each requested server
           ONCE under Kiro Crew's sandbox (OS isolation + credential-scrubbed env +
           gateway secret/.env hidden on disk + fork-bomb gate) and keeps it alive
           owned by this ACP session. A server that cannot start raises
           :class:`~kiro_crew.acp_server.mcp_supervisor.McpSpawnError`, which the
           dispatch layer turns into an ACP error. The whole setup is bounded by
           ``_MCP_SETUP_DEADLINE`` so a hung handshake cannot stall session/new.
        2. **Register** — POST the *proxy* specs (canonical ACP shape) to the
           gateway, scoped to this slot. Each proxy runs the trusted
           :mod:`kiro_crew.acp_server.mcp_proxy` relay against a per-server Unix
           socket; kiro-cli spawns only the proxy, and the MCP handshake flows
           end-to-end to the sandboxed child (single spawn, no double-init).

        An empty *servers* list tears the session's hosted set down and clears the
        slot's stored config — this is what makes a ``session/load`` /
        ``session/resume`` replacement drop stale servers.
        """
        validated = list(servers)
        if not validated:
            # Clear: stop any children this session hosted, then clear the slot's
            # stored config so the next prompt's provider comes up without MCP.
            await self._mcp.teardown(session_id)
            await self._clear_slot_mcp(session_id)
            self._mcp_sessions.discard(session_id)
            return
        # Host the REAL children under Kiro Crew's sandbox and get trusted proxy
        # specs back; only the proxies (socket path + token-file path) are ever
        # sent to the provider — the untrusted command/env never leave here.
        try:
            proxies = await asyncio.wait_for(
                self._mcp.host(session_id, validated), timeout=_MCP_SETUP_DEADLINE
            )
        except asyncio.TimeoutError as exc:
            with contextlib.suppress(Exception):
                await self._mcp.teardown(session_id)
            raise _client_safe_error("MCP server setup exceeded the time budget") from exc
        try:
            await self._post_json(
                f"/api/chat/slots/{quote(session_id, safe='')}/mcp",
                {"servers": servers_to_acp_dicts(proxies)},
            )
        except Exception:
            # Registration failed — do not leave orphan sandboxed children.
            with contextlib.suppress(Exception):
                await self._mcp.teardown(session_id)
            raise
        self._mcp_sessions.add(session_id)

    async def get_session_selectors(self, session_id: str) -> "SelectorState":
        """Advertise the slot's effort modes + model config option.

        Reads the slot's current model + effort from the slot summary, the
        registry model list (``/api/models``, provider-aware) and the slot's
        effort levels (``/api/effort-levels``). All reads are best-effort
        (``allow_fail``): a gateway hiccup advertises fewer/no selectors rather
        than failing ``session/new|load|resume``.

        NOTE: ``/api/models`` is scoped to the gateway's configured provider; ACP
        slots use that provider, so the list matches the slot's agent in the
        common case.
        """
        summary = await self._get_slot_summary(session_id)
        raw_model = summary.get("model")
        current_model = raw_model if isinstance(raw_model, str) else ""
        raw_effort = summary.get("reasoning_effort")
        current_effort = raw_effort if isinstance(raw_effort, str) else ""
        models = await self._get_models()
        levels = await self._get_effort_levels(session_id)
        option = build_model_config_option(current_model, models)
        return SelectorState(
            modes=build_mode_state(current_effort, levels),
            config_options=[option] if option else None,
        )

    async def set_session_mode(self, session_id: str, mode_id: str) -> "SelectorState":
        """Apply an effort mode via the slot's reasoning-effort endpoint.

        ``SESSION_MODE_DEFAULT_ID`` maps back to the empty (provider-default)
        effort; any other id is the effort level verbatim. The gateway endpoint
        persists the slot value and pushes it live (or resets the session so the
        provider is recreated on the next prompt). A non-2xx / transport failure
        raises :class:`AcpGatewayError` (the server maps it to ``-32603`` and
        announces nothing, so the client's view is unchanged). Returns the
        refreshed selector snapshot on success.
        """
        summary = await self._get_slot_summary(session_id, required=True)
        if summary.get("running"):
            raise SelectorBusyError("slot prompt is in progress")
        effort = "" if mode_id == SESSION_MODE_DEFAULT_ID else mode_id
        await self._post_json(
            f"/api/chat/slots/{quote(session_id, safe='')}/reasoning-effort",
            {"reasoning_effort": effort},
        )
        return await self.get_session_selectors(session_id)

    async def set_session_config_option(
        self, session_id: str, config_id: str, value: str
    ) -> "SelectorState":
        """Apply a config option. Only the model selector is supported.

        POSTs the selected canonical model id to the slot's model endpoint, which
        persists it and resets the session so the provider is recreated on the
        next prompt (atomic switch-before-next-turn). A non-2xx / transport
        failure raises :class:`AcpGatewayError` (server ``-32603``, rollback).
        Returns the refreshed selector snapshot on success.
        """
        if config_id != CONFIG_OPTION_MODEL:
            raise AcpGatewayError(f"unsupported config option: {config_id}")
        summary = await self._get_slot_summary(session_id, required=True)
        if summary.get("running"):
            raise SelectorBusyError("slot prompt is in progress")
        await self._post_json(
            f"/api/chat/slots/{quote(session_id, safe='')}/model", {"model": value}
        )
        return await self.get_session_selectors(session_id)

    async def _get_models(self) -> list[dict[str, Any]]:
        """Registry-backed available models from the gateway (``/api/models``)."""
        data = await self._get_json("/api/models", allow_fail=True)
        return [m for m in data if isinstance(m, dict)] if isinstance(data, list) else []

    async def _get_effort_levels(self, session_id: str) -> list[str]:
        """The slot's reasoning-effort levels (``/api/effort-levels?slot=``)."""
        path = f"/api/effort-levels?slot={quote(session_id, safe='')}"
        data = await self._get_json(path, allow_fail=True)
        return [lvl for lvl in data if isinstance(lvl, str)] if isinstance(data, list) else []

    async def list_sessions(
        self, *, cwd: str | None = None, cursor: str | None = None
    ) -> dict[str, Any]:
        """List dashboard slots as ACP session descriptors."""
        del cursor  # All matching slots are returned in one page.
        slots = await self._get_slots()
        out: list[dict[str, Any]] = []
        for s in slots:
            key = s.get("key") or s.get("name")
            if not key:
                continue
            raw_project = s.get("project")
            project = raw_project if isinstance(raw_project, str) else ""
            if cwd and not _project_paths_match(project, cwd):
                continue
            title = s.get("title")
            item: dict[str, Any] = {
                "sessionId": str(key),
                "cwd": project,
                "title": title if isinstance(title, str) else None,
            }
            updated_at = s.get("last_activity_ts") or s.get("last_ts") or s.get("created")
            if isinstance(updated_at, str) and updated_at:
                item["updatedAt"] = updated_at
            out.append(item)
        out.sort(key=lambda item: item.get("updatedAt", ""), reverse=True)
        return {"sessions": out}

    # ── PromptHandler ──

    def prompt_handler(self) -> PromptHandler:
        async def handle_prompt(request: PromptRequest, sink: SessionSink) -> str:
            return await self._run_prompt(request, sink)

        return handle_prompt

    async def _run_prompt(self, request: PromptRequest, sink: SessionSink) -> str:
        slot = request.session_id
        # The slot owns its agent; loaded dashboard sessions may use a different one.
        body = {"message": request.text, "slot": slot}
        try:
            resp = await self._session.post(
                self._url("/api/chat"),
                headers=self._headers({"Content-Type": "application/json"}),
                json=body,
                timeout=_STREAM_TIMEOUT,
            )
            async with resp:
                if resp.status >= 400:
                    detail = (await resp.text())[:400]
                    await sink.send_text(f"\n\n**Error:** gateway {resp.status}: {detail}\n")
                    return "error"
                stop = await self._consume_sse(resp, slot, sink)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.warning("gateway stream failed for slot %s: %s", slot, exc)
            await sink.send_text(f"\n\n**Error:** gateway stream failed: {exc}\n")
            return "error"
        # Attach reply options (if the turn ended with a [OPTIONS: …] prompt) as a
        # namespaced ACP extension; the marker itself already streamed in the text.
        if stop != STOP_REASON_CANCELLED:
            try:
                await sink.send_options(await self._options_for(slot))
            except Exception:
                logger.debug("options lookup failed for slot %s", slot, exc_info=True)
        return stop or STOP_REASON_END_TURN

    async def _consume_sse(self, resp: Any, slot: str, sink: SessionSink) -> str:
        """Translate the /api/chat SSE stream onto the editor's session."""
        async for raw in resp.content:
            if sink.cancelled:
                await self.cancel(slot)
                return STOP_REASON_CANCELLED
            line = raw.decode("utf-8", errors="replace").strip()
            if not line.startswith("data: "):
                continue  # `: keepalive` comments and blank padding
            payload = line[6:]
            if payload == "[DONE]":
                return STOP_REASON_END_TURN
            try:
                chunk = json.loads(payload)
            except (ValueError, TypeError):
                logger.debug("unparsable SSE payload: %s", payload[:120])
                continue
            if isinstance(chunk, dict):
                await self._translate(chunk, slot, sink)
        return STOP_REASON_END_TURN

    async def _translate(self, chunk: dict[str, Any], slot: str, sink: SessionSink) -> None:
        ctype = chunk.get("type", "")
        content = chunk.get("content")
        text = content if isinstance(content, str) else ""
        raw_cls = chunk.get("cls")
        cls = raw_cls if isinstance(raw_cls, str) else ""

        if ctype == "chunk":
            if "thinking" in cls:
                await sink.send_thought(text)
            else:
                await sink.send_text(text)
        elif ctype == "assistant":
            # The final consolidated copy of text already streamed as `chunk`;
            # rendering both doubles the reply.
            return
        elif ctype == "tool":
            title = (text.split("\n", 1)[0] or "Tool")[:120]
            self._tool_seq += 1
            await sink.send_tool_call(
                f"gw-{self._tool_seq}",
                title,
                "other",
                status="completed",
                locations=_sanitize_locations(chunk.get("locations")),
            )
        elif ctype == "permission":
            await self._bridge_permission(chunk, slot, sink)
        elif ctype == "error":
            await sink.send_text(f"\n\n**Error:** {text}\n")
        elif ctype == "compacting":
            await sink.send_thought("\n_Compacting conversation…_\n")
        # `system`, `done`, and anything added later: ignored, not rendered.

    async def _bridge_permission(self, chunk: dict[str, Any], slot: str, sink: SessionSink) -> None:
        """Surface a gateway tool approval to the editor and answer the gateway.

        The gateway blocks the turn on an approval future; this asks the editor
        via ``session/request_permission`` and resolves that future via
        ``POST .../approve``. Fail-closed: any missing id or transport failure
        rejects.
        """
        meta = chunk.get("meta")
        meta = meta if isinstance(meta, dict) else {}
        request_id = str(meta.get("request_id", ""))
        if not request_id:
            logger.warning("permission frame without request_id; cannot answer")
            return
        title = meta.get("tool_title") or (
            chunk.get("content") if isinstance(chunk.get("content"), str) else ""
        )
        tool_call: dict[str, Any] = {
            "toolCallId": str(meta.get("tool_call_id") or request_id),
            "title": str(title or "Tool"),
            "kind": "other",
        }
        tool_input = meta.get("tool_input")
        if isinstance(tool_input, str) and tool_input:
            tool_call["content"] = [
                {"type": "content", "content": {"type": "text", "text": tool_input}}
            ]
        allowed = await sink.request_permission(tool_call)
        await self._post_json(
            f"/api/chat/slots/{quote(slot, safe='')}/approve",
            {"request_id": request_id, "action": "approved" if allowed else "rejected"},
            allow_fail=True,
        )

    async def _options_for(self, slot: str) -> list[str]:
        for s in await self._get_slots():
            if (s.get("key") or s.get("name")) == slot:
                opts = s.get("options")
                if s.get("has_options") and isinstance(opts, list):
                    return [str(o) for o in opts]
                return []
        return []

    # ── HTTP helpers ──

    def _url(self, pathname: str) -> str:
        # Presigned tokens ride in a header (see _headers), never in the URL
        # query string, so they cannot leak into gateway access logs or proxies.
        return f"{self._base_url}{pathname}"

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = dict(extra or {})
        if self._secret:
            headers["X-Internal-Secret"] = self._secret
        if self._token:
            headers["X-Presigned-Token"] = self._token
        return headers

    async def _get_slots(self) -> list[dict[str, Any]]:
        data = await self._get_json("/api/chat/slots", allow_fail=True)
        # GET /api/chat/slots returns a bare list; tolerate a {"slots": [...]} wrap.
        if isinstance(data, dict):
            data = data.get("slots", [])
        return [s for s in data if isinstance(s, dict)] if isinstance(data, list) else []

    async def _get_slot_summary(self, session_id: str, *, required: bool = False) -> dict[str, Any]:
        data = await self._get_json("/api/chat/slots", allow_fail=not required)
        if isinstance(data, dict):
            data = data.get("slots", [])
        if isinstance(data, list):
            for slot in data:
                if isinstance(slot, dict) and (slot.get("key") or slot.get("name")) == session_id:
                    return slot
        if required:
            raise AcpGatewayError(f"slot summary unavailable: {session_id}")
        return {}

    async def _get_json(self, pathname: str, *, allow_fail: bool = False) -> Any:
        try:
            async with self._session.get(self._url(pathname), headers=self._headers()) as resp:
                if resp.status >= 400:
                    if allow_fail:
                        return None
                    detail = (await resp.text())[:200]
                    raise AcpGatewayError(f"{pathname} -> {resp.status}: {detail}")
                return await resp.json()
        except (aiohttp.ClientError, aiohttp.ContentTypeError, ValueError) as exc:
            if allow_fail:
                return None
            raise AcpGatewayError(f"{pathname} failed: {exc}") from exc

    async def _post_json(
        self, pathname: str, body: dict[str, Any], *, allow_fail: bool = False
    ) -> dict[str, Any] | None:
        try:
            async with self._session.post(
                self._url(pathname),
                headers=self._headers({"Content-Type": "application/json"}),
                json=body,
            ) as resp:
                if resp.status >= 400:
                    if allow_fail:
                        logger.debug("%s -> %s", pathname, resp.status)
                        return None
                    detail = (await resp.text())[:200]
                    raise AcpGatewayError(f"{pathname} -> {resp.status}: {detail}")
                try:
                    return await resp.json()
                except (aiohttp.ContentTypeError, ValueError):
                    return {}
        except aiohttp.ClientError as exc:
            if allow_fail:
                logger.debug("%s failed: %s", pathname, exc)
                return None
            raise AcpGatewayError(f"{pathname} failed: {exc}") from exc

    async def _set_project(self, slot: str, cwd: str) -> None:
        """Assign the requested editor workspace before the session is used."""
        if not cwd:
            return
        await self._post_json(f"/api/chat/slots/{quote(slot, safe='')}/project", {"project": cwd})

    async def _activate_session(self, slot: str, cwd: str) -> None:
        await self._post_json(f"/api/chat/slots/{quote(slot, safe='')}/resume", {"key": slot})
        await self._set_project(slot, cwd)


class AcpGatewayError(RuntimeError):
    """The gateway was unreachable or refused a lifecycle request."""
