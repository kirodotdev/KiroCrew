"""The governance plane the gate consults: the ceiling-and-profile tool decision,
the spawn-capability decision, the script-hook capability gate and their SEL
audit rows.

Composed onto ``kiro_crew.hooks``; see :mod:`kiro_crew.hook_runtime`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kiro_crew.hooks import (
        _GATE_UNCOUNTED,
        logger,
        sel,
    )


def _governance_denial(
    ctx: object,
    tool_name: str,
    session_key: str,
    agent: str,
    app: str,
    tool_kind: str = "",
    raw_params: dict | None = None,
    diff_path: str = "",
    mcp_ref: str = "",
    extra_titles: tuple[str, ...] = (),
    alias_groups: tuple[tuple[str, ...], ...] = (),
    deny_aliases: tuple[str, ...] = (),
    spawn_target: str = "",
) -> str | None:
    """Return a denial reason if governance forbids *tool_name*, else None.

    *spawn_target* is the agent a backend-stated sub-agent spawn will start (set
    only from KAS's own ``_meta.kiro.consent``; see ``AcpEvent.spawn_target``).
    When set, ``capabilities.spawn`` is judged too -- the gate on, and the target
    in its ``agents`` scope -- on the SAME ceiling and profile this call resolved,
    so a spawn costs no second profile resolution and cannot be judged against a
    different profile snapshot. A spawn policy is not a ``tools`` rule, so the
    title question alone cannot answer it.

    *mcp_ref* is an already-canonical ``@server`` / ``@server/tool`` reference
    for the trusted MCP identity, evaluated in addition to (or instead of) the
    display title. It is passed as a reference rather than folded into
    *tool_name* because the title grammar cannot encode every identity; both are
    empty for a non-MCP call with no title, which governs nothing.

    *diff_path* is the diff content block's path for an edit-kind call; it joins
    the ``filesystem.write`` target set the gate classifies
    (``classify_tool_args``), so a diff-only edit is judged against an
    ALLOW-mode write confinement rather than reaching it pathless.

    Resolves the active profile (Level 2) for the calling surface and intersects
    it with the boot-frozen ceiling (Level 1).  Fast no-op when the host has
    neither a policy ceiling nor any profiles, so an ungoverned standalone host
    pays only an attribute read.  Emits a governance audit record on a deny.

    Fail-closed discipline mirrors the CPP shims: a ``PlatformCompositionError``
    (a non-standalone host that could not compose) is re-raised, never swallowed;
    any other unexpected error degrades to "no governance opinion" (None) so a
    transient profile-load glitch cannot wedge every tool call — the always-on
    deny floor in ``HookManager.on_tool_call`` already ran.
    """
    from kiro_crew.platform.context import PlatformCompositionError

    ceiling = getattr(ctx, "governance", None)
    try:
        from kiro_crew.platform.governance import gate_decision
        from kiro_crew.platform.governance_profiles import resolve_active_scope

        profile = resolve_active_scope(session_key, agent=agent, app=app)
        # Nothing to enforce: no ceiling and no bound/forced profile.
        if ceiling is None and profile is None:
            return None
        decision = gate_decision(
            ceiling,
            profile,
            tool_name,
            tool_kind=tool_kind,
            raw_params=raw_params,
            diff_path=diff_path,
            mcp_ref=mcp_ref,
            extra_titles=extra_titles,
            alias_groups=alias_groups,
            deny_aliases=deny_aliases,
        )
        if not decision.permitted:
            # The denied identity when the decision names one -- with the title,
            # the trusted tool name and the MCP reference all in one query, the
            # subject is whichever of them the rule matched, not always the title.
            subject = getattr(decision, "item", "") or tool_name or mcp_ref
            _audit_governance(session_key, agent, subject, decision)
            return f"Blocked by governance policy: {decision.reason}"
        if spawn_target:
            return _spawn_policy_denial(ceiling, profile, spawn_target, session_key, agent)
        return None
    except PlatformCompositionError:
        raise
    except Exception:
        # Wrap the late import + audit so a broken/renamed/partially-installed
        # governance_profiles cannot raise ImportError out of this except-branch
        # and convert the intended soft fail-open into a hard fail-closed that
        # wedges every tool call.
        try:
            from kiro_crew.platform.governance_profiles import audit_governance_degraded

            audit_governance_degraded("hooks.on_tool_call", session_key=session_key, app=app)
        except Exception:
            logger.debug("governance degrade audit unavailable", exc_info=True)
        return None


def _spawn_policy_denial(
    ceiling: Any, profile: Any, target: str, session_key: str, agent: str
) -> str | None:
    """The ``capabilities.spawn`` verdict for a spawn of *target*, or None.

    The two questions ``subagent._vet_spawn_governance`` asks -- is spawning on,
    and is *target* in the ``agents`` scope -- put to a ceiling and profile the
    caller already resolved. Fails CLOSED, unlike the ``tools`` question around
    it: this is an authorization for a spawn, and an evaluation error that
    permitted it would be the bypass the check exists to stop.
    """
    from kiro_crew.platform.context import PlatformCompositionError
    from kiro_crew.platform.governance import resolve

    try:
        gate = resolve(ceiling, profile, "capabilities.spawn", "")
        if not gate.permitted:
            _audit_governance(session_key, agent, target, gate)
            return f"Blocked by spawn policy: {gate.reason}"
        scoped = resolve(ceiling, profile, "capabilities.spawn", f"agents:{target}")
        if not scoped.permitted:
            _audit_governance(session_key, agent, target, scoped)
            return f"Blocked by spawn policy: agent {target!r} is not permitted"
        return None
    except PlatformCompositionError:
        raise
    except Exception:
        logger.warning("spawn policy could not be evaluated; refusing the spawn", exc_info=True)
        return "Blocked by spawn policy: it could not be evaluated"


def _audit_governance(session_key: str, agent: str, tool_name: str, decision: object) -> None:
    """Best-effort SEL audit of a governance denial (records scope/rule/layer)."""
    if _GATE_UNCOUNTED.get():
        return
    try:
        from kiro_crew.sel import sel

        sel().log_governance_decision(
            session_key=session_key,
            agent=agent or "kirocrew",
            tool_name=tool_name,
            outcome="denied",
            rule=getattr(decision, "rule", ""),
            layer=getattr(decision, "layer", ""),
            reason=getattr(decision, "reason", ""),
        )
    except Exception:
        logger.debug("governance audit emit failed", exc_info=True)


def _script_hooks_capability_denied(session_key: str = "") -> str | None:
    """Return a denial reason if governance disables ``capabilities.script_hooks``.

    Script hooks run an operator/agent-authored shell command in a subprocess
    (``run_script_hook`` → ``/bin/sh -c``), an arbitrary code-execution surface.
    The ``capabilities.script_hooks`` gate (default OFF in the catalog) lets a
    policy/profile forbid firing them.  Best-effort beyond the always-on
    sandbox/redaction guards: a ``PlatformCompositionError`` propagates
    (fail-closed CPP); any other error degrades to "no opinion" (None) so a
    transient governance glitch cannot wedge every hook.
    """
    from kiro_crew.platform.context import PlatformCompositionError

    try:
        from kiro_crew.platform.governance_profiles import governance_permits

        # item="" → the CapabilityGate's ``enabled`` flag is what is queried.
        decision = governance_permits("capabilities.script_hooks", "", session_key=session_key)
        if not getattr(decision, "permitted", True):
            return getattr(decision, "reason", "script_hooks capability disabled")
        return None
    except PlatformCompositionError:
        raise
    except Exception:
        # Wrapped (see _governance_denial): a late-import failure must not turn the
        # soft fail-open into a hard fail that wedges every script hook.
        try:
            from kiro_crew.platform.governance_profiles import audit_governance_degraded

            audit_governance_degraded(
                "run_script_hook", session_key=session_key, scope="capabilities.script_hooks"
            )
        except Exception:
            logger.debug("governance degrade audit unavailable", exc_info=True)
        return None


def _audit_governance_hook_decision(
    session_key: str, hook_label: str, outcome: str, reason: str
) -> None:
    """Best-effort SEL audit for a script/skills-only hook governance decision.

    Shared by both ``run_script_hook`` and the skills-only path in ``fire()`` to
    avoid duplicating the try/import/call pattern at every call site.
    """
    try:
        sel().log_governance_decision(
            session_key=session_key,
            tool_name=hook_label,
            scope="capabilities.script_hooks",
            outcome=outcome,
            reason=reason,
        )
    except Exception:
        logger.debug("hook governance audit (%s) failed", outcome, exc_info=True)
