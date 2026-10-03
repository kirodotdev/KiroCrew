"""Tool-permission policy a dashboard turn applies before it answers a request."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from kiro_crew.deny_guidance import DENY_CLASS_AWS_CREDENTIAL, DENY_CLASS_SSO_CREDENTIAL

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import (
        DENY_CAUSE_POLICY,
        Refusal,
        _name_grant_refusal_off_loop,
        classify_deny,
        log_decline,
        resolve_credential_tool_hint,
        safety_override,
        sel,
        shell_command_for_event,
    )


def _pre_tool_hooks_should_block(pre_hook_results: Any) -> bool:
    """Deny-by-default for unexpected hook output, plus explicit BLOCKED:.

    PreToolUse script hooks return a list of strings (each either a
    stdout-injection string or a 'BLOCKED:<name>:<reason>' marker emitted
    by ``_fire`` when a hook exits 2). This helper returns True when the
    auto-approve path must reject the tool: anything that's not a list of
    strings is treated as suspicious (deny-by-default), and any
    BLOCKED:-prefixed string blocks. An empty list is the documented
    pass-through contract (no hooks registered, or all registered hooks
    exited 0 with no stdout) and returns False.
    """
    if pre_hook_results is None or not isinstance(pre_hook_results, list):
        return True
    return any(not isinstance(r, str) or r.startswith("BLOCKED:") for r in pre_hook_results)


def _pre_tool_block_reason(pre_hook_results: Any) -> str:
    """Return the first hook-authored block reason, or a safe fallback."""
    if isinstance(pre_hook_results, list):
        for result in pre_hook_results:
            if isinstance(result, str) and result.startswith("BLOCKED:"):
                parts = result.split(":", 2)
                reason = parts[2].strip() if len(parts) == 3 else ""
                if reason:
                    return reason
    return "blocked by a PreToolUse policy hook"


#: The block ``_fire`` returns for a PreToolUse when the agent spec's own hooks
#: could not be read. A deny hook that was never loaded gave no verdict, and a
#: PreToolUse gate with no verdict blocks, as it does for an uninitialized store.
_SPEC_HOOKS_UNREADABLE_BLOCK = "BLOCKED:system:the agent spec's hooks could not be read"


def _spec_keys_notice(agent: str, keys: list[str]) -> str:
    """The session-start notice for spec keys this backend never receives."""
    return (
        f"ℹ️ Agent {agent} sets {' and '.join(keys)}, which this backend does not "
        "receive, so they have no effect in this session."
    )


def _spec_confirm_hooks_notice(agent: str, count: int) -> str:
    """The session-start notice for ``confirm: true`` spec hooks this backend skips."""
    hooks = "hook asks" if count == 1 else "hooks ask"
    return (
        f"ℹ️ Agent {agent} has {count} {hooks} to be confirmed before running. "
        "This backend cannot ask, so they do not run in this session."
    )


#: Deny classes a credential-vending MCP server can actually resolve. Only these
#: justify the capability-manager lookup: the hint is appended for them alone, so
#: probing on any other refusal would spend a subprocess to produce a string
#: nothing reads.
_CREDENTIAL_HINT_CLASSES = frozenset({DENY_CLASS_AWS_CREDENTIAL, DENY_CLASS_SSO_CREDENTIAL})


async def _credential_tool_hint_for(reason: str, cause: str, subject: str = "") -> str:
    """The host's credential-vendor hint, when *reason* is a refusal it can answer.

    Gated on the class rather than resolved unconditionally because the lookup
    shells out to the edition's package manager. A refusal is already a bad moment
    to add latency to, and for every non-credential class the result would be
    discarded by :func:`build_refusal_steer_notice` anyway.
    """
    if cause != DENY_CAUSE_POLICY:
        return ""
    if classify_deny(reason, subject) not in _CREDENTIAL_HINT_CLASSES:
        return ""
    return await resolve_credential_tool_hint()


def _slot_is_trusted(slot: Any) -> bool:
    """True when this slot's tool calls are auto-approved. TWO representations.

    * ``slot._trust`` — the interactive "trust this session" grant. A human clicked
      it, so it does not expire and the click is its own audit record.
    * ``slot._trust_scope`` — a ``SafetyOverride`` SCOPED grant, for an unattended
      app worker where there is no human to click anything. It is SEL-audited
      fail-closed at activation, TTL-bounded, and re-checked HERE on every approval
      via ``is_scope_active`` — so the grant lapsing is what revokes trust, with no
      cooperation required from whatever armed it.

    Strictly additive: the scope is consulted only when the slot actually carries a
    key, so a slot without the attribute — which is every ordinary chat session —
    takes exactly the decision it took before this existed.

    Deliberately does NOT renew the grant. The task runner slides its grant forward
    on tool activity because the run's own progress is the liveness signal; a crew's
    signal is its watchdog, and renewing here would let a crew whose watchdog died
    keep its grant alive off its own tool calls — which is the bound this is for.
    """
    if getattr(slot, "_trust", False):
        return True
    scope = str(getattr(slot, "_trust_scope", "") or "")
    if not scope:
        return False
    return bool(safety_override().is_scope_active(scope))


def _auto_approve_reason(slot: Any, yolo_active: bool) -> str:
    """SEL provenance for an auto-approval: yolo, session trust, or a scoped grant.

    Yolo first because it is process-wide and outranks anything per-slot, then the
    human's session flag, then the scoped grant — the same precedence
    :func:`_slot_is_trusted` decides by. Purely descriptive; it authorises nothing.
    """
    if yolo_active:
        return "yolo"
    if getattr(slot, "_trust", False):
        return "trust"
    if str(getattr(slot, "_trust_scope", "") or ""):
        return "trust_scope"
    return "trust"


def _persistable_session_policy(slot: Any, yolo_active: bool) -> str:
    """The session-level approval policy to STORE for this slot: ``"auto"`` or ``""``.

    Deliberately NOT :func:`_slot_is_trusted`, and that difference is the whole
    point of this function. Everything else on the trust path decides ONE approval
    and re-decides the next one; this value is written into the session store and
    read LATER — by the subagent spawn gate and by each subagent's own approval
    policy — at a point where nothing re-checks whether the grant still holds.

    So only a grant that cannot lapse may be cached here:

    * ``slot._trust`` — a human clicked "trust this session". It does not expire,
      and the click is its own audit record, so caching it changes nothing.
    * yolo — process-wide, and revoking it deactivates the override for everyone.

    A ``SafetyOverride`` SCOPED grant (``slot._trust_scope``) must NOT reach here.
    Its entire value is being re-checked on every approval, so a cached ``"auto"``
    would outlive it: pause or retire the crew, or disable the app, and a turn
    already in flight would keep auto-approving subagent tool calls off a policy
    written before the revocation — exactly the property the scoped grant exists to
    provide, defeated by caching it.

    A scope-trusted worker is not left stalling: its own tool approvals never
    consult this value. They go through :func:`_slot_is_trusted` per event, which
    re-checks the scope each time.
    """
    if yolo_active or getattr(slot, "_trust", False):
        return "auto"
    return ""


def _native_crew_should_auto_approve(native_tracker, state, slot) -> bool:
    """Return True only when a native crew subagent is ACTIVE *and* an
    auto-approve condition holds — otherwise deny (CWE-1188 secure default).

    Active-crew is a NECESSARY precondition: with no live native subagent the
    parent turn is not blocked on a crew tool, so this path must never
    auto-approve — regardless of the ``auto_approve_subagent_tools`` hook,
    the slot's trust, or yolo. Only when a crew is active do those signals grant
    approval; with all three false the tool still falls through to the normal
    interactive/trust gate rather than being silently approved here.
    """
    has_active_crew = bool(native_tracker) and any(
        not info.get("done") for info in native_tracker.values()
    )
    if not has_active_crew:
        return False
    return bool(
        (state.context_builder and state.context_builder.hooks.auto_approve_subagent_tools)
        or _slot_is_trusted(slot)
        or state.is_yolo_active()
    )


async def _name_grant_refusal_for(event: object) -> Refusal | None:
    """Why a shell *event* may not be auto-approved by program NAME, or ``None``.

    Every auto-approve tier is a statement about a PROGRAM, and the shell
    resolves the name itself afterwards through a ``PATH`` that legitimately
    leads with directories the agent can write.

    This lives here rather than inside ``HookManager.on_tool_call``, which is
    synchronous and called ON the loop. The hook layer decides its own tiers and
    this downgrades an auto-approve it granted, so a refusal costs one
    interactive prompt and never blocks.

    A thin wrapper over :func:`kiro_crew.name_grant.refusal_for_event` rather
    than an alias to it, so the module-level ``_name_grant_refusal_off_loop``
    stub seam still covers this path. The decline-not-raise guard lives inside
    :func:`kiro_crew.name_grant.refusal_for_command_off_loop` (the chokepoint
    every tier reaches), so this — and the trusted-pattern and trust-reads
    rungs that call the seam directly — inherit it without a second copy.

    ``None`` for a non-shell tool or an unrecoverable command: there is no
    program name to vouch for, and those tiers are unchanged.
    """

    command = shell_command_for_event(event)
    if command is None:
        return None
    return await _name_grant_refusal_off_loop(command)


def _audit_name_grant_refusal(
    *, session_key: str, slot: Any, event: Any, refusal: Refusal, tier: str
) -> None:
    """Record that a name-based auto-approve was DECLINED, and on which tier.

    A thin wrapper over :func:`kiro_crew.name_grant.log_decline`, which owns
    the payload convention (the CODE, never the ``detail``; redacted title;
    not ``critical``) for every surface. This module's ``sel`` binding is
    passed through so the dashboard's audit seam still observes the row.
    """

    log_decline(
        source="dashboard",
        session_key=session_key,
        agent=slot.agent or "kirocrew",
        event=event,
        refusal=refusal,
        tier=tier,
        sel_factory=sel,
    )
