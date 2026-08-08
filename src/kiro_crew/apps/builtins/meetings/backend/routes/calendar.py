"""Calendar routes — sync, the cache, credentials, and the OAuth handshake.

``GET  …/calendar``                  the cached upcoming meetings
``POST …/calendar/sync``             fetch from the configured provider
``GET  …/calendar/providers``        registered providers (for the settings picker)
``GET  …/calendar/credentials``      which providers are set up (never a value)
``PUT  …/calendar/credentials``      store a provider's credentials
``POST …/calendar/credentials/forget``  disconnect a provider
``POST …/calendar/oauth/start``      begin an OAuth flow, return the consent URL
``GET  …/calendar/oauth/callback``   where the provider redirects back to

The internal MCP path and its internal-website scraping fallback are gone; see
``backend/providers/calendar.py`` for the ``.ics`` replacement and the registry
an out-of-repo companion registers its own provider into.

**The OAuth redirect URI is derived from the request's own origin**, not from a
constant. Two reasons, and the second is the one that bites:

* The dashboard's port is configurable, so a constant would be wrong for anyone
  not on the default.
* The dashboard authenticates with an HMAC-signed **cookie**, and a cookie is
  scoped to the host it was set for. A user logged in at ``localhost:5476`` who
  was redirected to ``127.0.0.1:5476`` would arrive at the callback with no
  cookie and be rejected. Echoing the origin back keeps the browser on the host
  it is already authenticated against.

Both Google (Desktop app) and Microsoft (Mobile and desktop applications) accept
a loopback redirect on any port, so this adapts without re-registration.
"""

from __future__ import annotations

import asyncio
import html
import logging
from typing import Any, Callable

from aiohttp import web

from kiro_crew.apps.builtins.meetings.backend import constants as k
from kiro_crew.apps.builtins.meetings.backend import credentials, store
from kiro_crew.apps.builtins.meetings.backend.providers import calendar as cal
from kiro_crew.apps.builtins.meetings.backend.routes._common import (
    BadRequest,
    audit,
    data_root,
    field_str,
    json_body,
    query_int,
)

logger = logging.getLogger("kirocrew.app.meetings")

#: Which credential fields each provider accepts from a settings write.
#:
#: **This is an allowlist, not documentation.** Without it a client could PUT
#: ``access_token`` or ``refresh_token`` straight into the store and hand the app
#: a credential of its choosing; the OAuth tokens are written only by
#: :mod:`..oauth`, on the way out of a real token exchange. It is also what keeps
#: an unbounded set of keys out of a file this app has to read back.
_CREDENTIAL_FIELDS: dict[str, tuple[str, ...]] = {
    k.CALENDAR_PROVIDER_CALDAV: ("username", "password"),
    k.CALENDAR_PROVIDER_GOOGLE: ("client_id", "client_secret"),
    k.CALENDAR_PROVIDER_MICROSOFT: ("client_id", "client_secret"),
}

#: Providers that authenticate with OAuth, and the client describing each.
#: Built lazily through a callable so importing this module does not import
#: :mod:`..oauth` — that module imports from ``providers.calendar``, and the
#: provider classes import it back inside ``fetch()``.
_OAUTH_CLIENTS: dict[str, Callable[[], Any]] = {
    k.CALENDAR_PROVIDER_GOOGLE: cal.google_oauth_client,
    k.CALENDAR_PROVIDER_MICROSOFT: cal.microsoft_oauth_client,
}

#: Path of the OAuth callback, relative to the app's API base. Shared by the
#: start handler (which builds the redirect URI) and route registration, so the
#: two cannot drift — a mismatch would fail only at the provider, with a message
#: that does not name the cause.
OAUTH_CALLBACK_PATH = "/calendar/oauth/callback"


