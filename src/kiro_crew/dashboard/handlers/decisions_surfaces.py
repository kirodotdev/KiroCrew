"""Which other surfaces the ceiling lets send to a model the dashboard refuses.

``GET /api/decisions/provider`` (``handlers/decisions.py``) answers for the
dashboard surface: a profile bound to it can refuse the configured decision model
while the profile of another surface (Slack, the CLI, cron) permits it. The send
path asks the ceiling under the turn's own session key (``gate._consented_for``), so
a recorded consent still sends from those surfaces, and a picker reading only the
dashboard's answer would tell the owner nothing is sent. This module names them.

Each surface is asked the parts the send path asks: the row (and the local row for a
local preset), the model's ``providers`` entry, and the ``models`` and ``endpoints``
lists for the model id and host a decision is sent to. These are OFFERS, like
``decisions_offers``: no SEL row per surface, and every send asks again through the
audited checks.

Blocking: call off the event loop. ``handlers/decisions.py`` imports this module
inside ``_provider_payload``, so the imports at its top stay off the gateway boot path.
"""

from __future__ import annotations

from kiro_crew.config.sections import DECISION_PROVIDER_MODEL_DEFAULT
from kiro_crew.dashboard.handlers.decisions_sagemaker import endpoint_refused
from kiro_crew.decisions.capability import (
    PROVIDER_NONE,
    is_decisions_denied,
    is_local_preset,
    names_local_preset,
    offer_generation,
    provider_id,
)

#: The messaging channels ``sel._infer_source`` reads from a key's first segment.
_MESSAGING_SURFACES = (
    "slack",
    "discord",
    "telegram",
    "teams",
    "webex",
    "whatsapp",
    "imessage",
    "feishu",
    "wecom",
    "weixin",
)

#: ``(surface, a session key sel._infer_source classifies to it)`` for every surface
#: other than the dashboard a decision can be sent from. The background and heartbeat
#: keys are the runtime's own sentinels, so an unbound one reads as the deny-all its
#: real turns get (``governance_profiles.resolve_active_scope``).
SURFACE_KEYS: tuple[tuple[str, str], ...] = (
    ("cli", "cli_chat"),
    *((name, f"{name}:decisions-offer") for name in _MESSAGING_SURFACES),
    ("cron", "cron:decisions-offer"),
    ("subagent", "subagent:decisions-offer"),
    ("taskrunner", "taskrunner:decisions-offer"),
    ("background", "_bg"),
    ("heartbeat", "_hb"),
)


def refused_reading(endpoint: str, model: str, offers: dict, configured_refused: bool) -> list[str]:
    """:func:`permitted_surfaces` while the dashboard refuses the configured model, else ``[]``.

    *offers* is ``decisions_offers.provider_offers``'s answer for the dashboard and
    *configured_refused* whether its ceiling refuses the configured SageMaker address
    itself. "No model" and an address written by hand are never refused there, so
    they ask nothing; a permitted model asks nothing either, so the common read pays
    no probe per surface.
    """
    provider = provider_id(endpoint, model)
    if provider is None or provider == PROVIDER_NONE:
        return []
    if offers["provider_permitted"].get(provider, True) and not configured_refused:
        return []
    return permitted_surfaces(endpoint, model)


def permitted_surfaces(endpoint: str, model: str) -> list[str]:
    """The surfaces in :data:`SURFACE_KEYS` whose ceiling permits *endpoint*/*model*.

    *endpoint* is the configured address with the unset fallback applied
    (``gate.configured_endpoint``). Each surface is asked what the send path asks
    (``gate._consented_for`` and ``gate._targets_denied``): the rows, the ``providers``
    entry, and the ``models`` and ``endpoints`` lists for the model id it sends. A preset
    the runtime neither serves nor prepares is sent nothing from any surface.

    The whole reading, every surface's row, provider, model and host, is answered by ONE
    policy generation: a swap mid-read, or a generation nobody can read, names no
    surface rather than one no single policy permits.
    """
    local = is_local_preset(endpoint, model, serving=False)
    if not local and names_local_preset(endpoint, model):
        return []
    provider = provider_id(endpoint, model)
    sent_model = model or DECISION_PROVIDER_MODEL_DEFAULT
    before = offer_generation()
    if before is None:
        return []
    surfaces = [
        surface
        for surface, key in SURFACE_KEYS
        if not is_decisions_denied(key, local=local, provider=provider, audit=False)
        and not endpoint_refused(endpoint, audit=False, surface_key=key, model=sent_model)
    ]
    return surfaces if offer_generation() == before else []
