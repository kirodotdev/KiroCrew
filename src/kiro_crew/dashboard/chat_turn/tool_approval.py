"""Tool-permission policy a dashboard turn applies to a request, and the permission arm's own
answers (``_on_permission_request``): the hooks, the trust reads, the grants and auto-approve."""

from __future__ import annotations

import functools
import json
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal

from kiro_crew.agent_sdk.spec_hooks import post_tool_identity
from kiro_crew.dashboard.chat_utils import _broadcast_auto_tool, open_construct_at_end
from kiro_crew.dashboard.state import DENY_CAUSE_POLICY
from kiro_crew.deny_guidance import (
    DENY_CLASS_AWS_CREDENTIAL,
    DENY_CLASS_SSO_CREDENTIAL,
    classify_deny,
    resolve_credential_tool_hint,
)
from kiro_crew.executors import run_in_tool_gate_pool
from kiro_crew.hooks import (
    HOOK_EVENT_PRE_TOOL_USE,
    TOOL_ALLOW,
    TOOL_AUTO_APPROVE,
    TOOL_DENY,
    ToolHookResult,
    hook_gate_kwargs,
    identity_grant_covers_child,
)
from kiro_crew.name_grant import log_decline, shell_command_for_event, should_log_decline
from kiro_crew.permission_floor import OUTCOME_REJECTED_TRANSPORT_FLOOR
from kiro_crew.security.readonly_bash import command_invokes_git, is_read_only_bash
from kiro_crew.tool_gate_busy import hook_gate_busy
from kiro_crew.trust_patterns import GrantEnded, approval_command
from kiro_crew.trust_patterns import extract_bash_command as _extract_bash_command
from kiro_crew.trust_patterns import live_trusted_patterns as _live_trusted_patterns
from kiro_crew.trust_patterns import redirect_outside_grant as _redirect_outside_grant
from kiro_crew.trust_patterns import timed_grant_deadline as _timed_grant_deadline

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import (
        Refusal,
        _matches_trusted_pattern,
        _name_grant_refusal_off_loop,
        _validate_tool_name,
        capabilities_of,
        is_claude_backend_name,
        logger,
        read_effective_agent,
        redact_credentials,
        redact_exfiltration_urls,
        safety_override,
        sel,
    )
    from kiro_crew.dashboard.chat_turn.deny_chokepoints import (
        _redact_display_text,
        _reject_attributed,
        _reject_hook_blocked,
        _reject_hook_error,
        _reject_invalid_tool,
        _steer_policy_notice,
    )
    from kiro_crew.dashboard.chat_turn.native_cards import _safe_native_crew_debug_title
    from kiro_crew.dashboard.chat_turn.tool_rows import _tcid_identity_key, _tool_meta
    from kiro_crew.dashboard.chat_turn.turn_ledgers import _wt_note_approved
    from kiro_crew.dashboard.chat_turn.turn_locals import _ChatTurn, _Flow
    from kiro_crew.dashboard.chat_turn.turn_reply_seams import _cut_segment
    from kiro_crew.dashboard.chat_turn.turn_spec_hooks import _fire
    from kiro_crew.trust_patterns import GrantDeadline


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


def _spec_hooks_run_nowhere_notice(agent: str, events: list[str]) -> str:
    """The session-start notice for spec hooks that nothing runs on this backend."""
    return (
        f"ℹ️ Agent {agent} has hooks on {', '.join(events)}. This backend does not run "
        "an agent's hooks, so they have no effect in this session."
    )


