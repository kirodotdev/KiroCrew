"""How a native Slack turn delivers its answer and books its verdict.

``_deliver_answer`` runs once ``_run_turn`` has released or kept the session permit:
the answer's render and redaction passes, the reply decorator, the review-mode draft,
the answer-carrying sends and the one verdict they decide, then what the finished turn
posts after its answer (the reasoning, the redaction notice, the timing footer and its
OPTIONS control, the voice reply, the dashboard mirror and the auto-title task) with the
transcript row written before anything that invites an answer to it.

Reads off the ``_SlackTurn``: the inputs, the answer stream and what the turn body
left (the client, the stop reason, the completion and replay flags, the Working block,
the linked session); books through the turn's ``book_success`` / ``book_failure`` and
releases through its ``release_permit``.

Composed onto :mod:`kiro_crew.slack.handler`; see
:mod:`kiro_crew.slack.handler_runtime`.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from kiro_crew.slack.handler import (
        _NO_RESPONSE,
        ACTIVATION_REVIEW,
        STOP_REASON_CANCELLED,
        DashboardState,
        PostedOptions,
        _append_footer_actions,
        _condense_thinking,
        _convert_tables,
        _dashboard_state,
        _display_redactor,
        _is_slack_restricted,
        _maybe_auto_title_slack,
        _mirror_to_dashboard,
        _post_review_draft,
        _reply_by_voice,
        _safe_final_update,
        _SlackTurn,
        auto_title,
        build_timing_footer,
        count_redaction_tags,
        current_context,
        expire_slack_options,
        extract_options,
        get_dashboard_state,
        logger,
        mint_options_token,
        options_control_is_stale,
        redact_credentials,
        redact_exfiltration_urls,
        redact_for_display,
        redaction_notice,
        remember_slack_options,
        render_one_for_slack,
        save_conversation_turn_off_loop,
        split_message,
        strip_control_comments,
        strip_thinking_tags,
        track_background_task,
    )


async def _deliver_answer(turn: _SlackTurn) -> None:
    """Post the answer, book the turn's one verdict, then post what follows it.

    The rest of ``handle_message`` once ``_run_turn`` has released or kept the session
    permit: the render and redaction passes over the answer, the reply decorator, the
    review-mode draft, the answer-carrying sends and the verdict they decide, then the
    reasoning post, the redaction notice, the turn's persisted row, the timing footer and
    its OPTIONS control (whose delivery books a deferred verdict), the voice reply,
    consolidation, the dashboard mirror and the auto-title task. ``turn.release_permit``
    and the two ``finally`` blocks release the permit on every exit.
    """
    slack = turn.slack
    sessions = turn.sessions
    channel = turn.channel
    text = turn.text
    thread_ts = turn.thread_ts
    msg_ts = turn.msg_ts
    user_id = turn.user_id
    conversation_log = turn.conversation_log
    consolidator = turn.consolidator
    from_trusted_bot = turn.from_trusted_bot
    channel_activation = turn.channel_activation
    had_voice_input = turn.had_voice_input
    _t0 = turn._t0
    reply_ts = turn.reply_ts
    session_key = turn.session_key
    _agent = turn._agent
    _replayed = turn._replayed
    _stop_reason = turn._stop_reason
    _turn_completed_ok = turn._turn_completed_ok
    _working_ts = turn._working_ts
    answer = turn.answer
    client = turn.client
    linked_session_key = turn.routed_link_key

    if _replayed:
        # The nested call posted the reply, persisted the turn, booked its own
        # verdict and cleared the thread status; this abandoned attempt has
        # nothing of its own to show and holds no permit (the reset popped the
        # session whose permit it held), so nothing is released here.
        return

    # Structural release guarantee: delivery and accounting below can raise
    # on ANY step (a Slack send such as post_ephemeral, a conversation-log
    # write, or a CancelledError from stop/shutdown landing after the verdict
    # is decided). The permit is held across this whole region, so the release
    # must be in a finally rather than at hand-listed exit points — an
    # unlisted raise would otherwise strand the per-session semaphore with no
    # timeout and no other releaser, wedging every later turn on the folded
    # key. turn.release_permit() is idempotent, so the explicit releases inside
    # (before the post-answer decorations) remain correct and this finally is
    # a no-op once they have run.
    try:
        # ALL verdict state and the verdict helpers are bound BEFORE the first
        # suspension point below. Both ``finally`` blocks (the release finally
        # here and the decorations-tail finally) read ``_options_verdict_deferred``
        # and ``_verdict_booked`` and call these helpers, so a cancellation
        # delivered at the very first ``await`` must find them already bound. A
        # cleanup block may only read state that was bound before the first point
        # control can leave the body -- a finally that reads a local bound after a
        # yield is a landmine.
        _options_verdict_deferred = False  # set at the verdict step; read by both finallys
        turn._verdict_booked = not _turn_completed_ok  # model errors already booked
        _title_pin_held: auto_title.RecordPin | None = None  # set under the permit
        # Bound before the first suspension point so the release finally can read
        # it on a cancellation landing at any await. Recomputed at the delivery
        # step below; the default False is correct for a cancellation BEFORE
        # delivery (nothing reached the reader, so the finally books no success).
        _answer_reached = False
        # Whether this turn carries an [OPTIONS] control. Bound early (default
        # False) so the release finally can tell an OPTIONS turn apart even on a
        # cancellation that lands BEFORE ``options`` is computed at the delivery
        # step: on such a turn the choices ride only in the footer and the verdict
        # is owned by the footer site, so the finally must never book success for
        # it. ``_options_verdict_deferred`` cannot serve this role because it is
        # set only at 4710, AFTER the ``stop_stream`` await where a cancellation
        # can occur.
        _options_present = False

        # ``asyncio.sleep(0)`` lets ``status_ctrl.finalize`` fire. It lives INSIDE
        # this try, AFTER the verdict state above, so a cancellation landing on
        # this first yield reaches the release finally with every local it reads
        # already bound, rather than propagating with the permit held.
        await asyncio.sleep(0)

        # ── Cancelled check: suppress response if message was deleted mid-flight ──
        if sessions.is_cancelled(session_key, msg_ts):
            logger.info("Message %s cancelled (deleted) — suppressing response", msg_ts)
            # A clean model completion whose reply the user then deleted is a
            # success, not a failure: the model did its work and the suppression
            # is a user action, not a delivery fault. Book it before returning so
            # this cancellation exit is not a verdict hole.
            if _turn_completed_ok:
                turn.book_success()
            await slack.set_thread_status(channel, reply_ts, "")
            if answer.stream_ts:
                try:
                    await slack.delete_message(channel, answer.stream_ts)
                except Exception:
                    logger.debug("Failed to delete cancelled stream", exc_info=True)
            if answer.thinking_ts:
                try:
                    await slack.delete_message(channel, answer.thinking_ts)
                except Exception:
                    logger.debug("Failed to delete thinking placeholder", exc_info=True)
            if _working_ts:
                try:
                    await slack.delete_message(channel, _working_ts)
                except Exception:
                    pass
            turn.release_permit()
            return

        # Clear assistant thread status (skip in review mode — keep indicator until button press)
        if channel_activation != ACTIVATION_REVIEW:
            await slack.set_thread_status(channel, reply_ts, "")

        # Remove inline stop button
        if _working_ts:
            try:
                await slack.delete_message(channel, _working_ts)
            except Exception:
                pass

        # Suppress error replies for trusted bot messages to prevent echo loops
        if from_trusted_bot and turn._had_error:
            logger.info("Suppressing error reply to trusted bot message to prevent echo loop")
            if answer.thinking_ts:
                try:
                    await slack.delete_message(channel, answer.thinking_ts)
                except Exception:
                    logger.debug("Failed to delete thinking placeholder", exc_info=True)
            if conversation_log and not _is_slack_restricted(session_key):
                await save_conversation_turn_off_loop(
                    conversation_log,
                    session_key,
                    text,
                    "[suppressed: trusted bot error]",
                    source_thread=session_key,
                    source_user=user_id,
                    agent=_agent,
                )
            turn.release_permit()
            return

        # Strip any inline <thinking> tags that leaked into the text
        _untrimmed = ""
        if answer.accumulated:
            answer.accumulated, inline_thinking = strip_thinking_tags(answer.accumulated)
            # Trailing control-tag lines are peeled BEFORE the whitespace trim: the
            # trim would erase the indentation that marks a quoted, 4-space-indented
            # tag as code, and the tail grammar would then read it as protocol.
            _untrimmed = strip_control_comments(answer.accumulated)
            answer.accumulated = _untrimmed.strip()
            if inline_thinking:
                answer.thinking_accumulated += (
                    "\n\n" if answer.thinking_accumulated else ""
                ) + inline_thinking

        actually_streamed = answer.use_slack_stream and bool(answer.stream_ts)
        # render_one_for_slack normalises ANSI and redacts BEFORE converting, then
        # again after. Converting first (as this did) let to_slack_mrkdwn's ANSI strip
        # reassemble a credential the escapes had broken up, and let its 39,000-char
        # self-truncation cut one in half before the regex below could match it.
        # keep_tables is forced here because Slack's rich streaming renderer draws
        # tables itself when the stream actually started.
        #
        # _render_redacted carries whether that internal redaction fired. It is
        # load-bearing, not informational: the answer has ALREADY been posted
        # incrementally, and the only thing that replaces the visible copy is the
        # final-update condition below. The outer passes cannot supply that signal
        # any more, because by the time they run the render has already cleaned the
        # text and they find nothing left to redact.
        # Extract the OPTIONS tag from the RAW answer.accumulated text, BEFORE rendering.
        # It is a plain-text marker at the very end of the turn, so rendering first
        # makes the controls hostage to the render's size ceiling: a >39,000-char
        # answer ending in [OPTIONS: ...] is truncated, the tag goes with the tail,
        # and the buttons silently never appear. Matches the ordering used by the
        # cron, subagent-completion and dashboard-mirror paths.
        # From the UNTRIMMED text, for the same reason as above: a tag line that sat
        # before the OPTIONS trailer is protocol only with its own indent in view.
        _body_text, options = extract_options(_untrimmed) if answer.accumulated else ("", [])
        _body_text = strip_control_comments(_body_text).strip()
        _options_present = bool(options)

        _render = render_one_for_slack(_body_text, keep_tables=actually_streamed)
        final_text = _render.text or _NO_RESPONSE
        _render_redacted = _render.redacted

        # Second pass at the boundary: the decorator seam below can still introduce
        # text, and these warning lists drive the final chat_update decision.
        final_text, exfil_warnings = redact_exfiltration_urls(final_text)
        for w in exfil_warnings:
            logger.warning("Exfiltration URL redacted in response: %s", w)
        final_text, cred_warnings = redact_credentials(final_text)
        for w in cred_warnings:
            # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure -- pre-existing site, moved verbatim
            logger.warning("Credential redacted in response: %s", w)

        clean_text = final_text

        # Outbound-reply decorator seam (Default: identity, OSS-identical). The model
        # has finished speaking, so this is the outbound half of an active
        # conversation — an edition may refresh its Slack auth window's activity clock
        # and append a "<5 min left" expiry footer here. The public DefaultDashboard-
        # Contributor returns the text unchanged. Fail-safe: a raising decorator falls
        # back to the undecorated text so it can never break the reply.
        from kiro_crew.platform import safe_context_call

        _pre_decorate = clean_text
        clean_text = safe_context_call(
            lambda: current_context().dashboard.decorate_reply(
                clean_text, channel=channel, user_id=user_id
            ),
            fallback=clean_text,
            log_message="dashboard.decorate_reply failed; sending undecorated reply",
        )
        # Re-run the redaction passes on any text the decorator INTRODUCED. Redaction
        # above (3493-3498) ran before decoration, so a decorator that appends a URL or
        # a credential-shaped token would otherwise reach Slack unscanned (link-preview
        # exfiltration / credential disclosure). Only re-scan when the decorator changed
        # the text (the common Default path is a no-op identity, so this is skipped).
        if clean_text != _pre_decorate:
            clean_text, _exfil_after = redact_exfiltration_urls(clean_text)
            if _exfil_after:
                logger.warning(
                    "Redacted %d exfiltration URL(s) introduced by reply decorator",
                    len(_exfil_after),
                )
            clean_text, _cred_after = redact_credentials(clean_text)
            if _cred_after:
                # Log only the COUNT — the per-warning strings embed a truncated
                # prefix of the matched credential (redact_credentials returns
                # "Redacted credential pattern: <first 20 chars>..."), so logging
                # them verbatim would defeat the redaction we just performed.
                logger.warning(
                    "Redacted %d credential pattern(s) introduced by reply decorator",
                    len(_cred_after),
                )

        # Per-turn tally of redaction placeholders in the text actually SENT, so the
        # user learns their pasteable text was rewritten. Read from the TAG in
        # `clean_text` rather than from `cred_warnings`, which only reaches the log:
        # on the streaming path that list is empty here because each chunk was already
        # redacted upstream, so re-redacting `clean_text` reports nothing. Counting the
        # artifact answers the question the user has -- "is what I am about to copy
        # still what the assistant wrote?" -- and stays correct wherever the
        # substitution happened (per-chunk, the StreamRedactor wire pass, the final
        # render, or the post-decorator scan). The shared ``count_redaction_tags``
        # sums every tag the redactor can emit so an encoded-credential-only reply
        # is not missed, and counts the exfiltration-URL tag by its prefix, because
        # that tag interpolates the redacted domain and has no constant form to
        # equality-compare. Kept as separate counts because the notice is worded
        # by kind: the remedies differ (re-enter the secret vs re-check the URL).
        #
        # The thinking block (redacted separately below) adds to this SAME tally so a
        # single warning covers the turn if either the answer or the thinking was
        # rewritten -- one turn, one notice, never two identical warnings.
        _cred_redactions, _url_redactions = count_redaction_tags(clean_text)

        # ── Review mode: ephemeral draft instead of public post ──
        if channel_activation == ACTIVATION_REVIEW:
            if not await _post_review_draft(
                answer, slack, channel, reply_ts, thread_ts, user_id, session_key, clean_text
            ):
                if _turn_completed_ok:
                    await turn.book_failure()
                turn.release_permit()
                return
            # Persist conversation (draft counts as a turn). Best-effort: the draft
            # already reached the reader, so a persistence failure must not turn a
            # delivered draft into a failed turn — book success first, then persist.
            if _turn_completed_ok:
                turn.book_success()
            if conversation_log and not _is_slack_restricted(session_key):
                try:
                    await save_conversation_turn_off_loop(
                        conversation_log,
                        session_key,
                        text,
                        answer.accumulated,
                        source_thread=session_key,
                        source_user=user_id,
                        agent=_agent,
                    )
                except Exception:
                    logger.warning(
                        "Slack review-draft persist failed for %s", session_key, exc_info=True
                    )
            turn.release_permit()
            return

        # ── Answer delivery, then exactly one verdict ──────────────────────────
        # THE turn invariant, in one place. Read it before touching accounting:
        #
        #   answer-reached = the reader has the answer. The evidence differs by
        #     path, on purpose:
        #       * Streaming: the answer is delivered incrementally as it arrives,
        #         and a refused append is recoverable delivery-debt (a designed
        #         follow-up), not a turn failure -- so a stream that was used is
        #         answer-reached.
        #       * No-stream: the answer is delivered ONLY by the final send, so
        #         answer-reached requires that send to RETURN (not raise). A
        #         truthy placeholder handle is not evidence -- the send itself is.
        #       * No answer text to send (a tool-only turn): nothing to deliver,
        #         answer-reached by definition.
        #
        #   send classification (explicit, not implied by which try block a call
        #     sits in):
        #       answer-carrying -> stream appends; the no-stream fallback send
        #         (``_safe_final_update`` / ``post_message``); and the timing
        #         footer WHEN it carries an [OPTIONS] control, because the trailer
        #         was stripped from the answer and the choices ride only in the
        #         footer.
        #       decoration -> the ``stop_stream`` seal (text is already on screen),
        #         the redaction overwrite on an already-delivered stream, thinking
        #         posts, the credential notice, and a footer with no options.
        #
        #   verdict -> exactly one per turn, on every exit including exceptions and
        #     cancellation: a failed answer-carrying send books a failure, a
        #     reached answer books a success, a failed decoration send books
        #     nothing and logs. ``_verdict_booked`` (defined above, shared with the
        #     review path) guarantees the "exactly one" so no path double-books and
        #     none books zero.
        #
        # A ``clean_text`` of the ``_NO_RESPONSE`` placeholder is NOT answer text
        # to deliver: a reasoning-only / tool-only turn renders empty and picks up
        # the ``_No response._`` sentinel upstream (4429), so treating it as real
        # answer text would demand a confirmed stream append that legitimately
        # never happens, and the wholly-refused-stream predicate below would book
        # such an ordinary turn a FAILURE — driving the consecutive-failure breaker
        # toward a session-resetting trip. There is nothing to deliver, so the
        # (placeholder) answer reaches trivially.
        _answer_text_to_send = bool(clean_text) and clean_text != _NO_RESPONSE
        _answer_reached = not _answer_text_to_send

        try:
            if answer.use_slack_stream and answer.stream_ts:
                await answer.finish(_untrimmed)
                # On the streaming path the answer is delivered incrementally as
                # it arrives, and a refused append on a stream that DID land text
                # is recoverable delivery-debt (a designed follow-up), NOT a turn
                # failure: the model produced a complete answer and the drop is
                # transient. So a stream that delivered at least one real-text
                # append counts as answer-reached. But a stream on which EVERY
                # append was refused (a Slack outage for the whole turn) delivered
                # nothing to the reader, and booking that as a success would count
                # an answer that never arrived -- exactly the before-delivery
                # accounting this change exists to remove. ``answer.delivered``
                # rises only on a confirmed real-text append, so it separates the
                # two: used stream -> reached, wholly-refused stream -> failure.
                # A turn with no answer text to send (``_answer_text_to_send``
                # False: empty body or the ``_NO_RESPONSE`` placeholder) opened the
                # stream for reasoning/tools but has nothing to deliver, so no
                # append lands and ``answer.delivered`` is legitimately False;
                # clobbering ``_answer_reached`` to False there would book an
                # ordinary reasoning/tool-only turn a failure. Only apply the
                # wholly-refused-stream predicate when there WAS answer text.
                # Read after the seal: it sends the last held run of text.
                await answer.seal(
                    clean_text, redacted=bool(_render_redacted or exfil_warnings or cred_warnings)
                )
                if _answer_text_to_send:
                    _answer_reached = answer.delivered
            elif answer.stream_ts:
                # Legacy fallback (chat.startStream unavailable): the "Thinking…"
                # placeholder is replaced with the clean text. Answer-carrying —
                # nothing streamed — so a primary-send failure raises and books a
                # failure; a send that returns is confirmed delivery.
                final_text = _convert_tables(clean_text) if clean_text else _NO_RESPONSE
                await _safe_final_update(
                    slack,
                    channel,
                    answer.stream_ts,
                    final_text or _NO_RESPONSE,
                    reply_ts,
                    raise_on_primary_failure=True,
                )
                _answer_reached = True
            else:
                # No stream and no placeholder — post the answer directly.
                # Answer-carrying: a raise means the reader got nothing; a return
                # is confirmed delivery.
                await slack.post_message(channel, clean_text or _NO_RESPONSE, reply_ts)
                _answer_reached = True
        except Exception:
            # An answer-carrying send failed: the reader received no answer.
            logger.exception("Slack answer delivery failed for %s", session_key)
            await turn.book_failure()
            try:
                await slack.post_message(
                    channel,
                    "🔧 Something went wrong delivering the reply. Please try again.",
                    reply_ts,
                )
            except Exception:
                logger.debug("Failed to post delivery-failure notice", exc_info=True)
            # The answer did not land; skip the decorations and release the permit.
            turn.release_permit()
            return

        # Exactly one verdict, from the predicate: a reached answer on a clean
        # completion is a success; a clean completion whose answer did NOT reach
        # the reader (every append refused, no fallback) is a failure. A turn that
        # already booked a failure in ``_run_turn``'s except blocks (a model-level
        # error) is left as-is by the idempotent guard.
        #
        # OPTIONS exception: when the reply carries an [OPTIONS] control the
        # choices were stripped from the answer body and ride ONLY in the footer,
        # so that footer (or its fallback) is itself answer-carrying. For such a
        # turn the verdict and the permit release are DEFERRED to the footer site
        # below, which books success only once one of the two OPTIONS deliveries
        # returns — and books a failure if both fail. The permit stays held across
        # the intervening decorations (fast, best-effort) so the deferred verdict
        # is still written under it.
        # Pin the record for the naming turn while the permit is still held. Every
        # release below is followed by Slack round-trips before the auto-title block,
        # and a queued turn that takes the released permit can delete this key's
        # record and re-mint it in that span. A pin read down there reads the
        # REPLACEMENT, the guard matches it, and the title generated from this turn
        # names a conversation it never ran in. While the permit is held no other
        # turn for this key runs, so the identity read here is the record this turn
        # is about. The same cheap ``is_titled`` peek the block below uses gates it,
        # so an already-named conversation pays no thread hop.
        #
        # A key whose record has not landed yet pins ABSENT here and is re-pinned
        # below once this turn's own row is written: with no record there is nothing
        # a replacement can be mistaken for, and the first exchange stays nameable.
        if (
            not turn._had_error
            and not _is_slack_restricted(session_key)
            and not auto_title.is_titled(session_key)
        ):
            _title_pin_held = await auto_title.pin_record(conversation_log, session_key)
        _options_verdict_deferred = bool(_turn_completed_ok and options and _answer_reached)
        if not _options_verdict_deferred:
            if _turn_completed_ok:
                if _answer_reached:
                    turn.book_success()
                else:
                    await turn.book_failure()
            # Accounting is done; release the retained permit now. The decorations
            # below touch no verdict state on this path.
            turn.release_permit()
    finally:
        # F2/F1: a cancellation (BaseException, uncaught by ``except Exception``)
        # raised AFTER the answer reached the reader — at the first
        # ``await asyncio.sleep(0)`` on a stream that already delivered, or inside
        # the best-effort ``stop_stream`` await — but before the verdict step,
        # propagates straight here leaving the turn with no verdict booked though
        # delivery succeeded. Book the decided success so that window is not a
        # verdict hole (a missing success-reset that leaves the consecutive-
        # failure counter stale). The delivered signal is ``_answer_reached OR
        # answer.delivered``: ``_answer_reached`` is recomputed only at the
        # delivery step (past ``sleep(0)``), but ``answer.delivered`` rises at the
        # first confirmed real-text append — before that first await — so it makes
        # a fully-streamed turn cancelled at the ``sleep(0)`` yield book its
        # success too, closing the whole cancellation-at-any-await class rather
        # than one await at a time. Idempotent via ``_verdict_booked``; gated on
        # ``not _options_present`` because on an OPTIONS turn the choices ride only
        # in the footer whose verdict is owned by the footer site, so a
        # cancellation before the footer delivers must NOT book success here.
        if (
            (_answer_reached or answer.delivered)
            and not turn._verdict_booked
            and not _options_present
        ):
            turn.book_success()
        # Structural release for every non-deferred exit. When the OPTIONS verdict
        # is deferred the permit is intentionally still held here and released at
        # the footer site; turn.release_permit stays idempotent so this is a no-op in
        # every already-released case.
        if not _options_verdict_deferred:
            turn.release_permit()

    # Structural release for the deferred-OPTIONS case: when the verdict was
    # deferred to the footer below, the permit is still held across these
    # decorations, so a raise or cancellation in any of them must still release
    # it. turn.release_permit() is idempotent, so for every already-released turn
    # this finally is a no-op.
    try:
        # Render reasoning as a condensed, subdued blockquote. When a
        # placeholder was posted above the answer, update it in place so the thread
        # reads reasoning → answer. Otherwise (the stream started before any
        # reasoning arrived) fall back to a post after the answer.
        if answer.thinking_accumulated and answer.show_thinking:
            # answer.thinking_accumulated is built from raw event text and, unlike the answer
            # stream, has no StreamRedactor upstream -- so this render is its ONLY
            # redaction. Ordering matters most here for that reason.
            thinking_mrkdwn = render_one_for_slack(answer.thinking_accumulated).text
            thinking_mrkdwn, exfil_warnings = redact_exfiltration_urls(thinking_mrkdwn)
            for w in exfil_warnings:
                logger.warning("Exfiltration URL redacted in thinking: %s", w)
            thinking_mrkdwn, cred_warnings = redact_credentials(thinking_mrkdwn)
            for w in cred_warnings:
                # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure -- pre-existing site, moved verbatim
                logger.warning("Credential redacted in thinking: %s", w)
            # Fold thinking redactions into the SAME per-turn tally as the answer so
            # a single warning covers the turn (see the tally comment above the
            # review-mode branch). Count the fully redacted text before it is
            # condensed -- condensing can truncate, which would drop a placeholder
            # from the count even though the credential was still rewritten.
            _thinking_creds, _thinking_urls = count_redaction_tags(thinking_mrkdwn)
            _cred_redactions += _thinking_creds
            _url_redactions += _thinking_urls
            thinking_block = _condense_thinking(thinking_mrkdwn)
            if answer.thinking_ts:
                try:
                    await slack.update_message(channel, answer.thinking_ts, thinking_block)
                except Exception:
                    logger.warning("Failed to update thinking message", exc_info=True)
            else:
                for part in split_message(thinking_block):
                    try:
                        await slack.post_message(channel, part, reply_ts)
                    except Exception:
                        logger.warning("Failed to post thinking message", exc_info=True)
        elif answer.thinking_ts:
            # Placeholder was posted but no reasoning was captured — remove it so
            # the thread isn't left with a dangling "💭 Thinking…".
            try:
                await slack.delete_message(channel, answer.thinking_ts)
            except Exception:
                logger.debug("Failed to delete empty thinking placeholder", exc_info=True)

        # One notice per turn, AFTER the answer (and thinking) have been posted, so it
        # reads below the text it describes. Posted as a SEPARATE threaded message
        # rather than folded into the answer: Slack has already committed the rich
        # answer via stop_stream/chat_update above and the answer text must stay
        # exactly as redacted (never relaxed, never annotated inline). Best-effort --
        # a failed notice must not turn a delivered answer into a failed turn.
        if _cred_redactions > 0 or _url_redactions > 0:
            try:
                await slack.post_message(
                    channel, redaction_notice(_cred_redactions, _url_redactions), reply_ts
                )
            except Exception:
                logger.warning("Failed to post credential redaction notice", exc_info=True)

        # Persist the turn BEFORE posting anything that invites an answer to it.
        # The control below carries a staleness token derived from this session's last
        # persisted transcript row, so posting it while this turn is still unwritten
        # would stamp it with the PREVIOUS turn's position -- and these two rows
        # landing straight afterwards would read as the conversation having moved on,
        # refusing the very click the control was posted for.
        #
        # Durability-before-invitation is also right on its own terms: a question
        # about a turn that has no record is not answerable after a restart.
        _skip_writes = _is_slack_restricted(session_key)
        _turn_row_ts: str | None = None
        if conversation_log and not _skip_writes:
            # The per-turn hot path: two appends every turn, so this is where the
            # ~12ms of loop time was paid most often.
            _turn_row_ts = await save_conversation_turn_off_loop(
                conversation_log,
                session_key,
                text,
                answer.accumulated,
                source_thread=session_key,
                source_user=user_id,
                agent=_agent,
            )

        # ── Timing footer ──
        elapsed = time.monotonic() - _t0
        footer_blocks, footer_text = build_timing_footer(elapsed, client)
        # Gated on `options` alone. A top-level Slack message has no ``thread_ts``, so
        # gating on it left every root-thread control untokened -- unprotected on
        # exactly the path a restart strands. ``reply_ts`` is the thread this control
        # actually lands in (``thread_ts or msg_ts``), and ``session_key`` is the
        # conversation that ran this turn: resolving the asker from the thread instead
        # would name whoever owns it at mint time, so a link landing mid-turn would
        # stamp the control with a session that never asked the question.
        #
        # The position comes from the row this turn WROTE, not from re-reading the
        # tail. The session permit is released well above here, so a queued second
        # turn can persist in between; a re-read would then hand this control the
        # NEWER turn's position and a click on it -- by then obsolete -- would read as
        # current and be accepted. Minting from our own row also means no I/O and no
        # await here at all. No row (restricted session, or no log) means no provable
        # position, so the control posts untokened and its clicks are honoured.
        _options_token = (
            mint_options_token(
                cast("DashboardState | None", _dashboard_state),
                session_key,
                _turn_row_ts,
            )
            if options and _turn_row_ts
            else None
        )
        # Re-read the DASHBOARD link for the footer button only: a mid-turn
        # click on the turn-start button may have linked the thread to a slot
        # since turn start, and a second Link button would be redundant. (Not
        # the session map: the turn's own self-link lands there mid-turn too.)
        _ds_now = cast("DashboardState | None", _dashboard_state)
        _footer_link_key = linked_session_key
        if not _footer_link_key and thread_ts and _ds_now is not None:
            _slot_now = _ds_now.get_linked_slot(reply_ts)
            _footer_link_key = _slot_now.key if _slot_now is not None else None
        footer_blocks = _append_footer_actions(
            footer_blocks,
            options,
            thread_ts,
            _footer_link_key,
            _dashboard_state,
            _options_token,
        )
        # The footer is decoration EXCEPT when it carries an [OPTIONS] control:
        # the trailer was stripped from the answer, so the choices ride ONLY here,
        # which makes the footer (or its fallback) answer-carrying. For such a turn
        # the verdict was deferred to this site: success is booked only once one of
        # the two OPTIONS deliveries returns, and a failure is booked if BOTH fail
        # — the choices never reached the reader, so it is not a success. The
        # fallback text is model-authored, so it passes the SAME display-safe
        # redaction the answer path uses (never a second scrubber). A footer with
        # no options is pure decoration; its failure just logs.
        _footer_ts: str | None = None
        _options_delivered = False
        try:
            _footer_ts = await slack.post_blocks(channel, footer_blocks, footer_text, reply_ts)
            _options_delivered = True
        except Exception:
            logger.warning("Slack footer post_blocks failed for %s", session_key, exc_info=True)
            if options:
                try:
                    # The choices are model-authored, so the fallback carries the
                    # SAME display-safe obligation as the answer it stands in for:
                    # scan against what Slack RENDERS, not only the literal bytes.
                    # A bare exfil+credential scan misses ``[AKIA](url)REST`` and
                    # ``<!channel>`` obfuscated behind markup that Slack collapses
                    # on screen -- the primary OPTIONS blocks escape every choice
                    # and the renderer's fallback twin runs this same canonical
                    # scrub, so this path routes through it too rather than a
                    # weaker literal-only pass.
                    _fallback = redact_for_display(
                        "*Options:*\n" + "\n".join(f"• {o}" for o in options),
                        _display_redactor,
                    )[0]
                    await slack.post_message(channel, _fallback, reply_ts)
                    _options_delivered = True
                except Exception:
                    logger.warning(
                        "Slack options fallback post failed for %s", session_key, exc_info=True
                    )
        if _options_verdict_deferred:
            # The OPTIONS payload is answer-carrying: book the deferred verdict from
            # whether the choices reached the reader, then release the retained
            # permit. Idempotent helpers, so this is the single verdict for the turn.
            if _options_delivered:
                turn.book_success()
            else:
                await turn.book_failure()
            turn.release_permit()
        if options and _footer_ts:
            # Remember this turn's OPTIONS control so the next turn can strike it
            # through once the conversation has moved past the question it asked.
            #
            # Resolved ONCE and reused by the cleanup below. The record and the
            # expiry have to agree on the owner key or they can never pair up: a
            # thread linked to a dashboard mid-turn changes owner, so recording under
            # the key this turn started with files the control where the next turn's
            # expiry will not look. Reading it twice would reopen the same split if a
            # link landed in between.
            _options_owner = sessions.get_session_for_thread(reply_ts) or session_key
            try:

                remember_slack_options(
                    cast("DashboardState | None", get_dashboard_state()),
                    _options_owner,
                    PostedOptions(
                        channel=channel,
                        ts=_footer_ts,
                        choices=tuple(options),
                        blocks=tuple(footer_blocks),
                        text=footer_text,
                    ),
                )
            except Exception:
                logger.debug("Failed to record OPTIONS control", exc_info=True)

            # The conversation can move on while post_blocks is in flight -- a queued
            # message can acquire the permit this turn already released and run a whole
            # turn underneath us. The control we just posted would then be asking a
            # question nobody is on any more.
            #
            # Judged by the SAME predicate the click paths use, against the token that
            # went out on the control. That is the whole point of minting it: the
            # question "has this conversation moved past this control" has one answer,
            # computed one way, whether it is asked here or when a click arrives.
            #
            # Cosmetic. A click on a superseded control is refused on its own terms, so
            # failing to strike it through leaves the thread untidy, not unsafe.
            _superseded = _options_token is not None and await options_control_is_stale(
                cast("DashboardState | None", get_dashboard_state()),
                _options_token,
                reply_ts,
            )
            if _superseded:
                try:
                    # Narrowed to OUR footer's ts, never a session-wide drain: the
                    # very turn that superseded us can finish while we were awaiting
                    # post_blocks and record its OWN live control on this session, and
                    # draining the slot would strike that newer question through --
                    # silencing the one the conversation is now waiting on.
                    await expire_slack_options(
                        cast("DashboardState | None", get_dashboard_state()),
                        _options_owner,
                        ts=_footer_ts,
                    )
                except Exception:
                    logger.debug(
                        "Failed to expire OPTIONS control superseded mid-post",
                        exc_info=True,
                    )

        # ── Voice reply (fire-and-forget, non-blocking) ──
        await _reply_by_voice(
            slack,
            channel,
            reply_ts,
            user_id,
            session_key,
            answer.accumulated,
            final_text,
            had_voice_input,
        )

        # ── Update task banner with final state ──
        # History was persisted earlier, above the OPTIONS control, so that the
        # control's staleness token names this turn rather than the one before it.
        if conversation_log and not _skip_writes:
            if consolidator and _stop_reason != STOP_REASON_CANCELLED:
                consolidator.maybe_consolidate(session_key)

        # ── Bidirectional sync: mirror to dashboard if routed to a dashboard session ──
        if linked_session_key and _dashboard_state and answer.accumulated and not _skip_writes:
            _mirror_to_dashboard(linked_session_key, text, answer.accumulated)
        # ── Auto-title Slack thread (fire-and-forget) ──
        # Claim-early-unclaim-on-failure pattern: ``try_claim`` checks and marks in one
        # synchronous step, so concurrent messages (and the transport path, which
        # claims through the same shared tracker) cannot both fire a task. If the
        # background task fails or returns SKIP, it unclaims the key so the next
        # message retries. A message arriving between claim and unclaim is
        # intentionally skipped (no duplicate).
        if not turn._had_error and not _skip_writes and not auto_title.is_titled(session_key):
            # The ``is_titled`` peek above is a cheap synchronous membership test on
            # the same tracker ``try_claim`` checks below: once a key is claimed or
            # titled the claim cannot be taken again, so without the peek the pin's
            # thread hop would be paid and then discarded on every later message of
            # every already-named conversation.
            #
            # Pin BEFORE claiming, and both before the task is scheduled. The pin
            # read suspends on a thread, so claiming first would leave the claim
            # held across that await with nothing scheduled yet to release it: a
            # cancellation there (``!stop``) would strand it, and the claim is
            # process-wide, so this key could not be auto-titled again until the
            # gateway restarts. The pin still precedes ``create_task``, which is
            # what closes the scheduling-tick window -- see ``pin_record``.
            #
            # The pin itself is the one taken under the permit, well above here:
            # reading it at this point would sit after the release and after the
            # Slack round-trips in between, which is the window a replacement
            # record slips through. ABSENT is the one state worth re-reading, and
            # only because a key with no record has no replacement to confuse:
            # this turn's own row has landed by now, so the re-read is what makes a
            # brand-new conversation nameable from its first exchange.
            _title_pin = _title_pin_held
            if _title_pin is None or _title_pin.state == auto_title.RECORD_ABSENT:
                _title_pin = await auto_title.pin_record(conversation_log, session_key)
            if auto_title.try_claim(session_key):
                track_background_task(
                    asyncio.create_task(
                        _maybe_auto_title_slack(
                            slack,
                            sessions,
                            channel,
                            session_key,
                            conversation_log,
                            text,
                            answer.accumulated,
                            pin=_title_pin,
                        )
                    )
                )
    finally:
        # If the verdict was deferred to the footer and this tail is torn down
        # (a raise or cancellation in a decoration) before the footer books it,
        # the choices never reached the reader, so book the failure here rather
        # than exit with no verdict. Idempotent: a no-op once the footer booked.
        if _options_verdict_deferred and not turn._verdict_booked:
            await turn.book_failure()
        turn.release_permit()
