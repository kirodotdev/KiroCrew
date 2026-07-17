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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Awaitable

import aiohttp

from kirocrew_client.errors import KiroCrewError, ErrorCode, http_error

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "http://localhost:5476"
_DEFAULT_TIMEOUT = 30
_DEFAULT_MAX_RETRIES = 3
_DEFAULT_RETRY_BASE_DELAY = 1.0  # seconds
_MAX_BACKOFF = 30.0
_DEFAULT_MESSAGE_LIMIT = 40_000
_CONTEXT_BUFFER_LIMIT = 50

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})


def _is_loopback(url: str) -> bool:
    try:
        from urllib.parse import urlparse
        return urlparse(url).hostname in _LOOPBACK_HOSTS
    except Exception:
        return False


def _compute_backoff(attempt: int, base_delay: float) -> float:
    return min(base_delay * (2 ** attempt), _MAX_BACKOFF)


def _read_app_secret(app_name: str) -> str:
    """Read the per-app secret from disk. Returns empty string if unavailable."""
    home = os.environ.get("KIROCREW_HOME", str(Path.home() / ".kirocrew"))
    secret_path = Path(home) / "apps" / app_name / ".app_secret"
    try:
        return secret_path.read_text(encoding="utf-8").strip()
    except (OSError, FileNotFoundError):
        return ""


