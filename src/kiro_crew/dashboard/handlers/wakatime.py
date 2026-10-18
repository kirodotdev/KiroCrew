"""Dashboard HTTP handlers for the WakaTime integration.

Two read endpoints, both single-owner (no per-user identity: this is the local
dashboard owner's own configured WakaTime account):

- ``GET /api/wakatime/stats`` — aggregate stats for a named range, for the
  productivity view (coding time, language/project breakdown).
- ``GET /api/wakatime/export`` — hours grouped by project over a date range,
  as CSV (a download) or JSON, for billable-hours export.

When the integration is disabled or unconfigured the endpoints return a 200 with
``{"configured": false}`` rather than an error, so the frontend renders an
ordinary "connect WakaTime" empty state instead of an error banner. When the
range is empty but the upstream call itself failed, they return a 502 rather
than a false-empty result, so an outage is never mistaken for real zero data.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import secrets
import urllib.parse
from datetime import date
from typing import TYPE_CHECKING, Any

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.wakatime.client import WakaTimeClient

logger = logging.getLogger(__name__)

# Ranges the WakaTime stats endpoint accepts. Guard the query param against this
# allowlist so an arbitrary value is never interpolated into the upstream path.
_ALLOWED_RANGES = frozenset(
    {
        "last_7_days",
        "last_30_days",
        "last_6_months",
        "last_year",
        "today",
        "yesterday",
    }
)
_DEFAULT_RANGE = "last_7_days"

# YYYY-MM-DD is the only date shape WakaTime's summaries endpoint accepts.


def _valid_date(value: str) -> bool:
    """True only for a real calendar date in YYYY-MM-DD form.

    A shape-only regex accepts 2026-02-30, a plausible operator typo, which the
    upstream then rejects into an empty result — a false zero-hour export.
    fromisoformat validates both the format and the calendar.
    """
    try:
        # fromisoformat also accepts compact forms like "20260901" (3.11+), so
        # round-trip through isoformat() to hold the promised YYYY-MM-DD shape.
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _upstream_error() -> web.Response:
    return web.json_response(
        {
            "error": "WakaTime is unreachable or the API key was rejected",
            "code": "upstream_unavailable",
        },
        status=502,
    )


# The callback path WakaTime redirects back to. It is a fixed route, so the
# redirect URI is the dashboard's own origin plus this path. The same value is
# sent at authorize, exchange, and refresh; WakaTime requires all three match.
_OAUTH_CALLBACK_PATH = "/api/wakatime/oauth/callback"


def _oauth_redirect_uri(request: web.Request) -> str:
    """Build the absolute callback URL WakaTime redirects back to.

    WakaTime requires this to match the redirect URI registered on the OAuth
    app exactly, so the configured ``dashboard.url`` origin is authoritative
    when set: it is the stable, externally-reachable address a maintainer
    registered, not whatever a single request happened to arrive on.

    When ``dashboard.url`` is unset (the common local case), fall back to the
    request's own origin. The ``Host`` header is already validated against the
    dashboard allowlist by the DNS-rebinding barrier middleware before any
    handler runs, so it is not attacker-controlled here; the origin is assembled
    through ``urlunparse`` rather than string interpolation so the shape is
    explicit and the parts cannot smuggle extra URL structure.
    """
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.dashboard.urls import dashboard_origin

    configured = dashboard_origin((KiroCrewConfig.load().dashboard.url or "").strip())
    if configured:
        return configured.rstrip("/") + _OAUTH_CALLBACK_PATH

    proto = (request.headers.get("X-Forwarded-Proto") or request.scheme).split(",")[0].strip()
    host = request.headers.get("X-Forwarded-Host") or request.host
    return urllib.parse.urlunparse((proto, host, _OAUTH_CALLBACK_PATH, "", "", ""))


async def _build_authed_client(request: web.Request) -> "WakaTimeClient | None":
    """Build a WakaTime client with a fresh OAuth token when one is stored.

    Resolves (and refreshes if due) the OAuth access token, then builds the
    client with it. Falls back to the API key when no token is stored or a
    refresh fails. Returns ``None`` when the integration is off or has no key,
    matching ``service.build_client``.
    """
    from kiro_crew.wakatime import service

    redirect_uri = _oauth_redirect_uri(request)
    token = await service.resolve_oauth_access_token(redirect_uri)
    return await asyncio.to_thread(service.build_client, None, oauth_access_token=token or None)


def _oauth_state_cookie_name() -> str:
    return "wakatime_oauth_state"


async def api_wakatime_oauth_authorize(request: web.Request) -> web.Response:
    """POST /api/wakatime/oauth/authorize: start the OAuth read-auth flow.

    Owner-only. Returns the WakaTime authorize URL for the frontend to open,
    and sets a short-lived, HttpOnly state cookie the callback checks against
    the value WakaTime echoes back, so a forged callback cannot complete.
    """
    from kiro_crew.dashboard.handlers_system import _require_owner

    denied = await _require_owner(request, "wakatime.oauth.authorize")
    if denied is not None:
        return denied

    from kiro_crew.dashboard.origin import is_https_request
    from kiro_crew.wakatime import oauth

    redirect_uri = _oauth_redirect_uri(request)
    try:
        url, state = await asyncio.to_thread(oauth.build_authorize_url, redirect_uri)
    except oauth.WakaTimeOAuthError as exc:
        return web.json_response(
            {"error": "WakaTime OAuth is not configured", "code": exc.code}, status=400
        )

    response = web.json_response({"authorize_url": url})
    response.set_cookie(
        _oauth_state_cookie_name(),
        state,
        max_age=600,
        httponly=True,
        # request.scheme reads "http" behind a TLS-terminating proxy that forwards
        # plain HTTP to the loopback gateway, which would drop Secure and let the
        # state cookie travel in the clear. is_https_request honours a loopback-peer
        # X-Forwarded-Proto so the cookie stays Secure over a tunnelled HTTPS browser.
        secure=is_https_request(request),
        samesite="Lax",
        path=_OAUTH_CALLBACK_PATH,
    )
    return response


async def api_wakatime_oauth_callback(request: web.Request) -> web.Response:
    """GET /api/wakatime/oauth/callback?code=&state= : finish the flow.

    Owner-only. Verifies the state against the cookie, exchanges the code for
    tokens, and stores them. Redirects back to the settings page on success or
    with an error marker on failure, so the browser lands on a real page rather
    than a JSON blob.
    """
    from kiro_crew.dashboard.handlers_system import _require_owner

    denied = await _require_owner(request, "wakatime.oauth.callback")
    if denied is not None:
        return denied

    from kiro_crew.wakatime import oauth

    code = request.query.get("code", "")
    state = request.query.get("state", "")
    expected = request.cookies.get(_oauth_state_cookie_name(), "")
    # Constant-time compare: the state is a CSRF token, so a timing side channel
    # on the check is the kind of leak this parameter exists to prevent.
    if not code or not state or not expected or not secrets.compare_digest(state, expected):
        raise _oauth_redirect_back(request, ok=False, code="state_mismatch")

    redirect_uri = _oauth_redirect_uri(request)
    try:
        tokens = await oauth.exchange_code(code, redirect_uri)
        await asyncio.to_thread(oauth.store_tokens, tokens)
    except oauth.WakaTimeOAuthError as exc:
        raise _oauth_redirect_back(request, ok=False, code=exc.code)

    redirect = _oauth_redirect_back(request, ok=True, code=None)
    redirect.del_cookie(_oauth_state_cookie_name(), path=_OAUTH_CALLBACK_PATH)
    raise redirect


async def api_wakatime_oauth_disconnect(request: web.Request) -> web.Response:
    """POST /api/wakatime/oauth/disconnect: revoke and clear stored tokens.

    Owner-only. Revokes at the provider (best effort) then deletes the local
    tokens and resets the expiry, so the integration falls back to the API key.
    The local clear happens even if the provider revoke fails.
    """
    from kiro_crew.dashboard.handlers_system import _require_owner

    denied = await _require_owner(request, "wakatime.oauth.disconnect")
    if denied is not None:
        return denied

    from kiro_crew.wakatime import oauth

    # Hold the token-mutation lock across revoke+clear so a refresh running for
    # a concurrent read cannot re-store tokens between them. clear_tokens runs
    # in a finally so a provider revoke that raises still leaves no local token.
    async with oauth.token_mutation_lock():
        try:
            await oauth.revoke_tokens()
        finally:
            await asyncio.to_thread(oauth.clear_tokens)
    return web.json_response({"disconnected": True})


async def api_wakatime_oauth_status(request: web.Request) -> web.Response:
    """GET /api/wakatime/oauth/status: report the OAuth connection state.

    Owner-only. ``connected`` is true when an access token is stored, so the
    settings UI shows a Disconnect control; ``client_configured`` is true when
    an OAuth client id is set, so the UI can prompt for app setup before
    offering Connect. Reports no token material, only these two booleans.
    """
    from kiro_crew.dashboard.handlers_system import _require_owner

    denied = await _require_owner(request, "wakatime.oauth.status")
    if denied is not None:
        return denied

    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.wakatime import oauth

    connected = bool(await asyncio.to_thread(oauth.read_access_token))
    client_configured = bool((KiroCrewConfig.load().wakatime.oauth_client_id or "").strip())
    return web.json_response({"connected": connected, "client_configured": client_configured})


def _oauth_redirect_back(request: web.Request, *, ok: bool, code: str | None) -> web.HTTPFound:
    """Build a 302 back to the WakaTime settings page with a result marker.

    Returns the ``HTTPFound`` for the caller to ``raise`` (aiohttp's idiom for
    a redirect), so a cookie set on it before raising still rides along.
    """
    params = (
        {"wakatime_oauth": "connected"}
        if ok
        else {"wakatime_oauth": "error", "reason": code or "unknown"}
    )
    location = "/settings?" + urllib.parse.urlencode(params)
    return web.HTTPFound(location)


async def api_wakatime_stats(request: web.Request) -> web.Response:
    """GET /api/wakatime/stats?range=last_7_days — aggregate coding stats.

    Returns the WakaTime ``stats`` payload (languages, projects, totals) for the
    range, ``{"configured": false}`` when the integration is off, or a 502 when
    the upstream call fails (never a false-empty payload).
    """
    range_param = request.query.get("range", _DEFAULT_RANGE)
    if range_param not in _ALLOWED_RANGES:
        return web.json_response(
            {
                "error": f"unsupported range; allowed: {sorted(_ALLOWED_RANGES)}",
                "code": "invalid_range",
            },
            status=400,
        )

    # Import the optional WakaTime subsystem lazily, on first request, so it
    # stays off the gateway boot path (handlers/__init__ is imported at startup).
    from kiro_crew.wakatime import WakaTimeUnavailableError

    # Build with a fresh OAuth token when one is stored (refreshed ahead of its
    # expiry), else the API key. The config-load, vault-decrypt, and any refresh
    # run off the event loop so a request never stalls the loop.
    client = await _build_authed_client(request)
    if client is None:
        return web.json_response({"configured": False})

    try:
        # fetch_stats RAISES on an upstream failure rather than degrading to {},
        # so an outage is reported as a 502, never a false-empty stats payload.
        data = await client.fetch_stats(range_param)
    except WakaTimeUnavailableError:
        return _upstream_error()
    finally:
        await client.close()

    return web.json_response({"configured": True, "range": range_param, "stats": data})


# Characters a spreadsheet treats as the start of a formula. A project name
# is external data (it comes from WakaTime), so a name like "=cmd|..." would
# execute on import if written raw. Prefixing an apostrophe forces the cell to
# be read as text; the leading control chars are equivalents some parsers honor.
_CSV_FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(value: str) -> str:
    """Neutralize a leading formula trigger so a CSV cell imports as text."""
    if value and value[0] in _CSV_FORMULA_TRIGGERS:
        return "'" + value
    return value


def _rows_by_project(summaries: list[dict]) -> list[dict[str, Any]]:
    """Fold WakaTime daily summaries into per-project totals.

    Each summary day carries a ``projects`` list of ``{name, total_seconds}``;
    sum the seconds per project name across the range.
    """
    # Lazy, like the handlers below: keeps the WakaTime package off the boot path.
    from kiro_crew.wakatime.client import sum_seconds_by_name

    return [
        {
            "project": name,
            "seconds": round(secs, 2),
            "hours": round(secs / 3600.0, 4),
        }
        for name, secs in sum_seconds_by_name(summaries, "projects")
    ]


async def api_wakatime_export(request: web.Request) -> web.Response:
    """GET /api/wakatime/export?start=&end=&format=csv|json — billable hours.

    Hours grouped by project over ``start``..``end`` (inclusive, YYYY-MM-DD).
    ``format`` is ``csv`` (default, a download) or ``json``.
    """
    start = request.query.get("start", "")
    end = request.query.get("end", "")
    fmt = request.query.get("format", "csv").lower()

    if not _valid_date(start) or not _valid_date(end):
        return web.json_response(
            {"error": "start and end must be YYYY-MM-DD", "code": "invalid_date"},
            status=400,
        )
    if start > end:
        return web.json_response(
            {"error": "start must not be after end", "code": "invalid_range"},
            status=400,
        )
    if fmt not in ("csv", "json"):
        return web.json_response(
            {"error": "format must be csv or json", "code": "invalid_format"},
            status=400,
        )

    # Import the optional WakaTime subsystem lazily, on first request, so it
    # stays off the gateway boot path (handlers/__init__ is imported at startup).
    from kiro_crew.wakatime import WakaTimeUnavailableError

    # Build with a fresh OAuth token when one is stored (refreshed ahead of its
    # expiry), else the API key. The config-load, vault-decrypt, and any refresh
    # run off the event loop so a request never stalls the loop.
    client = await _build_authed_client(request)
    if client is None:
        return web.json_response({"configured": False})

    try:
        # fetch_summaries RAISES on an upstream failure of THIS endpoint, rather
        # than degrading to []. A blank billing export must not pass off an
        # outage as "zero hours worked", and only the summaries call itself can
        # tell those apart — a probe of a different endpoint cannot.
        summaries = await client.fetch_summaries(start, end)
    except WakaTimeUnavailableError:
        return _upstream_error()
    finally:
        await client.close()

    rows = _rows_by_project(summaries)

    if fmt == "json":
        # An attachment disposition (mirroring the CSV branch) so the browser
        # downloads a file rather than navigating the dashboard away to a raw
        # JSON blob with no way back.
        payload = json.dumps({"configured": True, "start": start, "end": end, "projects": rows})
        filename = f"wakatime-hours-{start}-to-{end}.json"
        quoted = urllib.parse.quote(filename, safe="")
        return web.Response(
            body=payload.encode("utf-8"),
            content_type="application/json",
            charset="utf-8",
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{quoted}",
                "X-Content-Type-Options": "nosniff",
            },
        )

    # CSV: generated in-memory, returned with a download disposition.
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["project", "hours", "seconds"])
    for r in rows:
        # Project names are neutralized by _csv_safe against formula injection;
        # the taint rule cannot see that wrapper, so scope a waiver to this sink.
        # nosemgrep: python.django.security.injection.csv-writer-injection.csv-writer-injection
        writer.writerow([_csv_safe(r["project"]), r["hours"], r["seconds"]])
    filename = f"wakatime-hours-{start}-to-{end}.csv"
    quoted = urllib.parse.quote(filename, safe="")
    return web.Response(
        body=buf.getvalue().encode("utf-8"),
        content_type="text/csv",
        charset="utf-8",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quoted}",
            "X-Content-Type-Options": "nosniff",
        },
    )
