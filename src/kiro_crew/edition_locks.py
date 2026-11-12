"""Local enables an LTS build ignores, for controls with no governance key yet.

RFC ``rfc-crew-lts-edition`` decision D3: in an LTS build only the
remote policy can turn a control on; local config, environment variables, profiles
and dashboard settings may only narrow it. A control that already has a governance
key gets that from the evaluator. The controls in :data:`UNKEYED_LOCKS` have no key
yet, so no remote rule can turn them on, and their local switch is ignored here: the
feature stays at its minimal set. A later change that adds a real key for one of them
replaces its row with a governance check.

Each feature calls :func:`local_enable_locked` where it reads its own switch. In the
``oss`` edition it returns ``False`` and nothing changes. In an LTS build it
returns ``True`` and logs once per control. A setting API reports a locked control
with :data:`LOCKED_BY_POLICY`.

The second half serves the RFC appendix "Red" rows, the controls governance cannot
reach: :func:`locked_off` (off in an LTS build), :func:`follows_policy` (for
a row the RFC writes as "follows ``<key>``; off by default") and the two sandbox
floors, so local config cannot lower the sandbox.
"""

from __future__ import annotations

import logging
import threading
from types import SimpleNamespace
from typing import Any, Dict

from kiro_crew import edition

logger = logging.getLogger(__name__)

#: The reason a setting API reports for a local value an LTS build ignores.
LOCKED_BY_POLICY = "locked by policy"

#: Control id -> the local source that is ignored and what the feature does instead.
#: Ids follow the RFC appendix "Crew key" column.
UNKEYED_LOCKS: Dict[str, str] = {
    "tunnel": "tunnel.enabled and tunnel.relay_enabled are ignored; no tunnel or relay is started",
    "telemetry.otlp_hosts": (
        "telemetry.otlp_endpoint and telemetry.otlp_endpoints are ignored; "
        "only endpoints this build ships or the policy names "
        "(capabilities.telemetry.otlp_endpoint) may receive metrics"
    ),
    # Named for the RFC "developer_mode" policy term; the key that replaces this row
    # is separate work.
    "developer_mode": "KIROCREW_DEV_MODE is ignored; developer-only features stay off",
    # RFC D12: an LTS build ships no unreleased surface.
    "feature_previews": "dashboard.feature_previews is ignored; every Feature Preview reads off",
    # RFC D12: the dashboard hides Settings > Developer > Debug tools. Browser-only;
    # the config read lists it in ``locked_controls`` so the dashboard learns of it.
    "debug_tools": "Settings > Developer > Debug tools is hidden; its overlays stay off",
}

#: Config path prefix of every Feature Preview switch (``dashboard.feature_previews.<name>``).
FEATURE_PREVIEWS_PATH = "dashboard.feature_previews"

#: config.json path -> the control whose lock makes that local value inert.
CONFIG_PATH_LOCKS: Dict[str, str] = {
    "tunnel.enabled": "tunnel",
    "tunnel.relay_enabled": "tunnel",
    "slack.use_tunnel_url": "tunnel",
    "telemetry.otlp_endpoint": "telemetry.otlp_hosts",
    "telemetry.otlp_endpoints": "telemetry.otlp_hosts",
}

_logged: "set[str]" = set()
_logged_lock = threading.Lock()


def local_enable_locked(control: str) -> bool:
    """True when this build ignores the local switch for *control*.

    Raises ``KeyError`` for a control not in :data:`UNKEYED_LOCKS`, so a misspelled
    id fails in tests instead of silently reading as unlocked.
    """
    reason = UNKEYED_LOCKS[control]
    from kiro_crew.edition import is_lts

    if not is_lts():
        return False
    with _logged_lock:
        first = control not in _logged
        _logged.add(control)
    if first:
        logger.warning("LTS build: %s (%s)", reason, LOCKED_BY_POLICY)
    return True


