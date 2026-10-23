"""How a turn with no interactive approver answers a tool permission request.

The approval policies, resolving one permission request through the deny checks, the
hook gate and the transport floor, and refusing a call by steering the host first,
with the orphaned rejects a cancelled refusal leaves scheduled.

Served through :mod:`kiro_crew.llm_helpers`, which callers and tests import and
patch; this module never imports it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from enum import Enum
from typing import TYPE_CHECKING, Any

from kiro_crew import name_grant, permission_floor
from kiro_crew.agent_sdk.drivers.acp_turn_vocab import LLMEvent, LLMProvider
from kiro_crew.constants import (
    DENY_CAUSE_INVALID_NAME,
    DENY_CAUSE_POLICY,
    DENY_CAUSE_SURFACE_POLICY,
)
from kiro_crew.deny_notice import steer_refusal_notice
from kiro_crew.executors import run_in_tool_gate_pool
from kiro_crew.hooks import (
    _EDIT_TOOL_KIND,
    _normalize_tool_name,
    hook_gate_kwargs,
    title_is_trusted_mcp_identity,
    untruncated_shell_title,
)
from kiro_crew.llm_helpers_parts.tool_input_denials import (
    _edit_target_denial,
    _extract_tool_input_strings,
    _first_tool_input_denial,
    _path_tier_exempt,
    _title_denial,
)
from kiro_crew.permission_floor import OUTCOME_REJECTED_TRANSPORT_FLOOR
from kiro_crew.platform.tool_paths import WRITE_PLANE_KINDS as _WRITE_PLANE_KINDS
from kiro_crew.platform.tool_paths import (
    command_shaped_strings,
    is_document_writing_tool,
    mcp_document_body_keys,
    split_document_bodies,
)
from kiro_crew.security import (
    is_denied,
    is_denied_synthesized_target,
    redact_credentials,
    redact_exfiltration_urls,
)
from kiro_crew.security.readonly_bash import command_invokes_git, is_read_only_bash
from kiro_crew.tool_gate_busy import hook_gate_busy

if TYPE_CHECKING:
    from kiro_crew.hooks import HookManager

logger = logging.getLogger("kiro_crew.llm_helpers")


# ── Tool Approval Policies ──


class ToolApprovalPolicy(Enum):
    """How to handle tool permission requests during streaming.

    ``READ_ONLY`` is the dashboard "Reads" approval mode's semantics ported to
    surfaces that have no interactive approver: provably read-only calls are
    auto-approved through the SAME hook gate the Reads mode uses (deny floor
    first, then the read-only classifier), and every call that is not provably
    read-only is rejected — where the Reads mode would ask, this policy
    refuses. Requires ``hooks``; without a gate to classify with it fails
    closed and rejects everything, exactly like ``REJECT_ALL``.

    Only the classifier's own verdict approves under ``READ_ONLY``. The hook
    gate is asked ``classifier_only``, so its GRANT tiers — the operator's
    ``auto_approve_tools`` globs and the app-own-server rule, which vouch for
    the caller and say nothing about what the call does — are skipped rather
    than honoured, and an auto-approve is then trusted only when the result
    carries the classifier's ``read_only`` tag. A grant that shadows a
    read-only call therefore still gets the read approved (by the classifier),
    and a grant that shadows a write approves nothing.

    With no approver to catch an over-approval, ``classifier_only`` also
    restricts proof to HOST-TRUSTED facts: the recovered shell command judged
    by ``is_read_only_bash``, or a built-in the host knows to be read-only
    (``hooks._HOST_READ_ONLY_BUILTIN_TOOLS``) named by the non-model-authored
    ``_meta.kiro.toolName`` with no MCP server behind it, and only when the
    event carries ``mcp_identity_trusted`` (the pair came from the
    provenance-verified caches, not an inline payload). The agent-influenced
    ACP ``kind`` and the model-authored title may narrow but never prove, so a
    mutating tool labelled ``kind="read"``, a read-looking title, and any
    MCP-served tool (no host-trusted read-only marker exists for one) are
    rejected here where the interactive Reads mode would still ask.
    """

    AUTO_APPROVE = "auto_approve"
    REJECT_ALL = "reject_all"
    HOOK_BASED = "hook_based"
    READ_ONLY = "read_only"


#: Rejects scheduled while a deny site was being CANCELLED mid-steer. Strongly
#: referenced so the event loop cannot drop them before they answer the wire.
_orphan_rejects: set[asyncio.Task[Any]] = set()


async def _steer_host_deny(
    provider: Any, event: Any, reason: str, *, cause: str, title: str = ""
) -> None:
    """Tell the model, in-band, that the HOST denied this call -- not the person.

    A rejected permission reaches the model as kiro-cli's fixed "User denied tool
    execution", so without this it reads a refusal that never happened and
    abandons or routes around a call nobody objected to. Awaited immediately
    BEFORE each host-deny ``reject_tool`` in ``kiro_crew.llm_helpers``: while the permission
    request is still unanswered the turn is provably in flight, which is what
    gets the notice queued rather than dropped (see ``kiro_crew.deny_notice``).

    Every deny in ``kiro_crew.llm_helpers`` is a HOST verdict, but *cause* says which kind,
    and it is REQUIRED because the wrong noun sends the model the wrong way.
    ``DENY_CAUSE_POLICY`` is for a safety rule judging the call itself (an
    always-deny pattern, a hook deny, a withheld name grant): its notice appends
    class-specific remediation keyed off the reason and the model's own title.
    ``DENY_CAUSE_SURFACE_POLICY`` is for the surface refusing the call (the
    reject-all / read-only policies, the tool-free background one-liner): no
    remediation, because on a surface where the tool cannot run at all, telling
    the model which sanctioned command to run instead -- triggered by nothing
    more than a credential-shaped word in its own title -- would be a second
    wall. ``DENY_CAUSE_INVALID_NAME`` is for the empty title, the one deny the
    model can simply fix. The one genuine USER rejection
    (``interactive_rejected``) must NOT call this: there kiro-cli's wording is
    the truth, and "this was NOT a user action" would be a lie.
    ``test_llm_helpers_deny_notice`` walks those modules to keep both halves honest.

    *reason* may echo agent-authored text (a matched path, a hook's reason), so it
    is redacted here; the shared helper redacts the title. *title* overrides the
    event's own when a caller renders the call differently -- the channel agent
    stream (``kiro_crew.channel``) reuses this helper and names a permission
    request by ``event.text`` where ``event.title`` is empty. The reason is
    otherwise passed VERBATIM, as the chat runner does: a rule-authored refusal's
    fixed lead (``Blocked by security policy: ``, the unverifiable-path stall
    prefix) is the structural key ``deny_guidance.classify_deny`` reads before any
    text scan, and trimming it would let an agent-spelled path move a stall into
    the credential class. The synthetic reasons ``kiro_crew.llm_helpers`` authors carry no lead
    of their own, since the notice writes ``Blocked: <title>: <reason>`` itself,
    and a title-less call (the empty-title deny) is named rather than left as a
    bare colon. Best-effort by
    construction: ``steer_refusal_notice`` probes ``supports_refusal_steer`` and swallows
    every failure, so a backend without a steer channel behaves exactly as before
    and the caller's reject always runs.

    Cancellation mid-steer (a timed background turn, a stalled pipe hitting the
    turn deadline) must still answer the wire: a stranded
    ``session/request_permission`` blocks the backend forever and wedges every
    later turn behind it, and the caller's own ``reject_tool`` is the statement
    the cancellation skips. The reject is scheduled as a strongly referenced
    task (``_orphan_rejects`` keeps it alive, ``_orphan_reject_done`` retires it
    and reads its outcome) and this coroutine re-raises IMMEDIATELY. It does not
    wait for the reject, not even bounded: the cancellation IS the caller's
    deadline, and the pipe that stalled the steer is the pipe the reject drains
    into, so any wait here runs after the caller's budget is spent and stretches
    a declared bound (a 5 s ``run_bg_oneliner`` would resolve at 10 s). The
    event loop steps the orphan task while the caller unwinds. Whether it still
    reaches the wire depends on the caller's teardown: the multiplexed
    ``_ProviderBgSession.destroy()`` only releases its turn semaphore and the
    shared transport stays up, while a runtime-backed ``AcpSessionHandle`` is
    terminated by its ``destroy()`` and the write then fails -- which
    ``_orphan_reject_done`` reports as the unanswered request it is, the same
    end state the caller's own skipped ``reject_tool`` would have left behind,
    now visible in the log. The SEL row for the decision is already written --
    every caller audits before this await.
    """
    safe_reason, _ = redact_exfiltration_urls(reason or "")
    safe_reason, _ = redact_credentials(safe_reason)
    title = title or str(getattr(event, "title", "") or "") or "unnamed tool call"
    try:
        await steer_refusal_notice(provider, title, safe_reason, cause=cause)
    except asyncio.CancelledError:
        reject = asyncio.ensure_future(provider.reject_tool(event.request_id))
        _orphan_rejects.add(reject)
        reject.add_done_callback(_orphan_reject_done)
        raise


def _orphan_reject_done(task: asyncio.Task[Any]) -> None:
    """Retire an orphan reject and RETRIEVE its outcome.

    ``_steer_host_deny`` re-raises without awaiting this task; if the caller then
    tears the transport down, the write fails after nobody is awaiting it.
    Reading the exception here is what keeps
    asyncio from reporting a bare "Task exception was never retrieved" at GC
    instead of the real signal -- that a permission request went unanswered.
    """
    _orphan_rejects.discard(task)
    if task.cancelled():
        logger.debug("orphan reject_tool cancelled before it reached the wire")
        return
    exc = task.exception()
    if exc is not None:
        logger.warning(
            "orphan reject_tool failed; the permission request may be unanswered: %r", exc
        )


async def _resolve_permission(
    provider: LLMProvider,
    event: LLMEvent,
    policy: ToolApprovalPolicy,
    hooks: HookManager | None,
    on_tool_approval: Callable[[LLMEvent], Awaitable[bool]] | None = None,
    session_key: str = "",
    agent: str = "",
    app: str = "",
    on_decision: Callable[[str, str, str], None] | None = None,
) -> bool:
    """Resolve a tool permission request. Returns True if approved.

    *on_decision*, when given, receives ``(outcome, mechanism, reason)`` for the
    single decision this call makes — the same values that reach the SEL audit
    row (``reason`` is the row's ``error``: the refusal text naming the rule that
    matched, empty for an approval).
    ``mechanism`` is one of ``"always_deny"`` / ``"always_deny_input"`` (the
    unconditional checks), ``"always_deny_hook"`` (a HookManager security deny),
    ``"policy_deny"`` (the governance ceiling, whether reached through the hook
    gate or through a regex-tier rule that only a governance PIN put back into
    the effective set), or ``""`` for an approval or an interactive rejection.
    The ``always_deny`` prefix is therefore what marks a refusal as "the attempt
    itself was the problem"; everything else describes policy state or an absent
    approver. Note that the same regex match can land under either label — what
    decides it is whether the rule survives without its pin.
    """
    from kiro_crew.hooks import TOOL_AUTO_APPROVE, TOOL_DENY
    from kiro_crew.sel import sel

    def _log(outcome: str, **extra):
        # Single funnel for every decision path, so the sink cannot miss one.
        if on_decision is not None:
            _meta = extra.get("metadata") or {}
            try:
                on_decision(
                    outcome, str(_meta.get("mechanism") or ""), str(extra.get("error") or "")
                )
            except Exception:
                logger.debug("on_decision sink failed", exc_info=True)
        sel().log_tool_invocation(
            session_key=session_key,
            agent=agent,
            tool_name=event.title,
            tool_kind=event.tool_kind,
            outcome=outcome,
            request_id=event.request_id,
            **extra,
        )

    # Audit FIRST, before any wire I/O for this decision, at every deny below:
    # the steer and the rejection both await the ACP pipe, and a backend that
    # stops reading stdin blocks those awaits until the turn deadline cancels
    # this coroutine -- an SEL write sequenced after them never runs (see
    # test_deny_audit_first for the chat runner's statement of the same rule).
    # The audit raises: a decision whose SEL row cannot be written is not
    # answered on the wire at all (backend-security-controls), so a deny never
    # proceeds unaudited. The caller's own deadline bounds the unanswered
    # request.
    if policy == ToolApprovalPolicy.REJECT_ALL:
        _log("rejected", metadata={"reason": "reject_all_policy"})
        await _steer_host_deny(
            provider,
            event,
            "this surface runs under a reject-all tool policy",
            cause=DENY_CAUSE_SURFACE_POLICY,
        )
        await provider.reject_tool(event.request_id)
        return False

    # ── Always-enforced deny checks (regardless of approval policy) ──
    # These run even for AUTO_APPROVE callers (workflows, crons, etc.)
    # to ensure BUILTIN_DENY_PATTERNS and sensitive-path protection cannot
    # be bypassed by callers that skip HookManager wiring.
    # The empty-title guard, the deny-regex/bash tiers and the reported title
    # all read the RAW title, exactly as the hook side does: its deny tier runs
    # over BOTH ``GateFacts.normalized`` AND the raw ``call.title``
    # (``deny_targets``), and a deny rule such as ``^Running: ...`` is written
    # against the prefixed title, so stripping it here would stop that rule
    # matching and let a denied tool through. Only the IDENTITY exemption below
    # reads the normalized title, matching ``GateFacts.exempt_identity`` (which
    # uses ``self.normalized``): that is the one check the two sides must agree
    # on -- ``Running: @server/tool`` and ``@server/tool`` name the same
    # verified identity, so both must be exempt.
    normalized = event.title or ""
    if not normalized:
        _log("denied", error="Blocked: missing tool title", metadata={"mechanism": "always_deny"})
        await _steer_host_deny(
            provider, event, "the tool call carried no title", cause=DENY_CAUSE_INVALID_NAME
        )
        await provider.reject_tool(event.request_id)
        return False
    # The frame named an MCP server or tool longer than Crew's own tool surface
    # admits (``mcp_identity_unreadable``): not a call that names no tool but one
    # whose exact per-tool deny cannot be checked, so it is refused here on every
    # surface -- the same as the handle and the client do before this tier -- and
    # never judged by its title under an empty identity.
    if getattr(event, "mcp_identity_unreadable", False):
        _log(
            "denied",
            error="Blocked: MCP tool identity is unreadable (name over the tool-surface bound)",
            metadata={"mechanism": "always_deny"},
        )
        await _steer_host_deny(
            provider,
            event,
            "the tool call named an MCP server or tool longer than the tool surface admits",
            cause=DENY_CAUSE_INVALID_NAME,
        )
        await provider.reject_tool(event.request_id)
        return False
    # The title tier judges a shell title in kiro-cli's cut shape (a leading
    # slice of the recovered command plus ``...``) in its uncut form,
    # exactly as ``hooks.on_tool_call`` does (``untruncated_shell_title``); any
    # other title is judged verbatim.
    _shell_command = getattr(event, "shell_command", None)
    judged_title = untruncated_shell_title(
        normalized, _shell_command if isinstance(_shell_command, str) else None
    )
    # Honor the user's Settings>Security opt-out + governance pins on this
    # surface too (cron / Slack / workflow / heartbeat). Without threading the
    # effective set, is_denied() fails closed to ALL built-ins here, which would
    # re-introduce "disabled but still blocked" on every non-dashboard surface.
    # No HookManager (rare) → None → fail-closed default (all built-ins).
    _denied_regexes = hooks.effective_denied_regexes() if hooks is not None else None

    def _regex_deny_mechanism(probe: str, unconditional: str) -> str:
        """Classify an already-decided regex-tier deny by its provenance.

        A governance pin re-adds a built-in rule the user disabled, so the SAME
        match can mean either "this host enforces this rule" or "policy
        currently overrides this host's opt-out". Only the former says anything
        about the tool being attempted; treating the latter as a security block
        durably auto-pauses a cron that a later policy loosening cannot revive,
        because clearing the pause never restores ``enabled``.

        Re-runs the match against the pin-free set — deny path only, so the
        common allow path pays nothing — and reports a match that survives ONLY
        with pins as policy state.
        """
        if hooks is None:
            return unconditional
        try:
            _unpinned = hooks.effective_denied_regexes(include_governance_pins=False)
        except Exception:
            # Classification must never change the deny itself. An unresolvable
            # opt-out state falls back to the unconditional label: over-counting
            # a block is recoverable by an operator, silently not counting one
            # restores the runaway this gate exists to catch.
            logger.debug("deny provenance unresolved; reporting unconditional", exc_info=True)
            return unconditional
        return unconditional if is_denied(probe, denied_regexes=_unpinned) else "policy_deny"

    # Defense-in-depth: the title AND every string in event.tool_input go through
    # the same three predicates. The title usually carries the full path/command
    # (kiro-cli convention), but tool_input may contain additional arguments or
    # the actual path when the title is a generic tool name (e.g. "Read", "Bash").
    _tool_input = event.tool_input or ""
    # A file EDIT's tool_input is the document being written, not a command
    # line; its gate is the target path (see _edit_target_denial). The reroute is
    # taken only on TRUSTED provenance, never on the payload's own word:
    # ``tool_kind`` on a permission frame is the agent-influenced ``kind`` the
    # payload carries (display/telemetry metadata -- see _dispatch), so a shell
    # call could forge ``kind="edit"`` to skip the command scan. What the client
    # itself established from the preceding tool_call frame is ``shell_classified``
    # (the shell cache hit) with ``is_shell`` False, and ``raw_params_trusted`` (the
    # params came from that same cache, not an inline fallback). A frame missing
    # any of those has no proven target to judge and keeps the document scan as
    # the fail-closed fallback. Once rerouted, the target set is the params'
    # paths plus ``event.diff_path`` (the content block's path the client
    # cached), and an empty set is denied -- see _edit_target_denial.
    _edit_params = (
        event.raw_tool_params
        if (
            event.tool_kind == _EDIT_TOOL_KIND
            and event.shell_classified
            and not event.is_shell
            and event.raw_params_trusted
            and isinstance(event.raw_tool_params, dict)
        )
        else None
    )
    # Target-gating and document-scan suppression are SEPARATE decisions. A
    # diff content block is write-plane evidence on its own — ``diff_path`` is
    # the client's own cache from the preceding tool_call frame, not the
    # agent-influenced ``kind`` — so the target denial also runs for a
    # kindless or mislabelled non-shell call that carries one (strictly
    # tightening: that call keeps its document scan below AND gains the
    # target gate). Suppressing the document scan stays keyed on the fully
    # trusted edit reroute (``_edit_params is not None``) alone.
    #
    # A DELETE (KAS ``delete_file``, ACP kind ``delete``) is on the write plane
    # too -- the file is gone -- and carries no document body, so it gains the
    # target gate under the same trusted-provenance conditions as an edit and
    # keeps its scan. Same predicate the hook tier and governance route on
    # (``tool_paths.is_edit_call`` / ``WRITE_PLANE_KINDS``), so the three cannot
    # disagree on which kinds a config-protecting hard deny applies to.
    _trusted_delete = (
        isinstance(event.tool_kind, str)
        and event.tool_kind in _WRITE_PLANE_KINDS
        and event.tool_kind != _EDIT_TOOL_KIND
        and event.shell_classified
        and not event.is_shell
        and event.raw_params_trusted
        and isinstance(event.raw_tool_params, dict)
    )
    _edit_target_gated = (
        _edit_params is not None or _trusted_delete or bool(event.diff_path and not event.is_shell)
    )
    # The params the target gate judges: an edit's (rerouted) or a trusted
    # delete's; a diff-only route passes None and is judged by ``diff_path``.
    _gated_params = (
        _edit_params
        if _edit_params is not None
        else (event.raw_tool_params if _trusted_delete else None)
    )
    # Every OTHER non-shell tool with client-established provenance gets a
    # FIELD-SCOPED scan: the same three predicates, over every string in the
    # trusted params except a document body (``platform.tool_paths.
    # DOCUMENT_BODY_KEYS`` -- ``content``, ``fileText``, ``newStr``, ...). A body
    # is prose or source, and reading it as a shell command line refused a write
    # that merely QUOTED ``rm -rf /`` or named a credential path. Provenance is
    # the client's, never the payload's: ``shell_classified`` with ``is_shell``
    # False (the shell cache the preceding tool_call frame populated -- a shell
    # tool keeps the full scan, for it ``command`` IS what executes),
    # ``raw_params_trusted`` (params from that same cache, so the strings judged
    # are the ones that execute), and ``mcp_identity_trusted`` (the tool_name /
    # server caches HIT, so the tool is a resolved built-in or a resolved MCP
    # tool, not an unknown), and the resolved name must be a BUILT-IN document
    # writer (``platform.tool_paths.is_document_writing_tool``): an MCP tool can
    # execute whatever it calls ``content``, so its fields are all scanned. A frame
    # missing any of those attributes, or carrying it as false, is an UNKNOWN tool
    # and keeps the full document scan (fail closed). Every non-body string --
    # a ``command`` word, a path, a URL -- still reaches the scan, and a walk
    # that hits its work cap is denied as unverifiable.
    _scoped_params = (
        event.raw_tool_params
        if (
            _edit_params is None
            and getattr(event, "shell_classified", False)
            and not event.is_shell
            and getattr(event, "raw_params_trusted", False)
            and getattr(event, "mcp_identity_trusted", False)
            and is_document_writing_tool(
                getattr(event, "tool_name", ""), getattr(event, "mcp_server_name", "")
            )
            and isinstance(getattr(event, "raw_tool_params", None), dict)
        )
        else None
    )
    # A Kiro Crew MCP tool listed in ``platform.tool_paths.
    # MCP_DOCUMENT_BODY_FIELDS`` (``knowledge_add_document``'s ``content``, a
    # spawn brief, a cron prompt, a message for a person, ...)
    # stores that field as document text, so the field skips the command-text
    # rules -- the deny list and the argv floor -- that read a page mentioning a
    # product subcommand as an attempt to run it. The body keeps the size
    # ceiling and the path tier; every other argument keeps the full scan. The
    # params and identity bar match the built-in scoping above, and the identity is the
    # cached server AND tool, so a same-named tool on another server, or a frame
    # whose identity did not come from the caches, keeps the full scan.
    #
    # Unlike the built-in scoping, ``shell_classified`` is not required: the
    # trusted cached server+tool pair (adapter-written ``_meta``, never the
    # payload) already names exactly what runs. kiro-cli's MCP ``tool_call``
    # frame carries no ``kind`` (only its later updates do), so the shell cache
    # is empty for a real kiro-cli MCP call. A frame that does report a shell
    # kind still sets ``is_shell`` and is refused the exemption.
    _mcp_body_keys = (
        mcp_document_body_keys(
            getattr(event, "tool_name", ""),
            getattr(event, "mcp_server_name", ""),
            event.raw_tool_params,
        )
        if (
            _edit_params is None
            and _scoped_params is None
            and not getattr(event, "is_shell", False)
            and getattr(event, "raw_params_trusted", False)
            and getattr(event, "mcp_identity_trusted", False)
            and isinstance(getattr(event, "raw_tool_params", None), dict)
        )
        else frozenset()
    )
    _scoped_truncated = False
    _body_strings: list[str] = []
    if _edit_params is not None:
        _input_strings: list[str] = []
    elif _scoped_params is not None:
        _scoped_strings = command_shaped_strings(_scoped_params)
        _scoped_truncated = _scoped_strings.truncated
        _input_strings = list(_scoped_strings)
    elif _mcp_body_keys and isinstance(event.raw_tool_params, dict):
        _rest_params, _body_strings = split_document_bodies(event.raw_tool_params, _mcp_body_keys)
        _scoped_strings = command_shaped_strings(_rest_params, body_keys=frozenset())
        _scoped_truncated = _scoped_strings.truncated
        _input_strings = list(_scoped_strings)
    else:
        _input_strings = _extract_tool_input_strings(_tool_input) if _tool_input else []

    def _scan_off_loop() -> tuple[str, str, str, str] | None:
        # One worker hop for the title and the whole tool_input loop. Both are
        # regex-heavy over agent-supplied text; on the event loop a ~9 KB shell
        # title held the loop past the 25 s stall watchdog and took the gateway
        # down (an inline title tier with only the tool_input
        # tier offloaded leaves exactly that crash path open).
        # ``re`` HOLDS the GIL for one match call, so the hop does not keep the
        # loop live inside a single scan -- the linear patterns and the size
        # ceiling do that; what the hop buys is the realpath I/O inside
        # ``is_sensitive_path`` (which does release the GIL) and yields between
        # the strings. Title first, so a request denied on its title
        # reports the title-tier reason and mechanism exactly as before.
        title_hit = _title_denial(
            judged_title,
            _denied_regexes,
            exempt_command=_path_tier_exempt(event),
            identity_exempt=title_is_trusted_mcp_identity(
                _normalize_tool_name(normalized),
                getattr(event, "mcp_server_name", "") or "",
                getattr(event, "tool_name", "") or "",
                mcp_identity_trusted=bool(getattr(event, "mcp_identity_trusted", False)),
            ),
            bash_subject=(
                _normalize_tool_name(judged_title) if judged_title != normalized else None
            ),
        )
        if title_hit is not None:
            return (title_hit[0], title_hit[1], judged_title, "always_deny")
        if judged_title != normalized and hooks is not None:
            # The operator's own regexes judge the title as SENT when it was
            # rebuilt, as ``hooks.on_tool_call`` does and through the same
            # floor-free tier, whole string and per segment: a rule written
            # against the cut text kiro-cli displayed keeps firing, including one
            # anchored to a single chained command, and no shipped rule sees that
            # text. Without a HookManager no operator rule is known.
            operator_regexes = hooks.operator_denied_regexes()
            if operator_regexes:
                shown_reason = is_denied_synthesized_target(
                    normalized, operator_regexes, segments=True
                )
                if shown_reason:
                    return ("regex", shown_reason, normalized, "always_deny")
        if _edit_target_gated:
            edit_hit = _edit_target_denial(_gated_params, event.diff_path, event.tool_kind)
            if edit_hit is not None:
                return (*edit_hit, "always_deny_input")
        if _scoped_truncated:
            # The field-scoped walk could not finish, so the strings it did
            # collect are not the whole payload: refuse rather than scan a part.
            return (
                "oversize",
                "Blocked: tool arguments too large to security-scan (deny-by-default)",
                "",
                "always_deny_input",
            )
        if _input_strings:
            input_hit = _first_tool_input_denial(
                _input_strings, _denied_regexes, exempt_command=_path_tier_exempt(event)
            )
            if input_hit is not None:
                return (*input_hit, "always_deny_input")
        if _body_strings:
            body_hit = _first_tool_input_denial(_body_strings, _denied_regexes, command_rules=False)
            if body_hit is not None:
                return (*body_hit, "always_deny_input")
        return None

    _hit = await asyncio.to_thread(_scan_off_loop)
    if _hit is not None:
        _kind, _reason, _matched, _tier = _hit
        _log(
            "denied",
            error=_reason,
            metadata={
                "mechanism": (_regex_deny_mechanism(_matched, _tier) if _kind == "regex" else _tier)
            },
        )
        await _steer_host_deny(provider, event, _reason, cause=DENY_CAUSE_POLICY)
        await provider.reject_tool(event.request_id)
        return False

    if policy == ToolApprovalPolicy.READ_ONLY and event.deepseek_read_only_issue:
        # A DeepSeek Harness session whose launch did not prove stock dsh owns its
        # read tools: any plugin it composes may register a tool under a read name,
        # so no call is provable read-only and the turn is REJECT_ALL.
        _log("rejected", metadata={"reason": "read_only_policy_unproven_composition"})
        await _steer_host_deny(
            provider,
            event,
            "this surface is read-only and the harness composes a plugin Kiro Crew "
            "cannot vouch for, so every tool call is refused",
            cause=DENY_CAUSE_SURFACE_POLICY,
        )
        await provider.reject_tool(event.request_id)
        return False

    if policy == ToolApprovalPolicy.READ_ONLY and hooks is None:
        # Fail closed: READ_ONLY's classifier IS the hook gate. Without one
        # there is no way to prove a call read-only, so the policy degrades to
        # REJECT_ALL rather than to the caller-less auto-approve below.
        _log("rejected", metadata={"reason": "read_only_policy_no_hooks"})
        await _steer_host_deny(
            provider,
            event,
            "this surface is read-only and has no hook gate to prove a call "
            "read-only, so every tool call is refused",
            cause=DENY_CAUSE_SURFACE_POLICY,
        )
        await provider.reject_tool(event.request_id)
        return False

    if policy == ToolApprovalPolicy.AUTO_APPROVE:
        reason = await asyncio.to_thread(
            permission_floor.refusal_for,
            event,
            session_key=session_key,
            agent=agent,
            app=app,
            security_only=False,
        )
        if reason is not None:
            # The permission floor's verdict is a host deny on the call itself
            # (a TOOL_DENY from the shared gate, or the gate failing closed):
            # policy wording, audited first, steered before the wire.
            _log(
                "denied",
                error=reason,
                metadata={"mechanism": "policy_deny"},
            )
            await _steer_host_deny(provider, event, reason, cause=DENY_CAUSE_POLICY)
            await provider.reject_tool(event.request_id)
            return False

    if policy in (ToolApprovalPolicy.HOOK_BASED, ToolApprovalPolicy.READ_ONLY) and hooks:
        tool_result = await run_in_tool_gate_pool(
            hooks.on_tool_call,
            event.title,
            on_queue_timeout=hook_gate_busy,
            session_key=session_key,
            agent=agent,
            app=app,
            **hook_gate_kwargs(event),
            # READ_ONLY asks for the classifier's verdict alone: the gate skips
            # its grant tiers (`auto_approve_tools`, app-own-server), which vouch
            # for the caller rather than for the call's effect, so a grant that
            # shadows a read still lets the classifier approve the read and a
            # grant that shadows a write approves nothing. Under READ_ONLY the
            # provenance flag above is also what lets a host-known built-in
            # (``fs_read``) count as proven read-only. HOOK_BASED keeps the
            # grants — its approver is the card they skip.
            classifier_only=policy == ToolApprovalPolicy.READ_ONLY,
        )
        if tool_result.action == TOOL_DENY:
            # A hook deny is either a hard security check or the governance
            # ceiling. Only the former says the attempt itself was the problem,
            # and the distinction rides on the result's own field rather than
            # its reason text.
            _log(
                "denied",
                error=tool_result.reason,
                metadata={
                    "mechanism": (
                        "always_deny_hook" if tool_result.security_deny else "policy_deny"
                    )
                },
            )
            await _steer_host_deny(provider, event, tool_result.reason, cause=DENY_CAUSE_POLICY)
            await provider.reject_tool(event.request_id)
            return False
        if tool_result.action == TOOL_AUTO_APPROVE:
            if policy == ToolApprovalPolicy.READ_ONLY and not tool_result.read_only:
                # READ_ONLY honours the classifier's verdict alone, and the
                # result says which route produced it: ``read_only`` is set only
                # by the read-only classifier, never by a grant (the operator's
                # `auto_approve_tools` globs, the app-own-server rule), which
                # vouches for the caller and says nothing about the call's
                # effect. The gate is asked classifier-only above, so an
                # untagged auto-approve here comes from a gate that does not
                # carry the tag — a double, a tier that omits it — and this
                # surface has no approver to hand it to. Refuse, as policy
                # state: the same call is allowed where a card exists.
                _log("rejected", metadata={"reason": "read_only_policy_unclassified"})
                await _steer_host_deny(
                    provider,
                    event,
                    "this surface is read-only and the call could not be proven " "read-only",
                    cause=DENY_CAUSE_SURFACE_POLICY,
                )
                await provider.reject_tool(event.request_id)
                return False
            # The hook granted this by NAME (its `auto_approve_tools` globs, or
            # the read-only allowlist). Verify it UNCONDITIONALLY: this helper
            # serves unattended callers (cron / autonudge / heartbeat / Meetings
            # transcript turns), some of which pass no approver, and the shell
            # resolves the command's program names again through a PATH that can
            # lead with agent-writable directories. A refusal DOWNGRADES to the
            # caller's normal path — the interactive approver when one is
            # present, else deny-by-default (reject) below — never a
            # silent auto-approve of a shadowed name on an unwatched turn.
            _ng_refusal = await name_grant.refusal_for_event(event)
            if _ng_refusal is None:
                approval_sent = await provider.approve_tool(event.request_id)
                if approval_sent is not False:
                    _log("auto_approved", metadata={"reason": "hook_auto_approve"})
                else:
                    _log(
                        OUTCOME_REJECTED_TRANSPORT_FLOOR,
                        metadata={"mechanism": "always_deny_transport"},
                    )
                return approval_sent is not False
            if name_grant.should_log_decline(session_key, _ng_refusal):
                logger.warning(
                    "declining a hook auto-approve: %s; the request falls through "
                    "to this caller's approval path",
                    _ng_refusal.log_text,
                )
            name_grant.log_decline(
                source="",
                session_key=session_key,
                agent=agent,
                event=event,
                refusal=_ng_refusal,
                tier="hook_auto_approve",
                sel_factory=sel,
            )
            # No interactive approver on this caller: the hook grant was the
            # only positive authorization and it was withheld, so fall through
            # to deny-by-default rather than the caller-less auto-approve below.
            if on_tool_approval is None:
                _log("rejected", metadata={"reason": "name_grant_headless_reject"})
                await _steer_host_deny(
                    provider,
                    event,
                    "the hook's name-based grant was withheld ("
                    f"{_ng_refusal.log_text}) and this surface has no approver",
                    cause=DENY_CAUSE_SURFACE_POLICY,
                )
                await provider.reject_tool(event.request_id)
                return False

    if policy == ToolApprovalPolicy.READ_ONLY:
        # Everything the hook gate did not deny (rejected above) or positively
        # classify as read-only (approved above, name-grant verified) lands
        # here: `allow` results, and auto-approves whose name grant was
        # withheld (a grant-shaped auto-approve is refused above, before the
        # name check). Where the interactive Reads mode would fall through to the
        # approval card, this policy refuses — reject is the fallback, and it
        # runs BEFORE the interactive callback so a caller passing one cannot
        # widen the policy.
        _log("rejected", metadata={"reason": "read_only_policy"})
        await _steer_host_deny(
            provider,
            event,
            "this surface is read-only and the call was not classified read-only",
            cause=DENY_CAUSE_SURFACE_POLICY,
        )
        await provider.reject_tool(event.request_id)
        return False

    # A git read the gate's read-only tier declined (git reads `.git/config`,
    # whose `core.fsmonitor` / `core.hooksPath` name a program no text
    # classifier sees) falls through to the surface's approver. With no
    # approver on a hook-based surface (a Meetings dispatch), the default
    # below would run it unchecked -- not even the name-grant check the
    # read-only approve ran -- so it is refused instead.
    if policy == ToolApprovalPolicy.HOOK_BASED and hooks and on_tool_approval is None:
        _git_command = name_grant.shell_command_for_event(event)
        if _git_command and command_invokes_git(_git_command) and is_read_only_bash(_git_command):
            _log("rejected", metadata={"reason": "git_read_headless_reject"})
            await _steer_host_deny(
                provider,
                event,
                "a git command is never auto-approved as a read (git runs programs "
                "its repository config names) and this surface has no approver",
                cause=DENY_CAUSE_SURFACE_POLICY,
            )
            await provider.reject_tool(event.request_id)
            return False

    # Interactive approval if callback provided
    if on_tool_approval:
        approved = await on_tool_approval(event)
        if not approved:
            # The person said no: no notice (kiro-cli's wording is the truth
            # here), but the audit still precedes the wire like every other deny.
            _log("rejected", metadata={"reason": "interactive_rejected"})
            await provider.reject_tool(event.request_id)
            return False

    # Default: auto-approve
    approval_sent = await provider.approve_tool(event.request_id)
    if approval_sent is not False:
        _log("auto_approved")
    else:
        _log(OUTCOME_REJECTED_TRANSPORT_FLOOR, metadata={"mechanism": "always_deny_transport"})
    return approval_sent is not False
