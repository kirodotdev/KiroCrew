"""``POST /api/telemetry/app-view``: count one app open or one foreground span.

The dashboard website calls this, fire and forget, when an app's page mounts
(``kirocrew.app.opened``), when the Library launches an app's ``openCommand``
(``opened`` with ``surface=desktop``), and when a span in which the app's page
was visible ends (``kirocrew.app.foreground_time``). A copy of
``telemetry_ui_control.py``:

* the dashboard owner's own session only (``require_owner_dashboard_request``),
  on the existing dashboard server -- no new listener or port. That gate also
  admits a Shared Crew teammate, whose request is answered ``204`` and not
  counted (``is_host_owner_dashboard_request``). So both metrics count the
  machine owner's views only, while ``session.started`` and the turn metrics
  count every session;
* a JSON object read through ``read_bounded_json`` (JSON content type, the whole
  body up to 256 bytes): ``{"app": <name>, "surface": <value>, "event":
  "opened" | "foreground", "seconds": <int>}``, ``seconds`` on ``foreground``
  only;
* the page names the app and nothing else about it. The public id and version
  are resolved here from the install records the gateway already read
  (``feature_usage.app_public_id`` / ``app_public_version``), so an app with no
  public id is ``private`` and a version is never taken from the page. A
  ``surface`` outside the closed list is ``other``; a span is cut to 24 hours,
  and one under a second is not counted;
* nothing is written to disk, and the answer is ``204`` with no body.

With telemetry consent off the recorder is a no-op, so the route records nothing.
"""

from __future__ import annotations

import logging

from aiohttp import web

from kiro_crew.apps.manifest import APP_NAME_MAX_LENGTH
from kiro_crew.metrics.feature_usage import record_app_foreground, record_app_opened

from ._shared import read_bounded_json, require_owner_dashboard_request

logger = logging.getLogger(__name__)

#: Largest body accepted. The longest app name (``APP_NAME_MAX_LENGTH``, 128)
#: with the other three fields is about 200 bytes.
_MAX_BODY_BYTES = 256

#: Longest ``surface`` string looked at; anything longer is refused.
_MAX_SURFACE_LEN = 64

#: Largest ``seconds`` accepted before the 24-hour cut, so a value is a plain
#: count and not an arbitrarily large number (ten years).
_MAX_SECONDS = 10 * 366 * 24 * 3600

_KEYS = {
    "opened": {"app", "surface", "event"},
    "foreground": {"app", "surface", "event", "seconds"},
}


def _invalid(message: str, code: str) -> web.Response:
    return web.json_response({"error": message, "code": code}, status=400)


async def api_app_view(request: web.Request) -> web.Response:
    """POST /api/telemetry/app-view -- one ``opened`` or ``foreground`` event; 204."""
    refusal = await require_owner_dashboard_request(request, "telemetry.app_view")
    if refusal is not None:
        return refusal
    body, body_err = await read_bounded_json(request, max_bytes=_MAX_BODY_BYTES)
    if body_err is not None:
        return body_err
    event = body.get("event") if body is not None else None
    # ``event`` is checked to be a string first: a list there is not a dict key.
    if body is None or not isinstance(event, str) or set(body) != _KEYS.get(event):
        return _invalid(
            'body must be {"app", "surface", "event": "opened"} or '
            '{"app", "surface", "event": "foreground", "seconds"}',
            "invalid_body",
        )
    app, surface = body["app"], body["surface"]
    if not (isinstance(app, str) and 0 < len(app) <= APP_NAME_MAX_LENGTH):
        return _invalid("app must be an app name", "invalid_app")
    if not (isinstance(surface, str) and 0 < len(surface) <= _MAX_SURFACE_LEN):
        return _invalid("surface must be a short string", "invalid_surface")
    seconds = body.get("seconds", 0)
    if (
        isinstance(seconds, bool)
        or not isinstance(seconds, int)
        or not 0 <= seconds <= _MAX_SECONDS
    ):
        return _invalid("seconds must be a whole number of seconds", "invalid_seconds")
    # Imported here, as in ``_shared_access``: ``source_providers`` reaches back
    # into sibling handler modules.
    from kiro_crew.dashboard.handlers.source_providers import is_host_owner_dashboard_request

    if not is_host_owner_dashboard_request(request):
        # A Shared Crew teammate passed the gate above; their views are not the
        # owner's, so they are not counted.
        return web.Response(status=204)
    if event == "opened":
        # Closed and labelled inside the recorder; never raises.
        record_app_opened(app, surface)
        return web.Response(status=204)
    # Cut to 24 h, and a span under 1 s dropped, inside the recorder; never raises.
    record_app_foreground(app, surface, seconds)
    return web.Response(status=204)
