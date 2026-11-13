"""``capabilities.browse`` on the gateway: one deny turns the whole browser off.

The ``browser`` MCP tool checks this row in the ``kirocrew-core`` shim before it
posts an op (``mcp_core_parts/governance.py``). That check alone is a half-control:
every other way into the browser runs in the gateway (the op route itself, takeover,
"Open live", the replay, a quick ask's answer, Settings' install and connect, the
startup auto-install). So the gateway asks the SAME row again at each of those, and
at the two places the engine itself starts or runs a helper
(``HarnessSupervisor._start`` and ``HarnessSupervisor.call``), so a deny also stops
an op that was already running, a reflex loop included.

**Fail-closed.** A ceiling that cannot be evaluated answers "denied": a wrong deny
costs a browser the agent can do without (``web_fetch`` still reads pages); a wrong
permit drives a browser on a fleet that forbade it. ``fail_closed=True`` also makes
``governance_permits`` record the degrade as a critical SEL event.

**Which surface.** An owner's dashboard request is classified by the fixed
``dashboard:ui`` key, never by a header the caller sets, so a profile bound to the
dashboard surface cannot be dodged (same pin as ``dashboard/social_share.py``). The
shim's internal-secret routes pass the session key the shim resolved, which is the
runtime's own identity for the turn.

**Audit.** A decision someone acted on (an op, an install, a takeover claim) goes
through ``vet_and_audit`` and leaves a ``governance_decision`` row. The per-frame
and per-keystroke legs (acks, input events, polls, helper calls) evaluate without
writing a decision row, or one held key would write one per repeat. A ceiling that
cannot be evaluated is still recorded, by ``governance_permits``, as a degrade.

Blocking: profile resolution may read from disk. Async callers use
:func:`browse_denied_async`.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

BROWSE_SCOPE = "capabilities.browse"

#: The surface key every owner-dashboard caller pins (see the module docstring).
DASHBOARD_SURFACE_KEY = "dashboard:ui"

#: The key the engine itself asks with. One engine serves every session on the
#: gateway, so it reads the ceiling with no surface (the policy, and a profile bound
#: to no surface), the same empty key ``governance_permits`` documents as its default.
#: A surface-bound profile is enforced where the caller is known: the shim and the
#: routes.
HOST_KEY = ""

#: The refusal code every gated route answers, and the text the tool shows. The
#: wording matches the shim's own refusal (``mcp_core._vet_browse_governance``).
DENIED_CODE = "policy_denied"
DENIED_TEXT = "web browsing is disabled by governance policy"

#: Tool name on the SEL row, so the trail tells these apart from the shim's ``browser``.
AUDIT_TOOL = "browser_harness"


class BrowsingDenied(RuntimeError):
    """The ceiling denies ``capabilities.browse``: nothing in the browser may run."""

    code = DENIED_CODE

    def __init__(self) -> None:
        super().__init__(DENIED_TEXT)


def browse_denied(session_key: str = DASHBOARD_SURFACE_KEY, *, audit: bool = True) -> bool:
    """Whether the ceiling denies web browsing for *session_key*. Fail-closed. Blocking."""
    from kiro_crew.platform.governance_profiles import governance_permits, vet_and_audit

    try:
        if audit:
            decision = vet_and_audit(
                BROWSE_SCOPE,
                "",
                session_key=session_key,
                tool_name=AUDIT_TOOL,
                log_warning=False,
                fail_closed=True,
            )
        else:
            decision = governance_permits(
                BROWSE_SCOPE, "", session_key=session_key, log_warning=False, fail_closed=True
            )
    except Exception:
        # ``governance_permits`` turns its own errors into a denying Decision, so
        # reaching here means the import or the composition failed: unevaluable,
        # which denies.
        logger.debug("browse governance unevaluable; denying", exc_info=True)
        return True
    return not getattr(decision, "permitted", False)


async def browse_denied_async(
    session_key: str = DASHBOARD_SURFACE_KEY, *, audit: bool = True
) -> bool:
    """:func:`browse_denied` off the event loop."""
    return await asyncio.to_thread(browse_denied, session_key, audit=audit)


def stop_running_harness() -> None:
    """After a deny: stop the gateway's browser if one is up or starting. Never raises.

    A deny that lands while a browser runs (a policy pushed mid-session) must not
    leave the engine, the relay and the screencast running until the next op, nor
    let a start parked on Chrome's Allow sheet finish once Allow is clicked. The
    stop is scheduled, not awaited, so the refusal answers at once.
    """
    try:
        from kiro_crew.browser_harness.supervisor import get_supervisor
        from kiro_crew.config.paths import config_dir

        sup = get_supervisor(config_dir())
        if not sup.holds_browser:
            return
        # One stop at a time, held by the supervisor (``stop_soon``).
        sup.stop_soon()
    except Exception:  # noqa: BLE001 - the refusal stands whether or not the stop runs
        logger.debug("browser harness policy stop failed", exc_info=True)


__all__ = [
    "AUDIT_TOOL",
    "BROWSE_SCOPE",
    "BrowsingDenied",
    "DASHBOARD_SURFACE_KEY",
    "DENIED_CODE",
    "DENIED_TEXT",
    "HOST_KEY",
    "browse_denied",
    "browse_denied_async",
    "stop_running_harness",
]
