"""Dynamic capabilities: a per-crewmate preview switch, off by default.

Each crewmate's record carries ``dynamic_capabilities`` (``agents.<name>`` in
``config.json``, ``KiroCrewAgentConfig.dynamic_capabilities``), set on the
crewmate's own Profile card (``pages/members/CrewProfileSettings.tsx``). It holds
only the crewmate-driven capability path: the crewmate finding a skill or an MCP
server for its own worker and asking for it on a card. There is no global key.

With a crewmate's flag off:

* ``find_capabilities`` is refused at its route
  (``member_capability_catalog.api_member_self_capabilities``, 403
  ``dynamic_capabilities_off``). Its tool stays listed: the pooled
  ``kirocrew-mate`` server caches a session's tool policy, so a cut made there
  would outlive a Turn on; the route reads the flag on every call;
* ``request_capabilities`` stays mounted. Its first call raises the Preview
  opt-in card instead of the capabilities card (``member_self``,
  :mod:`kiro_crew.mate_tool_access`): **Turn on** writes this flag and raises the
  held request as its own card, **Not now** leaves it off, and every later call
  while it is off is refused with a plain line, with no second card;
* a ``crewmate.capabilities`` card it proposes through ``propose_change`` is
  refused (``handlers/change_cards.api_cards_agent_propose``), so the opt-in is
  the only way in;
* the hand-off carries no capability summary
  (``crewmate_dispatch_capabilities._note``);
* no ``[Capabilities added]`` nudge (``handlers/change_cards._complete``);
* its conductor prompt and a worker's gap line name no ``find_capabilities``
  (``context_assembly/member.build_member_section``,
  ``mate_thread_wakes.report_prompt``).

The flag is read per request from the crewmate's record, so the routes see a
change at once, also in a running session. A check with no crewmate in scope
answers off.

The user adding or removing worker capabilities by hand in the crewmate's
advanced settings (``/api/agents/{member}/capabilities``, ``/worker-kit``) is not
this switch's; it works either way.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: The record field on ``KiroCrewAgentConfig``.
FIELD = "dynamic_capabilities"


#: Test seam: ``True``/``False`` answers every crewmate's flag; ``None`` reads it.
#: The suite pins it on (``test/conftest.py``) because it tests the path the flag
#: holds; the switch's own tests set it back to ``None``.
_override: bool | None = None


def _own(member: str | None, config: object | None = None) -> bool:
    """Crewmate *member*'s own flag; ``False`` when there is no crewmate or no record."""
    if _override is not None:
        return _override
    if not member:
        return False
    try:
        if config is None:
            from kiro_crew.config.loader import KiroCrewConfig

            config = KiroCrewConfig.load()
        record = getattr(config, "agents", {}).get(member)
    except Exception:  # noqa: BLE001 - an unreadable record is "off"
        logger.debug("dynamic capabilities: record of %r unreadable, off", member, exc_info=True)
        return False
    return bool(getattr(record, FIELD, False)) if record is not None else False


def enabled_for(member: str | None, config: object | None = None) -> bool:
    """Whether crewmate *member* drives its own capabilities: its flag is on."""
    return _own(member, config)


def restricted_for(member: str | None, config: object | None = None) -> bool:
    """*member*'s flag is off (or no crewmate is in scope)."""
    return not _own(member, config)


def member_name(member_id: str | None) -> str:
    """The roster name of the crewmate whose ``member_id`` is *member_id*, else ``""``.

    A worker's execution record carries the crewmate's ``member_id`` (``cove``),
    while the record is keyed by its name (``Cove``). Blocking (a config read).
    """
    if not member_id:
        return ""
    try:
        from kiro_crew.config.loader import KiroCrewConfig

        agents = KiroCrewConfig.load().agents
    except Exception:  # noqa: BLE001 - no roster names no crewmate
        return ""
    for name, record in agents.items():
        if getattr(record, "member_id", "") == member_id:
            return name
    return member_id if member_id in agents else ""
