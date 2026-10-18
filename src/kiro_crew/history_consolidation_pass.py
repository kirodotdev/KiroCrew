"""One consolidation pass end to end, and the billed LLM turn it runs on.

:meth:`_consolidate` snapshots a session's unconsolidated span, renders the prompt from
it and the current memory, runs one extraction turn through :meth:`_call_llm`, applies
the answer (history, preferences, projects, lessons, semantic and episodic memory, or one
atomic publication for a private member store), and then runs auto-skill detection. A
failed billed turn is charged through ``_note_failed_attempt``, and a finished span moves
the durable marker to the end of the PROMPTED prefix through ``_advance_history_marker``
(both stay on the class). Every
write is gated by ``memory.persistence_enabled`` and ``memory.writes_enabled``
(``_persistence_disabled``), checked before the pass is billed and again inside the
publication hold of the run's first commit. A pass claims the session's cross-process
consolidation lease (``history_consolidation_lease``) before its snapshot, keeps its
heartbeat while it runs, fences every durable write on the token and releases it in
``finally``; a busy session returns ``_CONSOLIDATION_BUSY``, a lost lease
``_CONSOLIDATION_REFUSED``. ``_facade_stream_and_collect_json`` reads ``kiro_crew.history`` at call
time, so a patch of ``kiro_crew.history.stream_and_collect_json`` reaches the turn.

Each function here whose first parameter is ``self`` is a ``HistoryConsolidator`` method:
the class binds it under the same name (``history_consolidation.py``), so it reads and
writes the consolidator's own fields and calls its siblings through ``self``.

Instance fields read: ``_archive_after_days``, ``_auto_skills_enabled``,
``_last_lifecycle``, ``_lesson_store``, ``_log``, ``_max_auto_skills``, ``_memory``,
``_migrated``, ``_running``, ``_seeded_keys``, ``_sessions``, ``_skills_loader``,
``_stale_after_days``, ``_vector_store``, ``on_abandoned``.

Instance fields written: ``_event_loop``, ``_last_lifecycle``, ``_running``.

Part of the ``kiro_crew.history_consolidation`` family: that module is the import path and
the patch surface; this owner imports only ``history_consolidation_span`` and
``history_consolidation_memory``.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import hashlib
import json
import time as _time
from collections.abc import Awaitable
from typing import TYPE_CHECKING, Any

from kiro_crew.executors import run_in_embed_pool
from kiro_crew.history_consolidation_memory import (
    _SEMANTIC_OMISSION_NOTICE,
    _bounded_semantic_table,
    _is_plausible_memory_file,
    _reroute_lesson_semantic_keys,
    _session_facets,
    _withhold_unseen_deletes,
)
from kiro_crew.history_consolidation_span import (
    _CONSOLIDATION_BUSY,
    _CONSOLIDATION_MAX_HEAD_SLICES,
    _CONSOLIDATION_REFUSED,
    AttemptedSpan,
    _consolidation_chunk,
    _consolidation_head_content,
    _ConsolidationNotDispatched,
    _ConsolidationRefusedSentinel,
    _fmt_message,
    _head_exceeds_budget,
    _head_slice_ceiling_reached,
    _LeaseLostMidRun,
    _lesson_extraction_disabled,
    _persistence_disabled,
    _PersistenceDisabledMidRun,
    _prompt_rows,
    _RunCommitState,
    _slice_head_for_budget,
)
from kiro_crew.history_projection import DISPLAY_ONLY_ROLES
from kiro_crew.lesson_validation import LESSON_APPLIES_INSTRUCTION
from kiro_crew.llm_helpers import ToolApprovalPolicy, background_turn
from kiro_crew.vector_memory_constants import (
    _MAX_EPISODIC_PER_CONSOLIDATION,
    _MAX_LESSONS_PER_CONSOLIDATION,
    _MAX_SEMANTIC_PER_CONSOLIDATION,
    _SEMANTIC_PROMPT_CAP_PER_CONSOLIDATION,
)

if TYPE_CHECKING:
    from kiro_crew.history_consolidation import HistoryConsolidator


def _facade_stream_and_collect_json(*args: Any, **kwargs: Any) -> Awaitable[dict | None]:
    from kiro_crew import history as history_facade

    return history_facade.stream_and_collect_json(*args, **kwargs)


async def _consolidate(
    self: HistoryConsolidator, key: str, include_history: bool = True
) -> _ConsolidationRefusedSentinel | None:
    """Run LLM consolidation for a session.

    Returns :data:`_CONSOLIDATION_REFUSED` when the retry-eligibility gate
    refuses the span, its transcript changes during extraction, or the
    pass loses its consolidation lease to a peer, and
    :data:`_CONSOLIDATION_BUSY` when another process already holds that
    lease; every other completion returns ``None``. Callers distinguish
    either from a completed pass with :func:`_no_pass_ran`, never against
    one instance. A changed source remains pending rather than consuming
    the failure/abandon budget of a different span.
    """
    # Capture the gateway loop so the thread-offloaded _process_auto_skills
    # can schedule the async dedupe judge back onto it.
    self._event_loop = asyncio.get_running_loop()
    # Flipped once the prompt actually reaches the provider, which is what
    # makes a failure expensive: everything before that point is free to
    # retry, everything after costs a turn that produced nothing durable.
    billed = False
    total = 0
    generation_at_snapshot = 0
    # Held from the acquisition below to the finally block. Declared out here
    # so the release is reachable from paths that return before it is taken.
    lease: str | None = None
    lease_heartbeat: asyncio.Future | None = None
    lease_lost = False
    owner = asyncio.current_task()

    def _abort_lost_lease() -> None:
        nonlocal lease_lost
        lease_lost = True
        if owner is not None:
            owner.cancel()

    # The span identity any failure charge is stamped with. Rebuilt from the
    # snapshot below; the zero value only ever reaches a charge if the snapshot
    # itself raised, and that path is not billed.
    # circular import: kiro_crew.history re-exports this module
    from kiro_crew.history import (
        TranscriptWithheld,
        is_incognito_transcript,
        transcript_lock_stems,
    )

    attempted = AttemptedSpan(0, 0, 0, 0)
    commit_state = _RunCommitState()
    # The output being published, named in the warning when a later hold is
    # refused after an earlier output committed (see the Withheld arm below).
    stage = "publication"
    try:
        # Persistence global switch, checked here as well as in the automatic
        # entry points so the manual triggers (POST /api/memory/consolidate,
        # ``kirocrew consolidate``) are covered too. The REFUSED sentinel
        # gives the entry-point done-callbacks the right semantics for free:
        # no pass ran, so offsets must not advance and throttles must not be
        # set.
        #
        # INSIDE the try, so the finally below clears self._running. The
        # entry points add the key before scheduling this task and their
        # done-callbacks never discard it, so returning ahead of the try
        # would strand the key and refuse every later consolidation for that
        # session — reachable when the switch is flipped off in the gap
        # between create_task and the task's first line.
        if _persistence_disabled():
            self._logger.info("consolidation skipped for %s: automatic memory writes are off", key)
            return _CONSOLIDATION_REFUSED

        from kiro_crew.execution_context import read_session_execution

        execution = await asyncio.to_thread(read_session_execution, key)
        if execution is not None and execution.memory_mode != "persistent":
            return _CONSOLIDATION_REFUSED

        metadata = await asyncio.to_thread(self._log.get_metadata, key)
        if isinstance(metadata, dict) and is_incognito_transcript(metadata.get("memory_mode")):
            return _CONSOLIDATION_REFUSED
        # Acquire before reading the authoritative span: a peer may have
        # completed it since scheduling, and busy callers need no snapshot.
        acquisition = asyncio.create_task(asyncio.to_thread(self._acquire_consolidation_lease, key))
        try:
            lease = await asyncio.shield(acquisition)
        except asyncio.CancelledError:
            # A worker cannot be cancelled mid-CAS. Recover its token so
            # finally releases a claim written during cancellation.
            lease = await acquisition
            raise
        if lease is None:
            self._logger.info("_consolidate skipped for %s: consolidation already in flight", key)
            return _CONSOLIDATION_BUSY
        # Every durable write from here on is fenced on this token: a peer
        # that takes the lease over while this pass runs (its renewals having
        # failed past the ceiling) refuses the remaining outputs instead of
        # publishing the span a second time.
        commit_state.lease_token = lease
        # Keep proving the holder is working for as long as the pass runs.
        # The ceiling measures silence, and everything after this line —
        # the prompt, the provider turn, the memory writes — is one stretch
        # of it with no natural place to stamp liveness.
        lease_heartbeat = asyncio.ensure_future(
            self._heartbeat_consolidation_lease(key, lease, _abort_lost_lease)
        )

        # Atomically snapshot the unconsolidated tail, the total message
        # count (the absolute offset handed to mark_consolidated below), and
        # the rotation generation under ONE lock hold. Reading them as
        # separate calls let an append trigger a rotation between them,
        # pairing a pre-rotation offset with a post-rotation generation —
        # mark_consolidated would then see matching generations and apply
        # the stale offset (retained-count fallback misses it too), silently
        # dropping messages from extraction. Offloaded to a worker thread:
        # _consolidate runs on the gateway event loop and _locked/file IO is
        # blocking (same rationale as the mark_consolidated offload below).
        # ``withhold_restricted``: the two privacy checks above read the line
        # BEFORE this snapshot, and a writer can tighten it in between (a
        # same-key hand-over landing a restricted tab's rows under a line
        # that was persistent a moment ago). The snapshot re-reads the line
        # under the same lock as the rows and refuses them together, so no
        # rows a restricted line governs ever reach the prompt below.
        # The lease is re-checked under the same lock hold, so the span
        # read here is one this pass still owns.
        def leased_snapshot():
            with self._log.locked_stems(transcript_lock_stems(key)):
                if lease is None or not self._lease_is_current(key, lease):
                    raise _LeaseLostMidRun(key)
                return self._log.snapshot_for_consolidation(key, withhold_restricted=True)

        try:
            (
                unconsolidated,
                total,
                generation_at_snapshot,
                sub_offset_at_snapshot,
            ) = await asyncio.to_thread(leased_snapshot)
        except TranscriptWithheld:
            return _CONSOLIDATION_REFUSED
        # Transcript caches may share nested message dictionaries with an
        # editor. Freeze the submitted evidence before awaiting the model.
        unconsolidated = copy.deepcopy(unconsolidated)
        if not unconsolidated:
            if key in self._seeded_keys:  # a finished seed stops being re-read
                self._forget_key(key)
            return None
        # Display-only rows (``notice``) are text drawn for the person
        # reading the transcript, not conversation: the Slack thread-parent
        # row is untrusted text whose only route to a model is a fenced block.
        # They never reach the prompt below, but they stay in
        # ``unconsolidated`` and ``total``, so a history pass can move
        # its offset past them. A span of nothing else has nothing to
        # learn from, so no model call is made; a history pass also marks
        # it consolidated, and a skill-detection pass leaves the offset
        # to that pass as every other early return here does.
        if not _prompt_rows(unconsolidated):
            if include_history:
                await asyncio.to_thread(
                    self._mark_consolidated_fenced, key, total, generation_at_snapshot, lease
                )
            return None
        # Retry-eligibility choke point: every entry point funnels through
        # this function, so a span inside its durable backoff is refused
        # here — before anything that can bill a provider turn — even if a
        # caller carries no pre-check of its own (a future entry point, or
        # a pre-check that raced the backoff being recorded). Callers keep
        # their cheaper pre-checks as scheduling short-circuits and UX (the
        # idle sweep's per-tick skip, maybe_consolidate's per-turn skip,
        # the dashboard trigger's 429); this gate is the enforcement that
        # holds when a new entry point forgets one. The count comes
        # from the atomic snapshot above — the same consistent read the
        # rest of this function uses — and retry_eligible costs one
        # metadata-line read, so no second transcript read lands on the
        # event loop. The refusal returns a sentinel rather than raising:
        # the finally block still releases self._running and the callers'
        # done-callbacks run normally (so the key is never stranded), while
        # the sentinel lets those callbacks tell a refusal from a completed
        # pass and leave their bookkeeping untouched.
        if not self.retry_eligible(key, message_count=total):
            self._logger.info("_consolidate refused for %s: consolidation retry backoff", key)
            return _CONSOLIDATION_REFUSED
        # Freeze the whole span identity from that one snapshot. The offset is
        # derived rather than returned because the snapshot slices at it
        # (``messages[offset:]``), so the subtraction is exact and comes from
        # the same lock hold — no second read that a concurrent rotation could
        # land between. A failure charge stamped with these values describes
        # what the turn attempted even if the file changed underneath it.
        offset = total - len(unconsolidated)
        # Bound what this pass prompts, and mark exactly that. History
        # consolidation owns a durable marker, so a bounded prompt is only
        # safe if the marker follows the prompt rather than the snapshot:
        # advancing to `total` after prompting a prefix is the same silent
        # loss the bound exists to prevent, just moved.
        #
        # Prefs-only passes keep the whole tail. Their window is tracked by
        # an in-memory offset that `maybe_consolidate`'s done-callback
        # advances to the count it scheduled against, with no channel back
        # from here — so bounding this prompt without also making that
        # offset follow it would drop the remainder from preference and
        # project extraction outright. Unbounded is the lesser fault while
        # that offset is a scheduling artifact rather than a durable marker.
        chunk = _consolidation_chunk(unconsolidated) if include_history else unconsolidated
        # Sub-message chunking. When history consolidation's bounded chunk is
        # a single message that alone exceeds the budget (the
        # returned-it-anyway case in _consolidation_chunk), prompting it whole
        # is rejected by any provider with a context ceiling, so the span
        # would only ever reach the abandon path and lose the whole message.
        # Instead slice a budget-sized, stable-boundary window of its content
        # from the durable sub-offset and prompt only that. The message
        # marker stays put until the LAST slice succeeds; a non-final slice
        # advances the durable sub-offset instead, so the next pass resumes
        # where this one stopped and the attempt cap can abandon at most one
        # slice rather than the message.
        #
        # Only history passes own the durable marker and sub-offset; a
        # prefs-only pass keeps the whole tail (see above), so it never slices.
        sub_offset_prompted = 0
        sub_slice_is_last = False
        is_sub_slice = False
        # The chunk is message-aligned and may carry display-only rows (a
        # ``notice`` such as an AutoNudge) alongside the one rendered head —
        # ``_consolidation_chunk`` keeps those rows inside the prefix even
        # though no prompt renders them. The slicing decision is about the
        # PROMPTED rows, so test ``_prompt_rows(chunk)``: a sole rendered head
        # that alone exceeds the budget is sliced regardless of how many
        # display-only rows sit beside it. Guarding on ``len(chunk) == 1``
        # instead let a notice next to an oversized head make the chunk two
        # rows long, skip slicing, prompt the whole head, and abandon the
        # entire message at the attempt cap without extraction — the exact
        # data loss this path exists to prevent.
        prompt_rows = _prompt_rows(chunk) if include_history else chunk
        if include_history and len(prompt_rows) == 1 and _head_exceeds_budget(prompt_rows[0]):
            # Per-head slice ceiling. Each pass slices one budget-sized piece
            # of an over-budget head and the next pass resumes at the durable
            # sub-offset, so a head near the transcript cap would slice into
            # ~160 sequential consolidation LLM calls, each holding up every
            # message behind it — the per-head cost would scale with the
            # message's size unbounded. Once the sub-offset has reached
            # ``_CONSOLIDATION_MAX_HEAD_SLICES`` whole spans into the head AND
            # more than one slice of content still remains, retire the
            # remainder the same bounded, FAIL-SAFE way the attempt cap
            # retires an unprocessable span: advance the marker past the
            # whole chunk, log the reason, and fire the user-facing abandon
            # notice — never silently (silently marking unread content
            # consolidated is the very bug this PR fixes). A head whose
            # remainder fits one more slice is NOT retired; it drains in full,
            # so no span that would fit a single extra slice is dropped
            # unread. The already-sliced prefix keeps whatever it extracted;
            # the tail behind the head consolidates on the next pass with its
            # own fresh budget.
            if _head_slice_ceiling_reached(prompt_rows[0], sub_offset_at_snapshot):
                stripped = _consolidation_head_content(prompt_rows[0])
                resume = (
                    sub_offset_at_snapshot if 0 <= sub_offset_at_snapshot < len(stripped) else 0
                )
                remainder = max(0, len(stripped) - resume)
                reason = (
                    f"per-head slice ceiling reached "
                    f"(~{_CONSOLIDATION_MAX_HEAD_SLICES} budget spans consolidated)"
                )
                self._logger.warning(
                    "Retiring consolidation head for %s after ~%d budget "
                    "spans (%s): the message's remaining %d characters are "
                    "marked consolidated WITHOUT a memory pass to bound the "
                    "per-head cost; the tail behind it consolidates next pass",
                    key,
                    _CONSOLIDATION_MAX_HEAD_SLICES,
                    reason,
                    remainder,
                )
                try:
                    await asyncio.to_thread(
                        self._mark_consolidated_fenced,
                        key,
                        offset + len(chunk),
                        generation_at_snapshot,
                        lease,
                    )
                except _LeaseLostMidRun:
                    raise
                except Exception:
                    self._logger.warning(
                        "Could not retire consolidation head for %s", key, exc_info=True
                    )
                    return _CONSOLIDATION_REFUSED
                callback = self.on_abandoned
                if callback is not None:
                    try:
                        callback(
                            key,
                            len(chunk),
                            reason,
                            char_count=remainder,
                            last_slice=True,
                            retired=True,
                        )
                    except Exception:
                        self._logger.debug(
                            "Abandoned-consolidation callback failed for %s",
                            key,
                            exc_info=True,
                        )
                return None
            is_sub_slice = True
            (
                sliced_head,
                sub_slice_start,
                sub_offset_prompted,
                sub_slice_is_last,
            ) = _slice_head_for_budget(prompt_rows[0], sub_offset_at_snapshot)
            # Keep the display-only rows in the span's prompted extent: the
            # marker must advance past them with the head on the final slice,
            # or they would be re-counted into the next chunk forever. Rebuild
            # the chunk as the display-only rows in their original order with
            # the single rendered head replaced by its slice.
            chunk = [
                sliced_head if m is prompt_rows[0] else m
                for m in chunk
                if m is prompt_rows[0] or m.get("role") in DISPLAY_ONLY_ROLES
            ]
            # The message marker advances past the WHOLE chunk prefix (head
            # plus the display-only rows carried with it) ONLY on the final
            # slice; a non-final slice leaves it at ``offset`` and records
            # progress through the sub-offset. ``sub_prompted`` carries the
            # char offset this slice reached so the retry accounting can tell
            # one slice of the head from the next and keep its cap through
            # message growth behind the head. ``sub_offset`` is the clamped
            # start the slice actually used, not the raw snapshot value.
            attempted = AttemptedSpan(
                total=total,
                generation=generation_at_snapshot,
                offset=offset,
                prompted=offset + (len(chunk) if sub_slice_is_last else 0),
                sub_offset=sub_slice_start,
                sub_prompted=sub_offset_prompted,
            )
        else:
            attempted = AttemptedSpan(
                total=total,
                generation=generation_at_snapshot,
                offset=offset,
                prompted=offset + len(chunk),
            )

        # Resolve the owning execution once. V2 learning requires its exact
        # database; only V1 retains Markdown and JSONL learning handles.
        from kiro_crew.context import store_of_session
        from kiro_crew.memory_stores import memory_store_version

        # Resolve through the same strict metadata reader as interactive
        # turns before any consolidation provider or memory write starts.
        def _resolve_memory_identity() -> tuple[str, bool]:
            store_name = (
                execution.store.legacy_name
                if execution is not None
                else store_of_session(self._log, key)
            )
            return store_name, bool(store_name and memory_store_version(store_name) == 2)

        store_name, member_memory = await asyncio.to_thread(_resolve_memory_identity)
        # V2 anchors are owner-managed essentials. Extract proposed facts
        # through the revision-aware structured path; never let a legacy
        # whole-file rewrite remove their rules or age out project guides.
        allow_markdown_updates = not self._migrated and not member_memory
        meta = metadata
        facets = _session_facets(meta, key)
        ws_name = meta.get("workspace")
        lessons_store = self._lesson_store
        if store_name:
            from kiro_crew.context import ContextBuilder

            vector_store = await ContextBuilder.ensure_store(store_name)
            memory = await asyncio.to_thread(ContextBuilder.get_memory_for, memory_store=store_name)
            lessons_store = (
                None
                if member_memory
                else await asyncio.to_thread(
                    ContextBuilder.get_lessons_for, memory_store=store_name
                )
            )
            # May be None when the store could not be stood up; the writes
            # below then skip the vector tier rather than falling back to the
            # global store. Losing a semantic row is recoverable, writing it
            # into another crew's memory is not.
        elif ws_name:
            from kiro_crew.context import ContextBuilder

            memory = await asyncio.to_thread(ContextBuilder.get_memory_for, ws_name)
            vector_store = self._vector_store
        else:
            memory = self._memory
            vector_store = self._vector_store

        source_id = ""
        if member_memory:
            if vector_store is None:
                raise RuntimeError("Member memory database is unavailable")
            # The receipt identity. For an ordinary message-aligned span this
            # is the pre-existing 5-element key, left byte-for-byte unchanged
            # so already-written receipts still match. A sub-slice of an
            # over-budget head shares (key, generation, offset) with every
            # OTHER slice of the same head, so without the sub-offset two
            # distinct slices would collide on one receipt — a slice-2 run
            # would replay slice-1's receipt and advance the wrong state.
            # Appending the sub-offset only when sub-slicing keeps the two
            # cases apart without churning the common case.
            source_id_parts: list = [
                key,
                generation_at_snapshot,
                attempted.offset,
                include_history,
                None if include_history else total,
            ]
            if is_sub_slice:
                source_id_parts.append(attempted.sub_offset)
            source_id = hashlib.sha256(json.dumps(source_id_parts).encode("utf-8")).hexdigest()
            committed = await asyncio.to_thread(vector_store.consolidation_receipt, source_id)
            if committed is not None:
                from kiro_crew.vector_memory import consolidation_source_digest

                count = committed["source_count"]
                # The committed run hashed exactly what it put in front of the
                # model: for a sub-slice that is the PROMPTED rows of the
                # chunk (``_prompt_rows`` drops the display-only ``notice``
                # rows ``_consolidation_chunk`` carries beside the sliced
                # head), for an ordinary span the plain prefix. Compare
                # against the SAME shape here, or a sub-slice receipt — whose
                # stored digest is over the prompted rows — could never be
                # recognised (``unconsolidated[:count]`` is the WHOLE head).
                # Hashing the prompted rows rather than the raw chunk also
                # keeps the receipt replayable when a one-time orphan
                # transcript migration later drops an older display-only
                # notice in at the marker: a row the prompt never rendered
                # must not change the fingerprint, or every replay raises
                # below and the head wedges in environment-failure backoff.
                replay_source = _prompt_rows(chunk) if is_sub_slice else unconsolidated
                if (
                    len(replay_source) < count
                    or await asyncio.to_thread(consolidation_source_digest, replay_source[:count])
                    != committed["source_digest"]
                ):
                    raise ValueError(
                        "Committed consolidation source changed before acknowledgement"
                    )
                if include_history:
                    if is_sub_slice:
                        # A sub-slice receipt. ``source_id`` folds the
                        # sub-offset in and the digest matched the same
                        # ``source_count``, so this replay's ``attempted``
                        # describes the SAME slice the committed run did.
                        # Re-apply its durable advance (the sub-offset for a
                        # non-final slice, the message marker for the final
                        # one) rather than a bare ``source_total``, which for
                        # a non-final slice is the unmoved message offset and
                        # would wrongly drop the sub-offset.
                        await self._advance_history_marker(key, attempted, lease)
                    else:
                        # The ordinary message-aligned receipt: advance to the
                        # EXACT prefix the committed run extracted
                        # (``source_total``), never this replay's possibly-
                        # larger snapshot — appended messages must stay unread.
                        await asyncio.to_thread(
                            self._mark_consolidated_fenced,
                            key,
                            committed["source_total"],
                            generation_at_snapshot,
                            lease,
                        )
                return None

        # The sub-slice chunk carries a head already image-ref-stripped over
        # the WHOLE head (:func:`_consolidation_head_content`); render it with
        # ``prestripped=True`` so ``_fmt_message`` does not strip a second
        # time. A re-strip of a slice that begins mid-fence misreads the
        # fenced block as prose and would replace a preserved path. A plain
        # message-aligned prefix (``is_sub_slice`` False) is a whole message
        # and is stripped once in ``_fmt_message`` as before.
        conversation = "\n".join(
            _fmt_message(m, prestripped=is_sub_slice) for m in _prompt_rows(chunk)
        )

        current_prefs, current_projects = await asyncio.to_thread(
            lambda: (memory.read_preferences(), memory.read_projects())
        )

        # Build prompt keys dynamically based on consolidation type
        keys: list[str] = []
        if include_history:
            keys.append(
                '"history_entry": A concise paragraph (2-5 sentences) summarizing '
                "what happened. Use local time [YYYY-MM-DD HH:MM]. Focus on "
                "decisions, outcomes, facts. Use user's real name if known."
            )

        # Structured memory extraction (when vector store is available).
        # Reads the RESOLVED store, never ``self._vector_store``: the rows
        # fetched here go into the prompt, and the prompt instructs the model
        # to update and delete them. Fetching globally would show crew B the
        # operator's semantic table and let its consolidation turn delete it.
        has_vector = vector_store is not None
        if has_vector and vector_store is not None:
            private_policy = getattr(vector_store, "algorithm_version", "v1") == "v2"
            # Offload: the fetch serializes on the store's _db_lock,
            # and this coroutine runs on the gateway event loop — a worker
            # holding the lock (backfill's FAISS rebuild, reconcile's bulk
            # UPDATEs) would otherwise block the whole loop here.
            current_semantic = await asyncio.to_thread(vector_store.get_all_semantic)

            def _prompt_value(e: dict) -> object:
                # A lesson row stores a mapping; the consolidation model
                # should read the rule prose, not a JSON envelope whose
                # field names dilute the instruction it is weighing.
                if str(e.get("key", "")).startswith("lesson."):
                    from kiro_crew.vector_memory import _lesson_display_text

                    try:
                        decoded = json.loads(e["value_json"])
                    except Exception:
                        return e["value_json"]
                    return _lesson_display_text(decoded) or e["value_json"]
                return e["value_json"]

            if private_policy:
                current_semantic = await run_in_embed_pool(
                    vector_store.with_record_metadata, current_semantic
                )
            semantic_entries = [
                {
                    "key": e["key"],
                    "value_json": _prompt_value(e),
                    "confidence": e["confidence"],
                    **(
                        {
                            "record_revision": e.get("record_revision", 0),
                            "metadata": e.get("record_metadata", {}),
                        }
                        if private_policy
                        else {}
                    ),
                }
                for e in current_semantic
            ]
            # The PROMPT's copy of the table is bounded; ``current_semantic``
            # itself stays whole, because the writers below read it as the
            # snapshot that decides update-versus-create and the revision a
            # correction is checked against. The keys the bounded copy shows
            # travel with it: a row the model never read is not its to
            # delete. Offloaded like the fetch: the fit is
            # found by re-serialising a table that can run to megabytes,
            # and this coroutine is on the gateway event loop.
            semantic_json, semantic_omitted, semantic_visible = await asyncio.to_thread(
                _bounded_semantic_table,
                current_semantic,
                semantic_entries,
                _SEMANTIC_PROMPT_CAP_PER_CONSOLIDATION,
            )
            if semantic_omitted:
                semantic_json += "\n" + _SEMANTIC_OMISSION_NOTICE.format(
                    count=semantic_omitted,
                    total=len(current_semantic),
                    limit=_SEMANTIC_PROMPT_CAP_PER_CONSOLIDATION,
                )
            semantic_fields = (
                '"delete": false, "metadata": {"category": "contact", "subject": "user", '
                '"predicate": "work_email", "scope": "", "source_ref": "brief evidence", '
                '"valid_from": "ISO date only if stated", "valid_until": "ISO date only if stated"}}. '
                "Metadata is optional: omit unknown dates/identity; do not guess them. "
                "For an explicit user replacement, add correction_quote containing an exact user quote "
                "with both old and new values and replacement language. Never fabricate a quote. "
                "Keep one atomic fact per key. The subject/predicate/scope tuple is exact identity, "
                "so retain it for updates and do not reuse it for a different person or project. "
                if private_policy
                else '"delete": false}. '
            )
            deletion_policy = (
                'To propose removal of a stale/invalidated key, set "delete": true. '
                "Changes to existing values and deletions require owner review; confidence does not authorize overwrites. "
                if private_policy
                else 'To DELETE a stale/invalidated key, set "delete": true (e.g. pet died → delete '
                "user.pet.name; project cancelled → delete project.x.status). "
            )
            keys.append(
                '"semantic": Array of structured facts to remember long-term. '
                'Each: {"key": "<dotted.key>", "value": <json_value>, "confidence": 0.0-1.0, '
                + semantic_fields
                + "Rules: keys must start with pref.*, project.*, or user.* "
                "(e.g. pref.color, user.favorite_language, project.name). "
                "confidence 1.0 = user stated, 0.8-0.9 = clearly implied, <0.8 = uncertain (rejected). "
                "value must be a JSON primitive (string, number, boolean) — NOT objects or arrays. "
                "IMPORTANT: Check existing semantic memory above. If a key already covers "
                "the same topic, UPDATE that key instead of creating a new one. "
                "Do NOT create near-duplicate keys (e.g. project.x.approach AND project.x.refined). "
                + deletion_policy
                + f"Max {_MAX_SEMANTIC_PER_CONSOLIDATION} items."
            )
            keys.append(
                '"episodic": Array of conversation fragments worth remembering. '
                'Each: {"text": "...", "tags": ["tag1"], "importance": 0.0-1.0}. '
                "Rules: text 10-2000 chars, factual. importance 0.9+ = critical, "
                "0.7-0.9 = useful, 0.5-0.7 = minor. Skip greetings/small talk. "
                f"Max {_MAX_EPISODIC_PER_CONSOLIDATION} items. "
                "IMPORTANT: Do NOT write simple key-value facts here that belong in semantic "
                "(e.g. 'Favorite color: blue'). Episodic is for events, decisions, and context "
                "— not for duplicating semantic facts."
            )

        # Markdown memory (backward compat when not migrated)
        if allow_markdown_updates:
            keys.append(
                '"preferences_update": The COMPLETE updated preferences file, '
                "included ONLY if the file needs changes. Merge duplicates, keep "
                "only newest if contradicted, remove stale one-off observations. "
                "Keep '# User Preferences' header. If nothing changed, OMIT this "
                "key entirely — never echo the file back and never answer with a "
                "placeholder word like 'unchanged': the value overwrites the file, "
                "so when present it must be the full file body."
            )
            keys.append(
                '"projects_update": The COMPLETE updated projects file, included '
                "ONLY if the file needs changes. Only active projects, remove "
                "stale entries, update facts. Keep '# Active Projects' header. "
                "If nothing changed, OMIT this key entirely — never echo the file "
                "back and never answer with a placeholder word like 'unchanged': "
                "the value overwrites the file, so when present it must be the "
                "full file body."
            )

        if include_history:
            keys.append(
                '"lessons": Array of corrections the user taught '
                '(e.g. "no, do X", "always Y", "never Z"). '
                'Each: {"rule": "...", "negative": "...", "category": "tool|preference|knowledge", '
                '"repo_scope": "...", "applies": "always|on_topic"}. '
                # The same instruction learn_add's schema carries, from one
                # constant, so both writers ask the model the same question.
                f'"applies": {LESSON_APPLIES_INSTRUCTION} '
                '"repo_scope" is OPTIONAL: include it ONLY when the correction is '
                "genuinely specific to one codebase worked on in the chat. Give a "
                "RELATIVE directory path inside that repository that is distinctive "
                'of it (e.g. "src/kiro_crew") -- never an absolute path, no leading '
                'slash or drive letter, no "." or ".." segments; a malformed scope '
                "drops the whole lesson. OMIT it when unsure and for anything that "
                "applies everywhere -- a scoped lesson is withheld outside its "
                "repository. "
                "Empty [] if no corrections. Skip general preferences. "
                f"Max {_MAX_LESSONS_PER_CONSOLIDATION} items. "
                "IMPORTANT: Only extract lessons that the user did NOT explicitly ask "
                "to remember (those are already saved via learn_add). Only extract "
                "implicit corrections the user made without saying 'remember'."
            )

        # ── Auto skill detection ──
        # Skill detection runs as its OWN pass (below, after the memory
        # writes) over a wider last-N window of the full session — not the
        # incremental history tail — so a reusable procedure that spans the
        # whole session is judged as a unit. It is therefore intentionally
        # absent from this consolidation prompt's keys.

        numbered = "\n\n".join(f"{i + 1}. {k}" for i, k in enumerate(keys))
        prompt_parts = [
            "You are a memory consolidation agent. Process this conversation "
            f"and return a JSON object with these keys:\n\n{numbered}",
        ]
        if has_vector:
            prompt_parts.append(f"\n\n## Current Semantic Memory\n{semantic_json}")
        if allow_markdown_updates or member_memory:
            if member_memory:
                prompt_parts.append(
                    "\n\nThe following member anchors are read-only. Do not return "
                    "preferences_update or projects_update; preserve the owner's core "
                    "guides. Extract new facts or proposed corrections into semantic "
                    "memory with their evidence instead."
                )
            prompt_parts.append(f"\n\n## Current Preferences\n{current_prefs or '(empty)'}")
            prompt_parts.append(f"\n\n## Current Projects\n{current_projects or '(empty)'}")
        prompt_parts.append(f"\n\n## Conversation to Process\n{conversation}")
        prompt_parts.append("\n\nRespond with ONLY valid JSON, no markdown fences.")
        prompt = "".join(prompt_parts)

        try:
            result = (
                await self._call_llm(prompt, memory_store=store_name, session_key=key)
                if member_memory
                else await self._call_llm(prompt, session_key=key)
            )
        except _ConsolidationNotDispatched as exc:
            # Nothing was sent, so nothing was billed. Charging this to the
            # attempt cap would let a handful of environment failures abandon
            # the span — writing the durable marker over messages no LLM has
            # ever read, which is the exact false-abandonment this accounting
            # exists to prevent. Arm the backoff only, so a broken host retries
            # on a widening interval instead of on every 60s tick.
            if include_history:
                await self._note_environment_failure(key, str(exc))
            return None
        billed = True
        if not result:
            # The turn reached the provider and produced nothing usable, so it
            # was spent while the marker below stays unwritten. Returning
            # silently would look like success to the done-callbacks, setting
            # the in-memory throttle while the durable count still says
            # unconsolidated: the span re-bills a full turn every idle window,
            # and immediately after every restart. Charge the attempt.
            if include_history and not await self._note_failed_attempt(
                key, attempted, "empty LLM result", lease
            ):
                # The lease went to a peer: nothing was charged, and this
                # is not a completed pass.
                return _CONSOLIDATION_REFUSED
            return None

        # A ``lesson.*`` key the model placed in the ``semantic`` array is moved
        # to ``lessons`` here, once, on the shared result -- before it branches
        # to the V1 (_write_structured_memory / _save_lessons) and V2
        # (apply_consolidation) write paths -- so a misfiled lesson gets the
        # lessons path's dedup and checks instead of a duplicate semantic row.
        result = _reroute_lesson_semantic_keys(result, self._logger)

        # A pending model result cannot authorize writes from a deleted or
        # edited transcript, or outrank a newer user turn. Appended assistant
        # replies may remain unconsolidated without invalidating the original
        # span. Recheck at the write boundary, before history or fact changes.
        # Same gate at the write boundary: a line tightened while the model
        # was thinking makes this result one derived from a restricted
        # transcript, so it is discarded -- nothing reaches history or memory.
        try:
            (
                latest,
                latest_total,
                latest_generation,
                latest_sub_offset,
            ) = await asyncio.to_thread(
                self._log.snapshot_for_consolidation, key, withhold_restricted=True
            )
        except TranscriptWithheld:
            self._logger.info(
                "Discarding consolidation result for %s: the transcript became restricted "
                "during extraction",
                key,
            )
            return _CONSOLIDATION_REFUSED
        if (
            latest_generation != generation_at_snapshot
            or latest_total < total
            or latest[: len(unconsolidated)] != unconsolidated
            or any(row.get("role") == "user" for row in latest[len(unconsolidated) :])
            # A sub-slice this run read started at ``sub_offset_at_snapshot``.
            # If the durable sub-offset moved while the model was thinking
            # (another process extracted an earlier slice of the same head),
            # this result describes a slice other than the one now at the
            # offset, so it must not advance anything. The generation check
            # above misses this: a sub-offset advance does not bump the
            # generation.
            or (is_sub_slice and latest_sub_offset != sub_offset_at_snapshot)
        ):
            self._logger.info(
                "Discarding consolidation result for %s: conversation changed during extraction",
                key,
            )
            return _CONSOLIDATION_REFUSED

        if member_memory:
            stage = "member memory"
            if vector_store is None:
                raise RuntimeError("Member memory database is unavailable")
            # The receipt must describe the span the model READ, not the
            # snapshot: the replay path above marks consolidated up to
            # ``committed["source_total"]`` once it recognises the digest,
            # so a receipt covering the whole tail advances the durable
            # marker past messages no model has seen.
            # Bound here because a nested def does not inherit the enclosing
            # scope's narrowing: the closure would see the un-narrowed
            # ``VectorMemoryStore | None`` and ``dict | None``.
            store = vector_store
            # The member store arbitrates the model's updates itself (a
            # conflicting one becomes an owner proposal); a delete of a row
            # the model never read is withheld here, the same fence the V1
            # writer applies, so the two paths cannot drift.
            consolidation, _ = _withhold_unseen_deletes(result, semantic_visible, self._logger)
            # memory.lesson_extraction_enabled covers the member store too:
            # its extracted lessons are dropped from the payload, and every
            # other update still applies.
            if _lesson_extraction_disabled():
                consolidation = {k: v for k, v in consolidation.items() if k != "lessons"}

            # ``source_total`` is the receipt-identity token and must satisfy
            # ``apply_consolidation``'s ``>= len(messages)`` guard. For an
            # ordinary span that is the prompted message boundary. For a
            # sub-slice the message marker does NOT move (``attempted.prompted``
            # is the unmoved ``offset``), so pass the head's own boundary
            # (``offset + len(chunk)`` == offset + 1) instead: it clears the
            # guard, is reproducible on replay, and is never read as the
            # durable advance — the sub-slice durable advance goes through
            # ``_advance_history_marker``, not ``source_total``.
            member_source_total = (
                attempted.offset + len(chunk) if is_sub_slice else attempted.prompted
            )
            # The receipt's digest must fingerprint exactly what the model
            # read. For a sub-slice that is the PROMPTED rows of the chunk,
            # not the raw chunk: ``_consolidation_chunk`` carries display-only
            # ``notice`` rows beside the sliced head, and no prompt renders
            # them. Hashing the raw chunk would fold those notices into the
            # fingerprint, so a later one-time orphan-transcript migration
            # that drops an older notice in at the marker would make the
            # replay digest diverge and wedge the head in backoff. The
            # message-marker accounting still spans the WHOLE chunk through
            # ``member_source_total`` above (prompted rows plus the
            # display-only rows carried with the head). An ordinary
            # message-aligned span keeps its receipt over ``chunk`` unchanged.
            receipt_messages = _prompt_rows(chunk) if is_sub_slice else chunk

            def _apply_member_consolidation() -> dict:
                with self._publication_hold_checked(key, commit_state) as publication:
                    applied = store.apply_consolidation(
                        source_id=source_id,
                        session_key=key,
                        source_total=member_source_total,
                        result=consolidation,
                        snapshot={row["key"]: row for row in current_semantic},
                        messages=receipt_messages,
                        facets=facets,
                    )
                    publication.mark_committed()
                    return applied

            await run_in_embed_pool(_apply_member_consolidation)

        if not member_memory and (entry := result.get("history_entry")):
            stage = "history entry"

            # Offloaded to a worker thread: append_history takes a blocking
            # advisory file lock (cross-process) and does synchronous file
            # IO, and _consolidate runs on the event loop thread (fired via
            # asyncio.create_task). Running it inline would let cross-process
            # lock contention stall the whole gateway loop.
            def _append_history_under_hold() -> None:
                with self._publication_hold_checked(key, commit_state) as publication:
                    memory.append_history(entry)
                    publication.mark_committed()

            await run_in_embed_pool(_append_history_under_hold)
            self._logger.info(
                "Consolidated %d of %d unconsolidated messages for %s",
                len(chunk),
                len(unconsolidated),
                key,
            )

        # Structured memory writes (Phase 2/3). Offloaded to a worker thread:
        # _write_structured_memory embeds each item via a blocking urllib call
        # to the in-process embedder, and _consolidate runs on the event loop thread (fired via
        # asyncio.create_task). Running it inline stalls the whole gateway loop
        # if the embedding endpoint is slow/hung (heartbeats, Slack, dashboard).
        if vector_store and not member_memory:
            stage = "structured memory"
            await run_in_embed_pool(
                self._write_structured_memory,
                result,
                key,
                vector_store,
                facets=facets,
                snapshot={row["key"]: row for row in current_semantic},
                visible_keys=semantic_visible,
                messages=unconsolidated,
                commit_state=commit_state,
            )

        # Legacy V1 Markdown writes (skip if migrated or private V2). Each value
        # replaces the whole file, so a non-file answer (e.g. the literal
        # word "unchanged") must be discarded, not written: once written it
        # re-enters the next prompt as the file's current content and primes
        # every later pass to repeat it (see _is_plausible_memory_file).
        if allow_markdown_updates:
            stage = "preferences"
            if prefs := result.get("preferences_update"):
                if not _is_plausible_memory_file(prefs, "# User Preferences"):
                    self._logger.warning(
                        "Discarding implausible preferences_update from "
                        "consolidation (missing '# User Preferences' header "
                        "or placeholder body; %d chars)",
                        len(prefs),
                    )
                elif prefs.strip() != current_prefs.strip():
                    # Offloaded like append_history above (blocking file
                    # I/O on the event loop thread). expected_baseline is
                    # the compare-and-swap guard: this whole-file result
                    # was merged from current_prefs, read BEFORE the
                    # minutes-long LLM call — if a dashboard Save landed
                    # in that window, writing would silently revert it,
                    # so the store skips the stale write instead.
                    def _write_preferences() -> bool:
                        with self._publication_hold_checked(key, commit_state) as publication:
                            wrote = memory.write_preferences(prefs, expected_baseline=current_prefs)
                            if wrote:
                                publication.mark_committed()
                            return wrote

                    wrote = await run_in_embed_pool(_write_preferences)
                    if not wrote:
                        self._logger.info(
                            "Consolidated preferences for %s discarded: file "
                            "changed during consolidation",
                            key,
                        )

            if projects := result.get("projects_update"):
                stage = "projects"
                if not _is_plausible_memory_file(projects, "# Active Projects"):
                    self._logger.warning(
                        "Discarding implausible projects_update from "
                        "consolidation (missing '# Active Projects' header "
                        "or placeholder body; %d chars)",
                        len(projects),
                    )
                elif projects.strip() != current_projects.strip():

                    def _write_projects() -> bool:
                        with self._publication_hold_checked(key, commit_state) as publication:
                            wrote = memory.write_projects(
                                projects, expected_baseline=current_projects
                            )
                            if wrote:
                                publication.mark_committed()
                            return wrote

                    wrote = await run_in_embed_pool(_write_projects)
                    if not wrote:
                        self._logger.info(
                            "Consolidated projects for %s discarded: file "
                            "changed during consolidation",
                            key,
                        )

        # Lesson extraction: _save_lessons calls write_lesson which embeds
        # each rule (+ up to 5 lazy backfills) via blocking urllib to Ollama.
        # Same rationale as _write_structured_memory above — must offload.
        if (
            not member_memory
            and (lessons_store or vector_store)
            and not _lesson_extraction_disabled()
            and (raw_lessons := result.get("lessons"))
        ):
            stage = "lessons"
            await run_in_embed_pool(
                self._save_lessons,
                raw_lessons,
                vector_store,
                lessons_store,
                facets=facets,
                key=key,
                commit_state=commit_state,
            )

        # Auto skill detection — a SEPARATE LLM pass over the full-session
        # window (see _run_skill_detection), not the incremental tail. Runs
        # only on history consolidation, guarded by flag + loader; failures
        # are logged, never fatal.
        # Auto-skills are shared install-wide. Private member experience
        # must not be published or contribute to another member's skills.
        if (
            not member_memory
            and include_history
            and self._auto_skills_enabled
            and self._skills_loader is not None
        ):
            try:
                await self._run_skill_detection(key, commit_state)
            except (_PersistenceDisabledMidRun, _LeaseLostMidRun):
                raise
            except Exception:
                self._logger.warning("Auto-skill detection failed for %s", key, exc_info=True)

        # Autonomous lifecycle: age-based archival must run even when this
        # pass created/approved no skill, otherwise skills never age out on
        # their own (create/approve were the only triggers). Consolidation is
        # the existing idle/periodic path; throttle to at most once/hour
        # across all sessions so frequent consolidations don't rescan the set.
        if (
            not member_memory
            and self._skills_loader is not None
            and (_time.time() - self._last_lifecycle) > 3600
        ):
            self._last_lifecycle = _time.time()
            try:
                await asyncio.to_thread(
                    self._skills_loader.run_skill_lifecycle,
                    max_auto_skills=self._max_auto_skills,
                    stale_after_days=self._stale_after_days,
                    archive_after_days=self._archive_after_days,
                )
            except Exception:
                self._logger.debug("Periodic skill lifecycle pass failed", exc_info=True)

        # Only advance the consolidated offset for history consolidation.
        # Prefs-only consolidation uses a separate in-memory offset.
        #
        # The marker lands at the end of the PROMPTED prefix, not at the
        # snapshot total: when the budget split the tail, everything past
        # the prefix is still unread and the next pass starts there. A
        # session whose tail outgrew one prompt therefore drains over
        # successive passes instead of losing the remainder in one write.
        #
        # mark_consolidated does a synchronous, fsync-backed rewrite of the
        # whole transcript (up to a couple of MB) behind the per-file lock.
        # _consolidate runs on the gateway event loop (fired via
        # asyncio.create_task), so offload the blocking rewrite to a worker
        # thread — otherwise a slow filesystem freezes the loop (heartbeats,
        # Slack, dashboard). Same rationale as the offloads above.
        #
        # _advance_history_marker routes a non-final sub-slice to the durable
        # sub-offset (the head is only partly extracted) and everything else
        # to the whole-message marker.
        if include_history:
            await self._advance_history_marker(key, attempted, lease)

    except TranscriptWithheld as exc:
        if not commit_state.committed:
            self._logger.info(
                "Discarding consolidation result for %s: the transcript became restricted "
                "during publication",
                key,
            )
            return _CONSOLIDATION_REFUSED
        # A durable output already landed under an earlier hold, so the
        # run's contract is the committed one (same latch as the persistence
        # flip above). Refusing here would leave the span pending, and the
        # idle sweep's re-run appends the history entry a second time:
        # append_history carries no receipt to recognise its own earlier
        # row. The outputs after the first are best-effort memory; a
        # restricted line must not be learned from and a lock that could
        # not be taken cannot be vouched for, so publication stops at this
        # stage and the span is marked so it is not re-run. A transcript
        # restricted mid-run is refused by the derivation seam on every
        # later run, so marking it loses nothing.
        self._logger.warning(
            "Consolidation for %s stopped at the %s stage after an earlier output "
            "committed (%s); the remaining outputs are skipped and the span is "
            "marked consolidated so the idle sweep does not repeat it",
            key,
            stage,
            exc,
        )
        if include_history:
            try:
                # The prompted prefix, not the snapshot: the messages past
                # it were never read, and the next pass prompts them. For a
                # non-final sub-slice this advances the durable sub-offset
                # rather than the message marker.
                await self._advance_history_marker(key, attempted, lease)
            except _LeaseLostMidRun:
                # Both fences failed: the line became restricted AND the lease
                # went to a peer. Nothing this pass still holds may be written,
                # and the span belongs to the new holder.
                self._logger.warning(
                    "Consolidation for %s lost its lease as the transcript became "
                    "restricted; the span is left to the new holder",
                    key,
                )
                return _CONSOLIDATION_REFUSED
            except Exception:
                # Same accounting as the arm below: an output committed, so
                # the turn was billed, and the unwritten marker must back
                # off rather than re-bill on the next tick.
                self._logger.exception("Consolidation failed for %s", key)
                await self._note_failed_attempt(
                    key, attempted, "exception after the LLM call", lease
                )
                raise
        return None
    except _PersistenceDisabledMidRun:
        self._logger.info(
            "Consolidation refused for %s: automatic memory writes turned off " "during the run",
            key,
        )
        return _CONSOLIDATION_REFUSED
    except _LeaseLostMidRun:
        # A peer claimed the lease while this pass was still running — its
        # renewals failed past the ceiling, and every write after that point
        # was refused at the fence. The span is the new holder's, so this is
        # a refusal: no attempt is charged here and no marker is advanced.
        self._logger.warning(
            "Consolidation for %s lost its lease to another process; the remaining "
            "outputs are not published",
            key,
        )
        return _CONSOLIDATION_REFUSED
    except asyncio.CancelledError:
        if lease_lost:
            return _CONSOLIDATION_REFUSED
        raise
    except Exception:
        self._logger.exception("Consolidation failed for %s", key)
        # Anything raised between the LLM call and mark_consolidated (memory
        # writes, lesson writes, the marker write itself) re-raises, so the
        # idle sweep's done-callback never sets its throttle and all of its
        # skip conditions are false again on the next 60s tick. Charging the
        # attempt here is what converts that tight loop into backoff.
        if billed and include_history:
            await self._note_failed_attempt(key, attempted, "exception after the LLM call", lease)
        elif include_history and attempted.total > 0:
            # Setup has a durable retry budget too, but consumes no model
            # attempt and must never abandon a span the model did not read.
            await self._note_environment_failure(key, "memory setup failed before the LLM call")
        raise
    finally:
        self._running.discard(key)
        if lease_heartbeat is not None:
            # The heartbeat stops before the release, so a renewal cannot
            # land after the lease is cleared and race a peer's fresh
            # acquisition into looking older than it is.
            lease_heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await lease_heartbeat
        if lease is not None:
            # Best effort, and deliberately still attempted while the task is
            # unwinding under cancellation: an unreleased lease excludes this
            # session's peers until the ceiling expires, which is the one
            # outcome worse than a slow release. asyncio.shield keeps the
            # to_thread call from being cancelled out from under us; if the
            # loop is already gone the lease is left to the liveness test,
            # which frees it as soon as this process is.
            try:
                await asyncio.shield(
                    asyncio.to_thread(self._release_consolidation_lease, key, lease)
                )
            except Exception:
                self._logger.debug(
                    "Consolidation lease release did not complete for %s", key, exc_info=True
                )
    return None


async def _call_llm(
    self: HistoryConsolidator, prompt: str, *, memory_store: str = "", session_key: str = ""
) -> dict | None:
    """Call LLM for extraction via a dedicated, idle-expirable session.

    Returns the parsed JSON dict, or ``None`` when the turn reached the
    provider but produced nothing usable (a failed or unparsable answer).

    Raises :class:`_ConsolidationNotDispatched` when the prompt never reached
    the provider at all — no session manager, or the background session could
    not be acquired because kiro-cli is missing, not logged in, or failing to
    start. That case is signalled separately rather than folded into ``None``
    because the two cost different things: a spent turn costs money and must
    consume the caller's retry budget, while a prompt that was never sent costs
    nothing and must not, or a broken host would abandon spans it never read.
    An exception (rather than a flag beside the result) is used so a caller
    cannot silently drop the distinction.

    Once ``stream_and_collect_json`` is entered the prompt counts as sent: a
    failure inside it may still have been billed, so it returns ``None`` and is
    charged rather than risk an unbounded retry loop over real spend.
    """
    if not self._sessions:
        self._logger.warning("LLM consolidation skipped — no session manager")
        raise _ConsolidationNotDispatched("no session manager")

    # Timing instrumentation measures both dedicated-session acquisition and
    # the LLM turn. Logged at DEBUG:
    # silent in normal operation, surfaced only when log_level is raised
    # to investigate a consolidation stall.
    t_start = _time.monotonic()
    async with contextlib.AsyncExitStack() as stack:
        try:
            from kiro_crew.session import CONSOLIDATE_KEY

            client = await stack.enter_async_context(
                background_turn(
                    self._sessions,
                    task="consolidation",
                    agent="kirocrew-lite",
                    session_key=CONSOLIDATE_KEY,
                    # This turn is spent on ONE session's transcript, so its
                    # cost belongs in that session's log even though the user
                    # never asked for it. Callers that pass no key -- skill
                    # detection, the dedupe and merge judges -- are not charged
                    # to a single session and record nothing.
                    crew_log_kind="memory_consolidation",
                    crew_log_session_key=session_key,
                    **({"memory_store": memory_store} if memory_store else {}),
                )
            )
        except Exception as exc:
            self._logger.warning(
                "Consolidation could not acquire its dedicated session "
                "after %.1fs — nothing was sent",
                _time.monotonic() - t_start,
                exc_info=True,
            )
            raise _ConsolidationNotDispatched("background session unavailable") from exc
        t_acquired = _time.monotonic()
        wait_s = t_acquired - t_start
        # Reject all tools: this is a text/JSON-only generation turn. kiro
        # scopes the kirocrew-lite session to tools:[] via set_mode, but the
        # Claude Code backend skips set_mode and injects the full
        # kirocrew-core/cron toolset — without REJECT_ALL a background
        # consolidation turn could fire side-effecting tools (send_message,
        # learn_add, spawn_run). REJECT_ALL keeps both providers tool-free.
        try:
            result = await _facade_stream_and_collect_json(
                client,
                prompt,
                approval_policy=ToolApprovalPolicy.REJECT_ALL,
                model_fallback=True,
                # History ABOUT a session: a path in it is quoted, never an
                # attachment, so no readable file may become an image block.
                allow_image=False,
            )
        except Exception:
            self._logger.warning(
                "LLM consolidation turn failed after %.1fs",
                _time.monotonic() - t_start,
                exc_info=True,
            )
            return None
        turn_s = _time.monotonic() - t_acquired
        self._logger.debug(
            "Consolidation LLM turn: wait=%.1fs turn=%.1fs total=%.1fs ok=%s",
            wait_s,
            turn_s,
            _time.monotonic() - t_start,
            result is not None,
        )
        return result
    # Reached only if the exit stack suppresses an exception. The prompt was
    # already sent by then, so the turn may have been billed: report it as a
    # spent-but-unusable result rather than a non-dispatch, which would hand
    # the caller a free retry it has not earned.
    return None
