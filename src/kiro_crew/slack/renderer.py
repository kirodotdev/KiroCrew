"""Slack ``Renderer`` for the channel-neutral ``TurnDriver``.

Maps the abstract ``OutputEvent`` stream emitted by
``kiro_crew.messaging.TurnDriver`` onto Slack's streaming + Block Kit surface
via ``SlackClientOps``. This is the Slack-specific half of the neutral
messaging layer.

Import direction: ``slack`` -> ``messaging`` (never the reverse), so this lives
in the ``slack`` package and consumes the neutral ``messaging`` contracts.

``prompt_choice`` (the first-class approval event) renders as Block Kit
approve/deny buttons. The interactive decision is awaited via
:class:`SlackApprovalDecider`, whose future is resolved by the Slack
interaction handler when the user clicks a button.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

from kiro_crew.messaging.renderer import Renderer
from kiro_crew.messaging.transport import TransportCapabilities
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.slack.format import extract_options, strip_thinking_tags
from kiro_crew.slack.handler import (
    _APPROVAL_TIMEOUT,
    _CURSOR,
    _EDIT_INTERVAL,
    _THINKING,
    StatusReactionController,
    _append_footer_actions,
    _filter_options_brackets,
    _safe_update,
    _tool_to_phase,
    build_timing_footer,
)
from kiro_crew.slack.transport import SLACK_CAPABILITIES

#: Block Kit action_id prefixes for tool approve/deny buttons.
TOOL_APPROVE_ACTION_PREFIX = "mc_tool_approve_"
TOOL_DENY_ACTION_PREFIX = "mc_tool_deny_"
#: "Trust" auto-approves all subsequent tools for THIS session only (not
#: global). Mirrors the native path's per-session trust_tool button.
TOOL_TRUST_ACTION_PREFIX = "mc_tool_trust_"

#: Thread-status text shown while the turn is in flight (mirrors handler).
_STATUS_WORKING = "is working on your request"

#: Slack channel capabilities live in ``slack/transport.py`` (imported above).
#: This module used to carry a second literal copy of the declaration; two
#: literals for one fact is a drift hazard, and they had already diverged once.


def _approval_registry_key(session_key: str, request_id: str | int) -> str:
    """Namespace a kiro-cli request id by session for the approval registry.

    kiro-cli's JSON-RPC request id counter restarts at ``1`` for every session
    (``acp/client.py``), so two concurrent Slack threads both produce
    ``request_id == 1``. Keying the process-global registry (and the button
    ``action_id``/``value``) by ``session_key:request_id`` keeps each thread's
    approval isolated — a click resolves ONLY its own turn's pending tool.
    Falls back to the bare id when no session is supplied (e.g. unit tests).
    """
    rid = str(request_id)
    return f"{session_key}:{rid}" if session_key else rid


def build_approval_blocks(
    title: str, request_id: str | int, session_key: str = ""
) -> list[dict[str, Any]]:
    """Build Block Kit approve/deny buttons for a tool-permission request.

    The ``action_id``s and ``value`` encode a session-namespaced approval token
    (``session_key:request_id``) so the interaction handler correlates a click
    back to the awaiting decider WITHOUT colliding across concurrent sessions
    (kiro-cli request ids restart at 1 per session).
    """
    token = _approval_registry_key(session_key, request_id)
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"🔧 Approve tool *{title}*?"},
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Approve"},
                    "style": "primary",
                    "action_id": f"{TOOL_APPROVE_ACTION_PREFIX}{token}",
                    "value": token,
                },
                {
                    # Per-session trust: auto-approve all subsequent tools for
                    # THIS session only (not global YOLO). Mirrors native.
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Trust session"},
                    "action_id": f"{TOOL_TRUST_ACTION_PREFIX}{token}",
                    "value": token,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Deny"},
                    "style": "danger",
                    "action_id": f"{TOOL_DENY_ACTION_PREFIX}{token}",
                    "value": token,
                },
            ],
        },
    ]


class SlackApprovalDecider:
    """Awaits a human approve/deny/trust decision for an interactive tool prompt.

    The Slack interaction handler calls :meth:`resolve` when a button is
    clicked; :meth:`__call__` (the ``TurnDriver`` decider) awaits that result.
    Registry is keyed by request id (stringified). ``session_key`` lets the
    interaction handler map a click back to its session (for per-session Trust).
    """

    #: Process-global registry mapping request_id -> the decider currently
    #: awaiting it. The Slack interaction handler (module-level, no direct
    #: reference to the per-turn decider) resolves clicks through this.
    _REGISTRY: dict[str, "SlackApprovalDecider"] = {}

    def __init__(self, session_key: str = "") -> None:
        self._futures: dict[str, asyncio.Future[bool]] = {}
        self.session_key = session_key

    async def __call__(self, event: Any) -> bool:
        rid = str(getattr(event, "request_id", ""))
        key = _approval_registry_key(self.session_key, rid)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[bool] = loop.create_future()
        # _futures is per-decider, so keying by the bare rid is unambiguous
        # here; the process-global _REGISTRY must use the session-namespaced
        # key to avoid cross-session collisions (kiro-cli rids restart at 1).
        self._futures[rid] = fut
        SlackApprovalDecider._REGISTRY[key] = self
        try:
            # Deny-by-default if the user never clicks within the window.
            return await asyncio.wait_for(fut, timeout=_APPROVAL_TIMEOUT)
        except asyncio.TimeoutError:
            return False
        finally:
            self._futures.pop(rid, None)
            if SlackApprovalDecider._REGISTRY.get(key) is self:
                SlackApprovalDecider._REGISTRY.pop(key, None)

    def resolve(self, request_id: str | int, approved: bool) -> bool:
        """Resolve a pending approval. Returns True iff a future was waiting."""
        fut = self._futures.get(str(request_id))
        if fut is not None and not fut.done():
            fut.set_result(approved)
            return True
        return False

    @classmethod
    def resolve_global(cls, registry_key: str | int, approved: bool) -> bool:
        """Resolve a pending approval via the process-global registry.

        *registry_key* is the session-namespaced token from the button
        (``session_key:request_id``, or a bare id when no session). Used by the
        Slack interaction handler, which has no direct reference to the per-turn
        decider. Returns True iff a matching pending prompt was resolved (False
        if it already expired / was answered).
        """
        dec = cls._REGISTRY.get(str(registry_key))
        if dec is None:
            return False
        # Split the namespaced token back to the bare rid for the decider's
        # per-turn _futures lookup (rsplit is safe: the rid never contains ':').
        rid = str(registry_key).rsplit(":", 1)[-1]
        return dec.resolve(rid, approved)

    @classmethod
    def session_for(cls, registry_key: str | int) -> str:
        """Return the session_key of the decider awaiting *registry_key* (or "").

        Lets the interaction handler grant per-session Trust for a click without
        a direct decider reference. *registry_key* is the session-namespaced
        token from the button.
        """
        dec = cls._REGISTRY.get(str(registry_key))
        return dec.session_key if dec is not None else ""


class SlackRenderer(Renderer):
    """Renders abstract output events onto a Slack thread.

    Holds (and exposes) the underlying ``SlackClientOps`` so the inline
    dashboard->Slack mirror keeps working unchanged (guardrail G2). The
    streaming message is lazily opened on the first text/tool event.
    """

    channel_type = "slack"

    def __init__(
        self,
        slack: Any,
        channel: str,
        thread_ts: str | None,
        *,
        react_ts: str | None = None,
        reactions_enabled: bool = True,
        show_thinking: bool = True,
        capabilities: TransportCapabilities | None = None,
        decider: SlackApprovalDecider | None = None,
        now: Callable[[], float] | None = None,
        user_id: str = "",
    ) -> None:
        super().__init__(capabilities or SLACK_CAPABILITIES)
        self.slack = slack
        self.channel = channel
        self.thread_ts = thread_ts
        # The sending user — passed to DashboardContributor.decorate_reply on the
        # final outbound text so a composed edition can refresh its auth window /
        # append an expiry footer on the transport reply path too (native
        # handle_message already wires decorate_reply; this closes the gap so the
        # DEFAULT non-review Slack traffic gets the same treatment). "" in OSS.
        self._user_id = user_id
        # Message ts to react to (the user's triggering message). Falls back
        # to thread_ts so reactions still attach when not supplied separately.
        self._react_ts = react_ts or thread_ts or ""
        self._reactions_enabled = reactions_enabled
        # When slack.show_thinking is on, surface the model's reasoning as a 💭
        # thread reply above the answer (mirrors native handle_message). Off =>
        # reasoning stays private, only the typing indicator moves.
        self._show_thinking = show_thinking
        self._thinking_accumulated = ""
        self._thinking_posted = False
        self.decider = decider
        # Monotonic clock seam: injectable so the edit-throttle is deterministic
        # in tests and harness-controllable once handle_message drives this.
        self._now = now or time.monotonic
        self._stream_ts: str | None = None
        self._use_slack_stream = True  # False => chat.update cursor fallback
        self._accumulated = ""
        self._bracket_hold = ""  # held text from '[' until ']' to filter [OPTIONS:]
        self._stream_buffer = ""  # unsent text buffered between throttled flushes
        self._last_edit = 0.0  # monotonic ts of the last stream edit (throttle)
        self._task_counter = 0
        self._active_task_id = ""
        self._active_task_title = ""
        self._tool_start_time = 0.0  # monotonic ts of current tool start
        self._tool_timer_task: asyncio.Task | None = None  # 30s elapsed updater
        self._controller: Any = None
        self._tool_to_phase: Any = None
        self._finalized = False  # guards close() from double-finalizing
        self._t0 = 0.0
        self._started = False  # guards on_turn_start against double-fire

    async def on_turn_start(self) -> None:
        # Native sets the working thread-status before streaming and arms the
        # reaction controller at "queued". Idempotent: the dispatcher fires this
        # early (before session acquisition) so the ack reaction reaches the user
        # immediately; the TurnDriver's later call then no-ops.
        if self._started:
            return
        self._started = True
        self._t0 = self._now()
        self._ensure_controller()  # set_phase("queued")
        await self.slack.set_thread_status(self.channel, self.thread_ts or "", _STATUS_WORKING)

    def _ensure_controller(self) -> Any:
        """Lazily create the (reused) StatusReactionController.

        Imported lazily to avoid a ``handler <-> renderer`` import cycle once
        ``handler`` drives this renderer. Reuses the real controller so the
        debounce/stall/emoji behavior matches native exactly.
        """
        if self._controller is None and self._reactions_enabled:
            self._tool_to_phase = _tool_to_phase
            self._controller = StatusReactionController(
                self.slack, self.channel, self._react_ts, enabled=True
            )
            self._controller.set_phase("queued")
        return self._controller

    def _set_phase(self, phase: str) -> None:
        ctrl = self._ensure_controller()
        if ctrl is not None:
            ctrl.set_phase(phase)
            ctrl.on_progress()

    async def _ensure_stream(self) -> str:
        if self._stream_ts is None:
            ts = await self.slack.start_stream(self.channel, self.thread_ts or "")
            if ts:
                self._stream_ts = ts
                self._use_slack_stream = True
            else:
                # No streaming surface — fall back to chat.update on a posted
                # placeholder message (native ``_ensure_stream_started``).
                self._use_slack_stream = False
                self._stream_ts = await self.slack.post_message(
                    self.channel, _THINKING, self.thread_ts
                )
        return self._stream_ts

    async def _rotate_stream(self) -> str | None:
        """Stop the dead stream and start a fresh one (native ``_rotate_stream``)."""
        if self._stream_ts:
            await self.slack.stop_stream(self.channel, self._stream_ts)
        new_ts = await self.slack.start_stream(self.channel, self.thread_ts or "")
        if new_ts:
            self._stream_ts = new_ts
        else:
            self._use_slack_stream = False
        return new_ts

    async def _append_stream(self, text: str) -> bool:
        """Append to the stream, rotating once on failure (native ``_append_stream``)."""
        if not text or not self._stream_ts:
            return True
        ok = await self.slack.append_stream(self.channel, self._stream_ts, text)
        if not ok and self._use_slack_stream:
            if await self._rotate_stream():
                assert self._stream_ts is not None
                return await self.slack.append_stream(self.channel, self._stream_ts, text)
        return ok

    async def _flush_stream_buffer(self) -> None:
        """Strip thinking tags and flush the buffered stream text (if any)."""
        if not self._stream_buffer:
            return
        flush, _ = strip_thinking_tags(self._stream_buffer, strip_whitespace=False)
        self._stream_buffer = ""
        await self._append_stream(flush)

    async def _append_task(
        self, task_id: str, title: str, status: str, details: str = ""
    ) -> bool:
        """Append a task card, rotating once on failure (native ``_append_task``)."""
        if not self._stream_ts:
            return False
        ok = await self.slack.append_task(
            self.channel, self._stream_ts, task_id, title, status, details=details
        )
        if not ok and self._use_slack_stream:
            if await self._rotate_stream():
                assert self._stream_ts is not None
                return await self.slack.append_task(
                    self.channel, self._stream_ts, task_id, title, status, details=details
                )
        return ok

    def _tool_elapsed_str(self) -> str:
        """Formatted elapsed time for the active tool, or '' (native helper)."""
        if not self._tool_start_time:
            return ""
        elapsed = self._now() - self._tool_start_time
        if elapsed < 1:
            return ""
        mins, secs = divmod(elapsed, 60)
        if mins:
            return f"⏱ {int(mins)}m {secs:.1f}s"
        return f"⏱ {secs:.1f}s"

    async def _tool_elapsed_updater(self) -> None:
        """Every 30s, refresh the active task card's title with elapsed time."""
        while True:
            await asyncio.sleep(30)
            if self._active_task_id and self._tool_start_time and self._use_slack_stream:
                elapsed = self._now() - self._tool_start_time
                mins, secs = divmod(int(elapsed), 60)
                time_str = f"{mins}m {secs}s" if mins else f"{secs}s"
                # Elapsed goes in the TITLE (Slack replaces title on same
                # task_id), never details (which Slack appends).
                await self._append_task(
                    self._active_task_id, f"{self._active_task_title}  ⏱ {time_str}", "in_progress"
                )

    def _start_tool_timer(self) -> None:
        self._cancel_tool_timer()
        self._tool_start_time = self._now()
        self._tool_timer_task = asyncio.ensure_future(self._tool_elapsed_updater())

    def _cancel_tool_timer(self) -> None:
        if self._tool_timer_task and not self._tool_timer_task.done():
            self._tool_timer_task.cancel()
        self._tool_timer_task = None

    async def close(self) -> None:
        """Idempotent teardown for the transport dispatcher's ``finally``.

        Cancels the 30s ``_tool_elapsed_updater`` timer and finalizes the
        reaction controller. Without this, a ``TurnDriver.run()`` exception
        (which skips ``on_done``) would leave the timer task alive, issuing
        ``append_task`` calls every 30s against a dead stream until the event
        loop shuts down. Safe to call after ``on_done`` (no-op) and multiple
        times: the ``_finalized`` guard prevents flipping a already-successful
        turn's reaction to the error state.
        """
        self._cancel_tool_timer()
        if not self._finalized and self._controller is not None:
            try:
                self._controller.finalize(error=True)
            except Exception:
                pass  # non-critical teardown; never raise from close()
        self._finalized = True

    async def on_text_chunk(self, text: str) -> None:
        # Reuses the native streaming machinery verbatim: bracket-hold filter,
        # edit-throttle batching (``_EDIT_INTERVAL``), and the chat.update
        # cursor fallback when streaming is unavailable.
        # Flush any accumulated reasoning as a 💭 reply first so it lands above
        # the answer (no-op when show_thinking is off or nothing accumulated).
        await self._maybe_post_thinking()
        self._set_phase("thinking")
        ts = await self._ensure_stream()
        # Accumulate the FULL raw text (incl. any [OPTIONS:...]) so the final
        # extract_options in on_done sees it; only filter what is *shown*.
        self._accumulated += text
        # Bracket-hold: suppress [OPTIONS:...] markup on BOTH paths. Streaming
        # flushes (and clears) the buffer each edit; the no-stream fallback
        # never flushes, so the buffer accumulates the full *filtered* text —
        # used for the intermediate chat.update below while _accumulated keeps
        # the raw text for the final extract_options in on_done.
        self._bracket_hold, self._stream_buffer = _filter_options_brackets(
            text, self._bracket_hold, self._stream_buffer
        )
        now = self._now()
        if now - self._last_edit >= _EDIT_INTERVAL:
            if self._use_slack_stream:
                await self._flush_stream_buffer()
            else:
                # No-stream fallback: strip thinking tags for the intermediate
                # chat.update, mirroring _flush_stream_buffer on the stream path
                # so <thinking>…</thinking> never leaks into interim renders.
                filtered, _ = strip_thinking_tags(
                    self._stream_buffer, strip_whitespace=False
                )
                await _safe_update(self.slack, self.channel, ts, filtered + _CURSOR)
            self._last_edit = now

    async def on_thinking(self, text: str) -> None:
        self._set_phase("thinking")
        # Thinking is surfaced via the thread status indicator, not the stream.
        await self.slack.set_thread_status(self.channel, self.thread_ts or "", "is_typing")
        # When enabled, accumulate the reasoning so it can be posted as a 💭
        # thread reply above the answer (honors slack.show_thinking, matching
        # native). When disabled, reasoning is never accumulated or surfaced.
        if self._show_thinking:
            self._thinking_accumulated += text or ""

    async def _maybe_post_thinking(self) -> None:
        """Post the accumulated reasoning as a 💭 thread reply, once per turn.

        Called lazily when the first answer text arrives so the reasoning lands
        above the answer (mirrors native). Redacted before posting — reasoning
        can contain credentials/URLs just like the answer.
        """
        if self._thinking_posted or not self._show_thinking:
            return
        self._thinking_posted = True
        reasoning = self._thinking_accumulated.strip()
        if not reasoning:
            return
        reasoning, _ = redact_exfiltration_urls(reasoning)
        reasoning, _ = redact_credentials(reasoning)
        await self.slack.post_message(self.channel, f"💭 {reasoning}", self.thread_ts)

    async def on_tool_call(
        self, tool_call_id: str, title: str, tool_kind: str = "", tool_purpose: str = ""
    ) -> None:
        # Mirror native EVENT_TOOL_CALL: flush pending text, status update,
        # complete the previous task (with elapsed), start a new in-progress
        # task + its 30s elapsed timer.
        await self._ensure_stream()
        tool_name = title.removeprefix("Running: ")
        self._ensure_controller()
        if self._controller is not None and self._tool_to_phase is not None:
            self._controller.set_phase(self._tool_to_phase(tool_name, tool_kind))
            self._controller.on_progress()
        await self.slack.set_thread_status(
            self.channel, self.thread_ts or "", f"is using {tool_name}"
        )
        # Flush any buffered streamed text before the tool status, like native.
        if self._use_slack_stream:
            await self._flush_stream_buffer()
        if self._active_task_id:
            elapsed = self._tool_elapsed_str()
            self._cancel_tool_timer()
            ct = f"{self._active_task_title}  {elapsed}" if elapsed else self._active_task_title
            await self._append_task(self._active_task_id, ct, "complete")
        self._task_counter += 1
        self._active_task_id = f"tool_{self._task_counter}"
        self._active_task_title = tool_purpose or tool_name
        await self._append_task(
            self._active_task_id, self._active_task_title, "in_progress", details=tool_name
        )
        self._start_tool_timer()
        # The `wait` tool blocks MCP for up to 30min — finalize the streaming
        # message now so Slack doesn't show an error; the next text chunk opens
        # a fresh stream when wait returns.
        if tool_name == "wait" and self._use_slack_stream and self._stream_ts:
            if self._active_task_id:
                elapsed = self._tool_elapsed_str()
                self._cancel_tool_timer()
                ct = (
                    f"{self._active_task_title}  {elapsed}" if elapsed else self._active_task_title
                )
                await self._append_task(self._active_task_id, ct, "complete")
                self._active_task_id = ""
            await self.slack.stop_stream(self.channel, self._stream_ts)
            self._stream_ts = None
            self._accumulated = ""
            self._stream_buffer = ""

    async def on_prompt_choice(
        self, options: list[dict[str, Any]], request_id: str | int
    ) -> None:
        title = options[0].get("label") or options[0].get("id", "tool") if options else "tool"
        # Namespace the approval buttons by this turn's session so a click can
        # only resolve THIS session's pending tool (kiro-cli rids restart at 1
        # per session — a bare id would collide across concurrent threads).
        session_key = self.decider.session_key if self.decider else ""
        await self.slack.post_blocks(
            self.channel,
            build_approval_blocks(title, request_id, session_key),
            "Tool approval requested",
            self.thread_ts,
        )

    async def on_compaction(self, context_usage_pct: float) -> None:
        await self.slack.set_thread_status(
            self.channel, self.thread_ts or "", "compacting context…"
        )

    async def on_done(self, stop_reason: str = "") -> None:
        # Surface any reasoning not already flushed by the first text chunk
        # (e.g. a tool-only turn that produced no answer text).
        await self._maybe_post_thinking()
        if self._controller is not None:
            self._controller.finalize(error=False)
        self._finalized = True  # close() must not re-finalize as error
        if self._active_task_id and self._stream_ts is not None:
            elapsed = self._tool_elapsed_str()
            self._cancel_tool_timer()
            ct = f"{self._active_task_title}  {elapsed}" if elapsed else self._active_task_title
            await self._append_task(self._active_task_id, ct, "complete")
            self._active_task_id = ""
        self._cancel_tool_timer()
        # Flush any buffered (throttled) stream text before finalizing.
        if self._use_slack_stream:
            await self._flush_stream_buffer()
        clean_text, options = extract_options(self._accumulated)
        # Defensive final full-text redaction before posting — belt-and-braces
        # with the driver's StreamRedactor (mirrors native's final redact pass),
        # so nothing unredacted reaches the channel even if a chunk slipped
        # through the rolling buffer.
        if clean_text:
            clean_text, _ = redact_exfiltration_urls(clean_text)
            clean_text, _ = redact_credentials(clean_text)
            # Outbound-reply decorator seam (Default: identity, OSS-identical) —
            # the transport-path twin of the native handle_message wiring, so the
            # DEFAULT non-review Slack path also refreshes a composed edition's auth
            # window / appends its expiry footer. Runs AFTER redaction; any text the
            # decorator INTRODUCES is re-scanned so a decorator cannot smuggle a URL
            # or credential past the redaction above. Fail-safe: a raising decorator
            # falls back to the undecorated text.
            from kiro_crew.platform import current_context, safe_context_call

            _pre_decorate = clean_text
            clean_text = safe_context_call(
                lambda: current_context().dashboard.decorate_reply(
                    _pre_decorate, channel=self.channel, user_id=self._user_id
                ),
                fallback=_pre_decorate,
                log_message="dashboard.decorate_reply failed; sending undecorated reply",
            )
            if clean_text != _pre_decorate:
                # Re-scan decorator-introduced text (the redaction above ran before
                # decoration). No module logger here — the redaction itself is the
                # security property; the native path logs counts, this path stays
                # silent to avoid adding a logger to the renderer.
                clean_text, _ = redact_exfiltration_urls(clean_text)
                clean_text, _ = redact_credentials(clean_text)
        if self._stream_ts is not None:
            if self._use_slack_stream:
                await self.slack.stop_stream(self.channel, self._stream_ts, clean_text or None)
            else:
                # No-stream fallback: _stream_ts is a regular message ts (the
                # _THINKING placeholder), not a stream handle — finalize it via
                # chat.update, mirroring the on_tool_call gating.
                await _safe_update(self.slack, self.channel, self._stream_ts, clean_text or "")
        # Clear thread status now that the turn is complete.
        await self.slack.set_thread_status(self.channel, self.thread_ts or "", "")
        # Timing footer (always posted at turn end), mirroring native.
        turn_elapsed = (self._now() - self._t0) if self._t0 else 0.0
        footer_blocks, footer_text = build_timing_footer(turn_elapsed, None)
        footer_blocks = _append_footer_actions(footer_blocks, options, self.thread_ts, None, None)
        await self.slack.post_blocks(self.channel, footer_blocks, footer_text, self.thread_ts)
