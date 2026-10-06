"""How a turn's answer reaches Slack.

``_AnswerStream`` carries one turn's answer onto Slack: the stream message and its
rotation, the rolling credential redactor, the delivery debt, the task cards and their
elapsed-time timer, the text, reasoning and tool-call projections, the pause for an
approval prompt, and the final flush and seal. Beside it sit the OPTIONS and control-tag
holds that keep a trailer off the append-only stream until the stream can tell it is one,
and the bounded message edits the non-streaming fallback uses.

Composed onto :mod:`kiro_crew.slack.handler`; see
:mod:`kiro_crew.slack.handler_runtime`.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.slack.handler import (
        _COMMENT_HOLD_MAX,
        _CURSOR,
        _EDIT_INTERVAL,
        _NO_RESPONSE,
        _REVIEW_PLACEHOLDER_TS,
        _STATUS_WORKING,
        _STREAM_CONTINUED,
        _THINKING,
        _THINKING_PLACEHOLDER,
        ACTIVATION_REVIEW,
        DELIVERY_DEBT_NOTICE,
        SLACK_MSG_LIMIT,
        TRUNCATION_NOTICE,
        LLMEvent,
        SlackClientOps,
        StatusReactionController,
        StreamRedactor,
        _convert_tables,
        _tool_to_phase,
        is_control_tag_tail,
        is_wait_identity,
        logger,
        redact,
        redact_credentials,
        redact_exfiltration_urls,
        split_message,
        strip_control_comments,
        strip_thinking_tags,
    )


def _at_tag_line_start(stream_buffer: str) -> bool:
    """Whether the next character lands where a control-tag LINE may begin.

    The tag grammar is line-leading with at most three characters of indent
    (CommonMark: four is an indented code block). The current line is whatever
    follows the buffer's last newline; an EMPTY buffer is admitted too, because
    the buffer is cleared at every flush and the hold cannot see what was
    already appended. That over-approximates once per flush boundary -- a
    quoted tag whose ``<`` is the first byte after a flush is judged as if
    line-leading -- and costs at most a hold that the tail rule below releases
    the moment content follows it; an ordinary comment or prose is released
    either way.
    """
    line = stream_buffer[stream_buffer.rfind("\n") + 1 :]
    return len(line) <= 3 and line.strip(" \t") == ""


def _comment_hold_is_protocol(hold: str) -> bool:
    """Whether the held span, from its line-leading ``<``, is so far NOTHING
    BUT a control-tag tail: complete recognized tag lines (stacked, with their
    bounded trailing whitespace) and at most one still-arriving tag prefix.

    Decided by the ONE backend grammar (``constants.is_control_tag_tail``,
    the anchored form of the streaming strip). Any other byte -- a diverging
    opener (``<div``), an ordinary comment body (``<!-- ordin``), a line break
    inside a tag, or CONTENT after a complete tag -- makes the span text, and
    the caller releases it verbatim.
    """
    return len(hold) <= _COMMENT_HOLD_MAX and is_control_tag_tail(hold)


def _filter_options_brackets(text: str, bracket_hold: str, stream_buffer: str) -> tuple[str, str]:
    """Filter ``[OPTIONS: ...]`` tags and control-tag comments from streaming
    text character-by-character.

    Returns the updated *(bracket_hold, stream_buffer)* tuple. The hold is one
    string and its first character says what it holds: ``[`` opens the OPTIONS
    bracket-hold, a line-leading ``<`` opens the comment hold.

    Slack streams by APPENDING and appended text is final (``chat.stopStream``
    does not replace it), so a control tag can only be kept off the stream by
    holding the bytes that might be one until the stream can tell. The comment
    hold is the bracket-hold's twin for ``<!-- keep-visible -->`` and its
    siblings, with one difference that matters: a recognized tag is NOT
    dropped when its ``-->`` arrives. Control tags are TAIL-anchored -- the
    same tag quoted mid-message (a fenced example, a line of prose after it)
    is visible content -- and an append-only stream learns which one it has
    only from what follows. So a complete tag stays held while it is still a
    possible tail (``_comment_hold_is_protocol``), is released verbatim the
    moment any content follows it, and is settled at the end of the turn by
    ``_resolve_comment_hold`` against the whole reply. Only what the tail
    grammar recognizes can ever be withheld: every other comment, a hold that
    diverges from ``<!--``, or one that spans a line break is released as soon
    as the diverging byte arrives -- and that byte is then processed on its
    own, so a ``[`` that ends a hold still opens the bracket-hold.
    """
    for ch in text:
        if bracket_hold and bracket_hold[0] == "<":
            if _comment_hold_is_protocol(bracket_hold + ch):
                bracket_hold += ch
                continue
            # The held span is content. It goes out as written, and the byte
            # that proved it falls through to be judged on its own.
            stream_buffer += bracket_hold
            bracket_hold = ""
        if bracket_hold:
            bracket_hold += ch
            if ch == "]":
                if bracket_hold.startswith("[OPTIONS:"):
                    bracket_hold = ""
                else:
                    stream_buffer += bracket_hold
                    bracket_hold = ""
        elif ch == "[":
            bracket_hold = ch
        elif ch == "<" and _at_tag_line_start(stream_buffer):
            bracket_hold = ch
        else:
            stream_buffer += ch
    return bracket_hold, stream_buffer


def _resolve_comment_hold(bracket_hold: str, accumulated: str) -> tuple[str, str]:
    """Settle a comment hold when the stream ENDS; returns *(hold, release)*.

    The stream is over, so the held span is the reply's tail, and the tail
    grammar can now be asked directly on the whole reply -- fence parity and
    all: when ``strip_control_comments`` removes something from *accumulated*,
    the held tail IS the control tag and is dropped; when it removes nothing
    (an unterminated fence swallows the tail, a tag prefix that never
    completed, a body over the bound) the span is content and is released for
    one last append. A ``[`` hold is not this function's: it keeps the
    bracket-hold's own end-of-turn outcome.
    """
    if not bracket_hold or bracket_hold[0] != "<":
        return bracket_hold, ""
    if strip_control_comments(accumulated) != accumulated:
        return "", ""
    return "", bracket_hold


async def _safe_update(slack: SlackClientOps, channel: str, ts: str, text: str) -> None:
    """Update a Slack message, truncating if too long.

    Used for progressive streaming edits — truncation is fine here since
    the final message uses _safe_final_update which splits instead.
    """
    text, _ = redact_exfiltration_urls(text)
    if len(text) > SLACK_MSG_LIMIT:
        text = text[:SLACK_MSG_LIMIT] + TRUNCATION_NOTICE
    try:
        await slack.update_message(channel, ts, text)
    except Exception:
        logger.debug("Failed to update message %s", ts, exc_info=True)


async def _safe_final_update(
    slack: SlackClientOps,
    channel: str,
    ts: str,
    text: str,
    thread_ts: str | None = None,
    *,
    raise_on_primary_failure: bool = False,
) -> None:
    """Final message update — splits into multiple messages if too long.

    ``raise_on_primary_failure`` controls the FIRST (answer-carrying) part only.
    Left False (the default) the primary send is best-effort — the caller uses
    this when the answer is already on screen and this call is a redaction
    overwrite, so a failed overwrite must not fail an already-delivered turn. Set
    True on the no-stream path where this call is the ONLY delivery of the answer:
    a failed primary send then propagates so the caller can book a failure rather
    than a success for a reader who received nothing. Overflow continuations are
    best-effort regardless (a dropped tail is a truncated answer, not a missing
    one), matching the streaming path's overflow handling.
    """
    text, _ = redact_exfiltration_urls(text)
    parts = split_message(text)
    # First part updates the existing streaming message
    try:
        await slack.update_message(channel, ts, parts[0])
    except Exception:
        logger.debug("Failed to update message %s", ts, exc_info=True)
        if raise_on_primary_failure:
            raise
    # Overflow parts posted as follow-up messages in the same thread
    for part in parts[1:]:
        try:
            await slack.post_message(channel, part, thread_ts)
        except Exception:
            logger.debug("Failed to post continuation message", exc_info=True)


class _AnswerStream:
    """The Slack wire one native turn's answer is written to.

    ``handle_message`` builds one per turn, before its prompt opens. It holds every
    piece of per-turn state the Slack projection of the answer reads and writes: the
    stream message (``stream_ts``) and whether streaming is live, the reasoning
    placeholder posted above it, the answer and reasoning text accumulated so far, the
    unsent buffer and the OPTIONS / control-tag hold, the rolling credential redactor,
    the redaction and delivery-debt flags the end of the turn reads, and the task card
    of the tool that is running. The turn loop hands each event to the matching
    ``on_*`` method; the verdict, the approval ladder and the persistence stay in
    ``handle_message``, which reads this object's flags.
    """

    def __init__(
        self,
        slack: SlackClientOps,
        channel: str,
        reply_ts: str,
        *,
        team_id: str,
        user_id: str,
        channel_activation: str | None,
        show_thinking: bool,
        status: StatusReactionController,
    ) -> None:
        self.slack = slack
        self.channel = channel
        self.reply_ts = reply_ts
        self.team_id = team_id
        self.user_id = user_id
        self.channel_activation = channel_activation
        self.status = status
        self.use_slack_stream = False
        self.stream_ts: str | None = None
        self.thinking_ts: str | None = None  # 💭 reasoning placeholder, posted above the answer
        self.show_thinking = show_thinking
        self.had_redaction = False  # True when per-chunk redaction modified a streamed chunk
        self.delivered = False  # True once ANY real-text append is confirmed on the stream
        # Delivery debt: real answer text Slack refused for good — the append failed
        # AND its post-rotation retry failed, so those characters are on no message.
        #
        # Never cleared, including at a wait boundary. That boundary discards
        # ``accumulated`` and abandons the message, so text lost before it can no
        # longer be restated from anything the turn still holds — which is exactly why
        # the debt has to outlive it and be disclosed at the end.
        #
        # Only the streaming finalize reads it. A refused append always attempts a
        # rotation, so a for-good loss leaves the turn in one of two states: the
        # rotation succeeded and the answer now spans two messages, where the loss is
        # disclosed because restating the whole text in the message the reader is
        # watching would repeat the abandoned one; or the rotation failed and the
        # stream was demoted, where the end-of-turn ``chat.update`` already re-sends
        # that segment's complete text and there is nothing left to disclose.
        self.debt = False
        # Rolling-buffer redactor for the live Slack wire: withholds the trailing
        # credential-class run so a credential split across streaming chunks can't
        # reach Slack unredacted (issue 3). The final message is posted from the
        # complete, fully-redacted `accumulated`, so the held tail is superseded at
        # stop_stream — no data loss.
        self.redactor = StreamRedactor()
        self.accumulated = ""
        self.thinking_accumulated = ""
        self.stream_buffer = (
            ""  # unsent chunks for streaming API (buffered between rate-limited appends)
        )
        self.bracket_hold = ""  # text held back from '[' until ']' to filter [OPTIONS: ...]
        self.last_edit = 0.0
        self.task_counter = 0  # incrementing task ID for task cards
        self.active_task_id = ""  # current in-progress task
        self.active_task_title = ""  # display title (purpose or tool name)
        self.tool_start_time = 0.0  # monotonic time when current tool started
        self.tool_timer_task: asyncio.Task | None = None  # periodic elapsed-time updater
        self.status_dirty = False  # True when status needs reset to base on next text chunk
        self.tool_gap = False

    async def rotate(self) -> str | None:
        """Stop the dead stream and start a fresh one. Returns new ts or None.

        Best-effort: MUST NOT raise. The
        real ``SlackClient`` swallows its own API errors, but a client or
        transport that does not would send the exception up into the streaming
        loop, where the typed ``except`` arms are all ``kiro_crew.acp.client``
        errors — it reaches the generic ``except Exception`` catch-all, renders
        the terminal "🔧 Something went wrong" message, and records a session
        failure on a turn that is still live. A failed rotation is the existing,
        handled outcome (``new_ts`` None → demote to chat.update), so map a
        raise onto it.
        """
        if self.stream_ts:
            try:
                await self.slack.stop_stream(self.channel, self.stream_ts)
            except Exception:
                logger.warning(
                    "Slack stop_stream failed during rotation — abandoning old stream",
                    exc_info=True,
                )
        try:
            new_ts = await self.slack.start_stream(
                self.channel,
                self.reply_ts,
                initial_text=_STREAM_CONTINUED,
                team_id=self.team_id or None,
                user_id=self.user_id or None,
            )
        except Exception:
            logger.warning("Slack start_stream failed during rotation", exc_info=True)
            new_ts = None
        if new_ts:
            self.stream_ts = new_ts
            logger.info("Stream rotated: new ts=%s", new_ts)
        else:
            self.use_slack_stream = False
            logger.warning("Stream rotation failed — falling back to chat.update")
        return new_ts

    async def append(self, text: str) -> bool:
        """Append text to stream, rotating on failure.

        Streams through the rolling redactor (``redactor``) so a credential split
        across streaming chunks can't reach Slack unredacted (issue 3): only the
        confirmed-safe prefix is sent now; the trailing (possible-partial-
        credential) run is withheld until the next append. The final message is
        posted from the complete, fully-redacted ``accumulated`` at stop_stream,
        so the withheld tail is superseded — never lost.
        """
        if not self.stream_ts:
            return True
        if self.channel_activation == ACTIVATION_REVIEW:
            return True  # Suppress streaming text in review mode
        safe = self.redactor.feed(text)  # redacts the confirmed-safe prefix internally
        if not safe:
            return True  # whole delta withheld (partial credential) — nothing to send yet
        if "[REDACTED" in safe:
            self.had_redaction = True
        # Best-effort: MUST NOT raise. A raising append is the same event as a refused
        # append — the text is not on the stream — and the refusal path below (rotate,
        # then retry once) already handles it. Letting it raise would escape into the
        # turn loop's generic catch-all in ``handle_message`` and fake a terminal error
        # on a live turn.
        try:
            ok = await self.slack.append_stream(self.channel, self.stream_ts, safe)
        except Exception:
            logger.warning("Slack append_stream failed — attempting rotation", exc_info=True)
            ok = False
        if not ok and self.use_slack_stream:
            if await self.rotate():
                assert self.stream_ts is not None
                try:
                    ok = await self.slack.append_stream(self.channel, self.stream_ts, safe)
                except Exception:
                    logger.warning("Slack append_stream failed after rotation", exc_info=True)
                    ok = False
        # A delta that failed both the append and the post-rotation retry is on no
        # message. Record the debt rather than dropping it silently, so finalize
        # can tell the reader. The early returns above are not deliveries and never
        # reach here, so a withheld partial-credential run and a review-mode
        # suppression do not count as lost text.
        #
        # Record whether this real-text delivery was CONFIRMED. The finalize path
        # reads it to tell a used stream (answer reached, refused remainder is
        # delivery debt) from a stream that delivered NOTHING (every append
        # refused -- the reader got no answer, which is a failed turn).
        if ok:
            self.delivered = True
        else:
            self.debt = True
        return ok

    async def settle_debt(self, ts: str) -> None:
        """Disclose answer text Slack refused for good, on the message that lost it.

        Called at every point a stream is abandoned, so the notice goes out while
        an append can still reach the message the hole is in. Clearing the debt is
        part of settling it: a second notice on a later message would report a gap
        the reader has already been shown.

        Sent directly rather than through ``append``: the notice is not
        answer text, so it must not raise ``delivered`` and make a stream
        that delivered no answer look like one that did.

        ``append_stream`` reports a refusal by RETURNING False -- the client turns
        every exception into that return -- so the return value is the whole
        signal, and leaving it unread hides the very loss this notice exists to
        disclose. The two refusals that takes fall inside one Slack rate-limit or
        outage window, so they are correlated rather than independent. On refusal
        post a separate message, which does not depend on the stream that just
        refused.
        """
        if not self.debt:
            return
        self.debt = False
        _notice_ok = False
        try:
            _notice_ok = await self.slack.append_stream(self.channel, ts, DELIVERY_DEBT_NOTICE)
        except Exception:
            logger.warning("Slack: appending the delivery-debt notice failed")
        if not _notice_ok:
            try:
                await self.slack.post_message(self.channel, DELIVERY_DEBT_NOTICE, self.reply_ts)
            except Exception:
                logger.warning(
                    "Slack: the delivery-debt notice reached neither the "
                    "stream nor a separate message"
                )

    async def append_task(self, task_id: str, title: str, status: str, details: str = "") -> bool:
        """Append task card to stream. Never rotates — see below.

        A task card is progress decoration: the tool's name, its state, and the
        elapsed-time refresh ``_tool_elapsed_updater`` fires every 30s for as
        long as a tool runs. During a several-minute tool phase it is the ONLY
        thing appending to the stream, which makes it by far the likeliest call
        to meet a rate limit or a stream Slack has already closed.

        Rotating on that failure costs the reader their in-progress message and
        moves the rest of the answer into a new one, so a transient refusal on a
        decorative refresh renders as a failed reply plus a second reply minutes
        later. Skipping the card costs nothing: no answer text is withheld, and
        ``append`` still rotates when there is real text to deliver and
        the stream refuses it, which is the moment a rotation is worth its price.
        """
        if not self.stream_ts:
            return False
        if self.channel_activation == ACTIVATION_REVIEW:
            return True  # Suppress task cards in review mode
        # Best-effort, and it MUST NOT raise. ``SlackClient.append_task`` swallows
        # its own API errors and returns False, but a client that does not (or a
        # transport that raises before that guard) would send the exception up
        # into the streaming loop, where nothing catches a non-ACP error: the
        # typed ``except`` arms of the turn loop in ``handle_message`` are all
        # ``kiro_crew.acp.client`` errors, so it reaches the generic
        # ``except Exception`` catch-all, which
        # renders the terminal "🔧 Something went wrong" message and records a
        # session failure — on a turn that is still live and will finish. The
        # card is decorative (no answer text is withheld), so swallow the failure
        # here, logging the traceback at WARNING for diagnosis.
        try:
            return await self.slack.append_task(
                self.channel, self.stream_ts, task_id, title, status, details=details
            )
        except Exception:
            logger.warning("Slack append_task failed — skipping progress card", exc_info=True)
            return False

    async def _tool_elapsed_updater(self) -> None:
        """Periodically update the active task card with elapsed time (every 30s)."""
        while True:
            await asyncio.sleep(30)
            if self.active_task_id and self.tool_start_time and self.use_slack_stream:
                elapsed = time.monotonic() - self.tool_start_time
                mins, secs = divmod(int(elapsed), 60)
                time_str = f"{mins}m {secs}s" if mins else f"{secs}s"
                # Elapsed goes in the TITLE (Slack replaces title on same
                # task_id) — NOT details, which Slack APPENDS, causing the
                # "⏱ 30s ⏱ 1m 0s ⏱ 1m 30s" accumulation bug.
                await self.append_task(
                    self.active_task_id,
                    f"{self.active_task_title}  ⏱ {time_str}",
                    "in_progress",
                )

    def start_tool_timer(self) -> None:
        """Start the 30s elapsed-time updater for the current tool."""
        self.cancel_tool_timer()
        self.tool_start_time = time.monotonic()
        self.tool_timer_task = asyncio.ensure_future(self._tool_elapsed_updater())

    def cancel_tool_timer(self) -> None:
        """Cancel the tool elapsed-time updater."""
        if self.tool_timer_task and not self.tool_timer_task.done():
            self.tool_timer_task.cancel()
        self.tool_timer_task = None

    def tool_elapsed_str(self) -> str:
        """Return formatted elapsed time for the current tool, or empty string."""
        if not self.tool_start_time:
            return ""
        elapsed = time.monotonic() - self.tool_start_time
        if elapsed < 1:
            return ""
        mins, secs = divmod(elapsed, 60)
        if mins:
            return f"⏱ {int(mins)}m {secs:.1f}s"
        return f"⏱ {secs:.1f}s"

    async def ensure_started(self) -> None:
        """Lazy-start the stream on first event. Falls back to chat.update."""
        if self.stream_ts is not None:
            return
        if self.channel_activation == ACTIVATION_REVIEW:
            # No visible message — only thread status indicator is shown
            self.stream_ts = _REVIEW_PLACEHOLDER_TS
            self.use_slack_stream = False
            return
        # Reserve the 💭 reasoning slot ABOVE the answer *before* the response
        # message is created. This must run regardless of which
        # event arrived first: if a text/tool event precedes the first
        # reasoning chunk, posting the placeholder here is the only way to keep
        # reasoning above the answer (the reasoning-chunk branch never got the
        # chance). Guarded on thinking_ts is None so we never double-post when
        # the reasoning branch already claimed the slot. An empty placeholder
        # (no reasoning this turn) is cleaned up at end of turn.
        if self.show_thinking and self.thinking_ts is None:
            try:
                self.thinking_ts = await self.slack.post_message(
                    self.channel, _THINKING_PLACEHOLDER, self.reply_ts
                )
            except Exception:
                logger.debug("Failed to reserve thinking slot", exc_info=True)
        # Best-effort: MUST NOT raise. The real ``SlackClient.start_stream``
        # swallows its own errors and returns None, but a client or transport
        # that raises instead would escape into the loop's generic catch-all
        # from the first TEXT_CHUNK or TOOL_CALL event. A raise is the same
        # event as a None return — streaming is unavailable — so map it onto
        # the existing demotion path below.
        try:
            self.stream_ts = await self.slack.start_stream(
                self.channel,
                self.reply_ts,
                team_id=self.team_id or None,
                user_id=self.user_id or None,
            )
        except Exception:
            logger.warning("Slack start_stream failed — demoting to chat.update", exc_info=True)
            self.stream_ts = None
        self.use_slack_stream = self.stream_ts is not None
        if not self.use_slack_stream:
            # ``SlackClient.start_stream`` swallows its own errors and returns
            # None, but the ``chat.update`` fallback below goes through the base
            # ``post_message``, which raises (``resp["ts"]``) on a Slack refusal.
            # A raise here escapes the streaming loop while the ACP turn is still
            # live and reaches the turn loop's generic ``except Exception`` catch-all in
            # ``handle_message`` (the typed arms are all ``kiro_crew.acp.client`` errors),
            # rendering the terminal "🔧 Something went wrong" message on a run
            # that is still succeeding. Keep it best-effort. On failure leave
            # ``stream_ts`` as ``None``: there is no placeholder to update, and
            # every downstream reader treats falsy as "no placeholder"
            # (``append`` returns early; the end-of-turn delivery takes
            # its ``else`` branch and posts the final answer with a fresh
            # ``post_message`` rather than editing a ts that does not exist). Do
            # NOT substitute a truthy sentinel here — that routes end of turn into
            # the placeholder-edit branch against a non-existent message and loses
            # a single-part reply silently.
            try:
                self.stream_ts = await self.slack.post_message(
                    self.channel, _THINKING, self.reply_ts
                )
            except Exception:
                logger.warning("Failed to post chat.update placeholder", exc_info=True)
                self.stream_ts = None

    async def flush(self) -> None:
        """Send the buffered text, minus inline ``<thinking>`` tags, and empty the buffer."""
        if self.stream_buffer:
            self.stream_buffer, _ = strip_thinking_tags(self.stream_buffer, strip_whitespace=False)
            await self.append(self.stream_buffer)
            self.stream_buffer = ""

    async def on_text(self, event: LLMEvent) -> None:
        """Project one answer chunk: redact it, hold back what may be a trailer, and edit
        the message at most once per ``_EDIT_INTERVAL``."""
        if self.tool_gap and self.accumulated and self.accumulated[-1:] not in ("\n", " "):
            first = event.text[:1]
            if first and first not in ("\n", " "):
                event.text = "\n\n" + event.text
        event.text, _exfil_w = redact_exfiltration_urls(event.text)
        event.text, _cred_w = redact_credentials(event.text)
        if _exfil_w or _cred_w:
            self.had_redaction = True

        if event.text:
            self.tool_gap = False
        self.status.set_phase("thinking")
        self.status.on_progress()
        self.accumulated += event.text

        if self.status_dirty and self.use_slack_stream:
            # Best-effort: MUST NOT raise. The thread
            # status is decoration, and a raise here escapes into the
            # turn loop's generic ``except Exception`` catch-all in ``handle_message``,
            # faking a terminal error on a live turn.
            try:
                await self.slack.set_thread_status(self.channel, self.reply_ts, _STATUS_WORKING)
            except Exception:
                logger.warning(
                    "Slack set_thread_status failed — skipping status refresh",
                    exc_info=True,
                )
            self.status_dirty = False

        # ── Bracket hold-back: filter [OPTIONS: ...] from stream ──
        # When inside a bracket, accumulate into bracket_hold.
        # On ']', release if not OPTIONS, suppress if it is.
        if self.use_slack_stream:
            self.bracket_hold, self.stream_buffer = _filter_options_brackets(
                event.text, self.bracket_hold, self.stream_buffer
            )
        else:
            self.stream_buffer += event.text

        await self.ensure_started()

        now = time.monotonic()
        if now - self.last_edit >= _EDIT_INTERVAL:
            if self.use_slack_stream:
                await self.flush()
            else:
                # ``stream_ts`` may be None: the chat.update fallback in
                # ``ensure_started`` failed, so there is no
                # placeholder to edit. Skip the cursor edit — the final
                # answer is posted at end of turn from ``accumulated``.
                if self.stream_ts and self.channel_activation != ACTIVATION_REVIEW:
                    await _safe_update(
                        self.slack, self.channel, self.stream_ts, redact(self.accumulated) + _CURSOR
                    )
            self.last_edit = now

    async def on_thinking(self, event: LLMEvent) -> None:
        """Accumulate a reasoning chunk and claim the 💭 slot above the answer."""
        self.status.set_phase("thinking")
        self.status.on_progress()
        self.thinking_accumulated += event.text
        # Claim the 💭 slot as soon as reasoning starts so it appears
        # promptly during a long thinking phase (early feedback). This
        # is an optimization for the common reasoning-first case; the
        # ordering guarantee itself lives in ensure_started,
        # which reserves the slot before the answer message whenever it
        # hasn't been claimed yet (handles text/tool-first turns).
        if (
            self.show_thinking
            and self.thinking_ts is None
            and self.stream_ts is None
            and self.channel_activation != ACTIVATION_REVIEW
        ):
            try:
                self.thinking_ts = await self.slack.post_message(
                    self.channel, _THINKING_PLACEHOLDER, self.reply_ts
                )
            except Exception:
                logger.debug("Failed to post thinking placeholder", exc_info=True)

    async def on_tool_call(self, event: LLMEvent) -> None:
        """Project a tool call the provider is already running: show its task card (or
        an inline line without streaming), and seal the message before a ``wait``."""
        tool_name = event.title.removeprefix("Running: ")
        tool_name, _ = redact_exfiltration_urls(tool_name)
        tool_name, _ = redact_credentials(tool_name)
        tool_kind = event.tool_kind or ""
        self.status.set_phase(_tool_to_phase(tool_name, tool_kind))
        self.status.on_progress()
        tool_detail = event.tool_purpose or tool_kind
        tool_status = f"\n🫆 `{tool_name}`\n"
        await self.ensure_started()
        if self.use_slack_stream:
            # Best-effort: MUST NOT raise. Decoration
            # only — a raise escapes to the catch-all and fakes a
            # terminal error on a live turn.
            try:
                await self.slack.set_thread_status(
                    self.channel, self.reply_ts, f"is using {tool_name}"
                )
            except Exception:
                logger.warning(
                    "Slack set_thread_status failed — skipping tool status",
                    exc_info=True,
                )
            self.status_dirty = True
        if self.use_slack_stream:
            # Flush any buffered text before the tool status
            await self.flush()
            # Mark previous task complete, start new one
            if self.active_task_id:
                _elapsed = self.tool_elapsed_str()
                self.cancel_tool_timer()
                _ct = (
                    f"{self.active_task_title}  {_elapsed}" if _elapsed else self.active_task_title
                )
                await self.append_task(self.active_task_id, _ct, "complete")
            self.task_counter += 1
            self.active_task_id = f"tool_{self.task_counter}"
            self.active_task_title = event.tool_purpose or tool_name
            self.active_task_title, _ = redact_exfiltration_urls(self.active_task_title)
            self.active_task_title, _ = redact_credentials(self.active_task_title)
            await self.append_task(
                self.active_task_id,
                title=self.active_task_title,
                status="in_progress",
                details=tool_name if tool_detail else "",
            )
            self.start_tool_timer()
        else:
            self.accumulated += tool_status
            # ``stream_ts`` may be None here: ``ensure_started``
            # demoted (``use_slack_stream`` False) AND its chat.update
            # fallback post failed, so there is no placeholder to edit.
            # Skip the cursor edit — the final answer is posted by the
            # end-of-turn ``else`` branch with a fresh ``post_message``.
            if self.stream_ts and self.channel_activation != ACTIVATION_REVIEW:
                await _safe_update(
                    self.slack, self.channel, self.stream_ts, redact(self.accumulated) + _CURSOR
                )
        self.last_edit = time.monotonic()

        # wait tool blocks MCP for up to 30min — finalize the
        # streaming message now so Slack doesn't show an error.
        # ensure_started() will open a new message when
        # the next text chunk arrives after wait returns.
        # Keyed on the tool's programmatic identity when the transport
        # sent one (same rule as SlackRenderer); the title compare is the
        # fallback for a frame without ``_meta.kiro``.
        _is_wait = is_wait_identity(event.tool_name) if event.tool_name else tool_name == "wait"
        if _is_wait and self.use_slack_stream and self.stream_ts:
            if self.active_task_id:
                _elapsed = self.tool_elapsed_str()
                self.cancel_tool_timer()
                _ct = (
                    f"{self.active_task_title}  {_elapsed}" if _elapsed else self.active_task_title
                )
                await self.append_task(self.active_task_id, _ct, "complete")
                self.active_task_id = ""
            # Best-effort: MUST NOT raise. The stream is being
            # abandoned either way (``stream_ts`` is cleared just
            # below, and ``ensure_started`` opens a fresh
            # message after wait returns), so a raising ``stop_stream``
            # changes nothing except — unguarded — faking a terminal
            # error on a live turn via the catch-all.
            # This message ends here: a held comment is its tail, so
            # settle it against the source before that is discarded.
            self.bracket_hold, _released = _resolve_comment_hold(
                self.bracket_hold, self.accumulated
            )
            if _released:
                await self.append(_released)
            # Last chance to tell the reader: the seal below drops
            # ``stream_ts`` and ``accumulated``, so a turn that ends with
            # no post-wait text opens no further stream and reaches no
            # other disclosure point, while the lost characters are gone
            # from the text a later message could restate. Settling here
            # also puts the notice on the message the gap is in. Ordered
            # after the tail append so a refusal of that tail counts.
            await self.settle_debt(self.stream_ts)
            try:
                await self.slack.stop_stream(self.channel, self.stream_ts)
            except Exception:
                logger.warning(
                    "Slack stop_stream failed at wait finalize — abandoning stream",
                    exc_info=True,
                )
            self.stream_ts = None
            self.accumulated = ""

    async def prepare_for_approval(self) -> None:
        """Open the stream if it is not, show the approval status, and flush the buffer,
        so the text before a permission prompt is on screen above it."""
        await self.ensure_started()
        if self.use_slack_stream:
            await self.slack.set_thread_status(self.channel, self.reply_ts, "Waiting for approval…")
            self.status_dirty = True
            # Flush buffered text before approval pause
            await self.flush()

    async def on_tool_rejected(self) -> None:
        """Close the running tool's card as failed, or note the rejection inline."""
        if self.use_slack_stream and self.active_task_id:
            self.cancel_tool_timer()
            assert self.stream_ts is not None
            await self.append_task(self.active_task_id, self.active_task_title, "error")
            self.active_task_id = ""
        if not self.use_slack_stream:
            self.accumulated += "\n🚫 _Tool use rejected._"

    async def finish(self, untrimmed: str) -> None:
        """Complete the last task card and send what the stream still holds.

        A ``[`` hold is excluded — it's either a suppressed OPTIONS tag or an unclosed
        bracket we drop; a comment hold is settled against the whole reply and
        released when it is content.
        """
        # Mark last task complete
        if self.active_task_id:
            _elapsed = self.tool_elapsed_str()
            self.cancel_tool_timer()
            _ct = f"{self.active_task_title}  {_elapsed}" if _elapsed else self.active_task_title
            await self.append_task(self.active_task_id, _ct, "complete")
        self.bracket_hold, _released = _resolve_comment_hold(self.bracket_hold, untrimmed)
        self.stream_buffer += _released
        if self.stream_buffer:
            self.stream_buffer, _ = strip_thinking_tags(self.stream_buffer, strip_whitespace=False)
            await self.append(self.stream_buffer)

    async def seal(self, clean_text: str, *, redacted: bool) -> None:
        """Seal the stream with the final text, then overwrite it when redaction ran.

        Disclose real answer text Slack refused for good, while the
        stream is still open (an append after the seal is refused).

        Reaching here with debt means a rotation succeeded, because a
        refused append always attempts one and a failed rotation demotes
        the stream out of this branch. So the answer spans the abandoned
        message and this one: restating the whole text here would repeat
        what the reader already has above, and the characters lost before
        a wait boundary are absent from ``clean_text`` to restate at all.
        Saying so is what a reader can act on -- they can ask again --
        where a complete-looking answer with a hole in it gives them
        nothing to notice.
        """
        assert self.stream_ts is not None
        if self.debt:
            await self.settle_debt(self.stream_ts)
        # The seal is decoration: the answer is already on screen, so a
        # failed stop_stream does not un-deliver it.
        try:
            await self.slack.stop_stream(self.channel, self.stream_ts, clean_text or _NO_RESPONSE)
        except Exception:
            logger.warning("Slack stop_stream failed at finalize", exc_info=True)
        # Redaction overwrite is decoration on an already-delivered stream:
        # it corrects the visible copy, it does not deliver the answer.
        if self.had_redaction or redacted:
            fallback_text = _convert_tables(clean_text) if clean_text else _NO_RESPONSE
            await _safe_final_update(
                self.slack,
                self.channel,
                self.stream_ts,
                fallback_text or _NO_RESPONSE,
                self.reply_ts,
            )
