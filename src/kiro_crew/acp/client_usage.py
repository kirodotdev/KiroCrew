"""Fold an ``AcpClient`` session's usage, metadata and compaction frames into its turn stats.

Usage and prompt-token updates, context-window metadata, claude and codex inline
compaction, and ``wait_for_compaction``.

Each function here whose first parameter is ``self`` is an ``AcpClient`` method: the
class binds it by name, so it reads and writes the session's own state on the instance,
and a patch of the method on the class or on an instance reaches every caller.

Instance fields written: ``_between_turn_credits``, ``_claude_compaction_pending``,
``_codex_compaction_pending``, ``_compaction_failed_at``, ``_mcp_notifications``,
``last_compaction_transient``.

Instance fields read: ``_model``, ``_resolved_model_id``, ``last_prompt_stats``,
``memory_mode``.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from kiro_crew.acp._dispatch import (
    parse_claude_compaction_notice,
    parse_codex_compaction_update,
    parse_prompt_token_usage,
    parse_refusal,
    parse_usage_cost,
    parse_usage_update,
    redact_text,
)
from kiro_crew.acp.client_timeouts import _POST_COMPACTION_METADATA_GRACE_SECS, _READ_TIMEOUT
from kiro_crew.acp.transport_errors import (
    AcpError,
    compaction_failure_detail,
    compaction_failure_is_transient,
)
from kiro_crew.acp.types import (
    ACP_BACKENDS_INLINE_COMPACTION,
    ACP_BACKENDS_STRUCTURED_REFUSAL,
    ACP_BACKENDS_UNMARKED_COMPACTION_PAIR,
    EVENT_COMPACTION_STATUS,
    KNOWN_SESSION_UPDATES,
    METHOD_COMPACTION_STATUS,
    METHOD_METADATA,
    STOP_REASON_END_TURN,
    UPDATE_CONFIG_OPTION,
    UPDATE_USAGE,
    AcpEvent,
    JsonRpcMessage,
)
from kiro_crew.constants import COMPACT_WAIT_TIMEOUT_SECS

if TYPE_CHECKING:
    from kiro_crew.acp.client import AcpClient

#: The client's own logger: this code logged under that name before the split.
logger = logging.getLogger("kiro_crew.acp.client")


def _track_usage_update(self: AcpClient, msg: JsonRpcMessage) -> None:
    """Track context usage and config updates from session update notifications."""
    params = msg.params or {}
    update = params.get("update", {})
    kind = update.get("sessionUpdate") if isinstance(update, dict) else None
    if kind == UPDATE_USAGE:
        # used/size come straight from the agent process; a malformed
        # value (string, list, bool, NaN/Infinity, bignum beyond float
        # range) must degrade to "absent" rather than raise mid-turn.
        # parse_usage_update validates both fields at the shared
        # chokepoint used by AcpSessionHandle._handle_update too.
        used, size = parse_usage_update(update)
        if used is not None and size and size > 0:
            self.last_prompt_stats.context_pct = round((used / size) * 100, 1)
            # Keep the raw counts so the dashboard token text uses the real
            # served window (size) instead of re-deriving it from the model id.
            self.last_prompt_stats.context_used_tokens = int(used)
            self.last_prompt_stats.context_window_tokens = int(size)
            # Mark the counts authoritative so a later metadata
            # contextUsagePercentage cannot clobber this token-derived pct.
            self.last_prompt_stats.context_tokens_from_usage = True
            self.last_prompt_stats.note_pct_reported()
        else:
            logger.debug("usage_update missing used/size: %s", update)
        # Session-cumulative billing cost (claude seam); kiro never sends
        # the key so this is None on the kiro path. Delta'd per turn on
        # the stats object (monotonic guard lives there).
        cost = parse_usage_cost(update)
        if cost is not None:
            self.last_prompt_stats.apply_cost_cumulative(cost)
    elif kind == UPDATE_CONFIG_OPTION:
        self._handle_config_option_update(msg)
    elif self._is_claude and kind and kind not in KNOWN_SESSION_UPDATES:
        logger.debug("Unhandled session update type: %s", kind)


def _track_prompt_usage(self: AcpClient, result: Any) -> None:
    """Fold a PromptResponse's turn-scoped token counts into the stats.

    The claude-agent-acp adapter reports per-turn token counts on the
    prompt response; kiro-cli's response carries only ``stopReason``, so
    ``parse_prompt_token_usage`` returns None there and the stats are
    untouched (harness parity). Validation lives at that shared
    chokepoint, mirroring ``_track_usage_update``.
    """
    tokens = parse_prompt_token_usage(result)
    if tokens is not None:
        self.last_prompt_stats.apply_prompt_token_usage(*tokens)


def _backfill_context_window(self: AcpClient, pct: float) -> None:
    """Derive window/used tokens from a percentage-only reading.

    Thin wrapper binding this client's resolved model id; the shared logic
    lives on ``AcpPromptStats.backfill_context_window`` (the AcpSessionHandle
    path delegates to the same method, so the two cannot drift).
    """
    self.last_prompt_stats.backfill_context_window(pct, self._resolved_model_id or self._model)


def _track_metadata(self: AcpClient, msg: JsonRpcMessage, *, between_turns: bool = False) -> None:
    """Fold one ``_kiro.dev/metadata`` (or compaction-status) frame into the stats.

    *between_turns* is for the readers that run after a turn has billed its
    ``EVENT_COMPLETE`` (``wait_for_compaction`` and its grace drain). Their
    credits are held in ``_between_turn_credits`` instead of the stats window,
    which the next turn's ``carry_over()`` replaces before anything bills it;
    that next turn adds the held amount once. The caller says which window it
    reads for, because ``_turn_done`` cannot: ``stream_command`` never clears it.
    """
    params = msg.params or {}
    # Content-filter refusal envelope. Opt-in by membership (H6): a harness
    # that has not demonstrated the payload does not have its metadata
    # frames guessed at. Folded onto the terminal by ``terminal_refusal``.
    if self.backend in ACP_BACKENDS_STRUCTURED_REFUSAL:
        _refusal = parse_refusal(params)
        if _refusal is not None:
            self.last_prompt_stats.refusal = _refusal
    # A real usage_update is authoritative for both the token counts AND the
    # pct derived from them. kiro's metadata percentage can measure a
    # different window, so applying it here would desync the headline % from
    # the "used / total" token text (e.g. 73% shown next to 408K / 1000K).
    # sanitize_pct is the shared coercion (the KAS usagePercentage path uses
    # it too): it clamps NaN/±inf/out-of-range and returns None when absent.
    pct_f = self.last_prompt_stats.sanitize_pct(params.get("contextUsagePercentage"))
    if pct_f is not None and not self.last_prompt_stats.context_tokens_from_usage:
        self.last_prompt_stats.context_pct = pct_f
        self.last_prompt_stats.note_pct_reported()
        self._backfill_context_window(pct_f)
    # kiro streams per-turn billing as meteringUsage entries (unit="credit").
    # Accumulate across the turn's metadata notifications; reset per turn by
    # the AcpPromptStats re-init in _dispatch_events/send_message_stream.
    credits = 0.0
    metering = params.get("meteringUsage")
    if isinstance(metering, list):
        for entry in metering:
            if isinstance(entry, dict) and entry.get("unit") == "credit":
                try:
                    credits += float(entry.get("value", 0) or 0)
                except (TypeError, ValueError):
                    pass
    if between_turns:
        self._between_turn_credits = getattr(self, "_between_turn_credits", 0.0) + credits
    else:
        self.last_prompt_stats.credits += credits
        # Per-call context readings and the metered call count, shared with the
        # AcpSessionHandle tracker so both transports report the same turn. A
        # between-turns frame belongs to no turn's window, like its credits.
        self.last_prompt_stats.note_metadata_frame(pct_f, metering)


def _handle_compaction_status(self: AcpClient, msg: JsonRpcMessage) -> None:
    """Log a ``_kiro.dev/compaction/status`` notification and, on
    completion, drop the now-stale context-usage counts.

    This is the single chokepoint every compaction-status arrival routes
    through (all prompt dispatch loops and ``wait_for_compaction``), so the
    reset cannot be missed by one path. Without it the pre-compaction
    counts survive — and their ``context_tokens_from_usage=True`` flag
    blocks ``_track_metadata`` from applying any fresh percentage — so the
    dashboard's context meter would keep showing the old usage after a compact.
    """
    params = msg.params or {}
    status = params.get("status", "")
    logger.info("Compaction status: %s", status)
    # On failure, kiro-cli's notification carries no dedicated error/reason
    # field today (only `status.type` + an optional `summary`, which is
    # populated on success but typically empty on failure). Log the full
    # raw params at WARNING so a future occurrence is actually debuggable
    # instead of surfacing only "unknown error" to the user with nothing
    # to grep for server-side.
    s_type = status.get("type", "") if isinstance(status, dict) else str(status)
    if s_type == "failed":
        if self.memory_mode == "persistent":
            logger.warning("Compaction failed — raw notification params: %s", params)
        # Arm the bounded post-failure wait (see
        # _COMPACTION_FAILED_TURN_BUDGET): kiro-cli may never answer the
        # prompt this compaction was for.
        self._compaction_failed_at = time.monotonic()
        self.last_compaction_transient = compaction_failure_is_transient(params)
    elif s_type == "completed":
        self._compaction_failed_at = None
        self.last_prompt_stats.reset_after_compaction()


def _claude_compaction_event(self: AcpClient, chunk: str) -> AcpEvent | None:
    """Reclassify a claude-agent-acp compaction notice chunk as an event.

    The Claude adapter reports compaction as plain assistant text rather
    than an out-of-band notification, so this is the claude-side twin of
    ``_handle_compaction_status``: it applies the same state mutations
    (arm/disarm the post-failure budget, drop the stale context counts) and
    returns the EVENT_COMPACTION_STATUS every consumer already understands.
    ``None`` means the chunk is ordinary assistant text and must be yielded
    as-is.

    Backend-gated: the markers are anchored and kiro-cli has no reason to
    emit them, but only the Claude adapter is a KNOWN producer, so no other
    backend's prose can be reinterpreted as a control frame here.

    Callers MUST still forward the text. The event is a SIDE EFFECT, never
    a substitute for the chunk: the adapter ships these notices as ordinary
    assistant text with no marker of any kind, so recognizing one is a guess
    about prose, and a layer that swallowed the chunk would turn any wrong
    guess into deleted model output. A caller that yields structured events
    marks the forwarded chunk ``control_notice`` instead, so a consumer can
    show the text without counting it as the turn's own answer.

    A terminal is only accepted while a compaction is actually in flight.
    ``Compacting completed.`` standing alone is just prose — a user can ask
    for exactly that reply — and swallowing it would delete the answer AND
    reset the context counters against a window nobody summarized.  The
    ``started`` arm cannot be gated the same way: it is what arms the flag.
    """
    parsed = parse_claude_compaction_notice(chunk) if self._is_claude else None
    if parsed is None:
        return None
    status_type, detail = parsed
    if status_type != "started" and not self._claude_compaction_pending:
        return None
    logger.info("Compaction status (claude): %s", status_type)
    self._claude_compaction_pending = status_type == "started"
    if status_type == "completed":
        self._compaction_failed_at = None
        self.last_prompt_stats.reset_after_compaction()
    elif status_type == "failed":
        logger.warning("Compaction failed (claude): %s", detail or "no reason reported")
        # Arm the bounded post-failure wait, exactly as the kiro-cli path
        # does: the backend may never answer the prompt this compaction
        # was for.
        self._compaction_failed_at = time.monotonic()
        # The adapter ships prose, not a payload, so the parsed notice text
        # IS the whole reason — wrap it in the shape the classifier reads
        # rather than teaching it a second input type. ``reason`` is a
        # reason-bearing key, so the scan sees it.
        self.last_compaction_transient = compaction_failure_is_transient({"reason": detail or ""})
    # Backend-echoed text on its way to the dashboard — redact before it can
    # reach any surface (parity with the kiro-cli/KAS compaction summaries).
    return AcpEvent(kind=EVENT_COMPACTION_STATUS, text=status_type, title=redact_text(detail))


def _codex_compaction_event(self: AcpClient, msg: JsonRpcMessage) -> AcpEvent | None:
    """Reclassify a codex-acp context-compaction frame as an event.

    The codex-side twin of ``_handle_compaction_status`` and
    ``_claude_compaction_event``: it applies the same state mutation (drop the
    stale context counts on a terminal) and returns the
    ``EVENT_COMPACTION_STATUS`` every consumer already understands -- the
    dashboard notice and context-meter reset, the messaging drivers, and
    ``wait_for_compaction``. ``None`` means the frame is an ordinary
    ``tool_call`` and must be handled as one.

    Gated on ``ACP_BACKENDS_INLINE_COMPACTION``, the set of harnesses whose
    compaction lands INSIDE the prompt turn -- so a harness that earns that
    membership inherits the translation by joining the set. The FRAME decides:
    ``_meta.contextCompaction`` is codex-acp's own marker, and the unmarked 2.2.2
    shape is read only for ``ACP_BACKENDS_UNMARKED_COMPACTION_PAIR``. claude (a
    member) sends neither, so the parser declines its frames.

    Reached on this class through the dormant codex seam -- a live codex
    session is served by ``AcpRuntime``, whose ``AcpSessionHandle`` carries the
    same method. Both implementations answer, rather than one, because a
    capability the two transports disagree about is a capability that works on
    whichever one a reader did not test (harness-parity H6).

    Callers MUST still forward the frame. This is a SIDE EFFECT, never a
    substitute: the frame is also a real tool call in the transcript, and a
    layer that swallowed it would drop a row the user watched appear.

    There is no ``failed`` arm because codex-acp sends no such status -- see
    ``parse_codex_compaction_update``. A compaction that errors leaves the
    ``session/prompt`` request unanswered, which the prompt loop's own
    deadline owns; this method neither invents a terminal nor arms the
    post-failure budget on a guess.
    """
    if self.backend not in ACP_BACKENDS_INLINE_COMPACTION:
        return None
    params = msg.params or {}
    update = params.get("update")
    if not isinstance(update, dict):
        return None
    status_type = parse_codex_compaction_update(
        update,
        self._codex_compaction_pending,
        allow_unmarked=self.backend in ACP_BACKENDS_UNMARKED_COMPACTION_PAIR,
    )
    if status_type is None:
        return None
    if status_type != "started" and not self._codex_compaction_pending:
        return None
    logger.info("Compaction status (codex): %s", status_type)
    # The start's call id, so an unmarked (2.2.2) terminal can be matched to it.
    call_id = update.get("toolCallId")
    if status_type != "started":
        self._codex_compaction_pending = False
    elif isinstance(call_id, str) and call_id:
        self._codex_compaction_pending = call_id
    else:
        self._codex_compaction_pending = True
    if status_type == "completed":
        self._compaction_failed_at = None
        self.last_prompt_stats.reset_after_compaction()
    # No title: the adapter ships no summary with either frame, and an empty
    # string is what every consumer already renders for "compacted, no
    # summary offered".
    return AcpEvent(kind=EVENT_COMPACTION_STATUS, text=status_type, title="")


def _settle_codex_compaction(self: AcpClient, reason: str) -> AcpEvent | None:
    """Close out a codex compaction whose terminal never arrived, or None.

    Called at the turn's terminal. ``None`` -- the ordinary case -- means no
    codex compaction was in flight, or one was and it already reported.

    The verdict is ``failed``, and that is the opposite of what the claude
    twin synthesizes. The two harnesses differ in what a MISSING terminal
    means. claude-agent-acp sends its ``Compacting completed.`` text only for
    a MANUAL ``/compact``; an automatic mid-turn compaction takes a different
    path and emits no text at all, so for claude a turn that ends naturally
    after a ``started`` is evidence the compaction finished. codex-acp sends
    its terminal for BOTH -- the capture shows the marked
    ``tool_call_update`` on a manual ``/compact`` and on a native
    ``model_auto_compact_token_limit`` compaction alike -- so here a missing
    terminal is evidence the compaction did NOT finish. Reporting
    ``completed`` would reset the context meter against a window nobody
    summarized.

    One arm for every *reason*, unlike its claude twin, and for the same
    reason the verdict differs: this is not an inference from how the turn
    ended, it is the absence of a frame codex always sends on success. A turn
    cancelled mid-compaction did not compact either.

    Three things it deliberately does NOT do:

    * reset the context counts -- nothing was summarized, so the pre-compaction
      numbers are still the true ones;
    * arm the post-failure budget (``_compaction_failed_at``) -- that budget
      exists to bound a wait for a turn that may never end, and this runs AT
      the end of the turn, so arming it would charge the NEXT turn's idle
      clock for this one's failure;
    * claim the failure is retryable. ``last_compaction_transient`` is set
      False because an inferred failure carries no reason to classify, and
      False is the value that does not promise a user a retry will help.

    The title is text this module authors, not text a backend echoed, so it
    needs no redaction -- and it is there so the surfaces that render a
    failure reason stop collapsing this case to "unknown error".
    """
    if not self._codex_compaction_pending:
        return None
    self._codex_compaction_pending = False
    logger.warning(
        "Compaction status (codex): started with no terminal, turn ended %r",
        reason or "unknown",
    )
    self.last_compaction_transient = False
    return AcpEvent(
        kind=EVENT_COMPACTION_STATUS,
        text="failed",
        title="the turn ended without a compaction result",
        synthesized=True,
    )


def _settle_claude_compaction(self: AcpClient, reason: str) -> AcpEvent | None:
    """Synthesize the terminal an AUTOMATIC claude compaction never sends.

    The adapter emits ``Compacting completed.`` only from the SDK's
    ``compact_result`` status, which it documents as the manual ``/compact``
    signal; an automatic mid-turn compaction takes its ``compact_boundary``
    case instead and emits only a ``usage_update``.  So the turn ends with a
    dangling ``started``.  Called at the turn's terminal, this closes it out
    so consumers leave their compacting state and the context meter resets.

    Reporting ``completed`` is a statement about what the backend did, not a
    guess — but ONLY for *reason* ``end_turn``.  The turn having reached its
    own natural terminal is the whole evidence that the compaction finished:
    a compaction that had FAILED would have said so (the adapter emits its
    failure text from the same status handler that emits the success text).
    A turn that ends any other way — cancelled by the user's Stop, refused,
    or cut off on a limit — carries no such evidence, so it settles the
    pending flag WITHOUT claiming success: no ``completed`` event, no
    context-counter reset, and no clearing of a recorded failure.  Otherwise
    pressing Stop mid-compaction would fabricate a successful compaction and
    reset the meter against a context that was never actually summarized.
    """
    if not self._claude_compaction_pending:
        return None
    self._claude_compaction_pending = False
    if reason != STOP_REASON_END_TURN:
        logger.info(
            "Compaction status (claude): pending compaction abandoned, turn ended %r",
            reason or "unknown",
        )
        return None
    logger.info("Compaction status (claude): completed (synthesized at turn end)")
    self._compaction_failed_at = None
    self.last_prompt_stats.reset_after_compaction()
    # ``synthesized`` marks this terminal as manufactured at the turn's end
    # rather than observed mid-turn. It arrives AFTER every text chunk of the
    # turn, so a consumer that treats a compaction terminal as a segment
    # boundary would discard the answer a backend produced after compacting.
    return AcpEvent(kind=EVENT_COMPACTION_STATUS, text="completed", title="", synthesized=True)


async def wait_for_compaction(self: AcpClient, timeout: float = COMPACT_WAIT_TIMEOUT_SECS) -> dict:
    """Read messages until compaction completed/failed arrives. Returns status dict.

    On ``completed``, keeps draining for a short grace window: kiro-cli
    emits a fresh ``_kiro.dev/metadata`` with the REAL post-compaction
    ``contextUsagePercentage`` about a second after the completed status
    (live-probe confirmed). ``_handle_compaction_status`` has already
    dropped the stale counts (clearing the authoritative flag), so that
    metadata re-derives accurate numbers — the caller's ``context_usage``
    broadcast then reports the true compacted size instead of an unknown.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        msg = await self._read_message(timeout=min(remaining, _READ_TIMEOUT))
        if msg is None:
            continue
        if msg.is_method(METHOD_COMPACTION_STATUS):
            self._handle_compaction_status(msg)
            params = msg.params or {}
            status = params.get("status", {})
            s_type = status.get("type", "") if isinstance(status, dict) else str(status)
            if s_type in ("completed", "failed"):
                self._track_metadata(msg, between_turns=True)
                if s_type == "completed":
                    await self._drain_post_compaction_metadata()
                # Redact the backend-echoed summary before it reaches callers
                # (compact() surfaces this to the dashboard and channel
                # mirrors). Mirrors AcpSessionHandle.wait_for_compaction.
                summary = redact_text(str(params.get("summary", "") or ""))
                if s_type == "failed" and not summary:
                    # kiro-cli leaves `summary` empty on failure, so the
                    # manual /compact notice would say nothing — carry the
                    # notification's own reason, read by the same extractor
                    # the dispatch loop uses for the streaming notice.
                    summary = compaction_failure_detail(params)
                return {"type": s_type, "summary": summary}
        elif msg.is_method(METHOD_METADATA):
            self._track_metadata(msg, between_turns=True)
        else:
            # Don't drop — buffer for later processing
            if msg.method and not msg.id:
                self._mcp_notifications.append(msg)
    return {"type": "timeout"}


async def _drain_post_compaction_metadata(
    self: AcpClient, grace: float = _POST_COMPACTION_METADATA_GRACE_SECS
) -> None:
    """Drain for the post-compaction ``_kiro.dev/metadata`` notification.

    Returns as soon as a metadata frame carrying a real
    ``contextUsagePercentage`` is applied — a credits-only/empty metadata
    frame is consumed but does NOT end the drain, or the usage frame
    behind it would be stranded and the meter would fall back to the
    reset/unknown state. Gives up quietly at the grace deadline (the
    meter then self-corrects on the next turn's telemetry). Non-metadata
    notifications are buffered exactly like the main wait loop. Process
    death (``AcpError``) propagates — the outer ``wait_for_compaction``
    contract lets it, and swallowing it here would report a completed
    compaction on a dead runtime.
    """
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        try:
            msg = await self._read_message(timeout=remaining)
        except AcpError:
            raise
        except Exception:
            return
        if msg is None:
            continue
        if msg.is_method(METHOD_METADATA):
            self._track_metadata(msg, between_turns=True)
            if (msg.params or {}).get("contextUsagePercentage") is not None:
                return
            continue
        if msg.method and not msg.id:
            self._mcp_notifications.append(msg)