def _spec_withheld_hooks_notice(agent: str, *, user_hooks_run: bool) -> str:
    """The session-start notice for a project spec's ``hooks`` Crew does not run.

    *user_hooks_run* is whether the user-level spec of that name, the one read in
    the project spec's place, carries hooks this session runs. Only then does the
    notice say some still run; otherwise it names where a trusted hook can go.
    """
    lead = (
        f"ℹ Project hooks for agent {agent} do not run in this session: Crew does "
        "not run commands from a project checkout."
    )
    if user_hooks_run:
        return f"{lead} The hooks in your user-level spec for agent {agent} still run."
    return f"{lead} To run a hook you trust, add it to your user-level spec for agent {agent}."


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

    A Crew Mode card trust ("Trust this worker", ``slot._trust_card_scoped``) is
    a ``slot._trust`` too and IS cached: trusting a worker trusts the subagents it
    spawns (``approval_posture.trust_reaches_subagents``). The sessions it creates
    read the slot, not this value, and keep asking (``trust_carries``).
    """
    if yolo_active:
        return "auto"
    from kiro_crew.dashboard.approval_posture import trust_reaches_subagents

    return "auto" if trust_reaches_subagents(slot) else ""


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


#: The reads a conductor makes of its own crew members. ``session_send`` is NOT here:
#: the conductor skill pins that a send is never auto-approved (it runs text as the
#: target's user turn under the target's grants, and a conductor reads untrusted
#: content), so a send keeps prompting as it does on every other session.
_CREW_ROOT_OWN_WORKER_VERBS = frozenset({"session_status", "session_read_message"})


def _vouched_creator_of(target: str, slots: Mapping[str, Any]) -> str:
    """The slot that created live slot *target*, by gateway-vouched lineage only, or ``""``.

    The settled session tree's parent edge first (the crew log's own record, which
    an adoption moves and a ``session_release`` clears); with no settled tree, the
    birth stamp ``_created_by`` ONLY when ``_lineage_minted`` says THIS process
    stamped it at ``session_create`` time. Never the saved ``created_by`` a restore
    reads back from transcript metadata: that file is editable by an agent's file
    tools, and the slot list's display stamp (``crew_mode.live_stamps``) falls back
    to it, so a display rule cannot be the proof an approval exemption rests on.
    """
    from kiro_crew import crew_mode

    if crew_mode.lineage_is_settled():
        tracked = crew_mode.persisted_tree_slots()
        if tracked is not None and target in tracked:
            return crew_mode.persisted_parents().get(target, "")
    child = slots.get(target)
    if child is None or not bool(getattr(child, "_lineage_minted", False)):
        return ""
    return str(getattr(child, "_created_by", "") or "")


def _crew_root_own_worker_call(state: Any, slot: Any, event: Any) -> bool:
    """A Crew Mode conductor's OWN read of one of ITS crew members.

    Keyed on the verified ``kirocrew-dashboard`` identity, a read verb
    (:data:`_CREW_ROOT_OWN_WORKER_VERBS`), and the verified ``target`` argument,
    which must be the exact key of a live session whose gateway-vouched creator
    (:func:`_vouched_creator_of`) is this conductor. Anything else (a send, a
    subagent's call, a title-only or unverified identity, another session, a target
    by title, a creator known only from saved metadata) keeps asking. Inert
    (``False`` for every call) until ``crew_mode.APPROVAL_ROUTING_LIVE`` is on,
    which the Crew panel part turns on.
    """
    from kiro_crew import crew_mode

    if not crew_mode.APPROVAL_ROUTING_LIVE:
        return False
    if event.sub_session_id or not (event.mcp_identity_trusted and event.raw_params_trusted):
        return False
    if event.mcp_server_name != "kirocrew-dashboard":
        return False
    if event.tool_name not in _CREW_ROOT_OWN_WORKER_VERBS:
        return False
    params = event.raw_tool_params or {}
    # A deferred tool runs through tool_search's ``tool_call``: the real arguments
    # sit under ``arguments``, named by a ``tool_id`` that must be this same tool.
    if params.get("tool_id") == f"{event.mcp_server_name}::{event.tool_name}":
        params = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
    target = params.get("target")
    slots = getattr(state, "_slots", {}) or {}
    if not isinstance(target, str) or target == slot.key or target not in slots:
        return False
    if not crew_mode.slot_is_crew_mode_root(slot):
        return False
    return _vouched_creator_of(target, slots) == slot.key


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


def _pay_permission_cut(turn: _ChatTurn) -> None:
    """Cut the segment owed at a permission request, now that a row lands for it."""
    if turn.assistant_text and not open_construct_at_end(turn.assistant_text):
        _cut_segment(turn)
        _turn_flushed_visible_text = True
        turn._turn_flushed_visible_text = _turn_flushed_visible_text


async def _on_permission_request(turn: _ChatTurn, event) -> Literal[_Flow.CONTINUE] | None:
    """Decide a permission request, cutting the segment only where a row lands.

    The cut is owed, not made: a grant answered with no row (YOLO, session
    trust, a native crew) leaves the reply running, and cutting there saved one
    message as several rows with nothing between them. Codex sends a sub-agent's
    approvals on the parent session in the middle of the parent's message. The
    slot pays the cut before the first durable row the decision appends, and
    the interactive card gets it before it is drawn.
    """
    slot = turn.slot
    # The id the request names the tool by is what the call's PostToolUse
    # spec hooks match on, as PreToolUse's do here.
    if event.tool_call_id:
        _post_identity = post_tool_identity(
            turn.client,
            event,
            is_shell=False,
            earlier=turn._pending_tool_ids.get(event.tool_call_id),
        )
        if _post_identity is not None:
            turn._pending_tool_ids[event.tool_call_id] = _post_identity
    if turn.assistant_text and not open_construct_at_end(turn.assistant_text):
        slot._owed_segment_cut = functools.partial(_pay_permission_cut, turn)
    try:
        flow = await _decide_permission_request(turn, event)
    finally:
        owed = getattr(slot, "_owed_segment_cut", None)
        slot._owed_segment_cut = None
    if flow is None and owed is not None:
        owed()
    return flow


async def _decide_permission_request(turn: _ChatTurn, event) -> Literal[_Flow.CONTINUE] | None:
    """Decide a permission request the turn can answer itself: hooks, trust reads, grants and auto-approve."""
    _deferred_cut_text = turn._deferred_cut_text
    _native_tracker = turn._native_tracker
    _refusal_notices = turn._refusal_notices
    _refusal_reasons = turn._refusal_reasons
    _wt_pending_coding = turn._wt_pending_coding
    client = turn.client
    session_key = turn.session_key
    slot = turn.slot
    state = turn.state
    # Drain the key as soon as its permission decision begins. The
    # stream handles one decision at a time, so one bounded key is
    # enough for every approval arm below and in _on_permission_prompt;
    # denials need no state. The key is the same digest the add site
    # stored, so the two ends agree by construction.
    _wt_key = _tcid_identity_key(event.tool_call_id)
    turn._wt_deciding_coding = bool(_wt_key and _wt_key in _wt_pending_coding)
    if _wt_key:
        _wt_pending_coding.discard(_wt_key)
    # Permission is part of the tool group, not a break in it —
    # leaving in_tool_group True ensures the post-tool text fallback
    # (in _on_text_chunk, chat_turn/stream_chunks.py) still fires once the tool resolves
    # and the LLM continues with text. Resetting it here was the
    # cause of pills staying in "running" state until the next tool
    # call or message end.
    # The segment cut before a row this decision lands is owed by
    # _on_permission_request. The same open-construct deferral as the
    # tool-call cut applies: an approval-gated call (parent or child)
    # that lands inside an open options marker, fence or widget would
    # otherwise split it and undo a deferral the tool-call site just
    # made.
    _open_construct = open_construct_at_end(turn.assistant_text)
    if turn.assistant_text and _open_construct:
        _deferred_cut_text = turn.assistant_text
        turn._deferred_cut_text = _deferred_cut_text
        logger.debug(
            "segment cut deferred on %s — %s still open",
            slot.key,
            _open_construct,
        )
    _pre_tool_hooks_fired = False
    # "Not applied — approve this command explicitly" notices from a
    # rung that declined its grant. Held until the request actually
    # reaches the interactive card: a later unconditional rung
    # (native crew, session trust, YOLO) may still approve it, and
    # then the notice asks for an approval nobody is asked for. Keyed
    # by the refusal's detail: the hook and trusted-pattern rungs
    # refuse one shadowed name with the same detail, and one card
    # needs that sentence once.
    _declined_grant_notices: dict[str, str] = {}
    turn._declined_grant_notices = _declined_grant_notices
    # Backend-subagent request whose SECURITY context is absent
    # (structured params missing, or shell with no recoverable
    # command — see AcpEvent.child_low_fidelity): every
    # auto-approve gate below is skipped for it, falling through
    # to the interactive card. A child WITH full context takes the
    # same branches as the main agent (mode parity).
    _child_low_fidelity = event.child_low_fidelity
    # Verified-identity half of the fidelity split — see
    # AcpEvent.child_unconditional_grant_eligible for which grant
    # paths may honor it (unconditional grants: trust-all / YOLO /
    # native crew) and which must not (content-matching paths).
    _child_grant_eligible = event.child_unconditional_grant_eligible
    if _child_low_fidelity:
        # Diagnostic for a path that is otherwise invisible in
        # logs: without it a trust-all session watching its
        # subagent stall on an approval card has no log line to
        # find (the annotate-and-prompt branch is silent).
        logger.info(
            "low-fidelity child permission request (child=%s, " "mcp_identity=%s): %s",
            event.sub_session_id,
            (
                f"verified {event.mcp_server_name}/{event.tool_name}"
                if event.child_mcp_identity_trusted
                else "unverified"
            ),
            (
                "unconditional and identity-keyed grants still apply"
                if _child_grant_eligible
                else "all auto-approve paths skipped"
            ),
        )
    # DISPLAY-ONLY warning for the interactive card: the human
    # must know the title is ALL there is (the params the gates
    # would verify are absent, so the displayed text is
    # agent-authored and unverifiable). Computed HERE — outside
    # the context-builder block — so the card is labeled whenever
    # the fidelity guards are active, including hosts with no
    # context builder. Never written into event.title: the
    # TrustDropdown derives its learned patterns from the title,
    # and a mutated title would store junk pattern entries for
    # exactly the requests that need the clearest presentation.
    _child_lf_warning = (
        "⚠️ UNVERIFIED child request (security context " "missing — title is agent-authored): "
        if _child_low_fidelity
        else ""
    )
    turn._child_lf_warning = _child_lf_warning
    if state.context_builder:
        # Pass the raw shell command (not just the display title)
        # so the security gate evaluates what actually executes.
        # event.title may be an LLM-authored description that hides
        # a dangerous command (see HookManager.on_tool_call).
        tool_result = await run_in_tool_gate_pool(
            state.context_builder.hooks.on_tool_call,
            event.title,
            on_queue_timeout=hook_gate_busy,
            session_key=session_key,
            agent=slot.agent or "",
            app=slot._app or "",
            **hook_gate_kwargs(event),
            # The RESOLVED agent (what actually served the turn), not
            # slot.agent — that is an alias resolve_agent_bindings
            # maps to a concrete kiro agent, so it must never decide
            # which builtin app an agent belongs to.
            resolved_agent=read_effective_agent(client),
            # The slot's Reads approval mode, so the gate's read-only
            # tier may prove one of Kiro Crew's OWN read-only MCP
            # tools by its trusted identity (``@kirocrew-core/
            # memory_recall`` and its siblings). A surface fact, like
            # ``classifier_only``: it unlocks that one proof and
            # nothing else, and the auto-approve it yields walks the
            # same hook branch below -- PreToolUse hooks and all.
            trust_reads=bool(slot._trust_reads),
        )
        if tool_result.action == TOOL_DENY:
            # Surface WHY: carry the deny reason into the pill so
            # the user sees "Blocked by security policy: ..." rather
            # than an opaque "(blocked)" (or, on the claude provider,
            # a cryptic "Tool use aborted" with no explanation).
            _deny_reason = (tool_result.reason or "blocked").strip()
            _deny_title = _redact_display_text(event.title)
            _deny_msg, _ = redact_exfiltration_urls(_deny_reason)
            _deny_msg, _ = redact_credentials(_deny_msg)
            # Audit FIRST, before any wire I/O for this decision
            # (see _reject_hook_blocked): a stalled ACP reader
            # cancels this coroutine at the turn deadline, and an
            # SEL write sequenced after the awaits never runs.
            sel().log_tool_invocation(
                session_key=session_key,
                agent=slot.agent or "kirocrew",
                source="dashboard",
                tool_name=_deny_title,
                tool_kind=event.tool_kind,
                outcome="denied",
                request_id=event.request_id,
                error="hook_deny",
            )
            # In-band first, and BEFORE the rejection goes on the
            # wire: holding the unanswered permission request is what
            # guarantees the turn is still in flight, so the notice is
            # queued and folded in at the boundary after this tool
            # resolves. Without it the model only ever sees kiro-cli's
            # generic "User denied tool execution" and concludes the
            # user cancelled.
            await _steer_policy_notice(
                client, _deny_title, _deny_msg, _refusal_notices, slot, state
            )
            await _reject_attributed(client, event.request_id, attribution="steered")
            slot.append(
                "tool",
                f"🚫 {_deny_title} — {_deny_msg}",
                "msg msg-tool",
                meta=_tool_meta(event),
            )
            # Broadcast a visible activity event (mirrors the
            # auto-approve branch) so the block isn't silent.
            state.broadcast_ws(
                "activity_event",
                {
                    "slot": slot.key,
                    "kind": "permission",
                    "text": f"Blocked: {_deny_title} — {_deny_msg}",
                },
            )
            # Recoverable host-gate refusal: record for auto-recovery.
            # _deny_title/_deny_msg are ALREADY redacted just above
            # (redact_exfiltration_urls + redact_credentials) for the
            # display pill; reuse those sanitized values so the
            # model-bound recovery prompt never sees raw command
            # fragments, paths, or credentials.
            _refusal_reasons.append((_deny_title, _deny_msg))
            return _Flow.CONTINUE
        if _child_low_fidelity:
            # Backend-subagent origin whose tool_call frames never
            # reached us (cache miss): command bytes are absent, so
            # every gate below would judge the LLM-authored title
            # alone. Fail closed past all auto-approve paths — the
            # request falls through to the interactive card (which
            # carries the _child_lf_warning display prefix). When
            # the child's session/update frames WERE routed (the
            # normal case), tool_input carries the real command
            # bytes and the child takes the exact same mode
            # branches as the main agent below.
            #
            # One grant survives the downgrade: a hook auto-approve
            # decided by the call's VERIFIED MCP identity
            # (ToolHookResult.identity_grant — the app-own-server
            # grant, or an ``auto_approve_tools`` pattern matched
            # against ``@server/tool`` from ``_meta.kiro``). Its
            # matched input is the same identity
            # ``child_mcp_identity_trusted`` verified, so a forged
            # title cannot reach it, and it is the user's own NARROW
            # grant — the alternative, session trust-all or YOLO,
            # approves every tool the child calls. A grant that read
            # the title, the payload's kind or a command is still
            # downgraded.
            _identity_grant_kept = identity_grant_covers_child(tool_result, event)
            if tool_result.action == TOOL_AUTO_APPROVE and not _identity_grant_kept:
                logger.info(
                    "downgrading auto-approve to interactive card for "
                    "low-fidelity subagent permission request (child=%s)",
                    event.sub_session_id,
                )
                tool_result = ToolHookResult(action=TOOL_ALLOW)
            elif _identity_grant_kept:
                logger.info(
                    "honoring identity-keyed hook auto-approve for "
                    "identity-verified subagent permission request "
                    "(child=%s, mcp=%s/%s)",
                    event.sub_session_id,
                    event.mcp_server_name,
                    event.tool_name,
                )
        if tool_result.action == TOOL_AUTO_APPROVE:
            # The hook layer granted this by NAME (its
            # `auto_approve_tools` globs, or the read-only allowlist).
            # Ask off-loop whether the names still identify the
            # programs they appear to name; a shadowed, agent-tree or
            # unidentified resolution falls through to the interactive
            # card instead. Done HERE rather than inside the hook
            # because the answer needs filesystem work that its
            # synchronous, loop-bound method must not perform.
            _hook_shim = await _name_grant_refusal_for(event)
            if _hook_shim is not None:
                # The LINE is gated, the audit row below is not: a
                # platform-scope refusal says the same thing about
                # every command this session runs.
                if should_log_decline(session_key, _hook_shim):
                    logger.warning(
                        "declining a hook auto-approve: %s; the request "
                        "falls through to interactive approval",
                        _hook_shim.log_text,
                    )
                _audit_name_grant_refusal(
                    session_key=session_key,
                    slot=slot,
                    event=event,
                    refusal=_hook_shim,
                    tier="hook_auto_approve",
                )
                _declined_grant_notices.setdefault(
                    _hook_shim.detail,
                    "🛡️ Auto-approve not applied — "
                    f"{_redact_display_text(_hook_shim.detail)}. "
                    "Approve this command explicitly.",
                )
                tool_result = ToolHookResult(action=TOOL_ALLOW)
        if tool_result.action == TOOL_AUTO_APPROVE:
            try:
                validated_tool = _validate_tool_name(
                    event.title,
                    is_shell=event.is_shell,
                    canonical_name=event.tool_name,
                )
            except ValueError as e:
                await _reject_invalid_tool(
                    client,
                    slot,
                    event,
                    session_key=session_key,
                    error=e,
                    refusal_notices=_refusal_notices,
                    state=state,
                    refusal_reasons=_refusal_reasons,
                )
            else:
                # Declarative auto-approve must NOT bypass scripted
                # PreToolUse hooks — those are the audit/policy gate
                # and exit-2 BLOCKED takes precedence over auto-approve.
                try:
                    _parsed_input = json.loads(event.tool_input) if event.tool_input else None
                except Exception:
                    _parsed_input = None
                try:
                    pre_hook_results = await _fire(
                        turn,
                        HOOK_EVENT_PRE_TOOL_USE,
                        tool_name=validated_tool,
                        tool_input=_parsed_input,
                        tool_id=event.harness_tool_id,
                        tool_identity=event.tool_name,
                        mcp_server=event.mcp_server_name,
                    )
                except Exception as hook_exc:
                    await _reject_hook_error(
                        client,
                        slot,
                        event,
                        session_key=session_key,
                        error=str(hook_exc),
                        refusal_reasons=_refusal_reasons,
                        refusal_notices=_refusal_notices,
                        state=state,
                    )
                    return _Flow.CONTINUE
                if _pre_tool_hooks_should_block(pre_hook_results):
                    await _reject_hook_blocked(
                        client,
                        slot,
                        event,
                        session_key=session_key,
                        pre_hook_results=pre_hook_results,
                        refusal_reasons=_refusal_reasons,
                        refusal_notices=_refusal_notices,
                    )
                    return _Flow.CONTINUE
                approval_sent = await client.approve_tool(event.request_id)
                if approval_sent is False:
                    sel().log_tool_invocation(
                        session_key=session_key,
                        agent=slot.agent or "kirocrew",
                        source="dashboard",
                        tool_name=_redact_display_text(event.title),
                        tool_kind=event.tool_kind,
                        outcome=OUTCOME_REJECTED_TRANSPORT_FLOOR,
                        request_id=event.request_id,
                    )
                    return _Flow.CONTINUE
                _wt_note_approved(turn, event)
                _tool_title = _broadcast_auto_tool(state, slot, event)
                # Defense-in-depth: _broadcast_auto_tool already
                # returns a redacted title, but re-redact before this
                # second external surface (activity feed + sel log) so
                # the guarantee is local and idempotent — event.title
                # is LLM-controlled display text (see below).
                _tool_title, _ = redact_exfiltration_urls(_tool_title)
                _tool_title, _ = redact_credentials(_tool_title)
                state.broadcast_ws(
                    "activity_event",
                    {
                        "slot": slot.key,
                        "kind": "permission",
                        "text": f"Auto-approved: {_tool_title}",
                    },
                )
                sel().log_tool_invocation(
                    session_key=session_key,
                    agent=slot.agent or "kirocrew",
                    source="dashboard",
                    tool_name=_tool_title,
                    tool_kind=event.tool_kind,
                    outcome="auto_approved",
                    request_id=event.request_id,
                )
            return _Flow.CONTINUE
        try:
            validated_tool = _validate_tool_name(
                event.title,
                is_shell=event.is_shell,
                canonical_name=event.tool_name,
            )
        except ValueError as e:
            await _reject_invalid_tool(
                client,
                slot,
                event,
                session_key=session_key,
                error=e,
                refusal_notices=_refusal_notices,
                state=state,
                refusal_reasons=_refusal_reasons,
            )
            return _Flow.CONTINUE
        try:
            _parsed_input = json.loads(event.tool_input) if event.tool_input else None
        except Exception:
            _parsed_input = None
        try:
            pre_hook_results = await _fire(
                turn,
                HOOK_EVENT_PRE_TOOL_USE,
                tool_name=validated_tool,
                tool_input=_parsed_input,
                tool_id=event.harness_tool_id,
                tool_identity=event.tool_name,
                mcp_server=event.mcp_server_name,
            )
        except Exception as hook_exc:
            await _reject_hook_error(
                client,
                slot,
                event,
                session_key=session_key,
                error=str(hook_exc),
                refusal_notices=_refusal_notices,
                state=state,
                refusal_reasons=_refusal_reasons,
            )
            return _Flow.CONTINUE
        if _pre_tool_hooks_should_block(pre_hook_results):
            await _reject_hook_blocked(
                client,
                slot,
                event,
                session_key=session_key,
                pre_hook_results=pre_hook_results,
                refusal_reasons=_refusal_reasons,
                refusal_notices=_refusal_notices,
            )
            return _Flow.CONTINUE
        _pre_tool_hooks_fired = True
        # Hooks passed — fall through to patterns/trust-reads/trust/yolo/interactive
    # Native crew auto-approve: when a native subagent (crew pipeline)
    # is active and the session is configured to auto-approve subagent
    # tools, approve immediately to avoid deadlocking the blocked
    # parent turn. Deny-by-default (CWE-1188): with no active crew this
    # predicate is False no matter the trust flags, so the tool falls
    # through to the normal trust gate below, then _on_permission_prompt's card.
    # The claude backend never takes either path. Its permission requests do not
    # say which session sent them, so an open child card cannot tell a child's
    # request from the parent's, and the parent's own tool calls would be approved
    # with no card; for the same reason a subagent's send to a crew member would
    # read as the conductor's own.
    _claude_backend = is_claude_backend_name(capabilities_of(client).backend)
    _own_worker_call = not _claude_backend and _crew_root_own_worker_call(state, slot, event)
    if (
        _own_worker_call
        or (not _claude_backend and _native_crew_should_auto_approve(_native_tracker, state, slot))
    ) and _child_grant_eligible:
        logger.debug(
            "Native crew auto-approve: %r (request_id=%s)",
            _safe_native_crew_debug_title(event.title),
            event.request_id,
        )
        approval_sent = await client.approve_tool(event.request_id)
        if approval_sent is False:
            sel().log_tool_invocation(
                session_key=session_key,
                agent=slot.agent or "kirocrew",
                source="dashboard",
                tool_name=_redact_display_text(event.title),
                tool_kind=event.tool_kind,
                outcome=OUTCOME_REJECTED_TRANSPORT_FLOOR,
                request_id=event.request_id,
            )
            return _Flow.CONTINUE
        _wt_note_approved(turn, event)
        _tool_title = _broadcast_auto_tool(state, slot, event)
        # Defense-in-depth: re-redact before this second external
        # surface (activity feed + sel log). event.title is
        # LLM-controlled display text; _broadcast_auto_tool already
        # redacts, so both passes are idempotent.
        _tool_title, _ = redact_exfiltration_urls(_tool_title)
        _tool_title, _ = redact_credentials(_tool_title)
        state.broadcast_ws(
            "activity_event",
            {
                "slot": slot.key,
                "kind": "permission",
                "text": f"Auto-approved (crew): {_tool_title}",
            },
        )
        sel().log_tool_invocation(
            session_key=session_key,
            agent=slot.agent or "kirocrew",
            source="dashboard",
            tool_name=_tool_title,
            tool_kind=event.tool_kind,
            outcome="auto_approved",
            request_id=event.request_id,
            metadata={"reason": "crew_mode_own_worker" if _own_worker_call else "native_crew"},
        )
        return _Flow.CONTINUE
    # Session-trusted patterns: auto-approve commands matching user globs.
    # Security: shell scope comes from structured tool_input; non-shell
    # scope comes from the ACP-cached canonical server/tool identity.
    # event.title is model-authored display prose and is NEVER authority.
    # Missing identity, unrecognized structured input, or transport
    # redaction skips matching (deny-by-default). Otherwise a reused title
    # or collapsed redaction marker could authorize a different tool.
    #
    # A backend-subagent request whose ARGUMENTS are unverified but whose
    # canonical MCP identity is (child_mcp_identity_trusted) is admitted
    # too: approval_command keys a non-shell grant on exactly that
    # identity and on nothing the agent authors, so the grant the user
    # clicked for this tool covers the child's call to the same tool. A
    # child with no verified identity stays excluded. The admission
    # rule is AcpEvent.child_unconditional_grant_eligible — the same
    # boolean the unconditional grants below read — bound once above
    # as _child_grant_eligible, so it cannot drift here.
    #
    # Timed grants are checked HERE, at decision time, rather than
    # by a timer: a grant past its deadline is dropped before the
    # match, so it can never approve the call in front of it. They
    # live in their own store, so the set below is the chat-long
    # patterns plus whichever timed ones are still live.
    _tp_deadlines = getattr(slot, "_trusted_pattern_deadlines", None)
    _tp_live = (
        _live_trusted_patterns(
            slot._trusted_patterns,
            _tp_deadlines,
            wall=time.time(),
            mono=time.monotonic(),
        )
        if _tp_deadlines
        else slot._trusted_patterns
    )
    if _tp_live and _child_grant_eligible and not event.tool_input_redacted:
        _tp_command = approval_command(
            event.tool_input or "",
            is_shell=event.is_shell,
            tool_name=event.tool_name,
            mcp_server_name=event.mcp_server_name,
            raw_tool_params=event.raw_tool_params,
        )
        matched = _matches_trusted_pattern(_tp_command, _tp_live) if _tp_command else None
        if (
            matched
            and event.is_shell
            and _tp_command
            and _redirect_outside_grant(_tp_command, _tp_live)
        ):
            # The user granted a program, not the files the shell
            # opens for it: a write redirect needs a grant that
            # spells it. Declining costs one interactive prompt;
            # the command is neither blocked nor rewritten.
            logger.info("trusted pattern not applied: the grant does not cover a write redirect")
            sel().log_tool_invocation(
                session_key=session_key,
                agent=slot.agent or "kirocrew",
                source="dashboard",
                tool_name=_redact_display_text(event.title),
                tool_kind=event.tool_kind,
                outcome="auto_approve_declined",
                request_id=event.request_id,
                error="the command may write through a redirect the grant does not cover",
                metadata={
                    "reason": "redirect_outside_grant",
                    "tier": "trusted_pattern",
                },
            )
            _declined_grant_notices.setdefault(
                "redirect_outside_grant",
                "🛡️ Trusted pattern not applied — the command may write through "
                "a redirect, which the grant does not cover. "
                "Approve this command explicitly.",
            )
            matched = None
        if matched and event.is_shell and _tp_command:
            # The user granted a PROGRAM NAME. Do not honour it when
            # that name does not identify the program it appears
            # to name — the shell resolves it again, through a PATH
            # that can lead with directories the agent writes.
            # Declining costs one interactive prompt; the command is
            # neither blocked nor rewritten.
            #
            # `is_shell` is tested explicitly because `_tp_command`
            # is non-empty for a non-shell grant too, where it is a
            # canonical `mcp-trust:v1:...` identity rather than a
            # command. There is no program name to vouch for there,
            # so that tier stays unchanged.
            _tp_shim = await _name_grant_refusal_off_loop(_tp_command)
            if _tp_shim:
                # The CODE, not the detail and not the pattern: both
                # are derived from user/agent input, and a log sink is
                # where that becomes a disclosure. The detail still
                # reaches the person, on _on_permission_prompt's card.
                logger.warning("trusted pattern not applied: %s", _tp_shim.log_text)
                _audit_name_grant_refusal(
                    session_key=session_key,
                    slot=slot,
                    event=event,
                    refusal=_tp_shim,
                    tier="trusted_pattern",
                )
                _declined_grant_notices.setdefault(
                    _tp_shim.detail,
                    "🛡️ Trusted pattern not applied — "
                    f"{_redact_display_text(_tp_shim.detail)}. "
                    "Approve this command explicitly.",
                )
                matched = None
        # The program-name check above and the transport's own floor
        # check are awaited hops, long enough for a timed grant to
        # pass its deadline (a slow pool, a host suspended mid-call).
        # An approval that rests on timed grants carries their
        # deadline down to the send, where the transport checks it
        # last; one the chat-long patterns cover alone carries none.
        _tp_deadline: GrantDeadline | None = (
            _timed_grant_deadline(_tp_command, slot._trusted_patterns, _tp_deadlines)
            if matched and _tp_deadlines
            else None
        )
        if matched:
            try:
                validated_tool = _validate_tool_name(
                    event.title,
                    is_shell=event.is_shell,
                    canonical_name=event.tool_name,
                )
            except ValueError as e:
                await _reject_invalid_tool(
                    client,
                    slot,
                    event,
                    session_key=session_key,
                    error=e,
                    refusal_notices=_refusal_notices,
                    state=state,
                    metadata={
                        "reason": "invalid_tool_name",
                        "pattern": matched,
                    },
                    refusal_reasons=_refusal_reasons,
                )
                return _Flow.CONTINUE
            if _tp_deadline is None:
                approval_sent = await client.approve_tool(event.request_id)
            else:
                # A timed grant backs this approval: the transport
                # checks its deadline again after the floor and, if it
                # has passed, sends nothing and raises. The call then
                # falls through to the normal prompt in _on_permission_prompt.
                try:
                    approval_sent = await client.approve_tool(
                        event.request_id, deadline=_tp_deadline
                    )
                except GrantEnded:
                    approval_sent = None
            if approval_sent is not None:
                if approval_sent is False:
                    sel().log_tool_invocation(
                        session_key=session_key,
                        agent=slot.agent or "kirocrew",
                        source="dashboard",
                        tool_name=_redact_display_text(event.title),
                        tool_kind=event.tool_kind,
                        outcome=OUTCOME_REJECTED_TRANSPORT_FLOOR,
                        request_id=event.request_id,
                    )
                    return _Flow.CONTINUE
                _wt_note_approved(turn, event)
                _tool_title = _broadcast_auto_tool(state, slot, event)
                _tool_title, _ = redact_exfiltration_urls(_tool_title)
                _tool_title, _ = redact_credentials(_tool_title)
                slot.append(
                    "tool",
                    f"🔧 {_tool_title}",
                    "msg msg-tool",
                    # `_tool_meta` for parity with every other rung: same
                    # `_MAX_TOOL_PURPOSE` cap (a hand-rolled 200-char cap
                    # here truncated restored purposes mid-sentence), same
                    # redacted `tool_call_id` (the live↔replay join key),
                    # and `input`/`kind` so historical rows open the same
                    # inline detail panel as live ones.
                    meta=_tool_meta(event),
                )
                sel().log_tool_invocation(
                    session_key=session_key,
                    agent=slot.agent or "kirocrew",
                    source="dashboard",
                    tool_name=_tool_title,
                    tool_kind=event.tool_kind,
                    outcome="auto_approved",
                    request_id=event.request_id,
                    metadata={"reason": "trusted_pattern", "pattern": matched},
                )
                return _Flow.CONTINUE
    # Trust-reads: auto-approve read-only bash commands
    # Detect bash tools by tool_input content (title is human-readable)
    cmd = _extract_bash_command(event.tool_input) if event.tool_input else ""
    yolo_active = state.is_yolo_active()
    # Evaluated ONCE for both branches below. Two separate calls could
    # straddle a scoped grant's expiry and disagree with each other, and
    # a scope check is not a pure read — it retires a lapsed grant and
    # logs that, which must happen once per event, not twice.
    slot_trusted = _slot_is_trusted(slot)
    if (
        slot._trust_reads
        and not slot_trusted
        and not yolo_active
        and cmd
        and not _child_low_fidelity
    ):
        # Trust-reads never auto-approves git: git reads
        # ``.git/config`` on every invocation and ``core.fsmonitor``
        # / ``core.hooksPath`` name a program there that no text
        # classifier sees. A git command falls through to the card.
        _tr_read_only = is_read_only_bash(cmd) and not command_invokes_git(cmd)
        _tr_shim = await _name_grant_refusal_off_loop(cmd) if _tr_read_only else None
        if _tr_shim is not None:
            logger.warning("trust-reads not applied: %s", _tr_shim.log_text)
            _audit_name_grant_refusal(
                session_key=session_key,
                slot=slot,
                event=event,
                refusal=_tr_shim,
                tier="trust_reads",
            )
        if _tr_read_only and _tr_shim is None:
            try:
                validated_tool = _validate_tool_name(
                    event.title,
                    is_shell=event.is_shell,
                    canonical_name=event.tool_name,
                )
            except ValueError as e:
                await _reject_invalid_tool(
                    client,
                    slot,
                    event,
                    session_key=session_key,
                    error=e,
                    metadata={"reason": "trust_reads"},
                    refusal_reasons=_refusal_reasons,
                    refusal_notices=_refusal_notices,
                    state=state,
                )
                return _Flow.CONTINUE
            approval_sent = await client.approve_tool(event.request_id)
            if approval_sent is False:
                sel().log_tool_invocation(
                    session_key=session_key,
                    agent=slot.agent or "kirocrew",
                    source="dashboard",
                    tool_name=_redact_display_text(event.title),
                    tool_kind=event.tool_kind,
                    outcome=OUTCOME_REJECTED_TRANSPORT_FLOOR,
                    request_id=event.request_id,
                )
                return _Flow.CONTINUE
            _wt_note_approved(turn, event)
            _tool_title = _broadcast_auto_tool(state, slot, event)
            slot.append(
                "tool",
                f"🔧 {_tool_title}",
                "msg msg-tool",
                meta=_tool_meta(event),
            )
            sel().log_tool_invocation(
                session_key=session_key,
                agent=slot.agent or "kirocrew",
                source="dashboard",
                tool_name=_redact_display_text(event.title),
                tool_kind=event.tool_kind,
                outcome="auto_approved",
                request_id=event.request_id,
                metadata={"reason": "trust_reads"},
            )
            return _Flow.CONTINUE
    # Trust mode (per-slot) or YOLO mode (global) — auto-approve.
    # Both are UNCONDITIONAL grants: the decision consumes no
    # agent-authored event data, so a low-fidelity child event
    # with a VERIFIED canonical MCP identity still qualifies
    # (_child_grant_eligible — arguments unverified, but the grant
    # never reads them). A child with neither full context nor a
    # verified identity falls through to the interactive card;
    # children WITH cached bytes take these branches exactly like
    # the main agent (mode parity).
    if (slot_trusted or yolo_active) and _child_grant_eligible:
        try:
            validated_tool = _validate_tool_name(
                event.title,
                is_shell=event.is_shell,
                canonical_name=event.tool_name,
            )
        except ValueError as e:
            await _reject_invalid_tool(
                client,
                slot,
                event,
                session_key=session_key,
                error=e,
                refusal_notices=_refusal_notices,
                state=state,
                refusal_reasons=_refusal_reasons,
            )
            return _Flow.CONTINUE
        if not _pre_tool_hooks_fired:
            try:
                _parsed_input = json.loads(event.tool_input) if event.tool_input else None
            except Exception:
                _parsed_input = None
            try:
                pre_hook_results = await _fire(
                    turn,
                    HOOK_EVENT_PRE_TOOL_USE,
                    tool_name=validated_tool,
                    tool_input=_parsed_input,
                    tool_id=event.harness_tool_id,
                    tool_identity=event.tool_name,
                    mcp_server=event.mcp_server_name,
                )
            except Exception as hook_exc:
                await _reject_hook_error(
                    client,
                    slot,
                    event,
                    session_key=session_key,
                    error=str(hook_exc),
                    refusal_notices=_refusal_notices,
                    state=state,
                    refusal_reasons=_refusal_reasons,
                )
                return _Flow.CONTINUE
            if _pre_tool_hooks_should_block(pre_hook_results):
                await _reject_hook_blocked(
                    client,
                    slot,
                    event,
                    session_key=session_key,
                    pre_hook_results=pre_hook_results,
                    refusal_reasons=_refusal_reasons,
                    refusal_notices=_refusal_notices,
                )
                return _Flow.CONTINUE
        # always=False — Kiro Crew owns trust scope; per-call request_permission
        # is required for PreToolUse hooks to run on every tool invocation.
        approval_sent = await client.approve_tool(event.request_id)
        if approval_sent is False:
            sel().log_tool_invocation(
                session_key=session_key,
                agent=slot.agent or "kirocrew",
                source="dashboard",
                tool_name=_redact_display_text(event.title),
                tool_kind=event.tool_kind,
                outcome=OUTCOME_REJECTED_TRANSPORT_FLOOR,
                request_id=event.request_id,
            )
            return _Flow.CONTINUE
        _wt_note_approved(turn, event)
        _tool_title = _broadcast_auto_tool(state, slot, event)
        # Defense-in-depth: re-redact before the sel log (idempotent).
        _tool_title, _ = redact_exfiltration_urls(_tool_title)
        _tool_title, _ = redact_credentials(_tool_title)
        sel().log_tool_invocation(
            session_key=session_key,
            agent=slot.agent or "kirocrew",
            source="dashboard",
            tool_name=_tool_title,
            tool_kind=event.tool_kind,
            outcome="auto_approved",
            request_id=event.request_id,
            # Provenance, so an auditor can separate a human's session
            # trust from an unattended worker's expiring scoped grant.
            metadata={"reason": _auto_approve_reason(slot, yolo_active)},
        )
        return _Flow.CONTINUE
    return None
