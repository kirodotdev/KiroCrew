"""KiroCrewClient — async Python client for the KiroCrew Gateway.

Standalone package (no dependency on kiro_crew main package).
Uses aiohttp for HTTP and WebSocket communication.

Mirrors the TypeScript @kirocrew/client API surface.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Awaitable
from urllib.parse import quote, urlparse

import aiohttp

from kirocrew_client.errors import KiroCrewError, ErrorCode, http_error
from kirocrew_client.ws_client import WsClient

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "http://localhost:5476"
_DEFAULT_TIMEOUT = 30
_DEFAULT_MAX_RETRIES = 3
_DEFAULT_RETRY_BASE_DELAY = 1.0  # seconds
_MAX_BACKOFF = 30.0
_DEFAULT_MESSAGE_LIMIT = 40_000
_CONTEXT_BUFFER_LIMIT = 50
_RETRYABLE_METHODS = frozenset({"GET", "PUT", "DELETE"})

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})


def _is_loopback(url: str) -> bool:
    try:
        from urllib.parse import urlparse

        return urlparse(url).hostname in _LOOPBACK_HOSTS
    except Exception:
        return False


@asynccontextmanager
async def _network_errors(request: Any) -> AsyncIterator[aiohttp.ClientResponse]:
    """Translate aiohttp transport failures without retrying the request."""
    try:
        async with request as response:
            yield response
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise KiroCrewError(ErrorCode.NETWORK_ERROR, str(exc)) from exc


def _list_field(result: Any, key: str) -> list[dict[str, Any]]:
    """Read a list the Gateway returns bare or wrapped as ``{key: [...]}``."""
    if isinstance(result, dict):
        if key not in result:
            raise KiroCrewError(
                ErrorCode.SERVER_ERROR,
                f"unexpected response shape: missing '{key}'",
            )
        result = result[key]
    return result if isinstance(result, list) else []


def _compute_backoff(attempt: int, base_delay: float) -> float:
    if base_delay <= 0:
        return min(base_delay, _MAX_BACKOFF)
    if base_delay >= _MAX_BACKOFF:
        return _MAX_BACKOFF
    if attempt >= math.log2(_MAX_BACKOFF) - math.log2(base_delay):
        return _MAX_BACKOFF
    return base_delay * (2**attempt)


def _auth_cookie_name(base_url: str, cookie_port: int | None = None) -> str:
    """Name of the Gateway's access cookie for *base_url* or *cookie_port*.

    The Gateway only reads ``mc_token_<port>``, keyed by the port in the Host
    header the request carries (``token_auth._cookie_port_from_host``). A bare
    ``mc_token`` cookie is never read, so a token sent that way authenticates
    nothing. By default the client derives the key from the URL it dials. A URL
    without an explicit port uses its scheme default (443 for HTTPS/WSS, 80 for
    HTTP/WS). A reverse proxy may rewrite or omit the incoming Host port, so its
    public URL cannot always describe the key selected by the Gateway. Set
    *cookie_port* to the Gateway's listen port for that deployment.
    """
    if cookie_port is not None:
        return f"mc_token_{cookie_port}"
    parsed = urlparse(base_url)
    try:
        port = parsed.port
    except ValueError:
        port = None
    if port is None:
        port = 443 if parsed.scheme in ("https", "wss") else 80
    return f"mc_token_{port}"


#: ``GET``/``PUT /api/config/<key>`` exists only for these keys. Anything else
#: would be a request to a route the Gateway does not register.
GATEWAY_CONFIG_KEYS = frozenset({"kirocrew", "stt", "theme", "default-agent"})

#: Actions ``POST /api/approvals/{id}/{action}`` accepts.
_GLOBAL_APPROVAL_ACTIONS = frozenset({"approve", "reject", "reject_once"})

#: Actions ``POST /api/chat/slots/{slot}/approve`` accepts.
_SLOT_APPROVAL_ACTIONS = frozenset(
    {"approved", "rejected", "trust", "trust_reads", "trust_command", "trust_base", "yolo"}
)
_APPROVAL_MODES = frozenset({"normal", "trust_reads", "trust", "yolo"})
_SLOT_SCOPED_APPROVAL_MODES = frozenset({"normal", "trust_reads", "trust"})


def _read_app_secret(app_name: str) -> str:
    """Read the per-app secret from disk. Returns empty string if unavailable."""
    home = os.environ.get("KIROCREW_HOME", str(Path.home() / ".kiro" / "crew"))
    secret_path = Path(home) / "apps" / app_name / ".app_secret"
    try:
        return secret_path.read_text(encoding="utf-8").strip()
    except (OSError, FileNotFoundError):
        return ""


async def _exchange_app_token(base_url: str, app_name: str, secret: str) -> str:
    """Exchange an app secret for an app-scoped token via the Gateway."""
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{base_url}/api/apps/{quote(app_name, safe='')}/token",
            headers={"X-App-Secret": secret, "Content-Type": "application/json"},
        ) as resp:
            if not resp.ok:
                raise KiroCrewError(
                    ErrorCode.AUTH_EXPIRED,
                    f"App token exchange failed: HTTP {resp.status}",
                    status=resp.status,
                )
            data = await resp.json()
            return str(data.get("token", ""))


@dataclass
class ContextEntry:
    content: str
    source: str | None = None
    ephemeral: bool = True
    max_age: float | None = None  # seconds
    injected_at: float = field(default_factory=time.time)


class KiroCrewClient:
    """Async HTTP client for the KiroCrew Gateway.

    Usage::

        async with KiroCrewClient(app_name="my-app") as mc:
            ok = await mc.ping()
            slots = await mc.list_slots()
    """

    def __init__(
        self,
        *,
        base_url: str = "",
        token: str = "",
        app_name: str = "",
        timeout: int = _DEFAULT_TIMEOUT,
        max_retries: int = _DEFAULT_MAX_RETRIES,
        retry_base_delay: float = _DEFAULT_RETRY_BASE_DELAY,
        message_length_limit: int = _DEFAULT_MESSAGE_LIMIT,
        on_auth_expired: Callable[[], Awaitable[str]] | None = None,
        cookie_port: int | None = None,
    ):
        port = os.environ.get("KIROCREW_PORT", "5476")
        self.base_url = (base_url or f"http://localhost:{port}").rstrip("/")
        self.token = token
        self.app_name = app_name
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_base_delay = retry_base_delay
        self.message_length_limit = message_length_limit
        self.cookie_port = cookie_port
        self._session: aiohttp.ClientSession | None = None
        self._pending_buffer: list[ContextEntry] = []
        self._default_slot: str | None = None

        # Auto-token: if app_name is set and no explicit auth, read secret
        self._app_secret = ""
        has_explicit_auth = bool(token or on_auth_expired)
        if app_name and not has_explicit_auth:
            self._app_secret = _read_app_secret(app_name)

        if self._app_secret and not on_auth_expired:
            self._on_auth_expired = self._auto_refresh_token
        else:
            self._on_auth_expired = on_auth_expired

    async def _auto_refresh_token(self) -> str:
        """Exchange the on-disk app secret for a fresh token."""
        return await _exchange_app_token(self.base_url, self.app_name, self._app_secret)

    async def authenticate(self) -> bool:
        """Bootstrap app auth by exchanging the on-disk secret for a token.

        Call once after construction if no explicit token was provided.
        No-op if the client already has a token or no app secret is available.
        """
        if self.token or not self._app_secret or not self.app_name:
            return True
        try:
            self.token = await _exchange_app_token(self.base_url, self.app_name, self._app_secret)
            return True
        except Exception:
            return False

    async def __aenter__(self) -> KiroCrewClient:
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.timeout),
        )
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    def _ensure_session(self) -> aiohttp.ClientSession:
        if not self._session:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            )
        return self._session

    def _auth_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        headers.update(self._cookie_headers())
        return headers

    def _cookie_headers(self) -> dict[str, str]:
        if not self.token:
            return {}
        cookie_name = _auth_cookie_name(self.base_url, self.cookie_port)
        return {"Cookie": f"{cookie_name}={self.token}"}

    def _check_auth(self) -> None:
        if not self.token and not _is_loopback(self.base_url):
            raise KiroCrewError(
                ErrorCode.AUTH_REQUIRED,
                "Auth token required for remote Gateway connections",
            )

    async def _request(
        self,
        method: str,
        path: str,
        body: Any = None,
    ) -> Any:
        """Core request with retry logic."""
        self._check_auth()
        session = self._ensure_session()
        url = f"{self.base_url}{path}"
        last_error: KiroCrewError | None = None
        auth_refreshed = False
        attempt = 0
        method = method.upper()

        # A token refresh re-sends the request without spending a retry: with
        # ``max_retries=0`` a ``for`` loop ran out on the refresh itself and
        # surfaced a bare "Request failed" instead of the refreshed answer.
        while attempt <= self.max_retries:
            try:
                kwargs: dict[str, Any] = {"headers": self._auth_headers()}
                if body is not None:
                    kwargs["json"] = body

                async with session.request(method, url, **kwargs) as resp:
                    # Auth expired — try refresh once
                    if resp.status in (401, 403) and self._on_auth_expired and not auth_refreshed:
                        auth_refreshed = True
                        try:
                            self.token = await self._on_auth_expired()
                            continue
                        except Exception:
                            pass

                    if resp.ok:
                        ct = resp.headers.get("content-type", "")
                        if "application/json" in ct:
                            return await resp.json()
                        return {}

                    # Non-retryable 4xx (except 429)
                    if 400 <= resp.status < 500 and resp.status != 429:
                        text = await resp.text()
                        raise http_error(resp.status, text or None)

                    # Retryable
                    text = await resp.text()
                    last_error = http_error(resp.status, text or None)

                    if resp.status != 429 and method not in _RETRYABLE_METHODS:
                        raise last_error
                    if attempt < self.max_retries:
                        if resp.status == 429:
                            retry_after = resp.headers.get("Retry-After")
                            try:
                                delay = (
                                    float(retry_after)
                                    if retry_after
                                    else _compute_backoff(attempt, self.retry_base_delay)
                                )
                            except (ValueError, TypeError):
                                delay = _compute_backoff(attempt, self.retry_base_delay)
                        else:
                            delay = _compute_backoff(attempt, self.retry_base_delay)
                        await asyncio.sleep(delay)

            except KiroCrewError:
                # Non-retryable errors (4xx except 429) were raised directly
                # above — re-raise them immediately. Retryable errors (5xx,
                # 429) are stored in last_error and fall through to the
                # backoff sleep, so they never reach this branch.
                raise
            except Exception as exc:
                last_error = KiroCrewError(
                    ErrorCode.NETWORK_ERROR,
                    str(exc),
                )
                if method not in _RETRYABLE_METHODS:
                    raise last_error from exc
                if attempt < self.max_retries:
                    await asyncio.sleep(_compute_backoff(attempt, self.retry_base_delay))

            attempt += 1

        raise last_error or KiroCrewError(ErrorCode.NETWORK_ERROR, "Request failed")

    async def _get(self, path: str) -> Any:
        return await self._request("GET", path)

    async def _post(self, path: str, body: Any = None) -> Any:
        return await self._request("POST", path, body)

    async def _put(self, path: str, body: Any = None) -> Any:
        return await self._request("PUT", path, body)

    async def _patch(self, path: str, body: Any = None) -> Any:
        return await self._request("PATCH", path, body)

    async def _delete(self, path: str) -> Any:
        return await self._request("DELETE", path)

    # ── Connection ──

    async def ping(self) -> bool:
        try:
            await self._get("/api/status")
            return True
        except Exception:
            return False

    async def get_status(self) -> dict[str, Any]:
        return await self._get("/api/status")

    async def get_system_info(self) -> dict[str, Any]:
        return await self._get("/api/system")

    # ── Slots ──

    async def create_slot(self, name: str, agent: str = "") -> dict[str, Any]:
        body: dict[str, str] = {"name": name}
        if agent:
            body["agent"] = agent
        return await self._post("/api/chat/slots", body)

    async def list_slots(self) -> list[dict[str, Any]]:
        result = await self._get("/api/chat/slots")
        return result if isinstance(result, list) else []

    async def delete_slot(self, slot_id: str) -> None:
        await self._delete(f"/api/chat/slots/{quote(slot_id, safe='')}")

    async def get_slot_history(self, slot_id: str, limit: int = 50) -> dict[str, Any]:
        """Return the slot's detail with its most recent *limit* messages."""
        if limit < 1:
            raise KiroCrewError(ErrorCode.VALIDATION_ERROR, "limit must be >= 1")
        return await self._get(f"/api/chat/slots/{quote(slot_id, safe='')}?limit={limit}")

    async def stop_slot(self, slot_id: str, force: bool = False) -> dict[str, Any]:
        """Stop the slot's running turn; ``force=True`` hard-stops it."""
        qs = "?force=true" if force else ""
        return await self._post(f"/api/chat/slots/{quote(slot_id, safe='')}/stop{qs}")

    async def edit_resend(
        self, slot_id: str, content: str, *, index: int | None = None, ts: str | None = None
    ) -> dict[str, Any]:
        """Replace a user message (by ``index`` or ``ts``) and regenerate from it."""
        self._check_message_length(content)
        body: dict[str, Any] = {"content": content}
        if index is not None:
            body["index"] = index
        if ts is not None:
            body["ts"] = ts
        return await self._post(f"/api/chat/slots/{quote(slot_id, safe='')}/edit-resend", body)

    async def stream_chat(self, slot_id: str, message: str) -> AsyncIterator[dict[str, Any]]:
        """Send *message* to *slot_id* and yield each streamed chunk as a dict.

        ``POST /api/chat`` normally answers with Server-Sent Events: one
        ``data: <json>`` frame per chunk and a final ``data: [DONE]``. The
        iterator ends at ``[DONE]``; an SSE stream that closes before it raises
        ``NETWORK_ERROR``. A busy or orchestrator-control send instead returns
        one JSON receipt; the iterator yields that dict once and ends. A non-2xx
        answer raises :class:`KiroCrewError` before anything is yielded. An
        authentication refusal may refresh the token and replay once because
        the Gateway has not started a turn; no other response or stream failure
        is retried.
        """
        self._check_message_length(message)
        self._check_auth()
        session = self._ensure_session()
        auth_refreshed = False
        while True:
            completed = False
            async with _network_errors(
                session.post(
                    f"{self.base_url}/api/chat",
                    json={"message": message, "slot": slot_id},
                    headers=self._auth_headers(),
                    # A turn can stream for far longer than the per-request timeout.
                    timeout=aiohttp.ClientTimeout(total=None, sock_connect=self.timeout),
                )
            ) as resp:
                if resp.status in (401, 403) and self._on_auth_expired and not auth_refreshed:
                    auth_refreshed = True
                    try:
                        self.token = await self._on_auth_expired()
                        continue
                    except Exception:
                        pass
                if not resp.ok:
                    text = await resp.text()
                    raise http_error(resp.status, text or None)
                content_type = resp.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                if content_type == "application/json":
                    receipt = await resp.json()
                    yield receipt if isinstance(receipt, dict) else {"value": receipt}
                    return
                if content_type != "text/event-stream":
                    raise KiroCrewError(
                        ErrorCode.NETWORK_ERROR,
                        f"unexpected chat response content type: {content_type or 'missing'}",
                    )
                async for raw in resp.content:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[len("data:") :].strip()
                    if payload == "[DONE]":
                        completed = True
                        break
                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError:
                        chunk = {"text": payload}
                    yield chunk if isinstance(chunk, dict) else {"value": chunk}
            if not completed:
                raise KiroCrewError(
                    ErrorCode.NETWORK_ERROR,
                    "chat stream ended before the [DONE] frame",
                )
            return

    def _check_message_length(self, text: str) -> None:
        if len(text) > self.message_length_limit:
            raise KiroCrewError(
                ErrorCode.VALIDATION_ERROR,
                f"Message length {len(text)} exceeds limit {self.message_length_limit}",
            )

    async def send_message(self, slot_id: str, message: str) -> None:
        if len(message) > self.message_length_limit:
            raise KiroCrewError(
                ErrorCode.VALIDATION_ERROR,
                f"Message length {len(message)} exceeds limit {self.message_length_limit}",
            )
        if self._default_slot and slot_id == self._default_slot and self._pending_buffer:
            await self.flush_pending_context(slot_id)
        await self._post("/api/chat", {"message": message, "slot": slot_id})

    # ── Subagents ──

    async def spawn(self, task: str, agent: str = "") -> str:
        body: dict[str, str] = {"task": task}
        if agent:
            body["agent"] = agent
        result = await self._post("/api/spawn", body)
        return str(result.get("id", ""))

    async def spawn_many(self, tasks: list[str], agents: list[str] | None = None) -> list[str]:

        coros = [
            self.spawn(task, agents[i] if agents and i < len(agents) else "")
            for i, task in enumerate(tasks)
        ]
        return list(await asyncio.gather(*coros))

    async def list_subagents(self) -> list[dict[str, Any]]:
        return _list_field(await self._get("/api/spawn"), "agents")

    async def get_subagent_status(self, agent_id: str) -> dict[str, Any]:
        return await self._get(f"/api/spawn/{quote(agent_id, safe='')}")

    # ── Cron ──

    async def add_cron(self, name: str, **options: Any) -> dict[str, Any]:
        return await self._post("/api/crons", {"name": name, **options})

    async def list_crons(self) -> list[dict[str, Any]]:
        return _list_field(await self._get("/api/crons"), "jobs")

    async def update_cron(self, job_id: str, **options: Any) -> dict[str, Any]:
        return await self._patch(f"/api/crons/{quote(job_id, safe='')}", options)

    async def remove_cron(self, job_id: str) -> None:
        await self._delete(f"/api/crons/{quote(job_id, safe='')}")

    async def pause_cron(self, job_id: str) -> None:
        await self._post(f"/api/crons/{quote(job_id, safe='')}/enable", {"enabled": False})

    async def resume_cron(self, job_id: str) -> None:
        await self._post(f"/api/crons/{quote(job_id, safe='')}/enable", {"enabled": True})

    # ── Lessons ──

    async def add_lesson(self, rule: str, category: str, scope: str = "") -> None:
        await self._post("/api/lessons", {"rule": rule, "category": category, "scope": scope})

    async def list_lessons(self) -> list[dict[str, Any]]:
        return _list_field(await self._get("/api/lessons"), "lessons")

    async def remove_lesson(self, query: str) -> None:
        await self._delete_with_body("/api/lessons", {"rule": query})

    async def _delete_with_body(self, path: str, body: Any) -> Any:
        return await self._request("DELETE", path, body)

    # ── Messages ──

    async def send_notification(self, text: str, **options: Any) -> None:
        if len(text) > self.message_length_limit:
            raise KiroCrewError(
                ErrorCode.VALIDATION_ERROR,
                f"Message length {len(text)} exceeds limit {self.message_length_limit}",
            )
        await self._post("/api/send-message", {"text": text, **options})

    async def list_notifications(self) -> dict[str, Any]:
        """Return ``{"notifications": [...], "unread": int}``."""
        return await self._get("/api/notifications")

    async def ack_notification(self, ts: str) -> None:
        """Mark the notification with timestamp *ts* read."""
        if not ts:
            raise KiroCrewError(ErrorCode.VALIDATION_ERROR, "ts is required")
        await self._post("/api/notifications/ack", {"ts": ts})

    async def ack_all_notifications(self) -> None:
        await self._post("/api/notifications/ack-all")

    # ── Approvals ──

    async def list_approvals(self) -> list[dict[str, Any]]:
        """List pending tool approvals across all slots."""
        result = await self._get("/api/approvals")
        return result if isinstance(result, list) else []

    async def resolve_approval(
        self,
        request_id: str,
        action: str = "approve",
        slot_id: str = "",
        *,
        pattern: str = "",
    ) -> None:
        """Resolve a pending tool approval.

        Without *slot_id* this calls ``POST /api/approvals/{id}/{action}``,
        which accepts ``approve``, ``reject`` and ``reject_once``
        (``approved`` / ``rejected`` are accepted as aliases).

        With *slot_id* it calls the slot-scoped ``POST
        /api/chat/slots/{slot}/approve``, which takes ``approved`` /
        ``rejected`` (``approve`` / ``reject`` are sent as those, since the
        route denies any action it does not recognise) and the trust actions
        (``trust``, ``trust_reads``, ``trust_command``, ``trust_base``,
        ``yolo``). ``trust_command`` and
        ``trust_base`` require the pending card's server-derived *pattern*.
        The Gateway enforces which actions an app token may use.
        """
        if not request_id:
            raise KiroCrewError(ErrorCode.VALIDATION_ERROR, "request_id is required")
        if action in ("trust_command", "trust_base") and not pattern:
            raise KiroCrewError(
                ErrorCode.VALIDATION_ERROR,
                f"pattern is required for {action}",
            )
        if slot_id:
            # The slot route denies any action it does not recognise, so the
            # global spellings must be translated rather than passed through.
            slot_action = {"approve": "approved", "reject": "rejected"}.get(action, action)
            if slot_action not in _SLOT_APPROVAL_ACTIONS:
                raise KiroCrewError(
                    ErrorCode.VALIDATION_ERROR,
                    f"action must be one of {sorted(_SLOT_APPROVAL_ACTIONS)} with a slot_id",
                )
            body = {"action": slot_action, "request_id": request_id}
            if pattern:
                body["pattern"] = pattern
            await self._post(
                f"/api/chat/slots/{quote(slot_id, safe='')}/approve",
                body,
            )
            return
        http_action = {"approved": "approve", "rejected": "reject"}.get(action, action)
        if http_action not in _GLOBAL_APPROVAL_ACTIONS:
            raise KiroCrewError(
                ErrorCode.VALIDATION_ERROR,
                f"action must be one of {sorted(_GLOBAL_APPROVAL_ACTIONS)} without a slot_id",
            )
        await self._post(
            f"/api/approvals/{quote(request_id, safe='')}/{quote(http_action, safe='')}"
        )

    async def set_approval_mode(self, mode: str, slot_id: str = "") -> None:
        """Set an approval mode globally or on one slot when the mode supports it.

        ``normal``, ``trust_reads``, and ``trust`` may name a slot. ``yolo`` is
        process-global, so combining it with ``slot_id`` is rejected rather than
        silently widening the grant to every slot.
        """
        if mode not in _APPROVAL_MODES:
            raise KiroCrewError(
                ErrorCode.VALIDATION_ERROR,
                f"mode must be one of {sorted(_APPROVAL_MODES)}",
            )
        if slot_id and mode not in _SLOT_SCOPED_APPROVAL_MODES:
            raise KiroCrewError(
                ErrorCode.VALIDATION_ERROR,
                f"mode {mode!r} cannot be scoped to a slot",
            )
        body: dict[str, str] = {"mode": mode}
        if slot_id:
            body["slot"] = slot_id
        await self._post("/api/chat/mode", body)

    # ── Models ──

    async def list_models(self) -> list[dict[str, Any]]:
        return _list_field(await self._get("/api/models"), "models")

    async def set_slot_model(self, slot_id: str, model: str) -> dict[str, Any]:
        return await self._post(
            f"/api/chat/slots/{quote(slot_id, safe='')}/model", {"model": model}
        )

    # ── Gateway Config ──

    async def get_gateway_config(self, key: str) -> dict[str, Any]:
        """Read one Gateway config section; *key* is one of ``GATEWAY_CONFIG_KEYS``."""
        config_key = self._config_key(key)
        return await self._get(f"/api/config/{quote(config_key, safe='')}")

    async def set_gateway_config(self, key: str, value: dict[str, Any]) -> dict[str, Any]:
        config_key = self._config_key(key)
        return await self._put(f"/api/config/{quote(config_key, safe='')}", value)

    @staticmethod
    def _config_key(key: str) -> str:
        if key not in GATEWAY_CONFIG_KEYS:
            raise KiroCrewError(
                ErrorCode.VALIDATION_ERROR,
                f"unknown config key {key!r}; expected one of {sorted(GATEWAY_CONFIG_KEYS)}",
            )
        return key

    # ── Speech-to-text ──

    async def transcribe(
        self,
        audio: bytes,
        *,
        filename: str = "recording.webm",
        content_type: str = "audio/webm",
    ) -> str:
        """Transcribe *audio* with the Gateway's STT engine and return the text.

        The audio is sent as the ``audio`` field of a multipart form, which is
        the only field ``POST /api/stt/transcribe`` reads. Errors (STT off,
        audio too large or too long) raise :class:`KiroCrewError`.
        """
        self._check_auth()
        session = self._ensure_session()
        auth_refreshed = False
        while True:
            form = aiohttp.FormData()
            form.add_field("audio", audio, filename=filename, content_type=content_type)
            # FormData sets its own multipart Content-Type; keep only the cookie.
            async with _network_errors(
                session.post(
                    f"{self.base_url}/api/stt/transcribe",
                    data=form,
                    headers=self._cookie_headers(),
                )
            ) as resp:
                if resp.status in (401, 403) and self._on_auth_expired and not auth_refreshed:
                    auth_refreshed = True
                    try:
                        self.token = await self._on_auth_expired()
                        continue
                    except Exception:
                        pass
                if not resp.ok:
                    text = await resp.text()
                    raise http_error(resp.status, text or None)
                data = await resp.json()
                return str(data.get("text", "")) if isinstance(data, dict) else ""

    # ── WebSocket ──

    def create_ws(self, **options: Any) -> WsClient:
        """Build a :class:`WsClient` for ``/api/ws`` that reuses this client's auth.

        The WebSocket reads the token at every (re)connect. When the client
        has a token refresher (``on_auth_expired`` or an app secret), it runs
        before each reconnect, since a socket that dropped on an expired token
        would otherwise retry with the same dead token forever. Keyword options
        are passed through to :class:`WsClient` and override client defaults.
        """
        parsed = urlparse(self.base_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        ws_url = parsed._replace(scheme=scheme, path=parsed.path.rstrip("/") + "/api/ws").geturl()
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if self._on_auth_expired and "on_reconnect" not in options:
            options["on_reconnect"] = self._refresh_token_quietly
        kwargs = {"origin": origin, "get_headers": self._cookie_headers}
        kwargs.update(options)
        return WsClient(ws_url, **kwargs)

    async def _refresh_token_quietly(self) -> None:
        if not self._on_auth_expired:
            return
        try:
            self.token = await self._on_auth_expired()
        except Exception:
            logger.debug("token refresh before WS reconnect failed", exc_info=True)

    # ── MCP Servers ──

    async def list_mcp_servers(self) -> list[dict[str, Any]]:
        # The list route is ``GET /api/mcp`` (bare list); ``/api/mcp/servers``
        # only registers the per-name PUT/DELETE routes.
        result = await self._get("/api/mcp")
        return result if isinstance(result, list) else []

    async def register_mcp_server(
        self,
        name: str,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        if not name or not command:
            raise KiroCrewError(ErrorCode.VALIDATION_ERROR, "MCP server requires name and command")
        body: dict[str, Any] = {"command": command}
        if args:
            body["args"] = args
        if env:
            body["env"] = env
        await self._put(f"/api/mcp/servers/{quote(name, safe='')}", body)

    async def remove_mcp_server(self, name: str) -> None:
        await self._delete(f"/api/mcp/servers/{quote(name, safe='')}")

    # ── Agent Runtime ──

    async def dispatch_agent(self, agent: str, prompt: str) -> dict[str, Any]:
        return await self._post(
            "/api/chat", {"message": prompt, "agent": agent, "app": self.app_name}
        )

    async def dispatch_agent_async(self, agent: str, prompt: str) -> str:
        result = await self._post("/api/spawn", {"task": prompt, "agent": agent})
        return str(result.get("id", ""))

    async def get_task_result(self, task_id: str) -> dict[str, Any]:
        return await self._get(f"/api/spawn/{quote(task_id, safe='')}")

    # ── App Storage ──

    def get_app_data_dir(self) -> Path:
        home = os.environ.get("KIROCREW_HOME", str(Path.home() / ".kiro" / "crew"))
        return Path(home) / "apps" / (self.app_name or "unknown") / "data"

    async def get_app_config(self) -> dict[str, Any]:
        return await self._get(f"/api/apps/{quote(self.app_name, safe='')}/config")

    async def set_app_config(self, config: dict[str, Any]) -> None:
        await self._put(f"/api/apps/{quote(self.app_name, safe='')}/config", config)

    # ── Memory ──

    async def memory_search(self, query: str, top_k: int = 8) -> list[dict[str, Any]]:
        # The route's page-size parameter is ``limit`` (capped at 50 server-side);
        # ``top_k`` is kept as this method's name for it.
        result = await self._get(
            f"/api/memory/episodic/search?q={quote(query, safe='')}&limit={top_k}"
        )
        return _list_field(result, "results")

    # ── Context Injection ──

    async def inject_context(
        self,
        slot_id: str | None,
        content: str,
        *,
        source: str | None = None,
        ephemeral: bool = True,
        max_age: float | None = None,
    ) -> None:
        entry = ContextEntry(
            content=content,
            source=source,
            ephemeral=ephemeral,
            max_age=max_age,
            injected_at=time.time(),
        )
        if slot_id is None:
            self._pending_buffer.append(entry)
            if len(self._pending_buffer) > _CONTEXT_BUFFER_LIMIT:
                self._pending_buffer.pop(0)
            return

        await self._post(
            f"/api/chat/slots/{quote(slot_id, safe='')}/context",
            {
                "content": content,
                "source": source,
                "ephemeral": ephemeral,
                "maxAge": max_age,
            },
        )

    async def flush_pending_context(self, slot_id: str) -> None:
        now = time.time()
        to_flush = [
            e for e in self._pending_buffer if e.max_age is None or e.injected_at + e.max_age >= now
        ]
        self._pending_buffer.clear()
        failed: list[Any] = []
        last_exc: Exception | None = None
        for entry in to_flush:
            try:
                await self._post(
                    f"/api/chat/slots/{quote(slot_id, safe='')}/context",
                    {
                        "content": entry.content,
                        "source": entry.source,
                        "ephemeral": entry.ephemeral,
                        "maxAge": entry.max_age,
                    },
                )
            except Exception as exc:
                last_exc = exc
                failed.append(entry)
        self._pending_buffer.extend(failed)
        if last_exc:
            raise last_exc

    def set_default_slot(self, slot_id: str) -> None:
        self._default_slot = slot_id

    @property
    def pending_context_count(self) -> int:
        return len(self._pending_buffer)
