"""Map ACP sessions to dashboard chat slots through the gateway HTTP API."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import uuid
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable
from urllib.parse import quote, urlparse

import aiohttp

from kiro_crew.acp.types import (
    CONFIG_CATEGORY_MODEL,
    CONFIG_OPTION_MODEL,
    CONFIG_OPTION_TYPE_SELECT,
    KIRO_TOOL_TODO_LIST,
    SESSION_MODE_DEFAULT_ID,
    SESSION_MODE_DEFAULT_NAME,
    STOP_REASON_CANCELLED,
    STOP_REASON_END_TURN,
)
from kiro_crew.acp_server.cleanup_receipts import CleanupReceipt, CleanupReceiptStore
from kiro_crew.acp_server.locations import sanitize_tool_locations
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
from kiro_crew.config.loader import read_local_secret
from kiro_crew.dashboard.urls import is_loopback
from kiro_crew.messaging.renderer import split_options_trailer
from kiro_crew.platform.context import redact_via_context

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

# The adapter reconnects after a transient dashboard WebSocket failure without
# interrupting the independent prompt SSE stream.
_TITLE_WS_RECONNECT_DELAY_SECS = 1.0
_TITLE_WS_CLOSE_TIMEOUT_SECS = 1.0
_ACP_MESSAGE_ID_CACHE_MAX = 4096

# Aggregate wall-clock ceiling for spawning and liveness-checking one session's
# whole MCP set. The trusted provider proxy owns the later initialize handshake.
_MCP_SETUP_DEADLINE = 60.0
_CLEANUP_REPLAY_LIMIT = 32
_CLEANUP_REPLAY_TIMEOUT_SECS = 2.0

# A single dashboard card has at most six validated labels, each capped at 200
# characters. Keep only that possible trailer plus the marker overhead.
_OPTIONS_TRAILER_BUFFER_MAX = 1400
_OPTIONS_MARKER = "[OPTIONS:"


async def _reject_gateway_redirect(
    _session: aiohttp.ClientSession,
    _trace_ctx: Any,
    _params: aiohttp.TraceRequestRedirectParams,
) -> None:
    raise aiohttp.ClientConnectionError("gateway redirect refused")


class _OptionsTrailerFilter:
    """Stream ordinary text while retaining only a possible trailing marker."""

    def __init__(self) -> None:
        self._tail = ""

    def feed(self, text: str) -> str:
        self._tail += text
        marker = self._tail.rfind(_OPTIONS_MARKER)
        if marker < 0:
            keep = max(
                (
                    size
                    for size in range(1, len(_OPTIONS_MARKER))
                    if self._tail.endswith(_OPTIONS_MARKER[:size])
                ),
                default=0,
            )
            if not keep:
                out, self._tail = self._tail, ""
                return out
            out, self._tail = self._tail[:-keep], self._tail[-keep:]
            return out
        if marker:
            out, self._tail = self._tail[:marker], self._tail[marker:]
            return out
        if len(self._tail) > _OPTIONS_TRAILER_BUFFER_MAX:
            out, self._tail = (
                self._tail[: -len(_OPTIONS_MARKER)],
                self._tail[-len(_OPTIONS_MARKER) :],
            )
            return out
        return ""

    def finish(self) -> tuple[str, bool]:
        body, choices = split_options_trailer(self._tail)
        complete = self._tail.startswith(_OPTIONS_MARKER) and self._tail.rstrip().endswith("]")
        self._tail = ""
        return body if complete else body, complete and bool(choices)


def build_mode_state(current_effort: str, effort_levels: list[str]) -> dict[str, Any] | None:
    """Build an ACP ``SessionModeState`` from a slot's effort + available levels.

    The provider-default effort (``""`` internally) is surfaced as the stable
    ``SESSION_MODE_DEFAULT_ID`` mode; the concrete levels are advertised verbatim
    from the runtime's own list (no invented fallbacks). ``currentModeId`` is the
    slot's current level, or the default id when the slot has no explicit level
    (or one unavailable at runtime). Returns ``None`` when there are no concrete levels
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
    """Filter gateway locations and suppress paths changed by egress redaction."""
    return sanitize_tool_locations(raw, redact_via_context) or None


def default_secret_path() -> Path:
    """Path to the gateway's owner-only internal IPC secret."""
    return config_dir() / ".local_secret"


def default_base_url() -> str:
    """Loopback dashboard URL, honoring the explicit KIROCREW_PORT override."""
    port = os.environ.get("KIROCREW_PORT", "5476")
    return f"http://127.0.0.1:{port}"


