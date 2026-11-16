"""The gate a command or a canvas comment asks right before its route, and the route.

Split out of :mod:`.routing` (the file-line cap); :mod:`.routing` keeps every
name as the import path (and the patch point) callers use.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from kiro_crew.slack.code_channels.store import Binding

if TYPE_CHECKING:
    from kiro_crew.slack.handler import LinkedRoute

logger = logging.getLogger("kiro_crew.slack.code_channels.routing")

#: Asked right before a bind or a route with the binding it acts on (None: no
#: binding yet): ``""`` admits it,
#: else why not (:func:`.surface.act_refusal`).
Admit = Callable[[Binding | None], Awaitable[str]]


async def admitted(binding: Binding, admit: Admit) -> bool:
    """Whether a route to *binding*'s session may start NOW (fails closed).

    The linked slot's window is loaded first: a sidebar row restored after a
    restart reads its transcript off the loop inside the route, an await that
    would otherwise fall between *admit* and the turn. Then *admit* is asked, so
    the caller routes right after it with no await between.
    """
    try:
        from kiro_crew.dashboard.state import load_window_off_loop
        from kiro_crew.slack.code_channels import routing

        state = routing._dashboard_state()
        get_linked = getattr(state, "get_linked_slot", None)
        slot = get_linked(binding.anchor_ts) if callable(get_linked) else None
        if slot is not None:
            await load_window_off_loop(slot)
        return not await admit(binding)
    except Exception:
        logger.debug("code channel %s: route gate failed", binding.channel_id, exc_info=True)
        return False


async def _route_to_slot(
    orch: Any,
    binding: Binding,
    text: str,
    sender_id: str,
    *,
    sender: dict[str, str] | None = None,
    request_id: str = "",
    admit: Callable[[], Awaitable[bool]] | None = None,
) -> LinkedRoute:
    """Deliver *text* to the bound session as a person's message.

    *sender* is the message's Slack sender (:func:`creation.slack_sender`),
    stored on the session's user row as ``meta["slack_sender"]`` for
    attribution; None leaves the key off. *request_id* names the Slack message
    a first turn delivers (:func:`_request_id`), stored as
    ``meta["slack_request"]`` on the row or its queued entry so a re-delivered
    event can tell this request reached the session. *admit*: the caller's
    gate, asked at the route's commit point (``route_linked_thread``).

    Only :attr:`LinkedRoute.ROUTED` means the session has the message. A
    REFUSED one was answered in the channel and appended nowhere, so it is
    never recorded as delivered (a HELD one was not answered either).
    """
    from kiro_crew.slack.code_channels import routing
    from kiro_crew.slack.handler import LinkedRoute, route_linked_thread

    meta: dict[str, Any] = {}
    if sender:
        meta["slack_sender"] = dict(sender)
    if request_id:
        meta[routing.REQUEST_META_KEY] = request_id
    routed = await route_linked_thread(
        text,
        binding.session_key,
        sender_id,
        binding.channel_id,
        routing._slack(orch),
        binding.anchor_ts,
        user_meta=meta or None,
        admit=admit,
    )
    if routed is LinkedRoute.ROUTED:
        routing._adopt_temps(binding.session_key)
    return routed