def relay_selected(cfg: Any) -> bool:
    """Whether ``tunnel.relay_enabled`` turns the Kiro relay path on in this build.

    The one rule for the relay switch: the public composition's tunnel slot, the
    dashboard app's relay ingress and the gateway's ingress publish all ask it. Only
    the boolean ``True`` reads as on, because a stand-in config object answers any
    attribute truthily, and an LTS build ignores the switch with the rest of
    the ``tunnel`` control (RFC D3). It imports nothing from ``kiro_crew.relay_client``,
    so a process that asks it with the relay off never loads that package.
    """
    if getattr(getattr(cfg, "tunnel", None), "relay_enabled", False) is not True:
        return False
    return not local_enable_locked("tunnel")


def locked_controls() -> "tuple[str, ...]":
    """Every control this build locks, for a setting API to report. Empty on oss."""
    from kiro_crew.edition import is_lts

    return tuple(sorted(UNKEYED_LOCKS)) if is_lts() else ()


def config_path_locked(path: str) -> bool:
    """True when this build ignores the local value at config *path*. Never logs."""
    from kiro_crew.edition import is_lts

    in_previews = path == FEATURE_PREVIEWS_PATH or path.startswith(FEATURE_PREVIEWS_PATH + ".")
    return (path in CONFIG_PATH_LOCKS or in_previews) and is_lts()


def feature_previews(previews: Any) -> Any:
    """``dashboard.feature_previews`` as a reader must take it: ``{}`` in an LTS build.

    Every server reader of a preview switch goes through this, so a value written by
    hand, by the CLI or by an older build still reads as off there (RFC D12). The
    stored value is left alone. On ``oss`` it returns *previews* unchanged.
    """
    return {} if local_enable_locked("feature_previews") else previews


def reset_logged() -> None:
    """Forget which locks have logged.  Test helper."""
    with _logged_lock:
        _logged.clear()


# ── Red controls (RFC appendix "Red") ──

#: Said by a refused action, so the reason names the build rather than a setting.
LOCKED_REASON = "turned off in the LTS edition"

#: Said when a "follows" row is refused because no policy turned its key on.
UNNAMED_REASON = "off by default in the LTS edition until a policy enables it"

#: Decision layers that carry a policy answer (``evaluator.resolve``). ``default``
#: (nothing governs the scope) and ``profile`` (a profile alone) are not.
_POLICY_LAYERS = frozenset({"policy", "both"})


def locked_off() -> bool:
    """True when this build hardcodes the Red control off."""
    return edition.is_lts()


def follows_policy(decision: object) -> bool:
    """Whether a PERMITTING *decision* may stand for a "follows ``<key>``" row.

    Always true in an ``oss`` build. In an LTS build only a permit the policy
    layer gave counts, so the feature is off by default and follows its key once a
    policy names it. Which policy tier may widen a key is the RFC's D3 work, not this.
    """
    if not locked_off():
        return True
    return getattr(decision, "layer", "") in _POLICY_LAYERS


def policy_governs(scope: str) -> bool:
    """Whether a policy answers *scope* here: always in an LTS build (the
    baseline fills an unnamed scope), otherwise when the installed policy names it.

    Fail-closed: a context that cannot be composed answers ``True``.
    """
    if locked_off():
        return True
    try:
        from kiro_crew.platform.context import current_context, installed_context

        ctx = installed_context()
        if ctx is None:
            ctx = current_context()
        ceiling = getattr(ctx, "governance", None)
    except Exception:
        logger.debug("could not read the %s ceiling; treating it as governed", scope)
        return True
    return ceiling is not None and ceiling.get(scope) is not None


