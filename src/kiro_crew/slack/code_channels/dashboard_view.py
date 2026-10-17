"""What the dashboard shows about a session's code channel.

A session bound to a code channel carries a ``code_channel`` object on its slot
row -- in the PERSON's views of the slot list only: ``GET /api/chat/slots`` and
the WebSocket snapshot for a dashboard user, and the broadcast's dashboard-user
and owner frames (``DashboardState.serialize_slots`` with ``dashboard_user`` or
``include_check_status``, and ``serialize_slot_views``). The sidebar and the
session header draw a "Slack code channel" badge from it, with a link that
opens the channel. A session with no binding carries no such key, and neither
does any session while the code-channels gate (:func:`.binding.code_channels_on`)
is shut, so with the feature off every row is exactly what it was without it.

**Who sees it.** The bare list -- what ``/api/stream`` (SSE) and app-token
callers of ``GET /api/chat/slots`` read -- never carries the field, and an app
token's WebSocket rows have it removed by ``filter_slots_for_app`` along with
provider status. Within the person's views it rides the row it describes, so
the member-principal filter on ``GET /api/chat/slots`` drops it with any row the
member may not see. Withholding a field from an audience that never had it is
not a per-request permission decision, and these reads are the sidebar's
high-frequency list, so it writes no SEL row of its own; the app-token row
filter that already decides each app row keeps auditing it.

Shape::

    {
      "channel_name": "fix-login-redirect",   # "" when not known
      "status": "active" | "wrapped_up" | "archived",
      "web_url": "https://slack.com/app_redirect?channel=C0123&team=T0123",
    }

``web_url`` is Slack's documented ``app_redirect`` URL: one link that opens the
channel in the desktop app when it is installed and in the web client
otherwise, so the badge needs no second, ``slack://`` link. It is "" when the
binding lacks a well-formed team or channel id, so a malformed record can never
put an attacker-shaped string into an ``href``. The object carries only what the
badge draws.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import quote

logger = logging.getLogger(__name__)

STATUS_ACTIVE = "active"
STATUS_WRAPPED_UP = "wrapped_up"
STATUS_ARCHIVED = "archived"

#: Slack workspace (``T``/``E``) and conversation (``C``/``G``/``D``) ids.
_TEAM_ID = re.compile(r"^[TE][A-Z0-9]{2,30}$")
_CHANNEL_ID = re.compile(r"^[CGD][A-Z0-9]{2,30}$")
#: Longest channel name Slack allows.
_NAME_MAX = 80


def slack_channel_url(team_id: str, channel_id: str) -> str:
    """The ``app_redirect`` URL that opens *channel_id*, or ``""``."""
    team = team_id.strip() if isinstance(team_id, str) else ""
    channel = channel_id.strip() if isinstance(channel_id, str) else ""
    if not (_TEAM_ID.match(team) and _CHANNEL_ID.match(channel)):
        return ""
    return f"https://slack.com/app_redirect?channel={quote(channel)}&team={quote(team)}"


def _stamp(binding: Any, name: str) -> bool:
    value = getattr(binding, name, 0)
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def binding_status(binding: Any) -> str:
    """``archived`` outranks ``wrapped_up``: an archived channel takes no replies."""
    if _stamp(binding, "archived_at"):
        return STATUS_ARCHIVED
    if _stamp(binding, "wrapped_up_at"):
        return STATUS_WRAPPED_UP
    return STATUS_ACTIVE


def _channel_name(binding: Any) -> str:
    name = getattr(binding, "channel_name", "")
    if not isinstance(name, str):
        return ""
    return name.strip().lstrip("#")[:_NAME_MAX]


def code_channel_view(binding: Any) -> dict[str, Any]:
    """The ``code_channel`` object for one binding (see the module docstring)."""
    return {
        "channel_name": _channel_name(binding),
        "status": binding_status(binding),
        "web_url": slack_channel_url(
            str(getattr(binding, "team_id", "") or ""),
            str(getattr(binding, "channel_id", "") or ""),
        ),
    }


def _gate_open() -> bool:
    from kiro_crew.slack.code_channels.binding import code_channels_on, live_config

    return code_channels_on(live_config())


_warming: set[Any] = set()


def _warm_store(st: Any) -> None:
    """Read *st* off the event loop, then re-push the slot list (once per store).

    Slack's own start-up loads the store off the loop, so this only runs when a
    slot list is serialized before that has happened. Outside a running loop
    (a script, a test) nothing is scheduled and the badge waits for a later read.
    """
    if st in _warming:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _warming.add(st)

    async def _load() -> None:
        try:
            await asyncio.to_thread(st.load)
            from kiro_crew.slack.handler_runtime.access import get_dashboard_state

            state = get_dashboard_state()
            if state is not None and hasattr(state, "push_slots_update"):
                state.push_slots_update()
        except Exception:
            logger.debug("code channel store warm-up failed", exc_info=True)
        finally:
            _warming.discard(st)

    task = loop.create_task(_load())
    _background.add(task)
    task.add_done_callback(_background.discard)


_background: set[asyncio.Task[None]] = set()


def code_channel_views(bindings: Iterable[Any] | None = None) -> dict[str, dict[str, Any]]:
    """``{slot key: code_channel}`` for every bound session, ``{}`` when gated off.

    Called once per slot-list serialization, on the event loop, so it never
    touches the disk: the gate is read first (with the feature off the store is
    never consulted), and a store not yet read from its file yields no badges
    while :func:`_warm_store` loads it off the loop. Never raises: a failure here
    must cost the badge, not the sidebar.
    """
    try:
        if not _gate_open():
            return {}
        if bindings is None:
            from kiro_crew.slack.code_channels.store import store

            st = store()
            if not st.loaded:
                _warm_store(st)
                return {}
            bindings = st.bindings()
        rows = [b for b in bindings if getattr(b, "session_key", "")]
        return {str(b.session_key): code_channel_view(b) for b in rows}
    except Exception:
        logger.debug("code channel views unavailable", exc_info=True)
        return {}


def with_code_channels(
    payloads: list[dict[str, Any]], views: Mapping[str, dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """*payloads* with ``code_channel`` on each bound row, as a new list.

    Never mutates a row: the audience views share their row dicts with the
    bare list SSE and app tokens read, so a bound row is copied before the key
    is added and every other row is passed through as it is.
    """
    if views is None:
        views = code_channel_views()
    if not views:
        return payloads
    out: list[dict[str, Any]] = []
    for payload in payloads:
        view = views.get(str(payload.get("key") or ""))
        out.append(payload if view is None else {**payload, "code_channel": dict(view)})
    return out
