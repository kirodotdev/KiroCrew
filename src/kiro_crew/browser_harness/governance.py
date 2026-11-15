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

**Sites.** The browser obeys the same ``network.egress`` host rule the other
egress surfaces obey once a policy or profile sets one: the shim asks
:func:`egress_rule` for the URL the agent named, the gateway an
:func:`egress_judge` for every page and frame a navigation commits on. A scope
only the LTS built-in baseline answers sets no rule.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Optional

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


#: The host ruleset every other egress surface asks (``web_fetch``, the git proxy).
EGRESS_SCOPE = "network.egress"

#: One URL judged against the rule: the part that refuses it, None when it admits it.
EgressJudge = Callable[[object], Optional[str]]

_UNEVALUABLE = "the governance network.egress rule could not be evaluated"


def egress_rule(
    url: object, session_key: str = HOST_KEY, *, app: str = "", fail_closed: bool = False
) -> str | None:
    """Which part of the ``network.egress`` rule refuses *url*'s host; None when it admits it.

    Audited: the shim's one check of the URL the agent named. None too when
    neither the policy nor the caller's profile sets a rule (:func:`_egress_layers`),
    so the browser is then exactly as before. *fail_closed* is the caller's own
    ``capabilities.browse`` disposition. Blocking.
    """
    from kiro_crew.platform.governance_profiles import vet_and_audit

    host = _egress_host(url)
    if not host:
        return None
    try:
        layers = _egress_layers(session_key, app)
        if layers is None:
            return None
        decision = vet_and_audit(
            EGRESS_SCOPE,
            host,
            session_key=session_key,
            tool_name="browser",
            app=app,
            log_warning=False,
            fail_closed=fail_closed,
            baseline=layers[0],
        )
    except Exception:
        logger.debug("egress governance unevaluable", exc_info=True)
        return _UNEVALUABLE if fail_closed else None
    return _refusal_text(decision)


def _no_rule(_url: object) -> str | None:
    """The judge when no layer sets a ``network.egress`` rule: the browser as before."""
    return None


def judges_a_rule(judge: EgressJudge) -> bool:
    """Whether *judge* holds the browser to a ``network.egress`` rule (the operator opted in)."""
    return judge is not _no_rule


def egress_judge(session_key: str = HOST_KEY) -> EgressJudge:
    """Read the rule for *session_key* once; the judge it returns does no IO. Blocking.

    The gateway judges every page and frame a navigation commits on, on its
    event loop, so it reads the ceiling and the caller's profile once per op
    off the loop (:func:`egress_judge_async`) and judges each URL from that
    read, without an audit row. A profile edited mid-op applies from the next
    op. Fail-closed, like the gateway's ``capabilities.browse`` check.
    """
    from kiro_crew.platform.governance import resolve

    try:
        layers = _egress_layers(session_key, "")
    except Exception:
        logger.debug("egress governance unevaluable", exc_info=True)
        return lambda url: _UNEVALUABLE if _egress_host(url) else None
    if layers is None:
        return _no_rule
    policy_named, ceiling, profile = layers

    def _judge(url: object) -> str | None:
        host = _egress_host(url)
        if not host:
            return None
        try:
            decision = resolve(ceiling, profile, EGRESS_SCOPE, host, baseline=policy_named)
        except Exception:
            logger.debug("egress governance unevaluable", exc_info=True)
            return _UNEVALUABLE
        return _refusal_text(decision)

    return _judge


async def egress_judge_async(session_key: str = HOST_KEY) -> EgressJudge:
    """:func:`egress_judge`, its read off the event loop."""
    return await asyncio.to_thread(egress_judge, session_key)


def _egress_host(url: object) -> str | None:
    """The host the browser would open (``url_gate.browser_host``), so the rule and
    the address gate read one host. A URL with no host is left to that gate."""
    from kiro_crew.browser_harness.url_gate import browser_host

    return browser_host(url) if isinstance(url, str) else None


def _refusal_text(decision: object) -> str | None:
    """The refusal for *decision*; it names the rule, never the host (a landed
    address is the page's choice)."""
    if getattr(decision, "permitted", True):
        return None
    if getattr(decision, "explicit_deny", False):
        return "its host matches a deny pattern in the governance network.egress rule"
    if getattr(decision, "rule", "") == "default":
        return _UNEVALUABLE
    return "its host is not allowed by the governance network.egress rule"


def _egress_layers(session_key: str, app: str) -> tuple[bool, Any, Any] | None:
    """``(policy names the scope, ceiling, profile)``, or None when no layer sets a rule.

    Only the LTS built-in baseline answering the scope sets no rule, whether boot
    folded it into the ceiling or the evaluator fills it per query
    (``evaluator.policy_names_scope``): the browser never asked ``network.egress``
    before, so a ceiling with no egress rule keeps that. When only the caller's
    profile sets one, the profile layer is judged alone (``baseline=False``), so
    a profile that denies one host never refuses every other one. Raises what
    the reads raise.
    """
    from kiro_crew.platform.context import current_context
    from kiro_crew.platform.governance_parts.evaluator import policy_names_scope
    from kiro_crew.platform.governance_profiles import resolve_active_scope

    ceiling = getattr(current_context(), "governance", None)
    policy_named = policy_names_scope(ceiling, EGRESS_SCOPE)
    profile = resolve_active_scope(session_key, app=app)
    if not policy_named and (profile is None or profile.get(EGRESS_SCOPE) is None):
        return None
    return policy_named, ceiling, profile


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
    "EGRESS_SCOPE",
    "EgressJudge",
    "HOST_KEY",
    "browse_denied",
    "browse_denied_async",
    "egress_judge",
    "egress_judge_async",
    "egress_rule",
    "judges_a_rule",
    "stop_running_harness",
]
