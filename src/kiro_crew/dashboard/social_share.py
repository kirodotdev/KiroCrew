"""``capabilities.social_share`` — the one switch that hides "Share as image".

The share card (``website/src/pages/chat/share/``) is rendered and exported in
the browser: the PNG never leaves the machine unless the user copies or downloads
it. What a managed fleet may forbid is the other half of the same menu — the
intent buttons hand the reply's caption text to X / LinkedIn inside a URL, which
makes the feature an egress path for agent output. There is no server-side share
action to refuse, so the control is the dashboard entry itself, and the dashboard
learns whether to draw it from ``GET /api/dashboard/config``
(``social_share_enabled``). This module resolves that answer.

The shape is the one ``handlers/mobile_connect.py`` established for the same job
(a dashboard read whose answer hides an entry):

**One pinned surface, every deny honoured.** The evaluation classifies by the
``dashboard:ui`` surface key rather than anything caller-controlled, so a profile
bound to the dashboard surface can withdraw the entry, and a denied decision from
ANY layer withdraws it — there is no "only a policy counts" carve-out here, because
this probe answers a per-request dashboard question, not a process-wide startup one.

**Every decision is audited.** The evaluation runs through ``vet_and_audit``, so
each answer the dashboard acts on leaves a ``governance_decision`` SEL row, and a
ceiling that cannot be evaluated is recorded as the denial it produces.
"""

from __future__ import annotations

import logging

from kiro_crew.platform.governance_profiles import vet_and_audit

logger = logging.getLogger(__name__)

SOCIAL_SHARE_SCOPE = "capabilities.social_share"

#: Tool name the SEL ``governance_decision`` row carries, so an operator reading
#: the trail can tell this decision apart from the write chokepoints other rows use.
AUDIT_TOOL = "dashboard_config_social_share"

#: Surface key for the evaluation. The config endpoint is a dashboard-operator
#: surface by construction, and the ``X-Session-Key`` header is CALLER-CONTROLLED —
#: classifying by it would let a request carrying ``slack:x`` dodge a profile bound
#: to the ``dashboard`` surface. Same pin, same rationale, as
#: ``handlers/mobile_connect.py``; the literal is the id the dashboard sends for
#: itself (``api/client.ts``), which ``_shared.py`` and ``token_auth.py`` also match.
DASHBOARD_SURFACE_KEY = "dashboard:ui"

_UNEVALUABLE_REASON = "governance unavailable (fail-closed)"


def is_share_denied() -> bool:
    """Return whether the ceiling withdraws "Share as image" for the dashboard.

    Resolved through the standard chokepoint helper so this decision comes from the
    same evaluator as every other governed surface, and audited on the way out by
    the same seam (:func:`vet_and_audit`) so it cannot drift from the audit shape
    the other capability rows write.

    FAIL-CLOSED on an evaluation error. The two dispositions are not symmetric: a
    wrong-DENY hides a convenience menu item; a wrong-PERMIT offers a "post this to
    a third-party site" button on a fleet that forbade it — an egress path, which
    puts this row with ``capabilities.publish`` / ``theme_install`` / ``telemetry``
    (``fail_closed=True``) rather than with the advisory probes. ``fail_closed``
    also makes ``governance_permits`` audit the degrade as a critical SEL event.

    Blocking (profile resolution may read from disk) — the handler calls it
    through ``asyncio.to_thread``.
    """
    try:
        decision = vet_and_audit(
            SOCIAL_SHARE_SCOPE,
            "",
            session_key=DASHBOARD_SURFACE_KEY,
            tool_name=AUDIT_TOOL,
            log_warning=False,
            fail_closed=True,
        )
    except Exception:
        # governance_permits converts its own internal errors into a denying
        # Decision, so reaching here means the import, the composition or the call
        # itself failed — the ceiling is unevaluable, which is the same condition
        # as a degrade. Fail closed, and record the denial the seam could not.
        logger.debug("social share governance probe failed; denying", exc_info=True)
        _audit_unevaluable()
        return True
    return not getattr(decision, "permitted", False)


def _audit_unevaluable() -> None:
    """Best-effort SEL record for the path ``vet_and_audit`` never reached. Never raises."""
    try:
        # Local import is DELIBERATE (matches the other SEL sites): the test suite
        # patches ``kiro_crew.sel.sel``, which a load-time binding would bypass.
        from kiro_crew.sel import sel

        sel().log_governance_decision(
            session_key=DASHBOARD_SURFACE_KEY,
            tool_name=AUDIT_TOOL,
            scope=SOCIAL_SHARE_SCOPE,
            item="",
            outcome="denied",
            reason=_UNEVALUABLE_REASON,
        )
    except Exception:
        # SEL writes to a file, but an audit failure must never wedge the config read.
        pass
