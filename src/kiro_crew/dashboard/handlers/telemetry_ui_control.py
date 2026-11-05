"""``POST /api/telemetry/ui-control``: count one click on an agent-rendered control.

The dashboard website calls this, fire and forget, from the click handlers of the
controls an agent renders in chat (option chips, question cards and follow-up
cards). It counts
``kirocrew.ui.control.clicked{control}`` and nothing else.

What the route accepts is deliberately narrow:

* the dashboard owner's own session only (``require_owner_dashboard_request``),
  on the existing dashboard server -- no new listener or port;
* a JSON object whose only key is ``control``, a short string, read through
  ``read_bounded_json`` (JSON content type, the whole body up to 256 bytes). A
  value outside ``feature_usage.UI_CONTROL_VALUES`` is counted as ``other``, so
  the series stays closed whatever a client sends;
* nothing else is read from the request, nothing is written to disk, and the
  answer is ``204`` with no body.

With telemetry consent off the recorder is a no-op, so the route records nothing.
"""

from __future__ import annotations

import logging

from aiohttp import web

from kiro_crew.metrics.feature_usage import record_ui_control_clicked

from ._shared import read_bounded_json, require_owner_dashboard_request

logger = logging.getLogger(__name__)

#: Largest body accepted. ``{"control": "followup_start_worktree"}`` is 37 bytes.
_MAX_BODY_BYTES = 256

#: Longest ``control`` string looked at; anything longer is refused.
_MAX_CONTROL_LEN = 64


async def api_ui_control_clicked(request: web.Request) -> web.Response:
    """POST /api/telemetry/ui-control -- body ``{"control": "<value>"}``; 204."""
    refusal = await require_owner_dashboard_request(request, "telemetry.ui_control")
    if refusal is not None:
        return refusal
    body, body_err = await read_bounded_json(request, max_bytes=_MAX_BODY_BYTES)
    if body_err is not None:
        return body_err
    if body is None or set(body) != {"control"}:
        return web.json_response(
            {"error": 'body must be {"control": <string>}', "code": "invalid_body"},
            status=400,
        )
    control = body["control"]
    if not isinstance(control, str) or len(control) > _MAX_CONTROL_LEN:
        return web.json_response(
            {"error": "control must be a short string", "code": "invalid_control"},
            status=400,
        )
    # Closed onto the list inside record_ui_control_clicked; never raises.
    record_ui_control_clicked(control)
    return web.Response(status=204)
