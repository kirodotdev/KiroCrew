"""Layer 2 -- the channel-neutral ``TurnDriver``.

The ``TurnDriver`` consumes a provider's event stream (``AcpEvent``s) and
emits abstract :class:`OutputEvent`s to a per-transport :class:`Renderer`.
It owns the channel-neutral turn concerns -- credential/exfiltration
redaction and the tool-approval decision -- so every channel inherits them
once.

This module stays dependency-neutral: it imports only the ``acp.types``
event constants (a stdlib-only leaf) and the ``security`` redactors (also a
leaf). It does NOT import ``kiro_crew.slack`` or ``kiro_crew.dashboard``.

This driver is the channel-neutral core shared by every transport;
Slack-specific rendering lives in the Slack ``Renderer``
(``kiro_crew.slack.renderer``).
"""

from __future__ import annotations

import re
from typing import Any, Awaitable, Callable

from kiro_crew.acp.types import (
    EVENT_COMPACTION_STATUS,
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_STEER_CONSUMED,
    EVENT_TEXT_CHUNK,
    EVENT_THINKING_CHUNK,
    EVENT_TOOL_CALL,
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
from kiro_crew.security import StreamRedactor, redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel

# Approval modes (mirrors slack/handler APPROVAL_* + the dashboard ladder).
APPROVAL_AUTO = "auto"
APPROVAL_TRUST = "trust"
APPROVAL_TRUST_READS = "trust-reads"
APPROVAL_INTERACTIVE = "interactive"

#: A decision callback: given a permission-request event, return True to
#: approve. Used for the interactive ladder (each channel supplies its own,
#: e.g. by awaiting a button click). Returns None/False => deny.
ApprovalDecider = Callable[[Any], Awaitable[bool]]

#: A synchronous predicate: given a tool title, return True to auto-approve
#: that tool regardless of the interactive ladder. The caller injects this
#: (keeping the driver channel-neutral) to preserve hook-driven auto-approval
#: such as ``auto_approve_subagent_spawn`` for the ``spawn_run`` tool.
AutoApprovePredicate = Callable[[str], bool]

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
        Optional sync predicate ``(tool_title) -> bool``. When it returns
        True for a permission request, the tool is auto-approved immediately
        (no buttons, no decider wait), mirroring native ``handle_message``'s
        ``auto_approve_subagent_spawn`` hook for ``spawn_run``. Injected by the
        caller so the driver stays channel-neutral.
    auto_approve_session:
        Optional zero-arg predicate ``() -> bool``. When it returns True, every
        permission request in this turn is auto-approved immediately (no
        buttons, no wait). Injected by the caller to honor per-session Trust /
        global YOLO without the driver depending on any channel module.
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
        tool_gate: Callable[[Any], str] | None = None,
    ) -> None:
        self.provider = provider
        self.renderer = renderer
        self.approval_mode = approval_mode
        self.decider = decider
        self.auto_approve_tool = auto_approve_tool
        self.auto_approve_session = auto_approve_session
        # PreToolUse security gate: given a permission-request event, returns
        # "deny" (hard-block, un-overridable), "auto_approve" (hook approves,
        # e.g. reads), or "" (passthrough to the approval ladder). Injected by
        # the caller so the driver stays channel-neutral — it carries the
        # sensitive-path keystone + governance ceiling + deny-list that native
        # handle_message enforces via hooks.on_tool_call. Runs BEFORE the
        # auto/trust/YOLO ladder so a DENY can never be overridden.
        self.tool_gate = tool_gate

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
        async for event in self.provider.stream(message):
            kind = event.kind
            if kind == EVENT_TEXT_CHUNK:
                filtered = compaction_filter.feed(event.text or "")
                if filtered:
                    await dispatch_frames(steering_filter.feed(filtered))
            elif kind == EVENT_THINKING_CHUNK:
                await self.renderer.dispatch(
                    OutputEvent(kind=THINKING, text=_redact(event.text))
                )
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
                await self.renderer.dispatch(
                    OutputEvent(
                        kind=TOOL_CALL,
                        tool_call_id=event.tool_call_id,
                        title=_redact(event.title),
                        tool_kind=getattr(event, "tool_kind", ""),
                        tool_purpose=_redact(getattr(event, "tool_purpose", "")),
                    )
                )
            elif kind == EVENT_PERMISSION_REQUEST:
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
                # Early auto-approve paths take precedence over the interactive
                # ladder, mirroring native handle_message: approve immediately,
                # no buttons, no decider wait.
                #  - hook: auto_approve_subagent_spawn -> spawn_run
                #  - per-session Trust / global YOLO (injected predicate)
                _auto_reason = ""
                if self.auto_approve_tool is not None and self.auto_approve_tool(
                    getattr(event, "title", "") or ""
                ):
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
                    await self.renderer.dispatch(
                        OutputEvent(
                            kind=PROMPT_CHOICE,
                            options=[
                                {k: _redact(v) if isinstance(v, str) else v for k, v in o.items()}
                                for o in (event.options or [])
                            ],
                            request_id=event.request_id,
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
                pending = compaction_filter.flush()
                if pending:
                    await dispatch_frames(steering_filter.feed(pending))
                await dispatch_frames(steering_filter.flush())
                await flush_redactor()
                for _ in range(pending_steer_events):
                    await self.renderer.dispatch(OutputEvent(kind=STEER_CONSUMED))
                pending_steer_events = 0
                await self.renderer.dispatch(
                    OutputEvent(kind=DONE, stop_reason=event.stop_reason)
                )
        return accumulated

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
