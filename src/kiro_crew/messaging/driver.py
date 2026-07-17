"""Layer 2 -- the channel-neutral ``TurnDriver``.

The ``TurnDriver`` consumes a provider's event stream (``AcpEvent``s) and
emits abstract :class:`OutputEvent`s to a per-transport :class:`Renderer`.
It owns the channel-neutral turn concerns -- credential/exfiltration
redaction and the tool-approval decision -- so every channel inherits them
once.

This module stays dependency-neutral: it imports only the ``acp.types``
event constants (a stdlib-only leaf) and the ``security`` redactors (also a
leaf). It does NOT import ``kiro_crew.slack`` or ``kiro_crew.dashboard``.

v1b scope: extracts the channel-neutral core of the native
``slack/handler.py`` loop. Slack-specific rendering lives in the Slack
``Renderer`` (``kiro_crew.slack.renderer``); the native loop is rewired onto
this driver in Stage 3 (gated by the golden-transcript test).
"""

from __future__ import annotations

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
        """Drive one turn; return the accumulated (redacted) assistant text."""
        accumulated = ""
        # Rolling-buffer redactor for the streamed assistant text so a
        # credential split across two EVENT_TEXT_CHUNKs (e.g. "...AKIA1234" then
        # "5678...") is caught — per-chunk redaction alone would miss it and the
        # concatenation would reach the channel in the clear. feed() emits only
        # the safe prefix; flush() (on EVENT_COMPLETE) redacts the buffered tail.
        _sred = StreamRedactor(_redact)
        await self.renderer.on_turn_start()
        async for event in self.provider.stream(message):
            kind = event.kind
            if kind == EVENT_TEXT_CHUNK:
                text = _sred.feed(event.text or "")
                if text:
                    accumulated += text
                    await self.renderer.dispatch(OutputEvent(kind=TEXT_CHUNK, text=text))
            elif kind == EVENT_THINKING_CHUNK:
                await self.renderer.dispatch(
                    OutputEvent(kind=THINKING, text=_redact(event.text))
                )
            elif kind == EVENT_STEER_CONSUMED:
                # kiro-cli folded a mid-turn steer at a boundary — let the
                # renderer seal the pre-steer message so the steered
                # continuation opens as its own message.
                await self.renderer.dispatch(OutputEvent(kind=STEER_CONSUMED))
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
                # Flush the stream redactor's buffered tail BEFORE finalizing so
                # a credential held back at the last chunk boundary is emitted
                # (redacted) rather than dropped, and lands before DONE.
                tail = _sred.flush()
                if tail:
                    accumulated += tail
                    await self.renderer.dispatch(OutputEvent(kind=TEXT_CHUNK, text=tail))
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
