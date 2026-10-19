"""Async WakaTime REST client.

Structurally mirrors ``webex/client.py``: one lazily-created shared
``aiohttp.ClientSession`` behind a double-checked lock, a single ``_api`` core
that honors one ``429 Retry-After`` back-off then gives up, explicit per-call
``ClientTimeout``, and transport errors caught narrowly so a failed call
degrades to ``None``, an empty ``[]``/``{}``, or a ``0`` count instead of raising
into a turn. :meth:`verify` and the strict ``fetch_*`` reads are the exceptions:
they raise on a failed call, for callers that need a hard signal.

Auth has two modes, chosen by :meth:`_headers` from whether an OAuth access
token was given at construction. With a token the client sends
``Authorization: Bearer <token>`` on every call; without one it sends HTTP
Basic with the API key as the username (WakaTime's own scheme):
``Authorization: Basic base64(<api_key>:)``. WakaTime makes API keys write-only
on 1 November 2026 (https://wakatime.com/developers, Authentication), so after
that date reading stats, summaries, or durations needs an OAuth token; the key
still works for the heartbeat-send path and against a self-hosted
Wakapi/Hackatime backend that has no OAuth provider, which is why the Basic
fallback stays. A client built with a token therefore reads over OAuth; one
built without a token authenticates every call with the key. Either
credential is revealed only at the request site and never logged: a failing
request logs the exception TYPE only, because a WakaTime error message or a
request URL can carry it.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import platform
from typing import Any

import aiohttp

from kiro_crew import __version__ as _kirocrew_version

logger = logging.getLogger(__name__)

# Public WakaTime API. A self-hosted Wakapi/Hackatime backend overrides this via
# config.wakatime.api_base_url; both expose the same v1 surface.
DEFAULT_API_BASE = "https://wakatime.com/api/v1"

# WakaTime's own recommended cap on heartbeats per request. Callers batching
# more than this should chunk; the client does not silently drop the tail.
MAX_HEARTBEATS_PER_REQUEST = 25


def _user_agent() -> str:
    """A WakaTime-plugin-style User-Agent.

    WakaTime resolves the editor and operating system for its dashboard from
    this header, not from a heartbeat body field, so identifying Kiro Crew here
    is what keeps activity from landing under ``Unknown OS`` / ``Unknown``. The
    shape mirrors the official plugins,
    ``wakatime/<ver> (<os>-<release>) kirocrew/<ver>``, whose leading slot is
    the wakatime-cli version. Kiro Crew speaks the API directly and bundles no
    wakatime-cli, so both slots carry Kiro Crew's own package version.
    """
    try:
        os_name = platform.system() or "unknown"
        os_release = platform.release() or ""
    except Exception:
        os_name, os_release = "unknown", ""
    return f"wakatime/{_kirocrew_version} ({os_name}-{os_release}) kirocrew/{_kirocrew_version}"


_DEFAULT_TIMEOUT_SECS = 30

# Named ranges the stats endpoint rejects (it answers 400) but the summaries
# endpoint accepts, mapped to the summaries endpoint's own spelling.
_SINGLE_DAY_RANGES = {"today": "Today", "yesterday": "Yesterday"}


def sum_seconds_by_name(days: list[dict], key: str) -> list[tuple[str, float]]:
    """Sum one summaries breakdown (``languages``, ``projects``) by name.

    Returns ``(name, seconds)`` pairs, largest first. Shared by the stats fold
    below and the dashboard's billable-hours export.
    """
    totals: dict[str, float] = {}
    for day in days:
        for entry in day.get(key) or []:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            seconds = entry.get("total_seconds")
            if isinstance(name, str) and isinstance(seconds, (int, float)):
                totals[name] = totals.get(name, 0.0) + float(seconds)
    return sorted(totals.items(), key=lambda kv: kv[1], reverse=True)


def _merge_entries(days: list[dict], key: str) -> list[dict[str, Any]]:
    """The stats-shaped ``[{name, total_seconds}]`` form of one breakdown."""
    return [{"name": name, "total_seconds": secs} for name, secs in sum_seconds_by_name(days, key)]


def _stats_from_summaries(days: list[dict]) -> dict[str, Any]:
    """Fold daily summaries into the subset of the stats shape the view reads.

    A summary's ``grand_total`` counts the "Other" language; the stats
    endpoint's ``total_seconds`` does not, and carries the full figure in the
    ``*_including_other_language`` fields instead. Mirror that split so a day
    reads the same way as any other range.
    """
    languages = _merge_entries(days, "languages")
    total_with_other = 0.0
    for day in days:
        grand = day.get("grand_total")
        seconds = grand.get("total_seconds") if isinstance(grand, dict) else None
        if isinstance(seconds, (int, float)):
            total_with_other += float(seconds)
    other = sum(e["total_seconds"] for e in languages if e["name"] == "Other")
    total = max(total_with_other - other, 0.0)
    count = max(len(days), 1)
    return {
        "total_seconds": total,
        "total_seconds_including_other_language": total_with_other,
        "daily_average": total / count,
        "daily_average_including_other_language": total_with_other / count,
        "languages": languages,
        "projects": _merge_entries(days, "projects"),
    }


class WakaTimeAuthError(Exception):
    """Raised by :meth:`verify` on a 401/403, any other non-2xx status, or a transport failure.

    The message says which. A 401/403 is a rejected credential: a bad API key
    or, under OAuth, a rejected or expired bearer token. Unlike the plain read
    verbs, which degrade to an empty result, :meth:`verify` raises on each of
    these, so a caller validating a credential gets a hard signal.
    """


class WakaTimeUnavailableError(Exception):
    """Raised by the strict ``fetch_*`` read verbs when the upstream call fails.

    The plain read verbs (:meth:`get_summaries`, :meth:`get_stats`) swallow every
    ``_api`` failure into an empty result so an agent turn degrades quietly. A
    caller that must tell "the request failed" apart from "there is no data" — a
    billing export, where an empty result is a real figure — uses the strict verb
    instead and gets this exception on failure rather than a silent ``[]``.
    """


class WakaTimeClient:
    """Sends heartbeats and reads summaries/stats/durations from WakaTime."""

    def __init__(
        self,
        *,
        api_key: str,
        api_base: str = DEFAULT_API_BASE,
        oauth_access_token: str | None = None,
    ) -> None:
        self._api_key = api_key
        self._api_base = api_base.rstrip("/")
        # When set, every call authenticates with this bearer token instead of
        # the Basic key. A client is built with a token (OAuth, for reads after
        # the key cutoff) or without one (the key, for the heartbeat-send path
        # and self-hosted backends).
        self._oauth_access_token = oauth_access_token or None
        self._session: aiohttp.ClientSession | None = None
        self._session_lock: asyncio.Lock = asyncio.Lock()
        self._closed = False

    # ── Auth ──

    def _headers(self) -> dict[str, str]:
        # A configured OAuth token signs every call; otherwise the Basic key
        # does (the heartbeat-send path, pre-cutoff reads, and self-hosted
        # backends with no OAuth provider). Either credential is built here at
        # the request site and never stored in a logged structure.
        if self._oauth_access_token:
            auth = f"Bearer {self._oauth_access_token}"
        else:
            # WakaTime Basic auth: the API key is the username, password empty.
            token = base64.b64encode(f"{self._api_key}:".encode()).decode("ascii")
            auth = f"Basic {token}"
        return {"Authorization": auth, "User-Agent": _user_agent()}

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
        """Call a WakaTime endpoint. Returns the parsed JSON body on a 2xx
        (``{}`` for a 204 or a non-JSON body, None for an empty one), None on
        any error.

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
        """Confirm the credential works by reading the current user.

        Returns the user dict on success and raises :class:`WakaTimeAuthError`
        on a 401/403, any other non-2xx status, or a transport failure. A 2xx
        returns ``{}`` when its body is blank or parses to JSON without a
        ``data`` object; a body that does not parse lets the decoder's
        ``ValueError`` escape rather than :class:`WakaTimeAuthError`. For
        validating a newly-entered key before persisting it: unlike the read
        verbs, a failed call raises rather than returning an empty result.
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

    async def fetch_summaries(
        self, start: str, end: str, *, project: str | None = None
    ) -> list[dict]:
        """Like :meth:`get_summaries`, but RAISE on an upstream failure.

        ``_api`` returns ``None`` on any failure (non-2xx, transport error,
        timeout, exhausted 429) and the parsed body on a 2xx (``{}`` for a 204).
        This verb turns that ``None`` into :class:`WakaTimeUnavailableError` so a
        caller can tell "the summaries request failed" apart from "the range has
        no activity" — the two are indistinguishable through :meth:`get_summaries`,
        which is what makes a silent empty billing export possible. A successful
        summaries response always carries a ``data`` list (empty for a range with
        no activity), so anything without one — ``None`` from ``_api``, or the
        ``{}`` ``_api`` returns for a non-JSON 200 body (plausible from a
        self-hosted backend behind a proxy) — is treated as a failure and raises,
        never as a false-empty export.
        """
        params = {"start": start, "end": end}
        if project:
            params["project"] = project
        return await self._fetch_summary_days(params)

    async def _fetch_summary_days(self, params: dict[str, str]) -> list[dict]:
        """GET summaries for ``params``; raise unless the body has a data list."""
        result = await self._api("GET", "/users/current/summaries", params=params)
        data = result.get("data") if isinstance(result, dict) else None
        if not isinstance(data, list):
            raise WakaTimeUnavailableError("WakaTime summaries request failed or returned no data")
        return [d for d in data if isinstance(d, dict)]

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

    async def fetch_stats(self, wakatime_range: str = "last_7_days") -> dict[str, Any]:
        """Like :meth:`get_stats`, but RAISE on an upstream failure.

        Same contract as :meth:`fetch_summaries`: a successful stats response
        always carries a ``data`` dict (its fields empty for a range with no
        activity), so anything without one — ``None`` from ``_api`` on a failure,
        or the ``{}`` ``_api`` returns for a non-JSON 200 body — is a failure and
        raises :class:`WakaTimeUnavailableError` rather than degrading to a false
        empty result. Used where an empty payload must not be mistaken for real
        zero data.

        ``today`` and ``yesterday`` are served from the summaries endpoint,
        because the stats endpoint answers 400 for them.
        """
        summaries_range = _SINGLE_DAY_RANGES.get(wakatime_range)
        if summaries_range is not None:
            # The stats endpoint rejects single-day ranges with a 400, so a
            # day comes from summaries, folded into the stats shape.
            days = await self._fetch_summary_days({"range": summaries_range})
            return _stats_from_summaries(days)
        result = await self._api("GET", f"/users/current/stats/{wakatime_range}")
        data = result.get("data") if isinstance(result, dict) else None
        if not isinstance(data, dict):
            raise WakaTimeUnavailableError("WakaTime stats request failed or returned no data")
        return data

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
