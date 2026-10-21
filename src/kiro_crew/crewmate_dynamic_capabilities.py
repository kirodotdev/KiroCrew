"""Dynamic capabilities: a per-crewmate preview switch, off by default.

Each crewmate's record carries ``dynamic_capabilities`` (``agents.<name>`` in
``config.json``, ``KiroCrewAgentConfig.dynamic_capabilities``), set on the
crewmate's own Profile card (``pages/members/CrewProfileSettings.tsx``). It holds
only the crewmate-driven capability path: the crewmate finding a skill or an MCP
server for its own worker and asking for it on a card. There is no global key.

With Crewmate 2.0 on (:mod:`kiro_crew.crewmate_v2`) and a crewmate's flag off:

* ``find_capabilities`` and ``request_capabilities`` are left out of that
  crewmate's conductor tool list (``handlers/sessions_parts/tool_policy.py``
  excludes them for its thread) and their routes answer 404
  (``member_capability_catalog.api_member_self_capabilities``,
  ``member_self.api_member_self_propose`` for ``capabilities``);
* a ``crewmate.capabilities`` card it or its worker proposes through
  ``propose_change`` is refused (``handlers/change_cards.api_cards_agent_propose``);
* the hand-off carries no capability summary
  (``crewmate_dispatch_capabilities._note``);
* a worker's gap reaches the conductor as one plain line telling it to ask the
  user to add the capability in its advanced settings, never a card
  (``mate_thread_wakes.report_prompt``);
* no ``[Capabilities added]`` nudge (``handlers/change_cards._complete``);
* its worker is asked for no ``MISSING CAPABILITY:`` line
  (``mate_worker.resolve_worker_spec``) and its conductor prompt names none of
  those tools (``context_assembly/member.build_member_section``).

The flag is read per request from the crewmate's record, so it reaches the
crewmate's next session; a running conductor keeps the tool list it started
with, and the routes refuse at once. A check with no crewmate in scope answers
off. With 2.0 off nothing here applies (:func:`restricted_for` is False), and the
shape before the split keeps ``request_capabilities`` with a raw operations draft.

The user adding or removing worker capabilities by hand in the crewmate's
advanced settings (``/api/agents/{member}/capabilities``, ``/worker-kit``) is not
this switch's; it works either way.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: The record field on ``KiroCrewAgentConfig``.
FIELD = "dynamic_capabilities"

#: The two ``kirocrew-mate`` tools a crewmate without the preview does not mount.
CAPABILITY_TOOLS = frozenset({"find_capabilities", "request_capabilities"})

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
    """Whether crewmate *member* drives its own capabilities: Crewmate 2.0 AND its flag."""
    from kiro_crew import crewmate_v2

    return crewmate_v2.enabled() and _own(member, config)


def restricted_for(member: str | None, config: object | None = None) -> bool:
    """Crewmate 2.0 is on and *member*'s flag is off (or no crewmate is in scope).

    False while 2.0 is off, so the shape before the split keeps what it had.
    """
    from kiro_crew import crewmate_v2

    return crewmate_v2.enabled() and not _own(member, config)


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
