"""Async WakaTime REST client.

Structurally mirrors ``webex/client.py``: one lazily-created shared
``aiohttp.ClientSession`` behind a double-checked lock, a single ``_api`` core
that honors one ``429 Retry-After`` back-off then gives up, explicit per-call
``ClientTimeout``, and transport errors caught narrowly so a failed call
degrades to ``None``/``[]`` instead of raising into a turn.

Auth is HTTP Basic with the API key as the username (WakaTime's scheme):
``Authorization: Basic base64(<api_key>:)``. The key is revealed only at the
request site and never logged — a failing request logs the exception TYPE only,
because a WakaTime error message or a request URL can carry the key.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

# Public WakaTime API. A self-hosted Wakapi/Hackatime backend overrides this via
# config.wakatime.api_base_url; both expose the same v1 surface.
DEFAULT_API_BASE = "https://wakatime.com/api/v1"

# WakaTime's own recommended cap on heartbeats per request. Callers batching
# more than this should chunk; the client does not silently drop the tail.
MAX_HEARTBEATS_PER_REQUEST = 25

_DEFAULT_TIMEOUT_SECS = 30


class WakaTimeAuthError(Exception):
    """Raised by :meth:`verify` when the API key is rejected (401/403).

    Distinct from a transport failure (which returns ``None``): a wrong key is a
    configuration problem the caller should surface, not retry.
    """


class WakaTimeClient:
    """Sends heartbeats and reads summaries/stats/durations from WakaTime."""

    def __init__(self, *, api_key: str, api_base: str = DEFAULT_API_BASE) -> None:
        self._api_key = api_key
        self._api_base = api_base.rstrip("/")
        self._session: aiohttp.ClientSession | None = None
        self._session_lock: asyncio.Lock = asyncio.Lock()
        self._closed = False

    # ── Auth ──

    def _headers(self) -> dict[str, str]:
        # WakaTime Basic auth: the API key is the username, password empty.
        token = base64.b64encode(f"{self._api_key}:".encode()).decode("ascii")
        return {"Authorization": f"Basic {token}"}

    # ── Session lifecycle ──

    async def _ensure_session(self) -> aiohttp.ClientSession:
        """Return the shared ClientSession, creating it once on demand.

        Double-checked lock so concurrent callers cannot each build a session
        and leak one unclosed. Mirrors WebexClient/TelegramClient.
        """
        if self._closed:
            raise RuntimeError("WakaTimeClient is closed")
        if self._session is None or self._session.closed:
            async with self._session_lock:
                if self._closed:
                    raise RuntimeError("WakaTimeClient is closed")
                if self._session is None or self._session.closed:
                    self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        """Close the shared session. Idempotent."""
        self._closed = True
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    # ── REST core ──

    async def _api(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        payload: Any | None = None,
        timeout: int = _DEFAULT_TIMEOUT_SECS,
    ) -> Any:
        """Call a WakaTime endpoint. Returns the parsed JSON body on success
        (``{}`` for an empty 2xx), None on any error.

        Honors a single 429 ``Retry-After`` back-off, clamped, then gives up —
        mirroring WebexClient._api.
        """
        session = await self._ensure_session()
        url = f"{self._api_base}{path}"
        for attempt in range(2):
            try:
                async with session.request(
                    method,
                    url,
                    params=params,
                    json=payload,
                    headers=self._headers(),
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as resp:
                    if resp.status == 429 and attempt == 0:
                        try:
                            retry_after = float(resp.headers.get("Retry-After", "1"))
                        except (TypeError, ValueError):
                            retry_after = 1.0
                        await asyncio.sleep(min(max(retry_after, 0.5), 10.0))
                        continue
                    if 200 <= resp.status < 300:
                        if resp.status == 204:
                            return {}
                        try:
                            return await resp.json(content_type=None)
                        except ValueError:
                            return {}
                    # Never log the response body or the URL — either can carry
                    # the API key. Status only.
                    logger.warning("WakaTime API %s %s failed: http=%s", method, path, resp.status)
                    return None
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                logger.warning(
                    "WakaTime API %s %s transport error: %s", method, path, type(exc).__name__
                )
                return None
        return None

    # ── Public verbs ──

    async def verify(self) -> dict[str, Any]:
        """Confirm the key works by reading the current user. Raises
        :class:`WakaTimeAuthError` on 401/403, returns the user dict on success.

        Used to validate a newly-entered key before persisting it. Distinct from
        the read verbs: a caller wants a hard signal that the key is valid, not a
        silent empty result.
        """
        session = await self._ensure_session()
        url = f"{self._api_base}/users/current"
        try:
            async with session.get(
                url,
                headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=_DEFAULT_TIMEOUT_SECS),
            ) as resp:
                if resp.status in (401, 403):
                    raise WakaTimeAuthError("WakaTime rejected the API key")
                if 200 <= resp.status < 300:
                    body = await resp.json(content_type=None)
                    data = body.get("data") if isinstance(body, dict) else None
                    return data if isinstance(data, dict) else {}
                logger.warning("WakaTime verify failed: http=%s", resp.status)
                raise WakaTimeAuthError(f"WakaTime verify returned http={resp.status}")
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.warning("WakaTime verify transport error: %s", type(exc).__name__)
            raise WakaTimeAuthError("WakaTime verify could not reach the API") from exc

    async def send_heartbeats(self, heartbeats: list[dict[str, Any]]) -> int:
        """POST a batch of heartbeats. Returns the count the API accepted (0 on
        failure). Callers must chunk to :data:`MAX_HEARTBEATS_PER_REQUEST`.
        """
        if not heartbeats:
            return 0
        result = await self._api("POST", "/users/current/heartbeats.bulk", payload=heartbeats)
        if not isinstance(result, dict):
            return 0
        responses = result.get("responses")
        if not isinstance(responses, list):
            return 0
        # Each response is [body, status]; count the 201/202 accepted rows.
        return sum(
            1 for r in responses if isinstance(r, list) and len(r) == 2 and r[1] in (201, 202)
        )

    async def get_summaries(
        self, start: str, end: str, *, project: str | None = None
    ) -> list[dict]:
        """GET daily summaries between ``start`` and ``end`` (YYYY-MM-DD).

        Returns the ``data`` list (one entry per day), or ``[]`` on error.
        """
        params = {"start": start, "end": end}
        if project:
            params["project"] = project
        result = await self._api("GET", "/users/current/summaries", params=params)
        if isinstance(result, dict):
            data = result.get("data")
            if isinstance(data, list):
                return [d for d in data if isinstance(d, dict)]
        return []

    async def get_stats(self, wakatime_range: str = "last_7_days") -> dict[str, Any]:
        """GET aggregate stats for a named range (e.g. ``last_7_days``).

        Returns the ``data`` dict (languages, projects, totals), or ``{}``.
        """
        result = await self._api("GET", f"/users/current/stats/{wakatime_range}")
        if isinstance(result, dict):
            data = result.get("data")
            if isinstance(data, dict):
                return data
        return {}

    async def get_durations(self, date: str, *, project: str | None = None) -> list[dict]:
        """GET the duration blocks for a single ``date`` (YYYY-MM-DD).

        Returns the ``data`` list, or ``[]`` on error.
        """
        params = {"date": date}
        if project:
            params["project"] = project
        result = await self._api("GET", "/users/current/durations", params=params)
        if isinstance(result, dict):
            data = result.get("data")
            if isinstance(data, list):
                return [d for d in data if isinstance(d, dict)]
        return []