def _validate_gateway_base_url(raw: str) -> tuple[str, str]:
    """Return the request base and a path-free origin safe for logs."""
    base_url = raw.strip().rstrip("/")
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or not parsed.hostname:
        raise AcpGatewayError("gateway URL must be an absolute http or https URL")
    if parsed.username is not None or parsed.password is not None:
        raise AcpGatewayError("gateway URL must not include user information")
    if parsed.query or parsed.fragment:
        raise AcpGatewayError("gateway URL must not include a query or fragment")
    try:
        port = parsed.port
    except ValueError as exc:
        raise AcpGatewayError("gateway URL contains an invalid port") from exc
    host = parsed.hostname
    display_host = f"[{host}]" if ":" in host else host
    authority = f"{display_host}:{port}" if port is not None else display_host
    return base_url, f"{parsed.scheme}://{authority}"


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
        self._base_url, self._log_origin = _validate_gateway_base_url(base_url)
        self._gateway_is_loopback = is_loopback(urlparse(self._base_url).hostname or "")
        self._agent = agent or ""
        self._secret_path = secret_path
        self._token = token
        self._presigned_session = bool(token)
        self._prefix = session_prefix
        self._secret = ""
        self._session: Any = None  # aiohttp.ClientSession, created in open()
        # Owns the REAL, long-lived client MCP children: spawns each under
        # Kiro Crew's sandbox and exposes it to the provider through a trusted
        # per-session Unix-socket proxy. shutdown() in close() reaps them all.
        self._mcp = SessionMcpSupervisor()
        self._mcp_owner = uuid.uuid4().hex
        # Sessions on whose slot THIS adapter registered a non-empty MCP set.
        # Tracked so adapter EOF (close) clears only the slots it owns and never
        # a pre-existing slot it merely loaded/resumed.
        self._mcp_sessions: set[str] = set()
        self._mcp_proxy_specs: dict[str, list[dict[str, Any]]] = {}
        self._mcp_clear_receipts: dict[str, CleanupReceipt] = {}
        self._mcp_restore_needed: set[str] = set()
        self._mcp_restore_lock = asyncio.Lock()
        self._project_generations: dict[str, str] = {}
        self._created_project_fingerprints: dict[str, str] = {}
        self._pending_project_restores: dict[str, tuple[str, str]] = {}
        # A cleanup intent is durable before its remote mutation starts. Any ACP
        # adapter process can safely replay it because clears are owner-checked
        # and failed-slot deletes are project/empty-slot checked.
        self._cleanup_receipts = CleanupReceiptStore()
        self._tool_seq = 0
        # Correlate a refinement (SSE ctype="tool_update") back to the gw-N ID
        # emitted for the initial tool_call so send_tool_call_update targets
        # the same call. Kiro-cli streams tool params over two events for some
        # tools (e.g. fs_read: empty initial rawInput + populated refinement),
        # so the follow-along locations only arrive on the second event.
        self._tool_id_map: dict[tuple[str, str], str] = {}
        self._elicitation_tasks: dict[tuple[str, str, int], asyncio.Task[None]] = {}
        self._elicitation_follow_up_tasks: set[asyncio.Task[Any]] = set()
        self._session_info_handler: Callable[[str, str], Awaitable[None]] | None = None
        self._session_message_handler: Callable[[str, str, str, str], Awaitable[None]] | None = None
        self._session_plan_handler: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None
        self._seen_message_ids: set[str] = set()
        self._seen_message_order: deque[str] = deque()
        self._title_events_task: asyncio.Task[None] | None = None
        self._title_refresh_tasks: set[asyncio.Task[bool]] = set()
        self._title_ws: aiohttp.ClientWebSocketResponse | None = None
        self._title_session_ids: set[str] = set()
        self._closing = False

    # ── lifecycle ──

    async def _exchange_presigned_token(self) -> None:
        """Redeem the short-lived link token into the gateway's session cookie."""
        if not self._token:
            return
        async with self._session.get(
            self._url("/"),
            headers=self._headers(),
            timeout=aiohttp.ClientTimeout(total=_PROBE_TIMEOUT),
            allow_redirects=False,
        ) as resp:
            if resp.status >= 400:
                raise AcpGatewayError(
                    f"gateway token exchange failed ({resp.status}); mint a fresh presigned token"
                )
            if not any(name.startswith("mc_token_") for name in resp.cookies):
                raise AcpGatewayError("gateway token exchange did not establish a session")
        self._token = None

    async def open(self) -> None:
        """Load credentials and confirm the gateway is reachable."""
        self._closing = False
        parsed = urlparse(self._base_url)
        if not self._gateway_is_loopback and parsed.scheme != "https":
            raise AcpGatewayError(
                "non-loopback gateways require https so the presigned token "
                "is never sent in plaintext"
            )
        if not self._token:
            if not self._gateway_is_loopback:
                raise AcpGatewayError(
                    "non-loopback gateways require an explicit presigned token; "
                    "the local internal secret is never sent off-host"
                )
            await self._refresh_secret()
        trace = aiohttp.TraceConfig()
        trace.on_request_redirect.append(_reject_gateway_redirect)
        self._session = aiohttp.ClientSession(
            trace_configs=[trace],
            cookie_jar=aiohttp.CookieJar(unsafe=True),
        )
        try:
            await self._exchange_presigned_token()
            async with self._session.get(
                self._url("/api/chat/slots"),
                headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=_PROBE_TIMEOUT),
                allow_redirects=False,
            ) as resp:
                if resp.status == 403:
                    raise AcpGatewayError(
                        "gateway refused the request (403): needs the internal secret "
                        "from $KIROCREW_HOME/.local_secret or a presigned token"
                    )
                if resp.status >= 400:
                    raise AcpGatewayError(
                        f"gateway at {self._log_origin} answered {resp.status} — is it running?"
                    )
        except BaseException:
            await self._session.close()
            self._session = None
            raise
        await self._retry_cleanup_receipts()
        logger.info("gateway reachable at %s", self._log_origin)
        self._start_title_events()

    async def close(self) -> None:
        self._closing = True
        pending_elicitations = list(
            dict.fromkeys([*self._elicitation_tasks.values(), *self._elicitation_follow_up_tasks])
        )
        for task in pending_elicitations:
            task.cancel()
        if pending_elicitations:
            await asyncio.gather(*pending_elicitations, return_exceptions=True)
        self._elicitation_tasks.clear()
        self._elicitation_follow_up_tasks.clear()
        pending_title_refreshes = list(self._title_refresh_tasks)
        if pending_title_refreshes:
            await asyncio.gather(*pending_title_refreshes, return_exceptions=True)
        self._title_refresh_tasks.clear()
        if self._title_events_task is not None:
            self._title_events_task.cancel()
            await asyncio.gather(self._title_events_task, return_exceptions=True)
            self._title_events_task = None
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
        self._mcp_proxy_specs.clear()
        self._mcp_restore_needed.clear()
        self._project_generations.clear()
        self._created_project_fingerprints.clear()
        self._pending_project_restores.clear()
        await self._mcp.shutdown()
        if self._session is not None:
            await self._session.close()
            self._session = None

    def set_session_info_handler(self, handler: Callable[[str, str], Awaitable[None]]) -> None:
        """Forward live dashboard title changes to the owning ACP server."""
        self._session_info_handler = handler
        self._start_title_events()

    def set_session_message_handler(
        self, handler: Callable[[str, str, str, str], Awaitable[None]]
    ) -> None:
        """Forward finalized dashboard messages to the owning ACP server."""
        self._session_message_handler = handler
        self._start_title_events()

    def register_session_info(self, session_id: str) -> None:
        """Allow title events only for a session registered by this ACP server."""
        if not session_id:
            return
        self._title_session_ids.add(session_id)
        self._start_title_events()
        if self._title_ws is not None and not self._title_ws.closed:
            # Reconnect with the expanded allowlist; the Gateway binds the
            # subscription to the keys sent during the WebSocket handshake.
            task = asyncio.create_task(self._title_ws.close())
            self._title_refresh_tasks.add(task)
            task.add_done_callback(self._title_refresh_tasks.discard)

    def set_session_plan_handler(
        self, handler: Callable[[str, dict[str, Any]], Awaitable[None]]
    ) -> None:
        """Forward live dashboard task plans to the owning ACP server."""
        self._session_plan_handler = handler
        self._start_title_events()

    def _start_title_events(self) -> None:
        if (
            self._session is not None
            and (
                self._session_info_handler is not None
                or self._session_message_handler is not None
                or self._session_plan_handler is not None
            )
            and self._title_session_ids
            and self._title_events_task is None
            and not self._closing
        ):
            self._title_events_task = asyncio.create_task(
                self._watch_title_events(), name="acp-dashboard-title-events"
            )

    def _ws_url(self) -> str:
        parsed = urlparse(self._base_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        return parsed._replace(scheme=scheme, path=f"{parsed.path.rstrip('/')}/api/ws").geturl()

    async def _watch_title_events(self) -> None:
        """Keep the adapter subscribed to dashboard title events."""
        while not self._closing and self._session is not None:
            try:
                await self._refresh_secret()
                ws = await asyncio.wait_for(
                    self._session.ws_connect(
                        self._ws_url(),
                        headers=self._headers(
                            {
                                "Origin": self._log_origin,
                                "X-ACP-Title-Subscription": "1",
                            }
                        ),
                        heartbeat=30,
                        timeout=aiohttp.ClientWSTimeout(
                            ws_receive=None, ws_close=_TITLE_WS_CLOSE_TIMEOUT_SECS
                        ),
                    ),
                    timeout=_PROBE_TIMEOUT,
                )
                self._title_ws = ws
                await ws.send_json(
                    {
                        "type": "subscribe_acp_title",
                        "keys": sorted(self._title_session_ids),
                    }
                )
                try:
                    async for message in ws:
                        if message.type is aiohttp.WSMsgType.TEXT:
                            await self._handle_title_event(message.data)
                        elif message.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                            break
                finally:
                    if self._title_ws is ws:
                        self._title_ws = None
                    await ws.close()
            except asyncio.CancelledError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError):
                logger.debug("dashboard title WebSocket disconnected", exc_info=True)
            if not self._closing:
                await asyncio.sleep(_TITLE_WS_RECONNECT_DELAY_SECS)

    @staticmethod
    def _todo_to_plan(data: dict[str, Any]) -> dict[str, Any] | None:
        """Map a complete dashboard TODO snapshot to the ACP plan shape."""
        tasks = data.get("tasks")
        if not isinstance(tasks, list):
            return None
        entries: list[dict[str, Any]] = []
        found_current = False
        for task in tasks:
            if not isinstance(task, dict):
                continue
            text = task.get("text")
            if not isinstance(text, str) or not text:
                continue
            completed = task.get("completed") is True
            if completed:
                status = "completed"
            elif not found_current:
                status = "in_progress"
                found_current = True
            else:
                status = "pending"
            entries.append({"content": text, "priority": "medium", "status": status})
        description = data.get("description")
        metadata = (
            {"kirocrew": {"description": description}} if isinstance(description, str) else None
        )
        plan: dict[str, Any] = {"entries": entries}
        if metadata:
            plan["_meta"] = metadata
        return plan

    async def _handle_title_event(self, raw: str) -> None:
        """Translate scoped dashboard title and finalized-message events to ACP."""
        try:
            frame = json.loads(raw)
        except (TypeError, ValueError):
            return
        if not isinstance(frame, dict):
            return
        data = frame.get("data")
        if not isinstance(data, dict):
            return
        if frame.get("type") == "slot_title":
            session_id = data.get("key")
            title = data.get("title")
            if (
                isinstance(session_id, str)
                and session_id
                and isinstance(title, str)
                and self._session_info_handler is not None
            ):
                await self._session_info_handler(session_id, title)
            return
        if frame.get("type") == "acp_plan":
            session_id = data.get("slot")
            if (
                isinstance(session_id, str)
                and session_id in self._title_session_ids
                and self._session_plan_handler is not None
            ):
                plan = self._todo_to_plan(data)
                if plan is not None:
                    await self._session_plan_handler(session_id, plan)
            return
        if frame.get("type") != "acp_message":
            return
        session_id = data.get("slot")
        role = data.get("role")
        content = data.get("content")
        message_id = data.get("messageId")
        origin = data.get("origin")
        if (
            not isinstance(session_id, str)
            or session_id not in self._title_session_ids
            or role not in ("user", "assistant")
            or not isinstance(content, str)
            or not isinstance(message_id, str)
            or not message_id
            or self._session_message_handler is None
        ):
            return
        if not self._remember_message_id(message_id):
            return
        if origin == session_id:
            return
        await self._session_message_handler(session_id, role, content, message_id)

    def _remember_message_id(self, message_id: str) -> bool:
        """Remember one durable row id and return whether it was newly observed."""
        if message_id in self._seen_message_ids:
            return False
        if len(self._seen_message_order) >= _ACP_MESSAGE_ID_CACHE_MAX:
            evicted = self._seen_message_order.popleft()
            self._seen_message_ids.discard(evicted)
        self._seen_message_order.append(message_id)
        self._seen_message_ids.add(message_id)
        return True

    # ── SessionBackend ──

    async def create_session(self, cwd: str) -> str:
        """Create a fresh dashboard slot and scope it to *cwd*. Returns its key."""
        name = f"{self._prefix}-{uuid.uuid4().hex[:12]}"
        body: dict[str, Any] = {"name": name}
        if self._agent:
            body["agent"] = self._agent
        data = await self._post_json("/api/chat/slots", body)
        session_id = str((data or {}).get("key") or (data or {}).get("name") or name)
        try:
            await self._set_project(session_id, cwd)
            if cwd:
                self._created_project_fingerprints[session_id] = hashlib.sha256(
                    cwd.encode("utf-8", errors="surrogatepass")
                ).hexdigest()
        except (asyncio.CancelledError, Exception):
            try:
                await self.delete_session(session_id)
            except Exception:
                logger.warning(
                    "failed to retain partial-slot cleanup for %s",
                    session_id,
                    exc_info=True,
                )
            raise
        return session_id

    async def load_session(self, session_id: str, cwd: str) -> list[dict[str, str]]:
        """Activate an existing slot and return its conversation for replay."""
        fallback_project = (
            None if self._gateway_is_loopback else await self._snapshot_session_project(session_id)
        )
        activation: tuple[str | None, str | None] | None = None
        try:
            activation = await self._activate_session_for_lifecycle(
                session_id, cwd, fallback_project
            )
            data = await self._get_json(f"/api/chat/slots/{quote(session_id, safe='')}")
        except (asyncio.CancelledError, Exception):
            generation, previous_project = activation or (None, None)
            await self._restore_session_project(
                session_id,
                previous_project if previous_project is not None else fallback_project or "",
                expected_generation=generation,
            )
            raise
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
        fallback_project = (
            None if self._gateway_is_loopback else await self._snapshot_session_project(session_id)
        )
        activation: tuple[str | None, str | None] | None = None
        try:
            activation = await self._activate_session_for_lifecycle(
                session_id, cwd, fallback_project
            )
        except (asyncio.CancelledError, Exception):
            generation, previous_project = activation or (None, None)
            await self._restore_session_project(
                session_id,
                previous_project if previous_project is not None else fallback_project or "",
                expected_generation=generation,
            )
            raise

    async def get_session_cwd(self, session_id: str) -> str:
        """Return the slot project so a failed lifecycle change can restore it."""
        return await self._snapshot_session_project(session_id)

    async def restore_session_cwd(self, session_id: str, cwd: str) -> bool:
        """Restore project only while this adapter's lifecycle generation is current."""
        generation = self._project_generations.get(session_id)
        if generation is None:
            return False
        return await self._restore_project_generation(session_id, cwd, generation)

    async def _restore_project_generation(
        self, session_id: str, project: str, generation: str
    ) -> bool:
        """Retain a restore receipt until the gateway confirms applied or stale."""
        receipt = (project, generation)
        self._pending_project_restores[session_id] = receipt
        data = await self._post_json_mutation(
            f"/api/chat/slots/{quote(session_id, safe='')}/project",
            {"project": project, "expected_generation": generation},
        )
        if self._pending_project_restores.get(session_id) == receipt:
            self._pending_project_restores.pop(session_id, None)
        if self._project_generations.get(session_id) == generation:
            self._project_generations.pop(session_id, None)
        return isinstance(data, dict) and data.get("applied") is True

    async def _retry_pending_project_restore(self, session_id: str) -> None:
        """Finish an unresolved restore before allowing another lifecycle mutation."""
        pending = self._pending_project_restores.get(session_id)
        if pending is None:
            return
        project, generation = pending
        await self._restore_project_generation(session_id, project, generation)

    async def _activate_session_for_lifecycle(
        self, session_id: str, cwd: str, fallback_project: str | None
    ) -> tuple[str | None, str | None]:
        activation = asyncio.create_task(self._activate_session(session_id, cwd))
        try:
            return await asyncio.shield(activation)
        except asyncio.CancelledError:
            try:
                generation, previous_project = await self._finish_shielded(activation)
            except Exception:
                logger.warning(
                    "cancelled project activation did not settle for %s",
                    session_id,
                    exc_info=True,
                )
            else:
                await self._finish_shielded(
                    self._restore_session_project(
                        session_id,
                        (
                            previous_project
                            if previous_project is not None
                            else fallback_project or ""
                        ),
                        expected_generation=generation,
                    )
                )
            raise

    async def _snapshot_session_project(self, session_id: str) -> str:
        """Return a known project value, preserving an intentionally empty value."""
        summary = await self._get_slot_summary(session_id, required=True)
        project = summary.get("project")
        if not isinstance(project, str):
            raise AcpGatewayError(f"slot project unavailable: {session_id}")
        return project

    async def _restore_session_project(
        self,
        session_id: str,
        project: str,
        *,
        expected_generation: str | None,
    ) -> None:
        """Best-effort generation-checked restore without masking the original failure."""
        if expected_generation is None:
            return
        try:
            applied = await self._restore_project_generation(
                session_id, project, expected_generation
            )
            if not applied:
                logger.info(
                    "session project ownership changed for %s; rollback skipped", session_id
                )
        except Exception:
            logger.warning("failed to restore session project for %s", session_id, exc_info=True)

    def _forget_session_mcp(self, session_id: str) -> None:
        """Forget one adapter-owned MCP registration after its proxies are gone."""
        self._mcp_sessions.discard(session_id)
        self._mcp_proxy_specs.pop(session_id, None)
        self._mcp_restore_needed.discard(session_id)

    async def delete_session(self, session_id: str) -> None:
        """Delete a slot this adapter created, retaining unconfirmed cleanup."""
        project_fingerprint = self._created_project_fingerprints.pop(session_id, "")
        receipt = await asyncio.to_thread(
            self._cleanup_receipts.add_slot_delete,
            self._base_url,
            session_id,
            project_fingerprint=project_fingerprint,
        )
        self._forget_session_mcp(session_id)
        self._project_generations.pop(session_id, None)
        self._pending_project_restores.pop(session_id, None)
        with contextlib.suppress(Exception):
            await self._mcp.teardown(session_id)
        if self._session is None:
            return
        try:
            await self._perform_cleanup_receipt(receipt)
        except (AcpGatewayError, aiohttp.ClientError):
            logger.warning("retained failed-slot cleanup receipt for %s", session_id, exc_info=True)
            return
        await asyncio.to_thread(self._cleanup_receipts.discard, receipt)

    async def _retry_cleanup_receipts(self) -> None:
        """Replay this gateway's durable cleanup intents without blocking startup."""
        receipts = await asyncio.to_thread(self._cleanup_receipts.pending)
        matching = (receipt for receipt in receipts if receipt.gateway_origin == self._base_url)
        for index, receipt in enumerate(matching):
            if index >= _CLEANUP_REPLAY_LIMIT:
                break
            if await asyncio.to_thread(self._cleanup_receipts.mcp_owner_is_live, receipt):
                continue
            try:
                await asyncio.wait_for(
                    self._perform_cleanup_receipt(receipt),
                    timeout=_CLEANUP_REPLAY_TIMEOUT_SECS,
                )
            except (AcpGatewayError, aiohttp.ClientError, asyncio.TimeoutError):
                logger.warning("ACP cleanup receipt %s remains pending", receipt.receipt_id)
                continue
            await asyncio.to_thread(self._cleanup_receipts.discard, receipt)

    async def _perform_cleanup_receipt(self, receipt: CleanupReceipt) -> None:
        if receipt.kind == "mcp_clear":
            await self._post_json_mutation_with_id(
                f"/api/chat/slots/{quote(receipt.session_id, safe='')}/mcp",
                {
                    "servers": [],
                    "owner": receipt.owner,
                    "mode": "clear_if_owner",
                },
                receipt.mutation_id,
            )
            return
        if receipt.kind != "slot_delete":
            raise AcpGatewayError("unsupported ACP cleanup receipt")
        await self._refresh_secret()
        extra_headers = {"X-ACP-Cleanup-Require-Empty": "1"}
        if receipt.project_fingerprint:
            extra_headers["X-ACP-Cleanup-Project-Fingerprint"] = receipt.project_fingerprint
        async with self._session.delete(
            self._url(f"/api/chat/slots/{quote(receipt.session_id, safe='')}"),
            headers=self._headers(extra_headers),
            allow_redirects=False,
        ) as resp:
            if resp.status in (404, 409):
                return
            if resp.status >= 400:
                detail = redact_via_context((await resp.text())[:200])
                raise AcpGatewayError(
                    f"delete slot {receipt.session_id} -> {resp.status}: {detail}"
                )

    async def _finish_shielded(self, awaitable: Any) -> Any:
        """Finish rollback-critical I/O despite repeated cancellation requests."""
        task = asyncio.ensure_future(awaitable)
        current = asyncio.current_task()
        while True:
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                if task.cancelled():
                    raise
                uncancel = getattr(current, "uncancel", None)
                if uncancel is not None:
                    uncancel()

    @staticmethod
    def _previous_mcp_registration(data: Any) -> dict[str, Any] | None:
        previous = data.get("previous") if isinstance(data, dict) else None
        return previous if isinstance(previous, dict) else None

    async def _replace_slot_mcp(
        self, session_id: str, servers: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        body: dict[str, Any] = {
            "servers": servers,
            "owner": self._mcp_owner,
            "mode": "replace",
        }
        if self._gateway_is_loopback:
            body["return_previous"] = True
        post = asyncio.create_task(
            self._post_json_mutation(f"/api/chat/slots/{quote(session_id, safe='')}/mcp", body)
        )
        try:
            data = await asyncio.shield(post)
        except asyncio.CancelledError:
            try:
                completed = await self._finish_shielded(post)
                previous = self._previous_mcp_registration(completed)
                if previous is not None:
                    await self._finish_shielded(self.restore_session_mcp(session_id, previous))
            except (asyncio.CancelledError, Exception):
                logger.warning(
                    "cancelled MCP replacement rollback failed for %s",
                    session_id,
                    exc_info=True,
                )
            raise
        return self._previous_mcp_registration(data)

    async def restore_session_mcp(self, session_id: str, snapshot: Any) -> None:
        """Restore an atomic registration snapshot if this adapter still owns the slot."""
        if not isinstance(snapshot, dict):
            raise AcpGatewayError("invalid MCP registration snapshot")
        servers = snapshot.get("servers")
        owner = snapshot.get("owner")
        expected_generation = snapshot.get("expected_generation")
        if (
            not isinstance(servers, list)
            or not isinstance(owner, str)
            or not isinstance(expected_generation, str)
            or not expected_generation
        ):
            raise AcpGatewayError("invalid MCP registration snapshot")
        await self._post_json_mutation(
            f"/api/chat/slots/{quote(session_id, safe='')}/mcp",
            {
                "servers": servers,
                "owner": owner,
                "mode": "restore_if_owner",
                "expected_owner": self._mcp_owner,
                "expected_generation": expected_generation,
            },
        )

    async def _restore_gateway_mcp(self, session_id: str) -> None:
        """Restore this adapter's proxies only while the restarted slot is unowned."""
        if session_id not in self._mcp_restore_needed:
            return
        async with self._mcp_restore_lock:
            if session_id not in self._mcp_restore_needed:
                return
            servers = self._mcp_proxy_specs.get(session_id)
            if not servers:
                raise AcpGatewayError(f"MCP proxy registration unavailable: {session_id}")
            post = asyncio.create_task(
                self._post_json_mutation(
                    f"/api/chat/slots/{quote(session_id, safe='')}/mcp",
                    {
                        "servers": servers,
                        "owner": self._mcp_owner,
                        "mode": "restore_if_owner",
                        "expected_owner": "",
                    },
                )
            )
            cancelled = False
            try:
                data = await asyncio.shield(post)
            except asyncio.CancelledError:
                cancelled = True
                data = await self._finish_shielded(post)
            if not isinstance(data, dict) or data.get("applied") is not True:
                raise AcpGatewayError(f"slot MCP ownership changed: {session_id}")
            self._mcp_restore_needed.discard(session_id)
            if cancelled:
                raise asyncio.CancelledError

    async def _retain_mcp_clear_receipt(self, session_id: str) -> CleanupReceipt:
        receipt = self._mcp_clear_receipts.get(session_id)
        if receipt is not None:
            return receipt
        receipt = await asyncio.to_thread(
            self._cleanup_receipts.add_mcp_clear,
            self._base_url,
            session_id,
            self._mcp_owner,
        )
        self._mcp_clear_receipts[session_id] = receipt
        return receipt

    async def _clear_slot_mcp(self, session_id: str) -> None:
        """Clear this adapter's MCP generation with durable retry intent."""
        receipt = await self._retain_mcp_clear_receipt(session_id)
        await self._perform_cleanup_receipt(receipt)
        await asyncio.to_thread(self._cleanup_receipts.discard, receipt)
        if self._mcp_clear_receipts.get(session_id) == receipt:
            self._mcp_clear_receipts.pop(session_id, None)

    async def _clear_failed_mcp_setup(self, session_id: str) -> None:
        """Clear a possibly stale registration after replacement teardown."""
        self._mcp_sessions.add(session_id)
        await self._clear_slot_mcp(session_id)
        self._forget_session_mcp(session_id)

    async def _cancel_failed_mcp_setup(self, session_id: str) -> None:
        """Best-effort cleanup without replacing the caller's cancellation."""
        self._mcp_sessions.add(session_id)
        try:
            with contextlib.suppress(Exception):
                await self._mcp.teardown(session_id)
            await self._clear_slot_mcp(session_id)
        except (asyncio.CancelledError, Exception):
            return
        self._forget_session_mcp(session_id)

    async def cancel(self, session_id: str) -> None:
        """Stop the backing turn. The dashboard owns it; this is a soft stop."""
        await self._post_json(f"/api/chat/slots/{quote(session_id, safe='')}/stop", {})

    async def get_available_commands(self, _session_id: str) -> list[dict[str, Any]] | None:
        """Return the gateway's provider-aware slash-command catalog."""
        data = await self._get_json("/api/slash-commands", allow_fail=True)
        if not isinstance(data, list):
            return None
        return [item for item in data if isinstance(item, dict)]

    async def configure_session_mcp(
        self, session_id: str, cwd: str, servers: "list[StdioMcpServer]"
    ) -> dict[str, Any] | None:
        """Host this session's client MCP servers and register their proxies.

        The untrusted client command/env is NEVER handed to the model-side
        provider. Instead:

        1. **Host** — :class:`SessionMcpSupervisor` spawns each requested server
           under Kiro Crew's strict sandbox (OS isolation + credential-store mask +
           credential-scrubbed env + gateway secret/.env hidden on disk +
           fork-bomb gate) and owns it for this ACP session. A recreated provider
           gets a fresh child before its proxy starts a new MCP initialize lifecycle.
           A server that cannot start raises
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
        if validated and not self._gateway_is_loopback:
            raise _client_safe_error(
                "client-supplied MCP servers require a loopback gateway because their "
                "proxy capabilities are local to the ACP adapter host"
            )
        if not validated:
            # Clear only this adapter's remote registration. A local adapter gets
            # an atomic prior snapshot so a later lifecycle failure can restore it.
            try:
                await self._mcp.teardown(session_id)
                if self._gateway_is_loopback:
                    snapshot = await self._replace_slot_mcp(session_id, [])
                else:
                    await self._clear_slot_mcp(session_id)
                    snapshot = None
            except asyncio.CancelledError:
                await self._cancel_failed_mcp_setup(session_id)
                raise
            self._forget_session_mcp(session_id)
            return snapshot
        # Host the REAL children under Kiro Crew's sandbox and get trusted proxy
        # specs back; only the proxies (socket path + token-file path) are ever
        # sent to the provider — the untrusted command/env never leave here.
        try:
            proxies = await asyncio.wait_for(
                self._mcp.host(session_id, validated, cwd=cwd),
                timeout=_MCP_SETUP_DEADLINE,
            )
        except asyncio.CancelledError:
            await self._cancel_failed_mcp_setup(session_id)
            raise
        except asyncio.TimeoutError as exc:
            with contextlib.suppress(Exception):
                await self._mcp.teardown(session_id)
            await self._clear_failed_mcp_setup(session_id)
            raise _client_safe_error("MCP server setup exceeded the time budget") from exc
        except Exception:
            with contextlib.suppress(Exception):
                await self._mcp.teardown(session_id)
            await self._clear_failed_mcp_setup(session_id)
            raise
        proxy_specs = servers_to_acp_dicts(proxies)
        try:
            await self._retain_mcp_clear_receipt(session_id)
            snapshot = await self._replace_slot_mcp(session_id, proxy_specs)
        except asyncio.CancelledError:
            await self._cancel_failed_mcp_setup(session_id)
            raise
        except Exception:
            # Registration failed — reap the new children and remove any old
            # proxy specs whose sockets were destroyed by replacement.
            with contextlib.suppress(Exception):
                await self._mcp.teardown(session_id)
            await self._clear_failed_mcp_setup(session_id)
            raise
        self._mcp_sessions.add(session_id)
        self._mcp_proxy_specs[session_id] = proxy_specs
        self._mcp_restore_needed.discard(session_id)
        return snapshot

    async def restore_local_session_mcp(
        self, session_id: str, cwd: str, servers: "list[StdioMcpServer]"
    ) -> bool:
        """Re-host prior proxies without overwriting a newer adapter owner."""
        validated = list(servers)
        if validated and not self._gateway_is_loopback:
            return False
        try:
            if validated:
                proxies = await asyncio.wait_for(
                    self._mcp.host(session_id, validated, cwd=cwd),
                    timeout=_MCP_SETUP_DEADLINE,
                )
                proxy_specs = servers_to_acp_dicts(proxies)
            else:
                await self._mcp.teardown(session_id)
                proxy_specs = []
            data = await self._post_json_mutation(
                f"/api/chat/slots/{quote(session_id, safe='')}/mcp",
                {
                    "servers": proxy_specs,
                    "owner": self._mcp_owner,
                    "mode": "restore_if_owner",
                    "expected_owner": self._mcp_owner,
                },
            )
            if not isinstance(data, dict) or data.get("applied") is not True:
                with contextlib.suppress(Exception):
                    await self._mcp.teardown(session_id)
                self._forget_session_mcp(session_id)
                return False
        except Exception:
            with contextlib.suppress(Exception):
                await self._mcp.teardown(session_id)
            await self._clear_failed_mcp_setup(session_id)
            raise
        if proxy_specs:
            self._mcp_sessions.add(session_id)
            self._mcp_proxy_specs[session_id] = proxy_specs
            self._mcp_restore_needed.discard(session_id)
        else:
            self._forget_session_mcp(session_id)
        return True

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
        relocation_candidates: list[dict[str, Any]] = []
        for s in slots:
            key = s.get("key") or s.get("name")
            if not key:
                continue
            session_id = str(key)
            raw_project = s.get("project")
            project = raw_project if isinstance(raw_project, str) else ""
            project_matches = not cwd or await asyncio.to_thread(_project_paths_match, project, cwd)
            relocation_candidate = bool(
                cwd
                and project
                and session_id.startswith(f"{self._prefix}-")
                and not project_matches
            )
            if not project_matches and not relocation_candidate:
                continue
            title = s.get("title")
            item: dict[str, Any] = {
                "sessionId": session_id,
                "cwd": cwd if relocation_candidate else project,
                "title": title if isinstance(title, str) else None,
            }
            updated_at = s.get("last_activity_ts") or s.get("last_ts") or s.get("created")
            if isinstance(updated_at, str) and updated_at:
                item["updatedAt"] = updated_at
            if project_matches:
                out.append(item)
            else:
                relocation_candidates.append(item)
        # A moved editor workspace has no normal project match. In that case,
        # offer ACP-owned history as an explicit fallback without surfacing it
        # alongside sessions already scoped to the requested project.
        result = out or relocation_candidates
        result.sort(key=lambda item: item.get("updatedAt", ""), reverse=True)
        return {"sessions": result}

    # ── PromptHandler ──

    def prompt_handler(self) -> PromptHandler:
        async def handle_prompt(request: PromptRequest, sink: SessionSink) -> str:
            return await self._run_prompt(request, sink)

        return handle_prompt

    async def _run_prompt(self, request: PromptRequest, sink: SessionSink) -> str:
        # The slot owns its agent; loaded dashboard sessions may use a different one.
        return await self._stream_follow_up(
            "/api/chat",
            {"message": request.text, "slot": request.session_id},
            request.session_id,
            sink,
        )

    def _clear_tool_ids(self, slot: str) -> None:
        """Discard tool-call correlations when their session turn is complete."""
        self._tool_id_map = {
            key: value for key, value in self._tool_id_map.items() if key[0] != slot
        }

    async def _stream_follow_up(
        self, pathname: str, body: dict[str, Any], slot: str, sink: SessionSink
    ) -> str:
        """Start one gateway-owned turn and relay its SSE response to the editor.

        Elicitation answers are ordinary next turns after their initial ACP prompt
        has completed, so this helper remains usable from the background
        elicitation task as well as from ``session/prompt``.
        """
        try:
            await self._refresh_secret()
            await self._restore_gateway_mcp(slot)
            resp = await self._session.post(
                self._url(pathname),
                headers=self._headers(
                    {
                        "Content-Type": "application/json",
                        "X-ACP-Session-Id": slot,
                        "X-ACP-MCP-Owner": self._mcp_owner,
                    }
                ),
                json=body,
                timeout=_STREAM_TIMEOUT,
                allow_redirects=False,
            )
            async with resp:
                if resp.status >= 400:
                    detail = redact_via_context((await resp.text())[:400])
                    await sink.send_text(f"\n\n**Error:** gateway {resp.status}: {detail}\n")
                    self._clear_tool_ids(slot)
                    return "error"
                if resp.content_type == "text/event-stream":
                    stop, saw_options_trailer = await self._consume_sse(resp, slot, sink)
                elif resp.content_type == "application/json":
                    # A partial multi-question answer is acknowledged without
                    # starting a turn; only the completing answer produces SSE.
                    await resp.read()
                    stop, saw_options_trailer = STOP_REASON_END_TURN, False
                else:
                    raise AcpGatewayError(
                        f"unexpected gateway response content type: {resp.content_type or 'none'}"
                    )
        except (aiohttp.ClientError, asyncio.TimeoutError, AcpGatewayError) as exc:
            safe_error = redact_via_context(str(exc))
            logger.warning("gateway stream failed for slot %s: %s", slot, safe_error)
            await sink.send_text(f"\n\n**Error:** gateway stream failed: {safe_error}\n")
            self._clear_tool_ids(slot)
            return "error"
        if stop != STOP_REASON_CANCELLED:
            canonical_pending = await self._dispatch_pending_elicitations(slot, sink)
            try:
                if saw_options_trailer and not canonical_pending:
                    options = await self._options_for(slot)
                    if sink.supports_elicitation:
                        await self._dispatch_options_elicitation(slot, options, sink)
                    else:
                        await sink.send_options(options)
                elif not sink.supports_elicitation:
                    # Keep the namespaced extension during the compatibility period.
                    await sink.send_options(await self._options_for(slot))
            except Exception:
                logger.debug("options lookup failed for slot %s", slot, exc_info=True)
        self._clear_tool_ids(slot)
        return stop or STOP_REASON_END_TURN

    async def _consume_sse(self, resp: Any, slot: str, sink: SessionSink) -> tuple[str, bool]:
        """Translate the /api/chat SSE stream onto the editor's session."""
        trailer = _OptionsTrailerFilter() if sink.supports_elicitation else None
        async for raw in resp.content:
            if sink.cancelled:
                return STOP_REASON_CANCELLED, False
            line = raw.decode("utf-8", errors="replace").strip()
            if not line.startswith("data: "):
                continue
            payload = line[6:]
            if payload == "[DONE]":
                if trailer is not None:
                    text, matched = trailer.finish()
                    if text:
                        await sink.send_text(text)
                    return STOP_REASON_END_TURN, matched
                return STOP_REASON_END_TURN, False
            try:
                chunk = json.loads(payload)
            except (ValueError, TypeError):
                logger.debug("unparsable SSE payload: %s", payload[:120])
                continue
            if not isinstance(chunk, dict):
                continue
            if (
                trailer is not None
                and chunk.get("type") == "chunk"
                and "thinking" not in str(chunk.get("cls", ""))
            ):
                raw_text = chunk.get("content")
                if isinstance(raw_text, str):
                    visible = trailer.feed(raw_text)
                    if visible:
                        await sink.send_text(visible)
                    continue
            await self._translate(chunk, slot, sink)
        if trailer is not None:
            text, _matched = trailer.finish()
            if text:
                await sink.send_text(text)
        raise AcpGatewayError("gateway stream ended before completion")

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
        elif ctype == "user":
            meta = chunk.get("meta")
            row_meta = meta if isinstance(meta, dict) else {}
            if row_meta.get("_acp_session") == slot:
                return
            message_id = row_meta.get("mid")
            await sink.send_user_text(
                text,
                message_id=message_id if isinstance(message_id, str) and message_id else None,
            )
        elif ctype == "tool":
            if chunk.get("tool_name") == KIRO_TOOL_TODO_LIST:
                return
            title = (text.split("\n", 1)[0] or "Tool")[:120]
            self._tool_seq += 1
            gw_id = f"gw-{self._tool_seq}"
            original_id = chunk.get("tool_call_id")
            if isinstance(original_id, str) and original_id:
                # Cap the map so a runaway session cannot grow it unbounded.
                if len(self._tool_id_map) > 4096:
                    self._tool_id_map.clear()
                self._tool_id_map[(slot, original_id)] = gw_id
            await sink.send_tool_call(
                gw_id,
                title,
                "other",
                status="completed",
                locations=_sanitize_locations(chunk.get("locations")),
            )
        elif ctype == "tool_update":
            # Refinement of a prior tool_call — kiro-cli fills in rawInput on a
            # second event for streamed tools (fs_read). Emit a session/update
            # tool_call_update with the refined locations so the editor's
            # follow-along jumps to path:line. Skip silently if we never saw
            # the original (e.g. gateway restarted mid-turn): a stray update
            # with a wrong-shaped ID would land Zed on the wrong tool card.
            original_id = chunk.get("tool_call_id")
            if not isinstance(original_id, str) or not original_id:
                return
            mapped_id = self._tool_id_map.get((slot, original_id))
            if not mapped_id:
                return
            await sink.send_tool_call_update(
                mapped_id,
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

    @staticmethod
    def _question_form(question: dict[str, Any]) -> dict[str, Any] | None:
        text = question.get("question")
        options = question.get("options")
        if not isinstance(text, str) or not text or not isinstance(options, list):
            return None
        choices = [
            {
                "const": option["label"],
                "title": option["label"],
                "description": option["description"],
            }
            for option in options
            if isinstance(option, dict)
            and isinstance(option.get("label"), str)
            and option.get("label")
            and isinstance(option.get("description"), str)
        ]
        if not choices:
            return None
        if question.get("multiSelect"):
            answer: dict[str, Any] = {
                "type": "array",
                "title": text,
                "items": {"type": "string", "enum": [choice["const"] for choice in choices]},
                "minItems": 1,
            }
        else:
            answer = {"type": "string", "title": text, "oneOf": choices}
        schema: dict[str, Any] = {
            "type": "object",
            "properties": {"answer": answer},
            "required": ["answer"],
        }
        header = question.get("header")
        if isinstance(header, str) and header:
            schema["title"] = header
        return {"mode": "form", "message": text, "requestedSchema": schema}

    async def _dispatch_pending_elicitations(self, slot: str, sink: SessionSink) -> bool:
        """Schedule every unanswered canonical question for an elicitation-capable editor."""
        try:
            data = await self._get_json(
                f"/api/chat/slots/{quote(slot, safe='')}/questions", allow_fail=True
            )
        except Exception:
            logger.debug("pending question discovery failed for slot %s", slot, exc_info=True)
            return False
        records = data.get("questions") if isinstance(data, dict) else None
        if not isinstance(records, list):
            return False
        pending = False
        for record in records:
            if not isinstance(record, dict) or record.get("state") != "pending":
                continue
            card_id = record.get("question_id")
            questions = record.get("questions")
            answered = record.get("answers")
            if not isinstance(card_id, str) or not isinstance(questions, list):
                continue
            pending = True
            answered = answered if isinstance(answered, dict) else {}
            for index, question in enumerate(questions):
                if not isinstance(question, dict) or question.get("question") in answered:
                    continue
                form = self._question_form(question)
                if form is None or not sink.supports_elicitation:
                    continue
                key = (slot, card_id, index)
                if key in self._elicitation_tasks:
                    continue
                task = asyncio.create_task(
                    self._run_question_elicitation(key, question, form, sink),
                    name=f"acp-elicitation-{card_id}-{index}",
                )
                self._elicitation_tasks[key] = task

                def remove_task(
                    _task: asyncio.Task[None],
                    task_key: tuple[str, str, int] = key,
                ) -> None:
                    self._elicitation_tasks.pop(task_key, None)

                task.add_done_callback(remove_task)
        return pending

    async def _run_question_elicitation(
        self,
        key: tuple[str, str, int],
        question: dict[str, Any],
        form: dict[str, Any],
        sink: SessionSink,
    ) -> None:
        result = await sink.create_elicitation(form)
        if not result.accepted:
            return
        answer = result.content.get("answer") if result.content else None
        question_text = question.get("question")
        if not isinstance(question_text, str):
            return
        await self._stream_follow_up(
            (
                f"/api/chat/slots/{quote(key[0], safe='')}/questions/"
                f"{quote(key[1], safe='')}/answer?stream=1"
            ),
            {"answers": {question_text: answer}},
            key[0],
            sink,
        )

    async def _dispatch_options_elicitation(
        self, slot: str, options: list[str], sink: SessionSink
    ) -> None:
        if not sink.supports_elicitation or not options:
            return
        key = (slot, "options", 0)
        if key in self._elicitation_tasks:
            return
        form = {
            "mode": "form",
            "message": "Choose one or more options",
            "requestedSchema": {
                "type": "object",
                "properties": {
                    "answer": {
                        "type": "array",
                        "items": {"type": "string", "enum": options},
                        "minItems": 1,
                    }
                },
                "required": ["answer"],
            },
        }

        async def _answer_options() -> None:
            result = await sink.create_elicitation(form)
            content = result.content
            answer = content.get("answer") if isinstance(content, dict) else None
            if (
                result.accepted
                and isinstance(answer, list)
                and answer
                and all(isinstance(option, str) and option in options for option in answer)
            ):
                current = asyncio.current_task()
                if current is not None:
                    self._elicitation_follow_up_tasks.add(current)
                if self._elicitation_tasks.get(key) is current:
                    self._elicitation_tasks.pop(key, None)
                try:
                    await self._stream_follow_up(
                        "/api/chat",
                        {"message": ", ".join(dict.fromkeys(answer)), "slot": slot},
                        slot,
                        sink,
                    )
                finally:
                    if current is not None:
                        self._elicitation_follow_up_tasks.discard(current)

        task = asyncio.create_task(_answer_options(), name=f"acp-options-{slot}")
        self._elicitation_tasks[key] = task

        def remove_task(done: asyncio.Task[None]) -> None:
            if self._elicitation_tasks.get(key) is done:
                self._elicitation_tasks.pop(key, None)

        task.add_done_callback(remove_task)

    # ── HTTP helpers ──

    def _url(self, pathname: str) -> str:
        # Presigned tokens ride in a header (see _headers), never in the URL
        # query string, so they cannot leak into gateway access logs or proxies.
        return f"{self._base_url}{pathname}"

    def _read_secret(self) -> str:
        """Read the credential paired with the gateway generation being dialled."""
        if self._secret_path:
            try:
                return Path(self._secret_path).read_text(encoding="utf-8").strip()
            except OSError:
                return ""
        parsed = urlparse(self._base_url)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        return read_local_secret(port)

    async def _refresh_secret(self) -> None:
        """Refresh local auth off-loop, retaining the last usable credential."""
        if self._presigned_session:
            return
        try:
            secret = await asyncio.to_thread(self._read_secret)
        except (OSError, ValueError):
            secret = ""
        if secret:
            if self._secret and secret != self._secret:
                self._mcp_restore_needed.update(self._mcp_sessions)
            self._secret = secret
        elif not self._secret:
            logger.info("gateway authentication file unavailable")

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = dict(extra or {})
        headers.setdefault("Origin", self._log_origin)
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
            await self._refresh_secret()
            async with self._session.get(
                self._url(pathname),
                headers=self._headers(),
                allow_redirects=False,
            ) as resp:
                if resp.status >= 400:
                    if allow_fail:
                        return None
                    detail = redact_via_context((await resp.text())[:200])
                    raise AcpGatewayError(f"{pathname} -> {resp.status}: {detail}")
                return await resp.json()
        except (aiohttp.ClientError, aiohttp.ContentTypeError, ValueError) as exc:
            if allow_fail:
                return None
            safe_error = redact_via_context(str(exc))
            raise AcpGatewayError(f"{pathname} failed: {safe_error}") from exc

    async def _post_json(
        self, pathname: str, body: dict[str, Any], *, allow_fail: bool = False
    ) -> dict[str, Any] | None:
        try:
            await self._refresh_secret()
            async with self._session.post(
                self._url(pathname),
                headers=self._headers({"Content-Type": "application/json"}),
                json=body,
                allow_redirects=False,
            ) as resp:
                if resp.status >= 400:
                    if allow_fail:
                        logger.debug("%s -> %s", pathname, resp.status)
                        return None
                    detail = redact_via_context((await resp.text())[:200])
                    raise AcpGatewayError(f"{pathname} -> {resp.status}: {detail}")
                try:
                    return await resp.json()
                except aiohttp.ContentTypeError:
                    return {}
                except ValueError as exc:
                    raise AcpGatewayError(f"{pathname} returned malformed JSON") from exc
        except aiohttp.ClientError as exc:
            if allow_fail:
                logger.debug("%s failed: %s", pathname, redact_via_context(str(exc)))
                return None
            safe_error = redact_via_context(str(exc))
            raise AcpGatewayError(f"{pathname} failed: {safe_error}") from exc

    async def _post_json_mutation(
        self, pathname: str, body: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Retry an ambiguous transport failure without reapplying the mutation."""
        return await self._post_json_mutation_with_id(pathname, body, uuid.uuid4().hex)

    async def _post_json_mutation_with_id(
        self, pathname: str, body: dict[str, Any], mutation_id: str
    ) -> dict[str, Any] | None:
        """Apply an idempotent mutation using a caller-retained identity."""
        request_body = dict(body)
        request_body["mutation_id"] = mutation_id
        try:
            return await self._post_json(pathname, request_body)
        except AcpGatewayError as exc:
            if not isinstance(exc.__cause__, (aiohttp.ClientError, ValueError)):
                raise
        return await self._post_json(pathname, request_body)

    async def _set_project(self, slot: str, cwd: str) -> tuple[str | None, str | None]:
        """Assign the editor workspace and return its rollback receipt."""
        if not cwd:
            return None, None
        body: dict[str, Any] = {"project": cwd}
        if self._gateway_is_loopback:
            body["return_previous"] = True
        data = await self._post_json_mutation(
            f"/api/chat/slots/{quote(slot, safe='')}/project", body
        )
        generation = data.get("generation") if isinstance(data, dict) else None
        previous_project = data.get("previous_project") if isinstance(data, dict) else None
        if self._gateway_is_loopback and not isinstance(previous_project, str):
            raise AcpGatewayError("project assignment response omitted predecessor")
        return (
            generation if isinstance(generation, str) and generation else None,
            previous_project if isinstance(previous_project, str) else None,
        )

    async def _activate_session(self, slot: str, cwd: str) -> tuple[str | None, str | None]:
        await self._retry_pending_project_restore(slot)
        await self._post_json(f"/api/chat/slots/{quote(slot, safe='')}/resume", {"key": slot})
        generation, previous_project = await self._set_project(slot, cwd)
        if generation is not None:
            self._project_generations[slot] = generation
        else:
            self._project_generations.pop(slot, None)
        return generation, previous_project


class AcpGatewayError(RuntimeError):
    """The gateway was unreachable or refused a lifecycle request."""
