"""Layer 2 -- the channel-neutral ``TurnDriver``.

The ``TurnDriver`` consumes a provider's event stream (``AcpEvent``s) and
emits abstract :class:`OutputEvent`s to a per-transport :class:`Renderer`.
It owns the channel-neutral turn concerns -- credential/exfiltration
redaction and the tool-approval decision -- so every channel inherits them
once.

This module stays dependency-neutral: it imports only the ``acp.types``
event constants (a stdlib-only leaf), the ``security`` redactors (also a
leaf), and the ``session_directive`` codec (a third stdlib-only leaf). It
does NOT import ``kiro_crew.slack`` or ``kiro_crew.dashboard``.

This driver is the channel-neutral core shared by every transport;
Slack-specific rendering lives in the Slack ``Renderer``
(``kiro_crew.slack.renderer``).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Awaitable, Callable

from kiro_crew import name_grant, session_directive
from kiro_crew.acp.types import (
    EVENT_COMPACTION_STATUS,
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_STEER_CONSUMED,
    EVENT_SUBAGENT_ACTIVITY,
    EVENT_TEXT_CHUNK,
    EVENT_THINKING_CHUNK,
    EVENT_TOOL_CALL,
    EVENT_TOOL_RESULT,
)
from kiro_crew.messaging.renderer import (
    COMPACTION,
    DONE,
    PROMPT_CHOICE,
    STEER_CONSUMED,
    TEXT_CHUNK,
    THINKING,
    TOOL_CALL,
    OutputEvent,
    Renderer,
)
from kiro_crew.monitoring.completion import (
    MonitorCompletionHook,
    disposition_for_stop_reason,
    is_monitor_completion_evidence,
)
from kiro_crew.security import StreamRedactor, redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

# Approval modes (mirrors slack/handler APPROVAL_* + the dashboard ladder).
APPROVAL_AUTO = "auto"
APPROVAL_TRUST = "trust"
APPROVAL_TRUST_READS = "trust-reads"
APPROVAL_INTERACTIVE = "interactive"

#: A decision callback: given a permission-request event, return True to
#: approve. Used for the interactive ladder (each channel supplies its own,
#: e.g. by awaiting a button click). Returns None/False => deny.
ApprovalDecider = Callable[[Any], Awaitable[bool]]

#: A synchronous predicate: given the PERMISSION EVENT, return True to
#: auto-approve that tool regardless of the interactive ladder. The caller
#: injects this (keeping the driver channel-neutral) to preserve hook-driven
#: auto-approval such as ``auto_approve_subagent_spawn`` for the ``spawn_run``
#: tool. It receives the whole event — never just the title — because the
#: title is model-authored and a security predicate must key on canonical
#: identity (``event.tool_name`` / ``event.is_shell``).
AutoApprovePredicate = Callable[[Any], bool]

#: A session-directive consumer: ``(kind, args) -> awaitable``. The driver
#: invokes it when a genuine directive-tool result (see ``session_directive``)
#: carries a decoded marker; the caller injects a consumer bound to ITS OWN
#: session key (keeping the driver channel-neutral), typically
#: ``messaging.dispatch.build_directive_consumer``.
DirectiveConsumer = Callable[[str, dict[str, Any]], Awaitable[None]]

# kiro-cli embeds this protocol frame in ordinary agent_message_chunk text when
# it folds a mid-turn steer. It is transport metadata, not assistant speech.
_STEER_PREFIX = "[STEERING"
_STEER_MARKER_RE = re.compile(
    r"^\[STEERING\s+steer-[0-9a-f-]+(?:\s*:\s*(.*?))?\]$",
    re.IGNORECASE | re.DOTALL,
)
_MAX_STEER_MARKER_CHARS = 16_384

# These are KiroCrew-generated status prefixes, not model-authored prose. A
# legacy dashboard transcript can contain the completed summary as an assistant
# message; after a cold resume that provenance is lost and kiro-cli can echo it
# back as an ordinary text chunk. The channel boundary is the last layer that
# still knows the destination is external, so reserve the prefixes and replace
# the internal summary with a terse user-safe status. The dashboard does not use
# TurnDriver and retains its authoritative transcript/audit record.
_COMPACTION_NOTICE_PREFIXES = (
    "✅ Conversation compacted:",
    "Conversation compacted:",
    "✅ Compacted:",
)
_COMPACTION_NOTICE_REPLACEMENT = "✅ Context compacted."
_COMPACTION_PROBE_MAX = max(map(len, _COMPACTION_NOTICE_PREFIXES)) + 64


def is_internal_compaction_notice(text: str) -> bool:
    """Return whether *text* starts with a reserved summary-bearing notice."""
    probe = (text or "").lstrip()
    return any(probe.startswith(prefix) for prefix in _COMPACTION_NOTICE_PREFIXES)


class _CompactionNoticeFilter:
    """Classify a streamed turn before exposing its leading text.

    Only a whole-turn reserved prefix is suppressed. Ordinary mentions later in
    an answer and the user-facing ``[OPTIONS: ...]`` trailer pass unchanged.
    """

    __slots__ = ("_buffer", "_decided", "_suppress")

    def __init__(self) -> None:
        self._buffer = ""
        self._decided = False
        self._suppress = False

    def feed(self, chunk: str) -> str:
        if not chunk or self._suppress:
            return ""
        if self._decided:
            return chunk
        self._buffer += chunk
        probe = self._buffer.lstrip()
        if is_internal_compaction_notice(self._buffer):
            self._buffer = ""
            self._suppress = True
            return _COMPACTION_NOTICE_REPLACEMENT
        if (
            not probe or any(prefix.startswith(probe) for prefix in _COMPACTION_NOTICE_PREFIXES)
        ) and (len(self._buffer) <= _COMPACTION_PROBE_MAX):
            return ""
        self._decided = True
        out, self._buffer = self._buffer, ""
        return out

    def flush(self) -> str:
        if self._suppress:
            return ""
        out, self._buffer = self._buffer, ""
        self._decided = True
        return out


class _SteeringMarkerFilter:
    """Remove streamed ``[STEERING steer-…]`` frames without chunk leaks.

    The parser holds a possible marker prefix until it can classify the complete
    frame, so a UUID or summary split across provider chunks is never emitted.
    Complete markers become structured ``STEER_CONSUMED`` output events at the
    exact text boundary; everything else is returned byte-for-byte.
    """

    __slots__ = ("_buffer", "_dropping_oversized")

    def __init__(self) -> None:
        self._buffer = ""
        self._dropping_oversized = False

    def feed(self, chunk: str) -> list[tuple[str, str]]:
        if self._dropping_oversized:
            close = chunk.find("]")
            if close < 0:
                return []
            self._dropping_oversized = False
            chunk = chunk[close + 1 :]
            frames: list[tuple[str, str]] = [("steer", "")]
        else:
            frames = []
        self._buffer += chunk
        frames.extend(self._drain(final=False))
        return frames

    def flush(self) -> list[tuple[str, str]]:
        if self._dropping_oversized:
            self._dropping_oversized = False
            self._buffer = ""
            return []
        return self._drain(final=True)

    def _drain(self, *, final: bool) -> list[tuple[str, str]]:
        frames: list[tuple[str, str]] = []
        while self._buffer:
            start = self._buffer.find("[")
            if start < 0:
                hold = 0 if final else self._partial_prefix_len(self._buffer)
                emit = self._buffer if hold == 0 else self._buffer[:-hold]
                if emit:
                    frames.append(("text", emit))
                self._buffer = "" if hold == 0 else self._buffer[-hold:]
                break
            if start > 0:
                frames.append(("text", self._buffer[:start]))
                self._buffer = self._buffer[start:]
                continue

            upper = self._buffer.upper()
            prefix = _STEER_PREFIX.upper()
            if len(self._buffer) < len(_STEER_PREFIX) and prefix.startswith(upper):
                if final:
                    self._buffer = ""
                break
            if not upper.startswith(prefix):
                frames.append(("text", self._buffer[0]))
                self._buffer = self._buffer[1:]
                continue

            close = self._buffer.find("]")
            if close < 0:
                if len(self._buffer) > _MAX_STEER_MARKER_CHARS:
                    self._buffer = ""
                    self._dropping_oversized = True
                elif final:
                    self._buffer = ""
                break

            candidate = self._buffer[: close + 1]
            match = _STEER_MARKER_RE.match(candidate)
            if match is None:
                frames.append(("text", self._buffer[0]))
                self._buffer = self._buffer[1:]
                continue
            frames.append(("steer", (match.group(1) or "").strip()))
            self._buffer = self._buffer[close + 1 :]
        return frames

    @staticmethod
    def _partial_prefix_len(text: str) -> int:
        prefix = _STEER_PREFIX.upper()
        upper = text.upper()
        for size in range(min(len(text), len(prefix) - 1), 0, -1):
            if prefix.startswith(upper[-size:]):
                return size
        return 0


def sanitize_channel_replay_text(text: str) -> str:
    """Strip reserved channel protocol from one already-buffered message.

    Direct transcript replays bypass :class:`TurnDriver`, so they use this
    helper to apply the same fail-closed steering and compaction boundary.
    """
    if is_internal_compaction_notice(text):
        return ""
    parser = _SteeringMarkerFilter()
    frames = parser.feed(text)
    frames.extend(parser.flush())
    return "".join(payload for kind, payload in frames if kind == "text")


def _redact(text: str | None) -> str:
    """Scrub exfiltration URLs + credentials from text (deterministic)."""
    out, _ = redact_exfiltration_urls(text or "")
    out, _ = redact_credentials(out)
    return out


class TurnDriver:
    """Channel-neutral turn loop: provider events -> abstract output events.

    Parameters
    ----------
    provider:
        An ``LLMProvider`` whose ``stream(message)`` async-yields ``AcpEvent``s
        and which exposes ``approve_tool``/``reject_tool``.
    renderer:
        The per-transport :class:`Renderer` that maps output events to a
        native surface.
    approval_mode:
        One of ``auto`` / ``trust`` / ``trust-reads`` / ``interactive``.
        ``auto``/``trust`` auto-approve; ``interactive`` defers to *decider*.
    decider:
        Optional async callback for the interactive ladder. When omitted,
        interactive mode is deny-by-default.
    auto_approve_tool:
        Optional sync predicate ``(permission_event) -> bool``. When it
        returns True for a permission request, the tool is auto-approved
        immediately (no buttons, no decider wait), mirroring native
        ``handle_message``'s ``auto_approve_subagent_spawn`` hook for
        ``spawn_run``. It receives the EVENT so the check can use canonical,
        non-model-authored identity (``tool_name``/``is_shell``) rather than
        the forgeable title. Injected by the caller so the driver stays
        channel-neutral.
    deny_all_tools:
        Reject EVERY permission request, before any auto-approve path. For a turn
        driven by a sender the channel does not trust as its operator: the
        approval ladder alone cannot express this, because the PreToolUse hook's
        ``auto_approve`` verdict and the Trust/YOLO predicates both approve ahead
        of it. Defaults False, so every existing caller is unchanged.
    auto_approve_session:
        Optional zero-arg predicate ``() -> bool``. When it returns True, every
        permission request in this turn is auto-approved immediately (no
        buttons, no wait). Injected by the caller to honor per-session Trust /
        global YOLO without the driver depending on any channel module.
    directive_consumer:
        Optional async callback ``(kind, args) -> None``. When set, the driver
        decodes session-directive markers from genuine directive-tool results
        (``EVENT_TOOL_RESULT``) and invokes the callback with the validated
        payload, so a stateless session-bound tool (``monitor_start`` /
        ``autonudge_stop`` / ...) takes effect on standalone channel
        transports. Injected by the caller with its own session key bound, so
        the driver stays channel-neutral. When omitted, directive markers are
        ignored exactly as before.
    closing_gate:
        Optional synchronous gate invoked immediately before the provider stream
        starts. Callers use it to reject a lease that shutdown can no longer
        drain, and may also reject a structured monitor whose conversation
        generation changed. It must not await: the gate, monitor acceptance, and
        the stream's synchronous turn registration are one event-loop span.
    """

    def __init__(
        self,
        provider: Any,
        renderer: Renderer,
        *,
        approval_mode: str = APPROVAL_INTERACTIVE,
        decider: ApprovalDecider | None = None,
        auto_approve_tool: AutoApprovePredicate | None = None,
        auto_approve_session: Callable[[], bool] | None = None,
        deny_all_tools: bool = False,
        tool_gate: Callable[[Any], str] | None = None,
        directive_consumer: DirectiveConsumer | None = None,
        audit_session_key: str = "",
        audit_agent: str = "kirocrew",
        closing_gate: Callable[[], None] | None = None,
        monitor_completion: MonitorCompletionHook | None = None,
    ) -> None:
        self.provider = provider
        self.renderer = renderer
        self.approval_mode = approval_mode
        self.decider = decider
        self.auto_approve_tool = auto_approve_tool
        self.auto_approve_session = auto_approve_session
        self.deny_all_tools = deny_all_tools
        # Audit identity ONLY — injected by the caller (which owns the session
        # key and agent name) so the driver's security-decision audit rows are
        # attributable without the driver importing any channel module. Never
        # used for routing or authorization.
        self.audit_session_key = audit_session_key
        self.audit_agent = audit_agent
        # PreToolUse security gate: given a permission-request event, returns
        # "deny" (hard-block, un-overridable), "auto_approve" (hook approves,
        # e.g. reads), or "" (passthrough to the approval ladder). Injected by
        # the caller so the driver stays channel-neutral — it carries the
        # sensitive-path keystone + governance ceiling + deny-list that native
        # handle_message enforces via hooks.on_tool_call. Runs BEFORE the
        # auto/trust/YOLO ladder so a DENY can never be overridden.
        self.tool_gate = tool_gate
        # Session-directive consumer (see the class docstring). Applied on
        # EVENT_TOOL_RESULT for a tool call whose trusted ``_meta.kiro``
        # identity was recorded at EVENT_TOOL_CALL — the forgery gate.
        self.directive_consumer = directive_consumer
        self.monitor_completion = monitor_completion
        # Terminal stop reason of the last run() — read by the dispatcher's
        # post-turn bookkeeping (e.g. COMPACTION_FAILED -> session reset).
        self.last_stop_reason: str = ""
        # Synchronous pre-registration shutdown gate, supplied by the dispatcher
        # as a zero-arg closure over its SessionManager and session key. It lives
        # HERE rather than at each call site because the only placement that is
        # actually atomic is the one immediately before the provider stream opens,
        # and `run()` owns that line. Raising from it aborts the turn before any
        # prompt is registered. A driver built without one keeps the old ungated
        # behaviour, so a stand-in predating the parameter still works.
        self.closing_gate = closing_gate

    async def run(self, message: str) -> str:
        """Drive one turn; return the accumulated channel-safe assistant text."""
        accumulated = ""
        # Protocol framing runs BEFORE credential redaction. A steering marker
        # may split at any byte boundary; parsing it first ensures neither its
        # UUID nor its internal summary is ever committed to a renderer. The
        # security redactor then keeps its existing rolling credential boundary.
        compaction_filter = _CompactionNoticeFilter()
        steering_filter = _SteeringMarkerFilter()
        stream_redactor = StreamRedactor(_redact)
        pending_steer_events = 0
        unmatched_marker_events = 0
        # tool_call_id -> canonical directive-tool name, recorded at
        # EVENT_TOOL_CALL from the trusted ``_meta.kiro`` identity and consumed
        # at the matching EVENT_TOOL_RESULT. Only populated when a directive
        # consumer is injected, so a consumer-less turn does no extra work.
        pending_directives: dict[str, str] = {}
        # tool_call_ids owned by a NATIVE (in-session) sub-agent, announced via
        # EVENT_SUBAGENT_ACTIVITY before the child's flat tool_call/tool_result
        # frames arrive on this same stream. A native child's directive call
        # carries a genuine core-MCP identity but has no independently bindable
        # session, so it must be REFUSED rather than applied to the parent —
        # sub-agent isolation, mirroring the dashboard consumer's
        # ``_native_tc_card`` refusal.
        native_tool_call_ids: set[str] = set()
        # Directive tool_call_ids this turn has ALREADY consumed. Consumption
        # pops ``pending_directives``, so without this a later result frame for
        # an applied directive is indistinguishable from one that never had an
        # identity — and would log the "NOT APPLIED" diagnostic for a directive
        # that in fact applied. Diagnostic-only bookkeeping: nothing reads it to
        # authorize anything.
        consumed_directives: set[str] = set()
        # Identity OBSERVED on each tool_call frame, for the NOT-APPLIED
        # diagnostic only. It cannot be read off the result frame: the
        # tool_call_update path builds its event with no identity fields, so
        # they are always "" there. Two short strings per call, same per-turn
        # lifetime as the maps beside it. Nothing reads this to authorize
        # anything — the grant still comes solely from directive_tool_for at
        # call time.
        seen_tool_identity: dict[str, tuple[str, str]] = {}
        # Purpose text from each tool_call, keyed by its tool_call_id, so a
        # permission request can be paired with the purpose of the tool IT asks
        # about. The permission payload carries the title but no purpose, and the
        # two events are not necessarily adjacent, so a renderer remembering "the
        # last purpose" can pair one tool's name with another's purpose. Turn-local
        # and bounded by the turn's tool-call count.
        tool_purposes: dict[str, str] = {}

        async def emit_text(text: str) -> None:
            nonlocal accumulated
            safe = stream_redactor.feed(text)
            if safe:
                accumulated += safe
                await self.renderer.dispatch(OutputEvent(kind=TEXT_CHUNK, text=safe))

        async def flush_redactor() -> None:
            nonlocal accumulated
            tail = stream_redactor.flush()
            if tail:
                accumulated += tail
                await self.renderer.dispatch(OutputEvent(kind=TEXT_CHUNK, text=tail))

        async def dispatch_frames(frames: list[tuple[str, str]]) -> None:
            nonlocal pending_steer_events, unmatched_marker_events
            for frame_kind, payload in frames:
                if frame_kind == "text":
                    await emit_text(payload)
                    continue
                # Preserve a lexical separator where the inline marker was removed.
                # Flushing an unterminated credential tail directly would emit its
                # pre-steer half; join-based renderers could then concatenate the
                # post-steer half back into the full secret. Feeding and emitting a
                # newline first terminates that run, while the explicit flush keeps
                # every pre-steer byte ahead of the structured renderer boundary.
                await emit_text("\n")
                await flush_redactor()
                if pending_steer_events:
                    pending_steer_events -= 1
                else:
                    unmatched_marker_events += 1
                await self.renderer.dispatch(
                    OutputEvent(kind=STEER_CONSUMED, text=_redact(payload))
                )

        await self.renderer.on_turn_start()
        if self.monitor_completion is not None:
            if not await self.monitor_completion.authorize():
                return accumulated
        # The closing gate remains yield-free with stream registration. Monitor
        # acceptance below is synchronous, so it cannot reopen that race.
        if self.closing_gate is not None:
            self.closing_gate()
        if self.monitor_completion is not None:
            self.monitor_completion.mark_accepted()
        async for event in self.provider.stream(message):
            kind = event.kind
            if kind == EVENT_TEXT_CHUNK:
                filtered = compaction_filter.feed(event.text or "")
                if filtered:
                    await dispatch_frames(steering_filter.feed(filtered))
            elif kind == EVENT_THINKING_CHUNK:
                await self.renderer.dispatch(OutputEvent(kind=THINKING, text=_redact(event.text)))
            elif kind == EVENT_STEER_CONSUMED:
                # kiro-cli emits both a typed lifecycle event and an inline
                # marker, in either order. Pair them so renderers receive one
                # structured boundary, never two rotations. If an older backend
                # omits the marker, dispatch the unmatched event at turn end.
                if unmatched_marker_events:
                    unmatched_marker_events -= 1
                else:
                    pending_steer_events += 1
            elif kind == EVENT_TOOL_CALL:
                # Native handle_message treats every EVENT_TOOL_CALL uniformly
                # (complete previous task + start new), regardless of tool_final;
                # emit a single tool_call event so the renderer matches it.
                _purpose = _redact(getattr(event, "tool_purpose", ""))
                if event.tool_call_id and _purpose:
                    tool_purposes[str(event.tool_call_id)] = _purpose
                await self.renderer.dispatch(
                    OutputEvent(
                        kind=TOOL_CALL,
                        tool_call_id=event.tool_call_id,
                        title=_redact(event.title),
                        tool_kind=getattr(event, "tool_kind", ""),
                        tool_purpose=_purpose,
                    )
                )
                # Forgery gate: record the directive-tool name ONLY from the
                # trusted ``_meta.kiro`` identity — never the LLM-authored
                # title. The single shared predicate (also used by the
                # dashboard consumer in chat_runner) requires Kiro Crew's OWN
                # core MCP server and a canonical directive-tool name; a shell
                # tool (no mcp_server_name, canonical tool_name like
                # "execute_bash") or a third-party server exposing a same-named
                # tool can never register here.
                if self.directive_consumer is not None and event.tool_call_id:
                    canonical = session_directive.directive_tool_for(
                        getattr(event, "mcp_server_name", "") or "",
                        getattr(event, "tool_name", "") or "",
                    )
                    if canonical:
                        pending_directives[event.tool_call_id] = canonical
                    else:
                        seen_tool_identity[event.tool_call_id] = (
                            getattr(event, "mcp_server_name", "") or "",
                            getattr(event, "tool_name", "") or "",
                        )
            elif kind == EVENT_SUBAGENT_ACTIVITY:
                # Native sub-agent lifecycle marker. Its only role in this
                # driver is the isolation gate above: remember which
                # tool_call_ids belong to a child session so their directives
                # are refused. Tracked regardless of the consumer being set at
                # THIS point in the stream, because the set is only read when a
                # consumer exists and the bookkeeping is O(1) per event.
                if event.tool_call_id and getattr(event, "sub_session_id", ""):
                    native_tool_call_ids.add(event.tool_call_id)
            elif kind == EVENT_TOOL_RESULT:
                # Session directive: a stateless session-bound tool returns a
                # marker instead of resolving its own session identity; the
                # caller-injected consumer applies it against the caller's own
                # session. Gated on the identity recorded above — a forged
                # marker under any other tool resolves no entry and is
                # ignored. Without a consumer this event stays inert,
                # preserving the pre-consumer behavior exactly.
                if self.directive_consumer is not None:
                    await self._consume_directive(
                        event,
                        pending_directives,
                        native_tool_call_ids,
                        consumed_directives,
                        seen_tool_identity,
                    )
            elif kind == EVENT_PERMISSION_REQUEST:
                # Untrusted sender: no tool runs, full stop. This precedes even
                # the PreToolUse gate's auto_approve branch and the
                # trust/YOLO predicates, all of which approve and `continue`, so
                # setting the approval mode to `interactive` without a decider is
                # NOT sufficient on its own: the hook layer can still say
                # auto_approve, and a session carrying Trust still short-circuits.
                # A channel that admits senders other than its operator needs one
                # switch that means "deny every tool", and this is it.
                if self.deny_all_tools:
                    await self.provider.reject_tool(event.request_id)
                    sel().log_api_access(
                        caller="turn_driver",
                        operation="tool_permission",
                        outcome="denied",
                        source="messaging",
                        resources=(
                            f"request_id={event.request_id} "
                            f"mode={self.approval_mode} reason=untrusted_sender"
                        ),
                    )
                    continue
                # PreToolUse security gate — sensitive-path keystone +
                # governance ceiling + deny-list. Runs FIRST, before the
                # auto/trust/YOLO ladder, so a hard DENY can never be
                # overridden by auto-approve, per-session Trust, or YOLO
                # (mirrors native handle_message's hooks.on_tool_call gate).
                if self.tool_gate is not None:
                    _gate = self.tool_gate(event)
                    if _gate == "deny":
                        await self.provider.reject_tool(event.request_id)
                        sel().log_api_access(
                            caller="turn_driver",
                            operation="tool_permission",
                            outcome="denied",
                            source="messaging",
                            resources=(
                                f"request_id={event.request_id} "
                                f"mode={self.approval_mode} reason=hook_deny"
                            ),
                        )
                        continue
                    if _gate == "auto_approve":
                        # The gate's hook granted this by NAME (the
                        # `auto_approve_tools` globs, or the read-only
                        # allowlist). Honour it only while each program name in
                        # the command still resolves to the program it appears
                        # to name; a shadowed, agent-tree or unidentified
                        # resolution DOWNGRADES to the ladder below (spawn
                        # hook, session trust, interactive buttons,
                        # deny-by-default) — never a hard block. The check is
                        # awaited HERE, at the one honour point shared by every
                        # channel's gate, because each channel's `_tool_gate`
                        # is synchronous and loop-bound and must not do the
                        # check's filesystem work.
                        _ng_refusal = await name_grant.refusal_for_event(event)
                        if _ng_refusal is None:
                            await self.provider.approve_tool(event.request_id)
                            sel().log_api_access(
                                caller="turn_driver",
                                operation="tool_permission",
                                outcome="auto_approved",
                                source="messaging",
                                resources=(
                                    f"request_id={event.request_id} "
                                    f"mode={self.approval_mode} reason=hook"
                                ),
                            )
                            continue
                        logger.warning(
                            "declining a hook auto-approve: %s; the request "
                            "falls through to the channel's normal approval "
                            "ladder",
                            _ng_refusal.log_text,
                        )
                        # The decline row uses the shared cross-surface writer
                        # (log_tool_invocation) rather than this ladder's
                        # log_api_access rows, so every surface's decline has
                        # ONE shape and the disclosure rule (constant log_text
                        # + code, redacted title, never the detail) lives in
                        # one place.
                        name_grant.log_decline(
                            # Empty source: SEL infers the real transport
                            # (discord/telegram/...) from the session key's
                            # namespace prefix. The driver is channel-neutral,
                            # so naming a surface here would misattribute all
                            # four transports to one made-up value.
                            source="",
                            # The caller-injected audit identity: the driver
                            # itself is channel-neutral, but the one security
                            # decision it makes must be attributable to a
                            # session and agent.
                            session_key=self.audit_session_key,
                            agent=self.audit_agent,
                            event=event,
                            refusal=_ng_refusal,
                            tier="hook_auto_approve",
                            metadata={"mode": self.approval_mode},
                            sel_factory=sel,
                        )
                # Early auto-approve paths take precedence over the interactive
                # ladder, mirroring native handle_message: approve immediately,
                # no buttons, no decider wait.
                #  - hook: auto_approve_subagent_spawn -> spawn_run
                #  - per-session Trust / global YOLO (injected predicate)
                _auto_reason = ""
                if self.auto_approve_tool is not None and self.auto_approve_tool(event):
                    _auto_reason = "hook_auto_approve"
                elif self.auto_approve_session is not None and self.auto_approve_session():
                    _auto_reason = "session_trust"
                if _auto_reason:
                    await self.provider.approve_tool(event.request_id)
                    sel().log_api_access(
                        caller="turn_driver",
                        operation="tool_permission",
                        outcome="auto_approved",
                        source="messaging",
                        resources=(
                            f"request_id={event.request_id} "
                            f"mode={self.approval_mode} reason={_auto_reason}"
                        ),
                    )
                    continue
                # Only render approve/deny buttons when there's a decider to
                # await the click. Without one, _approve() denies by default,
                # so posting buttons would leave the user with dead controls.
                if self.approval_mode == APPROVAL_INTERACTIVE and self.decider is not None:
                    # The prompt names the tool from the PERMISSION event's own
                    # title, and its purpose from the tool_call that shares the
                    # id. A renderer that instead remembers the last titled
                    # tool_call names the PREVIOUS tool whenever a permission is
                    # not immediately preceded by its own, informed consent on a
                    # security prompt, so the correct name travels WITH the ask.
                    _tool_call_id = str(getattr(event, "tool_call_id", "") or "")
                    await self.renderer.dispatch(
                        OutputEvent(
                            kind=PROMPT_CHOICE,
                            options=[
                                {k: _redact(v) if isinstance(v, str) else v for k, v in o.items()}
                                for o in (event.options or [])
                            ],
                            request_id=event.request_id,
                            title=_redact(getattr(event, "title", "") or ""),
                            tool_purpose=tool_purposes.get(_tool_call_id, ""),
                            # The tool's own arguments, so a renderer can show what
                            # is being approved. Provider-authored text reaching a
                            # channel, so it takes the same redaction as everything
                            # else on this path.
                            tool_input=_redact(getattr(event, "tool_input", "") or ""),
                        )
                    )
                approved = await self._approve(event)
                if approved:
                    await self.provider.approve_tool(event.request_id)
                else:
                    await self.provider.reject_tool(event.request_id)
                sel().log_api_access(
                    caller="turn_driver",
                    operation="tool_permission",
                    outcome="approved" if approved else "denied",
                    source="messaging",
                    resources=f"request_id={event.request_id} mode={self.approval_mode}",
                )
            elif kind == EVENT_COMPACTION_STATUS:
                await self.renderer.dispatch(
                    OutputEvent(kind=COMPACTION, context_usage_pct=event.context_usage_pct)
                )
            elif kind == EVENT_COMPLETE:
                # Exposed for the dispatcher's post-turn bookkeeping: a
                # COMPACTION_FAILED terminal is synthetic (the backend never
                # sent end_turn) and needs a session reset the driver cannot
                # perform itself (it holds no session key).
                self.last_stop_reason = event.stop_reason or ""
                if self.monitor_completion is not None and is_monitor_completion_evidence(
                    event.stop_reason,
                    synthetic=event.synthetic_completion,
                ):
                    try:
                        await self.monitor_completion.complete(
                            disposition_for_stop_reason(event.stop_reason),
                            event.usage,
                        )
                    except Exception:
                        logger.warning(
                            "monitor turn completion callback failed",
                            exc_info=True,
                        )
                pending = compaction_filter.flush()
                if pending:
                    await dispatch_frames(steering_filter.feed(pending))
                await dispatch_frames(steering_filter.flush())
                await flush_redactor()
                for _ in range(pending_steer_events):
                    await self.renderer.dispatch(OutputEvent(kind=STEER_CONSUMED))
                pending_steer_events = 0
                await self.renderer.dispatch(OutputEvent(kind=DONE, stop_reason=event.stop_reason))
        return accumulated

    async def _consume_directive(
        self,
        event: Any,
        pending: dict[str, str],
        native_tool_call_ids: set[str],
        consumed: set[str] | None = None,
        seen_identity: dict[str, tuple[str, str]] | None = None,
    ) -> None:
        """Decode one directive-tool result and apply it via the consumer.

        SINGLE-CONSUME: one tool call can surface more than one result frame
        (a mid-stream content frame and the final ``status=completed``
        rawOutput frame), so the mapping is popped BEFORE the consumer runs —
        a second frame must never re-apply the effect (two armed loops, a
        repeated mutation). A mid-stream frame with no decodable marker leaves
        the mapping in place for the final frame. Mirrors the dashboard
        consumer in ``chat_runner``.
        """
        consumer = self.directive_consumer
        tool = pending.get(event.tool_call_id or "", "")
        if consumer is None or not tool:
            # DIAGNOSTIC ONLY (never a grant) — mirrors the dashboard consumer.
            # A marker with no recorded identity is the right outcome for a
            # forged result AND what a backend emitting no ``_meta.kiro``
            # produces for a real directive; the gate is silent, so log enough
            # to tell those apart.
            if (
                consumer is not None
                and (event.tool_call_id or "") not in (consumed or ())
                and session_directive.has_marker(event.tool_output)
            ):
                logger.warning(
                    "session-directive NOT APPLIED: marker present but the tool "
                    "call carried no core-MCP identity "
                    "(tool_call_id=%s, mcp_server_name=%r, tool_name=%r, "
                    "expected mcp_server_name=%r). Either a forged marker, or "
                    "this ACP backend does not emit _meta.kiro identity.",
                    event.tool_call_id,
                    (seen_identity or {}).get(event.tool_call_id or "", ("", ""))[0],
                    (seen_identity or {}).get(event.tool_call_id or "", ("", ""))[1],
                    session_directive.CORE_MCP_SERVER,
                )
            return
        if event.tool_call_id in native_tool_call_ids:
            # Sub-agent ISOLATION: a native child's tool calls surface as flat
            # events on the parent stream with a genuine core-MCP identity, but
            # the child has no independently bindable session — applying its
            # directive here would let a sub-agent arm/mutate its PARENT. Pop
            # (terminal for this call) and audit the refusal so the denial is
            # never a silent drop, mirroring the dashboard consumer.
            pending.pop(event.tool_call_id, None)
            if consumed is not None and event.tool_call_id:
                consumed.add(event.tool_call_id)
            sel().log_api_access(
                caller="turn_driver",
                operation="session_directive",
                outcome="denied",
                source="messaging",
                resources=(
                    f"tool={tool} tool_call_id={event.tool_call_id} "
                    "reason=native_subagent_isolation"
                ),
            )
            logger.info(
                "session-directive %r from a native sub-agent refused "
                "(tool_call_id=%s): no session of its own to act on",
                tool,
                event.tool_call_id,
            )
            return
        output = event.tool_output or ""
        args = session_directive.decode(output, tool)
        if args is None:
            if getattr(event, "tool_final", False):
                if session_directive.is_refusal(output):
                    # encode() refused to emit a marker (payload over the
                    # delivery limit): nothing was applied and the result text
                    # already told the model so. Terminal for this call.
                    pending.pop(event.tool_call_id, None)
                    if consumed is not None and event.tool_call_id:
                        consumed.add(event.tool_call_id)
                    logger.info(
                        "session-directive REFUSED for %r (tool_call_id=%s): "
                        "payload over the %d-char delivery limit; nothing applied",
                        tool,
                        event.tool_call_id,
                        session_directive.MAX_DIRECTIVE_CHARS,
                    )
                else:
                    # Authenticated directive tool, final frame, no marker:
                    # the effect is being dropped outright. Never let that be
                    # silent — this exact silence can hide a marker-escaping
                    # transport regression.
                    logger.warning(
                        "session-directive decode FAILED for %r (tool_call_id=%s, "
                        "out_len=%d) — effect dropped",
                        tool,
                        event.tool_call_id,
                        len(output),
                    )
            return
        pending.pop(event.tool_call_id, None)
        if consumed is not None and event.tool_call_id:
            consumed.add(event.tool_call_id)
        try:
            await consumer(tool, args)
        except Exception:
            # The consumer is injected code applying a side effect; a failure
            # there must never abort the rest of the turn's stream.
            logger.warning("session-directive consumer failed for %r", tool, exc_info=True)

    async def _approve(self, event: Any) -> bool:
        """Apply the approval ladder to a permission-request event."""
        if self.approval_mode in (APPROVAL_AUTO, APPROVAL_TRUST):
            return True
        if self.approval_mode == APPROVAL_TRUST_READS:
            return bool(getattr(event, "tool_kind", "") == "read")
        # interactive: deny-by-default unless a decider approves.
        if self.decider is not None:
            return bool(await self.decider(event))
        return False
