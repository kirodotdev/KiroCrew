"""What a sub-agent completion says: its report, its card facts and the wave digest.

Composing a report is pure apart from redacting what it quotes. The wave digest
is synchronous: from reading the wave's record to the hold-or-flush decision
nothing yields, so a sibling cannot close the wave between a member writing its
line and deciding how that line is delivered.

Orchestrator fields read: ``_batch_progress``, ``dashboard_state``, ``subagent_mgr``.
Orchestrator fields written: none.

Composed by :mod:`kiro_crew.slack.gateway`, whose globals its functions run on;
see :mod:`kiro_crew.slack.gateway_runtime`.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    import time

    from kiro_crew.slack.gateway import (
        DIGEST_HOLD_SECS,
        LEAKED_TOOL_CALL_NOTE,
        NEVER_STARTED_MEMORY_PREFIX,
        NO_TOOL_CALLS_NOTE,
        OUTCOME_FAILED,
        OUTCOME_OK,
        OUTCOME_STOPPED,
        SUBAGENT_BATCH_COMPLETION_PREFIX,
        SUBAGENT_COMPLETION_PREFIX,
        Any,
        GatewayOrchestrator,
        SubagentDelivery,
        SubagentInfo,
        format_subagent_usage,
        logger,
        redact_credentials,
        redact_exfiltration_urls,
        redact_for_display,
        redact_via_context,
        single_completion_meta,
        summarize_result,
        wave_chunk_meta,
        wave_final_meta,
    )


# Chunked wave-digest size: every multi-task wave delivers its completed
# results to the parent in digest CHUNKS of this many members (queue-style —
# each chunk is one injection turn), with a final partial chunk when the wave
# closes. A 60-agent wave = 6 digest turns spread across the wave's runtime
# instead of 60 per-agent turns (the parent-context flood at scale) or one
# straggler-gated mega-digest at the very end. Single-task spawns have no
# batch identity and keep the plain per-agent injection.
# Tunable via KIROCREW_SUBAGENT_DIGEST_CHUNK_SIZE. Guarded parse: a malformed
# value must never crash gateway import — fall back to the default and clamp
# to a sane positive range.
def _digest_chunk_size() -> int:
    try:
        return max(1, min(int(os.environ.get("KIROCREW_SUBAGENT_DIGEST_CHUNK_SIZE", "10")), 1000))
    except (TypeError, ValueError):
        return 10


SUBAGENT_DIGEST_CHUNK_SIZE = _digest_chunk_size()


class _SubagentReport(NamedTuple):
    """What a completion says; see ``_compose_subagent_completion``."""

    status: str
    emoji: str
    title: str
    result_path: str
    detail: str
    task_text: str
    usage: str
    body: str
    no_tools_note: str
    announce: str
    sub_meta: dict[str, Any]


class _WaveDigest(NamedTuple):
    """The announce and card facts a completion routes; see ``_subagent_wave_digest``."""

    announce: str
    sub_meta: dict[str, Any]


def _compose_subagent_completion(info: SubagentInfo, *, _flush_only: bool) -> _SubagentReport:
    """What one completion says: its status, detail, announce and card facts.

    Pure apart from the redaction of what it quotes. A wave member's announce and
    card facts may be replaced by ``_subagent_wave_digest``.
    """
    # Three-way outcome: a user stop is neutral — neither a success nor
    # a failure. The record contract keeps ``error`` unset for stops, so
    # every consumer below must branch on ``user_stopped`` explicitly
    # rather than inferring success from an empty error.
    if info.user_stopped:
        # The stop's own origin when the record carries one (a
        # parent-end verb), so the announce does not
        # credit the user with a stop they never pressed.
        status, emoji, single_outcome = (
            getattr(info, "_stop_origin", "") or "stopped by user",
            "⏹",
            OUTCOME_STOPPED,
        )
    elif info.error:
        status, emoji, single_outcome = "failed", "❌", OUTCOME_FAILED
    else:
        status, emoji, single_outcome = "completed", "✅", OUTCOME_OK
    title = f"Subagent `{info.id}` {emoji}"

    # Chat mode: inline info.result (subagent.py already trimmed it to
    # agent.completion_keep + completion_keep_chars) when it fits. When the
    # completion copy dropped content (result_truncated), emit a summary +
    # result_path pointer so the parent reads the full
    # transcript on demand (read / grep / spawn_status) instead of re-running
    # the subagent.
    result_path = info.result_path or ""
    if info.user_stopped:
        _partial = info.result or ""
        # Same origin as the status line above: a parent end or a row
        # chat Autopilot queued must not read as the user's own Stop in
        # the digest text.
        _origin = getattr(info, "_stop_origin", "") or "stopped by user"
        _who = "Stopped by the user" if _origin == "stopped by user" else f"Stopped ({_origin})"
        detail = (
            f"{_who} before completing. Do NOT treat this as "
            "a finished result or retry it unprompted."
            + (f"\n\nPartial output:\n{_partial}" if _partial else "")
        )
    elif info.error:
        detail = f"Error: {info.error}"
        # A run that ended on a stall / cancel / transport death keeps
        # what it streamed as a flagged PARTIAL (``info.partial``, set by
        # the stop-reason classifier in subagent_manager/run_turn.py); deliver
        # it so the parent continues from it instead of re-submitting the
        # whole task.
        if info.partial and info.result:
            detail += (
                "\n\nPartial output (the run did NOT finish — do not treat "
                f"this as a completed result):\n{info.result}"
            )
    elif result_path and info.result_truncated:
        detail = summarize_result(info.result, result_path)
    else:
        detail = info.result or "_No response._"
    detail, _ = redact_exfiltration_urls(detail)
    detail, _ = redact_credentials(detail)
    task_text, _ = redact_exfiltration_urls(info.task)
    task_text, _ = redact_credentials(task_text)
    task_text = task_text[:100]
    usage = format_subagent_usage(info.credits, info.elapsed)
    body = f"{task_text}\n\nUsage: {usage}\n\n{detail}"
    title, _ = redact_exfiltration_urls(title)
    title, _ = redact_credentials(title)

    # A completed run that made no tool call cannot have touched the disk,
    # so it carries the claim note in the event the parent reads its
    # narration from. A lone run or a single wave gets no synthesis turn,
    # so this event is the only place the parent can be told before it
    # reports the work to the user. Stops and failures say nothing: their
    # prose already tells the parent not to treat the output as finished.
    no_tools_note = ""
    if single_outcome == OUTCOME_OK and not _flush_only and info.made_no_tool_calls:
        no_tools_note = f"⚠ Agent `{info.id}` {NO_TOOL_CALLS_NOTE}"
    # A completed run whose final segment wrote a tool call as text instead of
    # running it (``SubagentInfo.leaked_tool_call``) reads as a success while
    # that call never executed. Same channel as the note above, so a lone run
    # and a wave digest line both carry it; the outcome stays completed.
    if single_outcome == OUTCOME_OK and not _flush_only and info.leaked_tool_call:
        _leak_note = f"⚠ Agent `{info.id}` {LEAKED_TOOL_CALL_NOTE}"
        no_tools_note = f"{no_tools_note}\n{_leak_note}" if no_tools_note else _leak_note
    announce = (
        f"{SUBAGENT_COMPLETION_PREFIX}\n"
        f"Agent `{info.id}`"
        f"{f' ({info.agent})' if info.agent else ''}"
        f" {status} {emoji}\n"
        f"Task: {task_text}\n\n"
        f"Usage: {usage}\n\n"
        f"{detail}" + (f"\n\n{no_tools_note}" if no_tools_note else "")
    )
    # Structured header facts for the dashboard card, stamped on the row
    # so a reword of the prose above cannot silently break rendering.
    # The card reads this; the frontend regexes are a fallback.
    # Reassigned for the wave-digest shapes below when this member's
    # completion is folded into a batch chunk instead of injected alone.
    sub_meta = single_completion_meta(
        agent_id=info.id,
        outcome=single_outcome,
        agent_name=info.agent or "",
        task=task_text,
        requested_model=info.requested_model or info.model or "",
        resolved_model=info.resolved_model or "",
    )

    if _flush_only:
        # The synthetic record has no result of its own. Its title/body
        # are only used by the "parent slot gone → notification only"
        # fallback, so make them describe the WAVE, not a phantom agent.
        title = "Wave results (partial)"
        body = task_text
    return _SubagentReport(
        status=status,
        emoji=emoji,
        title=title,
        result_path=result_path,
        detail=detail,
        task_text=task_text,
        usage=usage,
        body=body,
        no_tools_note=no_tools_note,
        announce=announce,
        sub_meta=sub_meta,
    )


def _subagent_wave_digest(
    self: GatewayOrchestrator,
    info: SubagentInfo,
    *,
    report: _SubagentReport,
    _flush_only: bool,
    _batch_id: str,
    _batch_total: int,
    _no_batch_pending: bool,
    _injection_slot_name: str,
) -> _WaveDigest | None:
    """Account one completion in its wave and decide how the wave delivers it.

    Synchronous on purpose: from reading ``_batch_progress`` to the hold-or-flush
    decision nothing may yield, so a sibling cannot close the wave between this
    member writing its line and deciding how that line is delivered. ``None`` means
    the completion is settled here (held for a later chunk, or nothing to release);
    otherwise the announce and card facts to route, a digest chunk's when one fires.
    """
    status, emoji, detail = report.status, report.emoji, report.detail
    task_text, usage, result_path = report.task_text, report.usage, report.result_path
    no_tools_note = report.no_tools_note
    announce, sub_meta = report.announce, report.sub_meta
    if _batch_id:
        if _flush_only:
            # Nothing to release (wave already closed / already flushed
            # by a completion that raced this sweep) → no-op.
            _bp = self._batch_progress.get(_batch_id)
            if _bp is None or _bp["done"] <= _bp["flushed"]:
                return None
            bp = _bp
            # A forced flush never closes the wave: the sweep only fires
            # while members are still outstanding, so the wave-close
            # digest (counts + release guidance) is still to come.
            _last = False
            _oc = ""
        else:
            bp = self._batch_progress.setdefault(
                _batch_id,
                {
                    "total": _batch_total,
                    "done": 0,
                    "ok": 0,
                    "err": 0,
                    "stopped": 0,
                    # Failed members a memory wait ended before they
                    # started: dropped work the parent may resubmit. A
                    # refusal is not counted; resubmitting it would only
                    # be refused again.
                    "not_run": 0,
                    "fail_lines": [],
                    "ok_lines": [],
                    "held_ok_deliveries": [],
                    # Members whose delivery is currently held, so the
                    # hold-deadline sweep's timestamps can be cleared
                    # when their chunk finally fires.
                    "held_infos": [],
                    # Chunked delivery bookkeeping: "flushed" = members whose
                    # results have already been delivered in a prior chunk;
                    # "chunks" = digest chunks emitted so far.
                    "flushed": 0,
                    "chunks": 0,
                },
            )
    if _batch_id and not _flush_only:
        # This member's report has been consumed: its contribution is
        # about to land in the wave's done-count, so it stops holding
        # the wave-close fallback open (batch_reports_in_flight).
        # Release in the same synchronous block as the
        # increment so the hold and the count can never be observed
        # apart. The release goes to the manager-level registry -- NOT
        # an agent-record flag -- so an operator clear (DELETE
        # /api/spawn) popping this member from ``_agents`` mid-flight
        # cannot have dropped the hold before this line lands it.
        if self.subagent_mgr is not None:
            self.subagent_mgr.consume_report_hold(_batch_id, info.id)
        bp["done"] += 1
        _oc = info.outcome
        if _oc == "stopped":
            bp["stopped"] += 1
        elif _oc == "failed":
            bp["err"] += 1
            if (info.error or "").startswith(NEVER_STARTED_MEMORY_PREFIX):
                bp["not_run"] += 1
        else:
            bp["ok"] += 1
        # Per-member model provenance in the PARENT-READ digest text: the
        # announce body the parent LLM consumes is built from
        # ok_lines/fail_lines, so surface each member's served
        # model inline there — rather than in a structured meta field
        # with no consumer.
        #
        # Print the SERVED model id only (no "(requested …)" qualifier):
        # `_res_model != _req_model` is NOT how the card decides a
        # downgrade — `isModelDowngrade` folds auto/default to "no pin"
        # and treats alias-vs-canonical / routing-prefix pairs as the
        # same model, so a raw inequality would print a false downgrade
        # on every member of a normal wave (default agent.model is
        # "auto"). Until this uses the same fold (or the registry
        # fold), show only the served id, and show nothing when there is
        # no served model — matching what the card renders in that case.
        # The value is caller-influenceable (spawn_run.model), so redact
        # it through the display context before it enters the digest
        # text broadcast to the dashboard/channels: credential-shaped
        # input must not reach metadata.
        _res_model = info.resolved_model or ""
        if _res_model:
            _res_model, _ = redact_for_display(_res_model, redact_via_context)
        _model_tag = f" · model {_res_model}" if _res_model else ""
        # Exception-first digest content: failures/stops carry detail,
        # successes are one pointer line (full output stays on disk).
        if _oc == "completed":
            # A completed run is partial for one of two causes, and the
            # digest names which: it kept its streamed output after a
            # generate failure, or it called tools and then closed with no
            # reply. The second case always ends with
            # EMPTY_TURN_NOTICE_AFTER_WORK, which run_turn appends after the
            # keep cap. Imported here: these functions run with the
            # gateway's module globals, which do not carry the notice.
            from kiro_crew.messaging.empty_turn_copy import EMPTY_TURN_NOTICE_AFTER_WORK

            if (info.result or "").endswith(EMPTY_TURN_NOTICE_AFTER_WORK):
                _kept = " (partial: ended without a closing reply after tool calls)"
            else:
                _kept = " (partial: backend failed to generate the final response)"
            bp["ok_lines"].append(
                f"— `{info.id}` ✅{_kept if info.partial else ''} {task_text[:80]}"
                f"{_model_tag} · {usage}"
                + (f"\n  → {result_path}" if result_path else "")
                + (f"\n  {no_tools_note}" if no_tools_note else "")
            )
        else:
            bp["fail_lines"].append(
                f"— `{info.id}` {status} {emoji} · {task_text[:80]}{_model_tag}\n"
                f"  Usage: {usage}\n"
                f"  {detail[:400]}{'…' if len(detail) > 400 else ''}"
            )
        _last = bp["total"] > 0 and bp["done"] >= bp["total"]
        if not _last:
            # Robustness: a wave member that failed AT SPAWN never
            # reaches this consumer, so done can never hit total.
            # Completion is decided by THIS batch's outstanding
            # members only (running OR still queued behind the
            # stagger gate) — an unrelated agent under the same
            # parent must neither hold the digest hostage nor
            # release it early.
            #
            # A member whose ``done`` flag has flipped but whose
            # terminal report has not reached this consumer yet is
            # OUTSTANDING too: ``batch_members_pending`` does not
            # count it while its contribution to bp["done"] is still
            # in flight, so without the reports-in-flight check a
            # sibling landing in that window finalizes the wave early
            # and the in-flight report then finalizes it again.
            #
            # The membership lookup ran before this block (see
            # ``_no_batch_pending``); the in-flight read is synchronous
            # and runs after this member released its own hold above.
            try:
                _last = bool(
                    self.subagent_mgr
                    and _no_batch_pending
                    and not self.subagent_mgr.batch_reports_in_flight(_batch_id)
                )
            except Exception:
                _last = False
        if _last:
            self._batch_progress.pop(_batch_id, None)
            # Prune per-wave bookkeeping for ALL wave sizes (bounds
            # _seen_batches / _batch_submitted growth).
            try:
                if self.subagent_mgr:
                    self.subagent_mgr.finalize_batch(_batch_id)
            except Exception:
                logger.debug("finalize_batch failed", exc_info=True)
            try:
                if self.dashboard_state:
                    self.dashboard_state.broadcast_ws(
                        "batch_finished",
                        {
                            "batch_id": _batch_id,
                            "slot": _injection_slot_name,
                            "total": bp["total"],
                            "ok": bp["ok"],
                            "err": bp["err"],
                            "stopped": bp["stopped"],
                        },
                    )
            except Exception:
                logger.debug("batch_finished broadcast failed", exc_info=True)
    if _batch_id:
        if _flush_only and bp["total"] <= 1:
            # Single-member wave: nothing is ever held, and falling
            # through would route the phantom per-agent announce.
            return None
        if bp["total"] > 1:
            # ── Chunked wave delivery ── completed results feed the
            # parent queue-style: every SUBAGENT_DIGEST_CHUNK_SIZE
            # completions flush ONE digest chunk (an injection turn);
            # the final member flushes the remaining partial chunk.
            # This bounds each digest's size AND gives the parent
            # incremental signal — one straggler does not withhold
            # every sibling's result for its entire runtime.
            #
            # The count trigger alone cannot deliver that incremental
            # signal for a wave smaller than the chunk size: _pending can
            # never reach it, so wave close is the only flush.
            # _flush_only is the LATENCY trigger the count
            # lacks — the reaper's hold-deadline sweep forces the
            # pending chunk out once results have been held too long.
            _pending = bp["done"] - bp["flushed"]
            _flush = _last or _flush_only or _pending >= SUBAGENT_DIGEST_CHUNK_SIZE
            if not _flush:
                # Held for the next chunk — the terminal WS event,
                # tracker accounting, and stats above already ran;
                # only the per-agent injection turn is suppressed.
                # Restart safety: flag the member so the run loop SKIPS
                # mark_delivered — its result is not in the parent's
                # context yet, and a "delivered" tombstone would hide it
                # from orphan reconciliation. If the gateway restarts
                # mid-chunk, the orphan path finds these undelivered
                # results and delivers a recovery digest; in normal
                # operation they are marked delivered when their chunk
                # flushes.
                info._digest_held = True
                # Hold clock for the reaper's hold-deadline sweep. Kept
                # separate from _digest_held (the restart-safety flag the
                # run loop reads) so the sweep never mutates that
                # contract; cleared when this member's chunk fires.
                # Monotonic, as the sweep's ``now`` is: a wall-clock step
                # must not force a partial digest out, or hold one past
                # its deadline.
                info._digest_held_at = time.monotonic()
                bp.setdefault("held_infos", []).append(info)
                if _oc == "completed":
                    bp["held_ok_deliveries"].append(
                        SubagentDelivery(info.id, info.elapsed, info.credits)
                    )
                elif getattr(info, "_report_owed", False) is True:
                    # A memory-wait expiry the store owes a report until
                    # it reaches the parent: the debt rides with this
                    # chunk, and the settle that delivers the chunk
                    # clears it. Held only here, a restart before the
                    # flush leaves it owed for the next start.
                    bp["held_ok_deliveries"].append(
                        SubagentDelivery(info.id, info.elapsed, info.credits, report_owed=True)
                    )
                logger.info(
                    "Subagent %s: completion held for digest chunk (%d/%d done)",
                    info.id,
                    bp["done"],
                    bp["total"],
                )
                return None
            # Chunk fires now. Do NOT settle the held members'
            # delivery tombstones here — composition precedes routing,
            # and marking "delivered" before the chunk is handed off
            # would re-open the restart-loss window.
            # Stash the ids on the flushing member: the run loop
            # settles them only after _on_done (which includes the
            # routing below) returns without raising.
            info._digest_settle_deliveries = list(bp.get("held_ok_deliveries", []))
            # These members are released: stop the hold clock so
            # the reaper's deadline sweep does not force a second flush
            # for results this chunk already carries.
            for _held in bp.get("held_infos", []):
                _held._digest_held_at = 0.0
            bp["held_infos"] = []
            _failures = bp["fail_lines"]
            _oks = bp["ok_lines"]
            _digest_body = "\n".join(_failures + _oks)
            if len(_digest_body) > 60_000:
                _digest_body = _digest_body[:60_000] + "\n…(digest truncated)"
            bp["chunks"] += 1
            bp["flushed"] = bp["done"]
            _chunk_k = bp["chunks"]
            # Total chunks: full chunks + one final partial. Completion
            # order fills chunks to exactly CHUNK_SIZE, so this is
            # ceil(total / chunk_size) — but a NON-final chunk always
            # has at least the wave-close chunk still to come, so it can
            # never honestly label itself k/k. Without the +1 a
            # deadline-forced flush on a small wave would announce
            # "1/1 — 1 still running", telling the parent the wave is
            # fully delivered while a member is outstanding.
            _chunk_j = max(
                _chunk_k if _last else _chunk_k + 1,
                -(-bp["total"] // SUBAGENT_DIGEST_CHUNK_SIZE),
            )
            _footer = (
                "Failures are listed first. Full outputs are on disk — "
                "read the result paths on demand; do NOT re-run "
                "completed agents."
            )
            if _last:
                # Final chunk: release the spawn-discipline gate.
                #
                # The tally counts this wave's DIRECT members only.
                # ``wave_has_live_nested_spawns`` reports whether a
                # member of this wave has itself spawned work that is
                # still running (its own independent batch, so not in
                # this total). When it is, the completion wording is
                # scoped to the direct members and states that their
                # nested work reports on its own; the unconditional
                # "This run is complete / All results delivered" claim
                # is reserved for a wave with no live nested work. The
                # check is read-only and never withholds the digest.
                try:
                    _nested_live = bool(
                        self.subagent_mgr
                        and self.subagent_mgr.wave_has_live_nested_spawns(_batch_id)
                    )
                except Exception:
                    _nested_live = False
                # Members a memory wait ended are dropped work, not
                # done work. After the tally sentence, which the card
                # regexes anchor on. "Once memory eases": a member the
                # spent episode ended at once is counted too, and a
                # resubmit while that episode lasts is ended at once.
                _not_run_line = (
                    f"{bp['not_run']} of the ❌ never started, so their "
                    f"work was not done: resubmit them once memory "
                    f"eases if it is still needed. "
                    if bp.get("not_run")
                    else ""
                )
                if _nested_live:
                    _completion_line = (
                        f"These {bp['total']} sub-agents finished: "
                        f"{bp['ok']} ✅ · {bp['err']} ❌ · "
                        f"{bp['stopped']} ⏹. {_not_run_line}"
                        f"Their results are below. "
                        f"NOTE: a sub-agent in this wave spawned further "
                        f"work that is still running; that nested work "
                        f"is tracked as its own wave and reports "
                        f"separately when it finishes — this digest does "
                        f"NOT cover it.\n"
                        f"Finish processing these results before "
                        f"spawning any follow-up sub-agents.\n"
                    )
                else:
                    _completion_line = (
                        f"wave finished: "
                        f"{bp['ok']} ✅ · {bp['err']} ❌ · "
                        f"{bp['stopped']} ⏹ of {bp['total']} agents. "
                        f"{_not_run_line}"
                        f"All results delivered.\n"
                        f"This run is complete. Finish processing all "
                        f"results before spawning any follow-up "
                        f"sub-agents.\n"
                    )
                announce = (
                    f"{SUBAGENT_BATCH_COMPLETION_PREFIX}\n"
                    f"Batch results {_chunk_k}/{_chunk_j} — "
                    f"{_completion_line}"
                    f"{_footer}\n\n{_digest_body}"
                )
                # This member's completion is delivered as the wave-close
                # digest, not a per-agent row — stamp the digest's facts
                # (tallies + chunk index) in place of the single-agent
                # meta built above.
                sub_meta = wave_final_meta(
                    chunk=_chunk_k,
                    chunks=_chunk_j,
                    ok=bp["ok"],
                    failed=bp["err"],
                    stopped=bp["stopped"],
                    total=bp["total"],
                )
            else:
                # Non-final chunk: spawn-discipline guidance — the
                # parent wakes mid-wave, so it must not start new
                # spawns that would interleave with the batches still
                # arriving from this run.
                _remaining = max(0, bp["total"] - bp["done"])
                # A deadline-forced flush is a STRAGGLER release, not a
                # full chunk: say so, or the parent reads "still
                # running" as normal progress and keeps waiting rather
                # than deciding whether the remainder is worth waiting
                # for.
                _why = (
                    f"The results below were finished and held for "
                    f"{int(DIGEST_HOLD_SECS)}s+ while the remaining "
                    f"agent(s) ran, so they are being delivered early "
                    f"as a PARTIAL result set. Synthesize what you can "
                    f"now; if a remaining agent never reports, work "
                    f"with what you have or tell the user.\n"
                    if _flush_only
                    else ""
                )
                announce = (
                    f"{SUBAGENT_BATCH_COMPLETION_PREFIX}\n"
                    f"Batch results {_chunk_k}/{_chunk_j} — "
                    f"{bp['done']} of {bp['total']} delivered, "
                    f"{_remaining} still running.\n"
                    f"{_why}"
                    f"Process these results now, but do NOT spawn new "
                    f"sub-agents yet — more result batches from this "
                    f"run are still arriving, and spawning now will "
                    f"interleave with them.\n"
                    f"{_footer}\n\n{_digest_body}"
                )
                # Mid-wave chunk: progress facts (delivered/running), no
                # tallies — mirrors the CHUNK regex the frontend demotes.
                sub_meta = wave_chunk_meta(
                    chunk=_chunk_k,
                    chunks=_chunk_j,
                    delivered=bp["done"],
                    total=bp["total"],
                    running=_remaining,
                )
                # Reset per-chunk buffers for the next chunk. (On the
                # final chunk bp was already popped from
                # _batch_progress above — nothing to reset.)
                bp["fail_lines"] = []
                bp["ok_lines"] = []
                bp["held_ok_deliveries"] = []
    return _WaveDigest(announce, sub_meta)