def policy_permits(scope: str, item: str = "", *, audit_tool: str = "") -> bool:
    """Whether the POLICY layer permits *item* in *scope*, for a process-wide switch.

    For a control with no session behind it (the bind address, the origin set, the
    tailnet, the desktop-control opt-in). The question is asked of the installed
    ceiling alone (``resolve(ceiling, None, ...)``): a profile is per surface, and
    an unprimed store answers a transient deny-all, so no profile is read. That is
    also pure in-memory work, safe on the event loop. In an LTS build a
    permit must come from the policy layer (:func:`follows_policy`), except for an
    open control (``BASELINE_UNGATED``), which keeps Crew's default; with no policy
    loaded the baseline answers. Fail-closed: an evaluation error, or a context that
    cannot be composed, answers ``False``. An ``oss`` build whose INSTALLED policy
    does not name *scope* permits without evaluating anything, as before the key
    existed; with no context installed it evaluates through ``current_context()``.

    Pass *audit_tool* from an enforcement call so the decision this returns is
    written to SEL (``log_governance_decision``), including the ``oss`` shortcut's
    permit (layer ``default``); a pure read passes nothing.
    """
    decision: object = None
    try:
        if not locked_off():
            from kiro_crew.platform.context import installed_context

            ctx = installed_context()
            if ctx is not None:
                ceiling = getattr(ctx, "governance", None)
                if ceiling is None or ceiling.get(scope) is None:
                    if audit_tool:
                        _audit_policy_decision(audit_tool, scope, item, True, _NOT_GOVERNED)
                    return True
        from kiro_crew.platform.context import current_context
        from kiro_crew.platform.governance import resolve
        from kiro_crew.platform.governance_parts.baseline import BASELINE_UNGATED

        decision = resolve(getattr(current_context(), "governance", None), None, scope, item)
        # An open control (RFC D3) keeps Crew's default when the policy is silent, so
        # its ``default``-layer permit stands; every other scope needs a policy permit.
        permitted = bool(getattr(decision, "permitted", False)) and (
            scope in BASELINE_UNGATED or follows_policy(decision)
        )
    except Exception:
        logger.debug("governance probe for %s failed; denying", scope, exc_info=True)
        permitted = False
    if audit_tool:
        _audit_policy_decision(audit_tool, scope, item, permitted, decision)
    return permitted


#: What :func:`policy_permits` records when no installed policy names the scope.
_NOT_GOVERNED = SimpleNamespace(rule="default", layer="default", reason="not governed")


def audit_refusal(tool_name: str, scope: str, item: str, reason: str) -> None:
    """SEL record of a refusal decided before the evaluator was asked. Never raises."""
    _audit_policy_decision(
        tool_name, scope, item, False, SimpleNamespace(rule="shape", layer="policy", reason=reason)
    )


def _audit_policy_decision(
    tool_name: str, scope: str, item: str, permitted: bool, decision: object
) -> None:
    """Best-effort SEL record of the answer :func:`policy_permits` gave. Never raises."""
    try:
        from kiro_crew.sel import sel

        sel().log_governance_decision(
            session_key="",
            tool_name=tool_name,
            scope=scope,
            item=item,
            outcome="allowed" if permitted else "denied",
            rule=getattr(decision, "rule", "") or "",
            layer=getattr(decision, "layer", "") or "",
            reason=getattr(decision, "reason", "") or "governance error; denied (fail-closed)",
            critical=decision is None,
        )
    except Exception:
        logger.debug("could not audit the %s decision", scope, exc_info=True)


def sandbox_mode_floor(mode: Any, default: Any) -> Any:
    """``agent.sandbox`` as read, except an LTS build cannot read ``off``.

    There ``off`` reads as the shipped *default*, so local config cannot go below
    Crew's own sandbox; ``strict`` still narrows ("Minimum sandbox level", RFC D7). An
    ``oss`` build returns *mode* unchanged. Read by
    ``sandbox_tiers._clamp_sandbox_mode_to_floor``, the clamp every spawn path shares.
    """
    if mode == "off" and locked_off():
        return default
    return mode


def unsandboxed_exec_floor(allow: bool, platform_default: bool) -> bool:
    """``sandbox_allow_unsandboxed_exec`` as read; an LTS build cannot widen it.

    There a declared ``true`` reads as *platform_default* (fail-closed where a
    backend can exist), for the same reason as :func:`sandbox_mode_floor`; a
    declared ``false`` still narrows. An ``oss`` build returns *allow* unchanged.
    Read by ``sandbox_unavailable._allow_unsandboxed_exec``, the one gate.
    """
    if allow and locked_off():
        return platform_default
    return allow