def _read_cached_calendar(root: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Read the app config and the cached calendar events. BLOCKING.

    Runs on a worker thread, never the event loop: two JSON file reads, and the
    event cache holds up to ``MAX_CALENDAR_EVENTS`` records.

    Grouped into one hop because the response pairs the cached events with the
    provider config they were fetched under — reading them in separate hops could
    report a freshly-changed provider alongside the previous provider's events.
    """
    return store.read_config(root), store.read_calendar_cache(root)


async def handle_get_calendar(request: web.Request) -> web.Response:
    """The last successful sync's events, straight from the cache."""
    config, events = await asyncio.to_thread(_read_cached_calendar, data_root(request))
    calendar_cfg = config.get("calendar") or {}
    return web.json_response(
        {
            "events": events,
            "provider": calendar_cfg.get("provider", k.DEFAULT_CALENDAR_PROVIDER),
            "configured": bool(calendar_cfg.get("source")),
        }
    )


async def handle_calendar_providers(request: web.Request) -> web.Response:
    return web.json_response({"providers": cal.available_calendar_providers()})


async def handle_calendar_sync(request: web.Request) -> web.Response:
    """Fetch from the configured provider and replace the cache.

    The fetch is fully async (aiohttp for a URL, an executor for a local file),
    so a slow or wedged calendar endpoint cannot park the gateway's event loop.
    """
    root = data_root(request)
    days = query_int(request, "days", default=k.CALENDAR_SYNC_DAYS, low=1, high=365)
    # Two hops rather than one grouped helper: the fetch between them is an
    # `await`, so the read and the write cannot share a thread. They touch
    # different files, so there is no read-modify-write to keep atomic.
    config = await asyncio.to_thread(store.read_config, root)
    calendar_cfg = config.get("calendar") or {}
    provider = cal.get_calendar_provider(
        str(calendar_cfg.get("provider") or ""), str(calendar_cfg.get("source") or "")
    )

    try:
        events = await provider.fetch(days=days)
    except cal.CalendarError as exc:
        audit("meetings.calendar_sync", provider.provider_id, outcome="error", error=str(exc))
        return web.json_response({"ok": False, "error": str(exc), "code": "calendar_sync_failed"}, status=502)

    payload = [event.to_dict() for event in events]
    await asyncio.to_thread(store.write_calendar_cache, payload, root)
    audit("meetings.calendar_sync", provider.provider_id, outcome="ok")
    return web.json_response(
        {"ok": True, "count": len(payload), "events": payload, "provider": provider.provider_id}
    )

# ── credentials ─────────────────────────────────────────────────────────────


def _known_provider(body: dict[str, Any]) -> str:
    """The ``provider`` field, validated against the live registry."""
    provider = field_str(body, "provider", max_len=64).lower()
    if provider not in {row["id"] for row in cal.available_calendar_providers()}:
        raise BadRequest("unknown calendar provider", code="unknown_provider")
    return provider


async def handle_get_calendar_credentials(request: web.Request) -> web.Response:
    """Which providers have credentials, and which fields are filled in.

    Field NAMES and booleans only — never a value. A settings page needs to
    render "connected" and "which of these did you fill in", and neither question
    needs the secret. There is deliberately no route that reads one back.
    """
    return web.json_response({"status": await credentials.credential_status()})


async def handle_put_calendar_credentials(request: web.Request) -> web.Response:
    """Store one provider's credentials.

    Write-only: the response repeats the status, not the values. An empty string
    clears its field, which is how the UI removes one without disconnecting the
    whole provider.
    """
    body = await json_body(request)
    provider = _known_provider(body)
    allowed = _CREDENTIAL_FIELDS.get(provider)
    if not allowed:
        raise BadRequest(
            "this calendar provider takes no credentials", code="no_credentials_needed"
        )
    raw = body.get("values")
    if not isinstance(raw, dict):
        raise BadRequest("values must be a JSON object")

    values: dict[str, str] = {}
    for name in allowed:
        if name not in raw:
            continue
        supplied = raw[name]
        # Hand-validated rather than routed through `field_str`, for the two
        # reasons `handle_put_note` documents: that helper treats a non-string as
        # MISSING (so a malformed request would answer 200 having silently skipped
        # a field the user thinks they set), and it strips (which would mangle a
        # password whose whitespace is significant).
        if supplied is None:
            values[name] = ""
            continue
        if not isinstance(supplied, str):
            raise BadRequest(f"{name} must be a string")
        if len(supplied) > k.MAX_CREDENTIAL_VALUE_CHARS:
            raise BadRequest(f"{name} is too long")
        values[name] = supplied

    if not values:
        raise BadRequest("no known credential fields were supplied")

    await credentials.write_for(provider, values)
    # The names, never the values -- an audit log is read by people who should not
    # be able to reconstruct a password from it.
    audit("meetings.calendar_credentials", provider, outcome="ok")
    return web.json_response({"ok": True, "status": await credentials.credential_status()})


async def handle_forget_calendar_credentials(request: web.Request) -> web.Response:
    """Disconnect a provider: forget its credentials and any pending flow."""
    body = await json_body(request)
    provider = _known_provider(body)
    await credentials.clear_for(provider)
    if provider in _OAUTH_CLIENTS:
        # A flow started and then abandoned would otherwise stay redeemable for
        # its TTL against a provider the user just disconnected.
        from kiro_crew.apps.builtins.meetings.backend import oauth

        oauth.forget_pending(provider)
    audit("meetings.calendar_disconnect", provider, outcome="ok")
    return web.json_response({"ok": True, "status": await credentials.credential_status()})


# ── OAuth ───────────────────────────────────────────────────────────────────


def _redirect_uri(request: web.Request) -> str:
    """The callback URL, on the SAME origin the caller reached us at.

    See the module docstring: the dashboard's auth cookie is host-scoped, so
    rewriting the host here would send the user back to an origin their session
    does not cover.
    """
    return f"{str(request.url.origin()).rstrip('/')}{k.API_BASE}{OAUTH_CALLBACK_PATH}"


def _oauth_client_or_400(provider: str) -> Any:
    factory = _OAUTH_CLIENTS.get(provider)
    if factory is None:
        raise BadRequest(
            "this calendar provider does not use OAuth", code="not_an_oauth_provider"
        )
    return factory()


async def handle_oauth_start(request: web.Request) -> web.Response:
    """Begin a flow and hand the caller the URL to open.

    The URL is returned rather than redirected to, because the caller is the
    dashboard's own fetch() and a 302 would be followed by the XHR instead of the
    user's browser.
    """
    from kiro_crew.apps.builtins.meetings.backend import oauth

    body = await json_body(request)
    provider = _known_provider(body)
    client = _oauth_client_or_400(provider)
    try:
        url = await oauth.begin(client, redirect_uri=_redirect_uri(request))
    except cal.CalendarError as exc:
        audit("meetings.calendar_oauth_start", provider, outcome="error", error=str(exc))
        return web.json_response(
            {"ok": False, "error": str(exc), "code": "calendar_oauth_failed"}, status=502
        )
    audit("meetings.calendar_oauth_start", provider, outcome="ok")
    return web.json_response({"ok": True, "authorize_url": url})


def _callback_page(title: str, detail: str) -> web.Response:
    """The little page the user's browser lands on.

    HTML rather than JSON because a person is looking at it. ``detail`` is escaped
    even though every caller passes text this process produced: the habit is what
    stops the next caller — one that passes a provider's ``error_description``, or
    worse a query parameter — from turning this into reflected XSS.
    """
    safe_title = html.escape(title)
    safe_detail = html.escape(detail)
    return web.Response(
        text=(
            "<!doctype html><html><head><meta charset='utf-8'>"
            f"<title>{safe_title}</title></head>"
            "<body style='font-family:system-ui;padding:2rem;max-width:34rem'>"
            f"<h1 style='font-size:1.25rem'>{safe_title}</h1>"
            f"<p>{safe_detail}</p>"
            "<p style='color:#666'>You can close this tab and return to KiroCrew.</p>"
            "</body></html>"
        ),
        content_type="text/html",
    )


async def handle_oauth_callback(request: web.Request) -> web.Response:
    """Where the provider redirects back to, in the user's browser.

    Answers HTML, and always 200 even on failure: this is a page a person reads,
    and a browser error page would hide the reason. The outcome is in the text
    and in the audit log.

    The authorization ``code`` is never echoed back into the page, and the
    provider's own ``error`` parameter is rendered escaped.
    """
    from kiro_crew.apps.builtins.meetings.backend import oauth

    provider = (request.query.get("provider") or "").strip().lower()
    # `state` is the real guard; `provider` only chooses which client to use, and
    # an unknown one cannot start a flow, so a forged value has nothing to redeem.
    if provider not in _OAUTH_CLIENTS:
        return _callback_page(
            "Could not finish connecting",
            "That calendar provider is not one this app can connect to.",
        )

    denial = (request.query.get("error") or "").strip()
    if denial:
        oauth.forget_pending(provider)
        audit("meetings.calendar_oauth_callback", provider, outcome="error", error=denial)
        return _callback_page(
            "Connection cancelled",
            f"The calendar provider reported: {denial}",
        )

    try:
        await oauth.complete(
            _oauth_client_or_400(provider),
            code=request.query.get("code") or "",
            state=request.query.get("state") or "",
        )
    except (cal.CalendarError, BadRequest) as exc:
        audit("meetings.calendar_oauth_callback", provider, outcome="error", error=str(exc))
        return _callback_page("Could not finish connecting", str(exc))

    audit("meetings.calendar_oauth_callback", provider, outcome="ok")
    return _callback_page(
        "Calendar connected",
        "KiroCrew can now read your upcoming meetings.",
    )