async def _exchange_app_token(base_url: str, app_name: str, secret: str) -> str:
    """Exchange an app secret for an app-scoped token via the Gateway."""
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{base_url}/api/apps/{app_name}/token",
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
    ):
        port = os.environ.get("KIROCREW_PORT", "5476")
        self.base_url = (base_url or f"http://localhost:{port}").rstrip("/")
        self.token = token
        self.app_name = app_name
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_base_delay = retry_base_delay
        self.message_length_limit = message_length_limit
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
            self.token = await _exchange_app_token(
                self.base_url, self.app_name, self._app_secret
            )
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
        if self.token:
            headers["Cookie"] = f"mc_token={self.token}"
        return headers

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

        for attempt in range(self.max_retries + 1):
            try:
                kwargs: dict[str, Any] = {"headers": self._auth_headers()}
                if body is not None:
                    kwargs["json"] = body

                async with session.request(method, url, **kwargs) as resp:
                    # Auth expired — try refresh once
                    if resp.status in (401, 403) and self._on_auth_expired and attempt == 0:
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

                    if attempt < self.max_retries:
                        if resp.status == 429:
                            retry_after = resp.headers.get("Retry-After")
                            try:
                                delay = float(retry_after) if retry_after else _compute_backoff(attempt, self.retry_base_delay)
                            except (ValueError, TypeError):
                                delay = _compute_backoff(attempt, self.retry_base_delay)
                        else:
                            delay = _compute_backoff(attempt, self.retry_base_delay)
                        await asyncio.sleep(delay)

            except KiroCrewError as e:
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
                if attempt < self.max_retries:
                    await asyncio.sleep(_compute_backoff(attempt, self.retry_base_delay))

        raise last_error or KiroCrewError(ErrorCode.NETWORK_ERROR, "Request failed")

    async def _get(self, path: str) -> Any:
        return await self._request("GET", path)

    async def _post(self, path: str, body: Any = None) -> Any:
        return await self._request("POST", path, body)

    async def _put(self, path: str, body: Any = None) -> Any:
        return await self._request("PUT", path, body)

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
        await self._delete(f"/api/chat/slots/{slot_id}")

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
        result = await self._get("/api/spawn")
        return result if isinstance(result, list) else []

    async def get_subagent_status(self, agent_id: str) -> dict[str, Any]:
        return await self._get(f"/api/spawn/{agent_id}")

    # ── Cron ──

    async def add_cron(self, name: str, **options: Any) -> dict[str, Any]:
        return await self._post("/api/crons", {"name": name, **options})

    async def list_crons(self) -> list[dict[str, Any]]:
        result = await self._get("/api/crons")
        return result if isinstance(result, list) else []

    async def update_cron(self, job_id: str, **options: Any) -> dict[str, Any]:
        return await self._put(f"/api/crons/{job_id}", options)

    async def remove_cron(self, job_id: str) -> None:
        await self._delete(f"/api/crons/{job_id}")

    async def pause_cron(self, job_id: str) -> None:
        await self._post(f"/api/crons/{job_id}/enable", {"enabled": False})

    async def resume_cron(self, job_id: str) -> None:
        await self._post(f"/api/crons/{job_id}/enable", {"enabled": True})

    # ── Lessons ──

    async def add_lesson(self, rule: str, category: str, scope: str = "") -> None:
        await self._post("/api/lessons", {"rule": rule, "category": category, "scope": scope})

    async def list_lessons(self) -> list[dict[str, Any]]:
        result = await self._get("/api/lessons")
        return result if isinstance(result, list) else []

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

    # ── MCP Servers ──

    async def list_mcp_servers(self) -> list[dict[str, Any]]:
        result = await self._get("/api/mcp/servers")
        return result if isinstance(result, list) else []

    async def register_mcp_server(
        self, name: str, command: str, args: list[str] | None = None, env: dict[str, str] | None = None,
    ) -> None:
        if not name or not command:
            raise KiroCrewError(ErrorCode.VALIDATION_ERROR, "MCP server requires name and command")
        body: dict[str, Any] = {"command": command}
        if args:
            body["args"] = args
        if env:
            body["env"] = env
        await self._put(f"/api/mcp/servers/{name}", body)

    async def remove_mcp_server(self, name: str) -> None:
        await self._delete(f"/api/mcp/servers/{name}")

    # ── Agent Runtime ──

    async def dispatch_agent(self, agent: str, prompt: str) -> dict[str, Any]:
        return await self._post("/api/chat", {"message": prompt, "agent": agent, "app": self.app_name})

    async def dispatch_agent_async(self, agent: str, prompt: str) -> str:
        result = await self._post("/api/spawn", {"task": prompt, "agent": agent})
        return str(result.get("id", ""))

    async def get_task_result(self, task_id: str) -> dict[str, Any]:
        return await self._get(f"/api/spawn/{task_id}")

    # ── App Storage ──

    def get_app_data_dir(self) -> Path:
        home = os.environ.get("KIROCREW_HOME", str(Path.home() / ".kirocrew"))
        return Path(home) / "apps" / (self.app_name or "unknown") / "data"

    async def get_app_config(self) -> dict[str, Any]:
        return await self._get(f"/api/apps/{self.app_name}/config")

    async def set_app_config(self, config: dict[str, Any]) -> None:
        await self._put(f"/api/apps/{self.app_name}/config", config)

    # ── Memory ──

    async def memory_search(self, query: str, top_k: int = 8) -> list[dict[str, Any]]:
        from urllib.parse import quote
        result = await self._get(f"/api/memory/episodic/search?q={quote(query)}&top_k={top_k}")
        return result if isinstance(result, list) else []

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
            content=content, source=source, ephemeral=ephemeral,
            max_age=max_age, injected_at=time.time(),
        )
        if slot_id is None:
            self._pending_buffer.append(entry)
            if len(self._pending_buffer) > _CONTEXT_BUFFER_LIMIT:
                self._pending_buffer.pop(0)
            return

        await self._post(f"/api/chat/slots/{slot_id}/context", {
            "content": content, "source": source,
            "ephemeral": ephemeral, "maxAge": max_age,
        })

    async def flush_pending_context(self, slot_id: str) -> None:
        now = time.time()
        to_flush = [
            e for e in self._pending_buffer
            if e.max_age is None or e.injected_at + e.max_age >= now
        ]
        self._pending_buffer.clear()
        failed: list[Any] = []
        last_exc: Exception | None = None
        for entry in to_flush:
            try:
                await self._post(f"/api/chat/slots/{slot_id}/context", {
                    "content": entry.content, "source": entry.source,
                    "ephemeral": entry.ephemeral, "maxAge": entry.max_age,
                })
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
