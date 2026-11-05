"""``capabilities.bot_share`` -- the switch that withdraws Bot export and Bot import.

A ``.kirobot`` file carries a Bot's rules, goals, cron jobs with their scripts and the
skills its sender wrote. Exporting one moves that out of the organisation's machine in
a single download; importing one creates an agent, with its rules and cron jobs, from
a file the organisation did not author. A managed fleet may forbid either, so every
way in or out asks this module before it does any work:

* ``POST /api/members/{slug}/export`` (``handlers/member_export.py``), preview and
  download alike, because the preview already reads what would leave, and its
  ``export/lessons-draft`` helper;
* ``POST /api/members/import`` (``handlers/change_cards.py``), which raises the
  ``crewmate.import`` card;
* the apply step of that card, because the ceiling is read at boot and a card lives a
  day, so a card raised under one policy can be pressed under another;
* ``CrewmateSDK.propose`` (``apps/crewmate_sdk.py``), the app path to the same card.

One row covers both directions on purpose: they move the same file in opposite
directions, and a fleet that forbids one almost never wants the other.

The shape is the one :mod:`kiro_crew.dashboard.social_share` established for a
dashboard question: evaluate on the pinned ``dashboard:ui`` surface (the
``X-Session-Key`` header is caller-controlled, so it never picks the surface), through
:func:`vet_and_audit` so every answer leaves a ``governance_decision`` SEL row, and
FAIL-CLOSED. Unlike social share this row guards an action rather than hiding a menu
entry, so a wrong-permit lets a file out on a fleet that forbade it.

An absent scope is permitted, so the ``oss`` edition and an ungoverned install are
unchanged. The enterprise baseline turns the row off
(``platform/governance_parts/baseline.py``).
"""

from __future__ import annotations

import logging

from kiro_crew.platform.governance_profiles import vet_and_audit

logger = logging.getLogger(__name__)

BOT_SHARE_SCOPE = "capabilities.bot_share"

#: Tool name the SEL ``governance_decision`` row carries.
AUDIT_TOOL = "bot_share"

#: Same pin, same reason as ``social_share.DASHBOARD_SURFACE_KEY``: both routes are
#: dashboard-owner surfaces, and the session header is caller-controlled.
DASHBOARD_SURFACE_KEY = "dashboard:ui"

_UNEVALUABLE_REASON = "governance unavailable (fail-closed)"

#: What a refused caller is told. It names the policy, not the scope key.
DENIED_MESSAGE = "this installation's policy does not allow exporting or importing a Bot"


def is_bot_share_denied() -> bool:
    """Whether the ceiling withdraws Bot export and import. Blocking.

    Profile resolution may read from disk, so a handler calls this through
    ``asyncio.to_thread``. An evaluation error is a denial, recorded in the audit
    trail because :func:`vet_and_audit` never reached its own row.
    """
    try:
        decision = vet_and_audit(
            BOT_SHARE_SCOPE,
            "",
            session_key=DASHBOARD_SURFACE_KEY,
            tool_name=AUDIT_TOOL,
            log_warning=False,
            fail_closed=True,
        )
    except Exception:
        logger.debug("bot share governance probe failed; denying", exc_info=True)
        _audit_unevaluable()
        return True
    return not getattr(decision, "permitted", False)


def _audit_unevaluable() -> None:
    """Best-effort SEL record for the path ``vet_and_audit`` never reached. Never raises."""
    try:
        # The import is local on purpose: tests patch ``kiro_crew.sel.sel``, which a
        # load-time binding would bypass.
        from kiro_crew.sel import sel

        sel().log_governance_decision(
            session_key=DASHBOARD_SURFACE_KEY,
            tool_name=AUDIT_TOOL,
            scope=BOT_SHARE_SCOPE,
            item="",
            outcome="denied",
            reason=_UNEVALUABLE_REASON,
        )
    except Exception:
        # An audit failure must never turn a refusal into a crash.
        pass
