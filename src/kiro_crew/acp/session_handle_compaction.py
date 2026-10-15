"""Context compaction.

The ``/compact`` prompt, the KAS compaction verb, codex and claude inline compaction,
``wait_for_compaction``, and the usage and metadata frames compaction is measured by.

Each function here whose first parameter is ``self`` is an ``AcpSessionHandle`` method: the
class binds it by name, so it reads and writes the session's own state on the instance,
and a patch of the method on the class or on an instance reaches every caller.

Instance fields written: ``_between_turn_credits``, ``_codex_compaction_pending``,
``_compact_result``, ``_compaction_failed_at``, ``last_compaction_transient``.

Instance fields read: ``_cancelled``, ``_compaction_terminal_seen``,
``_kas_compact_turn``, ``_last_stop_reason``, ``_model``, ``_queue``,
``_resolved_model_id``, ``_runtime``, ``_turn_done``, ``last_prompt_stats``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from kiro_crew.acp._dispatch import (
    parse_codex_compaction_update,
    parse_metadata,
    parse_prompt_token_usage,
    parse_refusal,
    redact_text,
)
from kiro_crew.acp.client import compaction_failure_detail, parse_slash_command
from kiro_crew.acp.types import (
    ACP_BACKEND_KAS,
    ACP_BACKENDS_INLINE_COMPACTION,
    ACP_BACKENDS_STRUCTURED_REFUSAL,
    ACP_BACKENDS_UNMARKED_COMPACTION_PAIR,
    EVENT_COMPACTION_STATUS,
    METHOD_KAS_SESSION_COMPACT,
    STOP_REASON_END_TURN,
    AcpEvent,
    JsonRpcMessage,
)
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.constants import COMPACT_WAIT_TIMEOUT_SECS

if TYPE_CHECKING:
    from kiro_crew.acp.session_handle import AcpSessionHandle

#: The session handle's logger: this code logged under that name before the split.
logger = logging.getLogger("kiro_crew.acp.session_handle")


# Post-compaction metadata grace: kiro-cli emits fresh _kiro.dev/metadata with
# the real post-compaction contextUsagePercentage ~1s after the completed
# status (live-probe confirmed). Mirrors AcpClient's constant.
_POST_COMPACTION_METADATA_GRACE_SECS = 5.0


# ── Compaction ──


async def compact(self: AcpSessionHandle, context: str = "") -> None:
    """Trigger context compaction via a ``/compact`` prompt.

    Sent through ``session/prompt`` (drained to turn end), NOT through
    ``_kiro.dev/commands/execute``: kiro-cli 2.14.0 exits rc=0 without a
    response on the STRING form of commands/execute (live-probe confirmed
    for /compact and /help alike; the object form used by /effort is
    fine). The prompt transport matches the dashboard's manual /compact
    and Slack's !compact: kiro ACKs the prompt (end_turn) and then emits
    ``_kiro.dev/compaction/status``, which ``wait_for_compaction()``
    picks up from the session queue. On KAS :meth:`prompt` sends the
    engine's ``_kiro/session/compact`` instead, and its outcome arrives
    inside the turn.
    """
    cmd = "/compact"
    if context:
        cmd = f"/compact {context}"
    # Capture a terminal status emitted MID-TURN (before end_turn) while
    # draining — otherwise it would be consumed and lost, stranding a
    # subsequent wait_for_compaction() until timeout even though the
    # compact succeeded. wait_for_compaction() consumes this cache first.
    self._compact_result = None
    async for event in self.prompt(cmd):
        if event.kind == EVENT_COMPACTION_STATUS and event.text in (
            "completed",
            "failed",
        ):
            self._compact_result = {
                "type": event.text,
                "summary": event.title or "",
            }


def _kas_compact_request(self: AcpSessionHandle, message: str) -> bool:
    """Whether *message* is a ``/compact`` that goes to KAS's compaction verb.

    Only on KAS, and only for a message whose first word is ``/compact``.
    Every other backend keeps the ``/compact`` prompt it already answers.
    """
    runtime = getattr(self, "_runtime", None)
    if getattr(runtime, "acp_backend", None) == ACP_BACKEND_KAS and message.strip().startswith("/"):
        name, _args = parse_slash_command(message)
        return name == "compact"
    return False


def _kas_compact_advertised(self: AcpSessionHandle) -> bool:
    """Whether the engine listed ``_kiro/session/compact`` at ``initialize``.

    Read from ``agentCapabilities._meta.kiro.extensionMethods`` as the
    runtime kept it. Fails closed: no record means no verb.
    """
    caps = getattr(getattr(self, "_runtime", None), "_agent_capabilities", None)
    meta = caps.get("_meta") if isinstance(caps, dict) else None
    kiro = meta.get("kiro") if isinstance(meta, dict) else None
    methods = kiro.get("extensionMethods") if isinstance(kiro, dict) else None
    return isinstance(methods, list) and METHOD_KAS_SESSION_COMPACT in methods


def _settle_kas_compaction(self: AcpSessionHandle, result: Any) -> AcpEvent | None:
    """The compaction status a KAS compaction answer still owes, or None.

    A run that commits a summary has already sent ``summarization_completed``,
    which reset the meter, so the answer adds nothing. Two answers come
    with no frame, and each gets its status here so no caller waits for one:

    * ``success: true`` with nothing summarized (an empty history, an empty
      summary, or a commit after the session moved on) is ``completed``.
      The meter is NOT reset, because the context did not change.
    * ``success: false`` is ``failed``. KAS refuses while a turn or another
      compaction is running, and on a timeout or model error, and does not
      say which. Not marked transient and no failure budget is armed, for
      the reasons on :meth:`_settle_codex_compaction`.
    """
    if getattr(self, "_compaction_terminal_seen", False):
        return None
    if isinstance(result, dict) and result.get("success") is True:
        return AcpEvent(kind=EVENT_COMPACTION_STATUS, text="completed", title="")
    self.last_compaction_transient = False
    return AcpEvent(
        kind=EVENT_COMPACTION_STATUS,
        text="failed",
        title="KAS did not compact the session; it may be busy, so try again shortly",
    )


def _inline_turn_finished_cleanly(self: AcpSessionHandle) -> bool:
    """Whether the last turn reached its own end boundary uncancelled.

    A cancelled turn is NOT a completed compaction, and this is the arm where
    getting that wrong is most expensive: a false ``completed`` resets the
    context meter and arms the compaction cooldown, so the session stays full
    AND stops retrying. The turn reaching its own end boundary is the whole
    evidence an inline harness offers, so the absence of that boundary has to
    withhold the answer.

    The test is POSITIVE -- the stop reason must BE ``end_turn`` -- rather than
    "not cancelled". Excluding cancels alone accepts every other way a turn can
    fail to finish: a refusal, a token limit, a stop reason this build does not
    recognise, or a turn that never reported one at all. Each of those means the
    compaction did not run, and each would otherwise be reported as a success.

    ``_cancelled`` is checked as well as the reason, because a cancel that never
    got an ack leaves the reason empty while the flag is already set -- and an
    unacked cancel is exactly the case where the compaction is least likely to
    have run. Both are THIS class's own turn state, so they are read directly
    here -- the sibling on ``AcpProvider`` asks
    ``AcpClient.turn_finished_cleanly`` instead, because there the state belongs
    to the client and reading it across that boundary would decide a
    compaction's fate from a shape the provider does not own. Both reads keep
    ``getattr`` defaults that fail CLOSED, so a handle built without a turn --
    the ``__new__`` shape several suites use -- does not get the shortcut.
    """
    if getattr(self, "_cancelled", True):
        return False
    return getattr(self, "_last_stop_reason", "") == STOP_REASON_END_TURN


def _compacts_inline(self: AcpSessionHandle) -> bool:
    """Whether this handle's backend finishes a compaction inside its turn.

    ``ACP_BACKENDS_INLINE_COMPACTION`` membership, read through
    ``capabilities_for`` so the answer comes from the same table every other
    consumer reads.

    Read off ``self._runtime.acp_backend``, the way every other capability on
    this class reads it -- the handle has no backend of its own, it fronts a
    runtime's.

    Fails CLOSED, and that direction is the point rather than caution: a
    handle built through ``__new__`` has no ``_runtime`` at all, which is the
    shape several suites use to exercise the queue drain without spawning a
    process. Returning False there leaves such a handle on the waiting arm,
    the one every backend outside ``ACP_BACKENDS_INLINE_COMPACTION`` takes.
    Claiming the capability instead would tell every one of those callers a
    compaction completed that nothing ever ran.
    """
    runtime = getattr(self, "_runtime", None)
    backend = getattr(runtime, "acp_backend", None)
    if not isinstance(backend, str):
        return False
    return capabilities_for(backend).compacts_inline


async def wait_for_compaction(
    self: AcpSessionHandle, timeout: float = COMPACT_WAIT_TIMEOUT_SECS
) -> dict[str, str]:
    """Wait for compaction completed/failed event from the session queue.

    Returns {"type": "completed"|"failed"|"timeout", "summary": "..."}.
    Consumes the result compact() captured mid-turn if there is one,
    otherwise drains the queue looking for COMPACTION_STATUS
    notifications (the async-after-end_turn case).
    """
    cached = self._compact_result
    if cached is not None:
        self._compact_result = None
        if cached.get("type") == "completed":
            # The dispatch loop reset the stats when it captured this
            # mid-turn; kiro's fresh post-compaction metadata arrives ~1s
            # after the completed status — wait briefly so callers can
            # broadcast the REAL compacted usage instead of the unknown
            # fallback.
            await self._drain_post_compaction_metadata()
        return cached
    if self._compacts_inline() and self._inline_turn_finished_cleanly():
        # The drained turn IS the result for a member of
        # ``ACP_BACKENDS_INLINE_COMPACTION`` — the same record as on
        # ``AcpProvider.wait_for_compaction``, including why it sits on the
        # wait rather than on ``compact()``. Both classes carry it because
        # both are reachable: this one fronts the shared-runtime sessions,
        # and an answer given on only one of the two leaves the other
        # stranding its callers for the full timeout.
        return {"type": "completed", "summary": ""}
    deadline = time.monotonic() + timeout
    # ONE buffer for this call AND the nested grace drain, restored at ONE
    # point (the finally below) strictly BEFORE any re-poison. Separate
    # buffers restored at different times invert the order around a death
    # sentinel: the nested drain would re-queue ``None`` while this frame
    # buffer was still held, so a concurrent command's already-received
    # response would land BEHIND the poison and its consumer would see
    # process death despite a completed command.
    buffered: list[JsonRpcMessage] = []
    poisoned = False
    try:
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                msg = await asyncio.wait_for(self._queue.get(), timeout=min(remaining, 5.0))
            except asyncio.TimeoutError:
                continue
            if msg is None:
                # Re-poison (in the finally, AFTER the buffered frames are
                # restored) so the live turn / next consumer also sees death.
                poisoned = True
                raise self._died("Runtime died while waiting for compaction")
            # Check for compaction status
            if msg.method == "_kiro.dev/compaction/status":
                params = msg.params or {}
                status = params.get("status", {})
                s_type = status.get("type", "") if isinstance(status, dict) else str(status)
                if s_type in ("completed", "failed"):
                    if s_type == "completed":
                        # This drain path bypasses the prompt dispatch
                        # loop, so it must drop the stale counts itself —
                        # mirrors AcpClient._handle_compaction_status.
                        self.last_prompt_stats.reset_after_compaction()
                        poisoned = await self._drain_post_compaction_metadata(buffered=buffered)
                    # Redact backend-echoed summary before it reaches callers
                    # (compact() surfaces this to the dashboard).
                    summary = redact_text(str(params.get("summary", "") or ""))
                    if s_type == "failed" and not summary:
                        # kiro-cli leaves `summary` empty on failure, so the
                        # manual /compact notice would say nothing — carry
                        # the notification's own reason, read by the same
                        # extractor the dispatch loop uses for the streaming
                        # notice. Mirrors AcpClient.wait_for_compaction.
                        summary = compaction_failure_detail(params)
                    return {"type": s_type, "summary": summary}
                continue
            # Track metadata if it arrives (also consumes it).
            if msg.method == "_kiro.dev/metadata":
                self._track_metadata(msg)
                continue
            # Any other frame belongs to a concurrent live turn — buffer it
            # (do not drop) so its dispatch loop / usage meter still sees it.
            buffered.append(msg)
        return {"type": "timeout"}
    finally:
        for _m in buffered:
            self._queue.put_nowait(_m)
        if poisoned:
            self._queue.put_nowait(None)


async def _drain_post_compaction_metadata(
    self: AcpSessionHandle,
    grace: float = _POST_COMPACTION_METADATA_GRACE_SECS,
    buffered: list[JsonRpcMessage] | None = None,
) -> bool:
    """Drain the session queue for kiro's post-compaction metadata.

    kiro-cli emits a fresh ``_kiro.dev/metadata`` with the real
    post-compaction ``contextUsagePercentage`` about a second after the
    ``completed`` status (live-probe confirmed). The compaction reset
    cleared the authoritative flag, so applying it re-derives accurate
    counts against the kept served window. Returns on the first metadata
    frame carrying a real percentage — a credits-only/empty metadata frame
    is consumed but does not end the drain (the usage frame behind it
    would be stranded). Gives up quietly at the grace deadline.

    ``buffered``: when the caller (``wait_for_compaction``) passes its own
    frame buffer, non-metadata frames are appended to it and the CALLER
    restores everything at one point before any re-poison — two buffers
    restored at different times would invert the order around a death
    sentinel and strand the caller's frames behind the ``None``. Without
    a shared buffer this method restores (and re-poisons) itself.
    Returns True when the poison sentinel was consumed, so a sharing
    caller re-queues it after the single restore.
    """
    if getattr(self, "_kas_compact_turn", False):
        # The last turn was a KAS compaction request. KAS sends no
        # ``_kiro.dev/metadata``; its next context reading arrives with the
        # next turn, so waiting here would only spend the whole grace.
        return False
    own_buffer = buffered is None
    frames: list[JsonRpcMessage] = [] if buffered is None else buffered
    deadline = time.monotonic() + grace
    poisoned = False
    try:
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                msg = await asyncio.wait_for(self._queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            if msg is None:
                poisoned = True
                return True
            if msg.method == "_kiro.dev/metadata":
                # Late compaction credits are tracked here like any other
                # metadata: ``_track_metadata`` holds credits read between
                # turns for the next turn's bill, so none is lost to the
                # next prompt's stats re-init or to its pre-turn drain.
                self._track_metadata(msg)
                if (msg.params or {}).get("contextUsagePercentage") is not None:
                    return False
                continue
            frames.append(msg)
        return False
    finally:
        if own_buffer:
            for _m in frames:
                self._queue.put_nowait(_m)
            if poisoned:
                self._queue.put_nowait(None)


def _track_prompt_usage(self: AcpSessionHandle, result: Any) -> None:
    """Fold a PromptResponse's turn-scoped token counts into the stats.

    Mirrors ``AcpClient._track_prompt_usage``: the claude-agent-acp adapter
    reports per-turn token counts on the prompt response; kiro-cli's
    response carries only ``stopReason``, so ``parse_prompt_token_usage``
    returns None there and the stats are untouched (harness parity).
    """
    tokens = parse_prompt_token_usage(result)
    if tokens is not None:
        self.last_prompt_stats.apply_prompt_token_usage(*tokens)


def _track_metadata(self: AcpSessionHandle, msg: JsonRpcMessage) -> None:
    """Capture per-turn context usage + kiro billing credits from _kiro.dev/metadata.

    Mirrors AcpClient._track_metadata so sessions on the shared runtime get the
    same per-turn credit attribution. kiro bills in credits (token fields are 0
    for the acp provider), streamed as meteringUsage entries with unit="credit".
    Accumulated across the turn; reset per turn by the AcpPromptStats re-init in
    prompt().

    Credits read while no turn is running (a compaction waited on between
    turns) are held in ``_between_turn_credits`` instead: the stats window
    open then is replaced by the next turn's re-init before anything bills
    it, so ``_run_turn`` adds the held amount to that next turn, once.
    """
    params = msg.params or {}
    pct, credits = parse_metadata(params)
    # A real usage_update is authoritative for context_pct + token counts;
    # kiro's metadata percentage can measure a different window, so applying
    # it here would desync the headline % from the "used / total" token text.
    # sanitize_pct is the shared coercion (the AcpClient path uses it too),
    # so the two metadata paths cannot drift: it clamps NaN/±inf/out-of-range
    # and returns None for a missing or unparseable value.
    pct_f = self.last_prompt_stats.sanitize_pct(pct)
    if pct_f is not None and not self.last_prompt_stats.context_tokens_from_usage:
        self.last_prompt_stats.context_pct = pct_f
        self.last_prompt_stats.note_pct_reported()
        self._backfill_context_window(pct_f)
    turn_done = getattr(self, "_turn_done", None)
    if credits and turn_done is not None and turn_done.is_set():
        self._between_turn_credits = getattr(self, "_between_turn_credits", 0.0) + credits
    else:
        self.last_prompt_stats.credits += credits
        # Per-call context readings and the metered call count, shared with the
        # AcpClient tracker so both transports report the same turn. A
        # between-turns frame belongs to no turn's window, like its credits.
        self.last_prompt_stats.note_metadata_frame(pct_f, params.get("meteringUsage"))
    # Gated on membership, exactly as the AcpClient path gates the same read.
    # The shared runtime carries hosts outside ACP_BACKENDS_STRUCTURED_REFUSAL,
    # so an unconditional read here hands a parser written for one vocabulary
    # another host's notification -- and its answer would be attached to the turn
    # as a refusal category that host never sent. Folded onto the terminal by
    # ``terminal_refusal``; a frame without the envelope, and a host outside the
    # set, both leave it alone.
    if self._runtime.acp_backend in ACP_BACKENDS_STRUCTURED_REFUSAL:
        _refusal = parse_refusal(params)
        if _refusal is not None:
            self.last_prompt_stats.refusal = _refusal


def _backfill_context_window(self: AcpSessionHandle, pct: float) -> None:
    """Derive window/used tokens from a percentage-only reading.

    Thin wrapper binding this handle's resolved model id (kiro-agent
    ``currentModelId``, else the user-picked alias); the shared logic lives
    on ``AcpPromptStats.backfill_context_window`` (the AcpClient path
    delegates to the same method, so the two cannot drift).
    """
    self.last_prompt_stats.backfill_context_window(pct, self._resolved_model_id or self._model)


def _settle_codex_compaction(self: AcpSessionHandle, reason: str) -> AcpEvent | None:
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

    One arm for every *reason*, unlike the claude twin, and for the same
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


def _codex_compaction_event(self: AcpSessionHandle, update: dict[str, Any]) -> AcpEvent | None:
    """Reclassify a codex-acp context-compaction frame as an event, or None.

    The runtime-side twin of ``AcpClient._codex_compaction_event``, and the
    one that runs for a live codex session: codex is a member of
    ``ACP_BACKENDS_ACP_RUNTIME``, so its frames arrive here. Both
    implementations answer rather than one, because a capability the two
    transports disagree about is a capability that works on whichever one a
    reader did not test (harness-parity H6).

    It applies the same state mutation the compaction branch of ``_dispatch_events`` applies on
    a kiro-cli ``completed`` -- drop the stale context counts so the meter
    resets -- and returns the ``EVENT_COMPACTION_STATUS`` every consumer
    already handles. That is what lets ``compact()`` capture a terminal while
    draining its own prompt turn, so ``wait_for_compaction()`` answers from
    the cache instead of waiting for a notification codex never sends.

    Takes the already-extracted *update* rather than the message, because the
    caller has validated it is a mapping and belongs to THIS session -- a
    child-routed frame returns earlier, so a native subagent's compaction can
    never reset the parent's meter.

    Gated on ``ACP_BACKENDS_INLINE_COMPACTION`` rather than on codex's identity,
    which is the sanctioned spelling on this path and also the useful one: the
    set names the harnesses whose compaction lands INSIDE the prompt turn, and
    a frame like this is what landing inside the turn looks like. A harness
    that earns that membership inherits the translation by joining the set,
    with no edit here. claude is a member and is unaffected -- it reports
    compaction as prose, with no marker and outside
    ``ACP_BACKENDS_UNMARKED_COMPACTION_PAIR``, so the parser declines its
    frames -- which is the point: the FRAME decides, and the sets only bound
    who is asked.

    No ``failed`` arm exists because codex-acp sends no such status: a
    compaction that errors leaves the ``session/prompt`` request unanswered,
    which the turn deadline owns, so nothing here arms the post-failure budget
    on a guess.
    """
    if self._runtime.acp_backend not in ACP_BACKENDS_INLINE_COMPACTION:
        return None
    status_type = parse_codex_compaction_update(
        update,
        self._codex_compaction_pending,
        allow_unmarked=self._runtime.acp_backend in ACP_BACKENDS_UNMARKED_COMPACTION_PAIR,
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
    # string is what every consumer already renders for "compacted, no summary
    # offered".
    return AcpEvent(kind=EVENT_COMPACTION_STATUS, text=status_type, title="")
