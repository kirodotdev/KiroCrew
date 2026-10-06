"""The transcript budget one history consolidation prompt carries.

The unconsolidated tail has no natural ceiling: a session that goes a long time
between passes, or whose consolidation kept failing, accumulates every message
since the marker and renders all of them into a single prompt. Past some length
no provider accepts that prompt, so the span that most needs extracting becomes
the one that can never be extracted.

Bounding the prompt is only half of it. The durable ``last_consolidated`` marker
is what says a message has been through a memory pass, so it has to follow the
PROMPT rather than the snapshot — on the success path and on the abandon path
alike. These tests pin the split, the separator accounting, the oversized-message
case, and that no offset ever advances past what a model actually read.
"""

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew import history as history_mod
from kiro_crew import memory_stores
from kiro_crew.history import (
    _CONSOLIDATION_MAX_ATTEMPTS,
    _CONSOLIDATION_PROMPT_BUDGET_CHARS,
    ConversationLog,
    HistoryConsolidator,
    _consolidation_chunk,
    _fmt_message,
)

KEY = "dashboard:chat-bounds"


def _msg(content: str, role: str = "user") -> dict:
    return {"ts": "2026-09-09T12:00:00", "role": role, "content": content, "tools": []}


def _rendered_size(messages: list[dict]) -> int:
    """Exactly what the prompt builder produces for *messages*."""
    return len("\n".join(_fmt_message(m) for m in messages))


def _make_consolidator(log: ConversationLog, **kw: Any) -> HistoryConsolidator:
    memory = MagicMock()
    memory.read_preferences.return_value = ""
    memory.read_projects.return_value = ""
    kw.setdefault("history_idle_secs", 0)
    kw.setdefault("sessions", None)
    return HistoryConsolidator(log=log, memory=memory, migrated=True, **kw)


def _log_with(tmp_path, contents: list[str]) -> ConversationLog:
    log = ConversationLog(base_dir=tmp_path / "sessions")
    log.init()
    with history_mod.allow_on_loop_persist():
        for body in contents:
            log.append(KEY, "user", body)
    return log


class TestChunkFitsTheBudget:
    def test_a_tail_within_the_budget_is_returned_whole(self) -> None:
        messages = [_msg(f"m{i}") for i in range(20)]
        assert _consolidation_chunk(messages) == messages

    def test_an_oversized_tail_is_split_at_a_message_boundary(self) -> None:
        # Quarter-budget bodies: four fit, the fifth cannot.
        messages = [_msg("x" * (_CONSOLIDATION_PROMPT_BUDGET_CHARS // 4)) for _ in range(8)]
        chunk = _consolidation_chunk(messages)

        assert 0 < len(chunk) < len(messages)
        assert chunk == messages[: len(chunk)], "the chunk must be a prefix, in order"
        assert _rendered_size(chunk) <= _CONSOLIDATION_PROMPT_BUDGET_CHARS

    def test_adding_the_next_message_would_exceed_the_budget(self) -> None:
        """The split is at the LAST message that fits, not an early bail-out."""
        messages = [_msg("x" * (_CONSOLIDATION_PROMPT_BUDGET_CHARS // 4)) for _ in range(8)]
        chunk = _consolidation_chunk(messages)

        assert _rendered_size(messages[: len(chunk) + 1]) > _CONSOLIDATION_PROMPT_BUDGET_CHARS

    def test_the_joining_newlines_are_charged(self) -> None:
        """A budget blind to the separators overshoots by one byte per message.

        Sized so the bodies alone land exactly on the budget: the only thing that
        can push the rendered block over is the ``"\\n"`` between them, so a chunk
        that still contains every message proves the separator went uncounted.
        """
        singles = [_msg(f"m{i}") for i in range(64)]
        used = sum(len(_fmt_message(m)) for m in singles)
        envelope = len(_fmt_message(_msg("")))
        slack = _CONSOLIDATION_PROMPT_BUDGET_CHARS - used - envelope
        assert slack > 0, "fixture must leave room for a filler message"
        messages = singles + [_msg("x" * slack)]
        # Rendered messages alone are exactly at the ceiling; the 64 separators
        # between them are not, so only a separator-blind budget keeps them all.
        assert sum(len(_fmt_message(m)) for m in messages) == _CONSOLIDATION_PROMPT_BUDGET_CHARS

        chunk = _consolidation_chunk(messages)

        assert len(chunk) < len(messages)
        assert _rendered_size(chunk) <= _CONSOLIDATION_PROMPT_BUDGET_CHARS


class TestAnOversizedMessageStillMakesProgress:
    def test_a_first_message_over_the_budget_is_prompted_alone(self) -> None:
        """Refusing it would stall the session — and everything behind it — forever.

        Its size is a permanent property of the transcript, so no amount of
        waiting changes the verdict. Sending it is no worse than the unbounded
        prompt the budget replaces, and it terminates through the ordinary
        attempt cap.
        """
        huge = _msg("x" * (_CONSOLIDATION_PROMPT_BUDGET_CHARS * 3))
        messages = [huge, _msg("small")]

        assert _consolidation_chunk(messages) == [huge]

    def test_the_chunk_is_never_empty(self) -> None:
        """An empty chunk would prompt nothing and mark nothing: a dead pass."""
        for width in (
            1,
            _CONSOLIDATION_PROMPT_BUDGET_CHARS,
            _CONSOLIDATION_PROMPT_BUDGET_CHARS * 10,
        ):
            assert _consolidation_chunk([_msg("x" * width)])


class TestTheMarkerFollowsThePrompt:
    @pytest.mark.asyncio
    async def test_only_the_prompted_prefix_is_marked_consolidated(self, tmp_path) -> None:
        log = _log_with(tmp_path, ["x" * (_CONSOLIDATION_PROMPT_BUDGET_CHARS // 2)] * 6)
        c = _make_consolidator(log)
        before = log.unconsolidated_count(KEY)

        with patch.object(c, "_call_llm", AsyncMock(return_value={"history_entry": "e"})):
            await c._consolidate(KEY, include_history=True)

        after = log.unconsolidated_count(KEY)
        assert 0 < after < before, "the unprompted tail must survive a bounded pass"

    @pytest.mark.asyncio
    async def test_the_prompt_carries_only_the_messages_that_are_marked(self, tmp_path) -> None:
        """The two must agree, or the marker retires a message no model read."""
        log = _log_with(tmp_path, [f"body-{i}-" + "x" * 40_000 for i in range(6)])
        c = _make_consolidator(log)
        before = log.unconsolidated_count(KEY)
        call = AsyncMock(return_value={"history_entry": "e"})

        with patch.object(c, "_call_llm", call):
            await c._consolidate(KEY, include_history=True)

        prompt = call.await_args.args[0]
        marked = before - log.unconsolidated_count(KEY)
        assert marked > 0
        for i in range(marked):
            assert f"body-{i}-" in prompt, "a marked message was never prompted"
        for i in range(marked, before):
            assert f"body-{i}-" not in prompt, "an unmarked message was prompted"

    @pytest.mark.asyncio
    async def test_successive_passes_drain_the_tail(self, tmp_path) -> None:
        """Bounded passes must reach the end, not stall partway."""
        log = _log_with(tmp_path, ["x" * (_CONSOLIDATION_PROMPT_BUDGET_CHARS // 2)] * 8)
        c = _make_consolidator(log)

        passes = 0
        with patch.object(c, "_call_llm", AsyncMock(return_value={"history_entry": "e"})):
            while log.unconsolidated_count(KEY) and passes < 20:
                before = log.unconsolidated_count(KEY)
                await c._consolidate(KEY, include_history=True)
                assert log.unconsolidated_count(KEY) < before, "a pass consolidated nothing"
                passes += 1

        assert log.unconsolidated_count(KEY) == 0
        assert passes > 1, "fixture no longer exercises the split"


class TestAbandonMarksOnlyWhatWasPrompted:
    @pytest.mark.asyncio
    async def test_the_unprompted_tail_survives_an_abandoned_span(self, tmp_path) -> None:
        """The cap retires the prefix that failed, not the tail behind it.

        Marking the whole snapshot would discard messages that were never in any
        prompt — the same silent loss the budget exists to prevent, arriving
        through the failure path instead of the success path.
        """
        log = _log_with(tmp_path, ["x" * (_CONSOLIDATION_PROMPT_BUDGET_CHARS // 2)] * 6)
        c = _make_consolidator(log)
        before = log.unconsolidated_count(KEY)
        with history_mod.allow_on_loop_persist():
            log.update_metadata(
                KEY,
                {
                    "consolidation_attempts": _CONSOLIDATION_MAX_ATTEMPTS - 1,
                    "consolidation_retry_at": 0.0,
                },
            )

        with patch.object(c, "_call_llm", AsyncMock(return_value=None)):
            await c._consolidate(KEY, include_history=True)

        after = log.unconsolidated_count(KEY)
        assert 0 < after < before, "the abandon marker must cover the prompted prefix only"

    @pytest.mark.asyncio
    async def test_an_abandoned_prefix_lets_the_tail_consolidate(self, tmp_path) -> None:
        """Abandoning is progress, not a dead end: the next pass starts after it."""
        log = _log_with(tmp_path, ["x" * (_CONSOLIDATION_PROMPT_BUDGET_CHARS // 2)] * 6)
        c = _make_consolidator(log)
        with history_mod.allow_on_loop_persist():
            log.update_metadata(
                KEY,
                {
                    "consolidation_attempts": _CONSOLIDATION_MAX_ATTEMPTS - 1,
                    "consolidation_retry_at": 0.0,
                },
            )
        with patch.object(c, "_call_llm", AsyncMock(return_value=None)):
            await c._consolidate(KEY, include_history=True)
        remaining = log.unconsolidated_count(KEY)
        assert remaining

        # The abandon write clears the accounting, so the next span starts with
        # its own budget rather than inheriting the failed one's.
        assert log.consolidation_retry_state(KEY) == (0, 0.0)
        with patch.object(c, "_call_llm", AsyncMock(return_value={"history_entry": "e"})):
            await c._consolidate(KEY, include_history=True)

        assert log.unconsolidated_count(KEY) < remaining


class TestPrefsPassesKeepTheWholeTail:
    @pytest.mark.asyncio
    async def test_a_prefs_only_pass_prompts_every_unconsolidated_message(self, tmp_path) -> None:
        """Its window is a scheduling artifact, not a durable marker.

        ``maybe_consolidate``'s done-callback advances an in-memory offset to the
        count it scheduled against, with no channel back from the pass. Bounding
        this prompt without also making that offset follow the bound would drop
        the remainder from preference and project extraction outright, so the
        unbounded prompt is the lesser fault until the offset is durable.
        """
        log = _log_with(tmp_path, [f"body-{i}-" + "x" * 40_000 for i in range(6)])
        c = _make_consolidator(log)
        call = AsyncMock(return_value={})

        with patch.object(c, "_call_llm", call):
            await c._consolidate(KEY, include_history=False)

        prompt = call.await_args.args[0]
        for i in range(6):
            assert f"body-{i}-" in prompt
        assert log.unconsolidated_count(KEY) == 6, "a prefs pass must not move the marker"


class TestSpanIdentityIsUnchangedByTheBound:
    @pytest.mark.asyncio
    async def test_the_attempt_stamp_still_describes_the_whole_transcript(self, tmp_path) -> None:
        """The cap holds only while the stamped extent stays put.

        ``total`` is what the retry accounting compares the live transcript
        against; stamping the prompted prefix instead would read as growth on
        every later check and hand a failing span an unlimited supply of billed
        retries.
        """
        log = _log_with(tmp_path, ["x" * (_CONSOLIDATION_PROMPT_BUDGET_CHARS // 2)] * 6)
        c = _make_consolidator(log)
        total = log.consolidation_counts(KEY)[0]

        with patch.object(c, "_call_llm", AsyncMock(return_value=None)):
            await c._consolidate(KEY, include_history=True)

        meta = log.get_metadata(KEY)
        assert int(meta["consolidation_attempts_count"]) == total
        assert (
            log.consolidation_retry_state(KEY, total)[0] == 1
        ), "the charge must still be attributed to this span"


class TestTheBudgetIsNotABehaviourChangeForOrdinarySessions:
    @pytest.mark.asyncio
    async def test_a_short_tail_consolidates_in_one_pass(self, tmp_path) -> None:
        log = _log_with(tmp_path, [f"m{i}" for i in range(12)])
        c = _make_consolidator(log)

        with patch.object(c, "_call_llm", AsyncMock(return_value={"history_entry": "e"})):
            await c._consolidate(KEY, include_history=True)

        assert log.unconsolidated_count(KEY) == 0
        assert log.consolidation_retry_state(KEY) == (0, 0.0)
        c._last_activity[KEY] = time.time() - 10
        c._tasks.clear()
        c.check_idle_sessions()
        assert not c._tasks


class TestABoundedAttemptIsNotReleasedByGrowth:
    """Appending messages cannot change a prefix that was already prompted.

    The attempt cap is what abandons a span that keeps failing. It is scoped to
    the content it measured, and growth releases it so one transient
    marker-write failure cannot refuse a session forever. Under the budget that
    scoping needs the prompted boundary too: a permanently over-budget head
    message renders the same chunk on every pass, so counting the turns arriving
    behind it as new content would reset the attempts before they could ever
    reach the cap — a session still receiving turns would be re-billed on every
    idle window, forever.
    """

    @pytest.mark.asyncio
    async def test_growth_keeps_the_attempt_charged_to_a_bounded_span(self, tmp_path) -> None:
        log = _log_with(tmp_path, ["x" * (_CONSOLIDATION_PROMPT_BUDGET_CHARS // 2)] * 6)
        c = _make_consolidator(log)

        with patch.object(c, "_call_llm", AsyncMock(return_value=None)):
            await c._consolidate(KEY, include_history=True)

        meta = log.get_metadata(KEY)
        assert int(meta["consolidation_attempts_prompted"]) < int(
            meta["consolidation_attempts_count"]
        ), "fixture no longer produces a bounded attempt"
        assert log.consolidation_retry_state(KEY, 6)[0] == 1

        with history_mod.allow_on_loop_persist():
            for _ in range(4):
                log.append(KEY, "user", "a turn arriving behind the blocked head")

        grown = log.consolidation_counts(KEY)[0]
        assert grown > 6
        assert (
            log.consolidation_retry_state(KEY, grown)[0] == 1
        ), "growth behind a prompted prefix is not new content for that prefix"

    @pytest.mark.asyncio
    async def test_growth_still_releases_an_unbounded_span(self, tmp_path) -> None:
        """The rescue the extent test exists for is untouched for whole tails."""
        log = _log_with(tmp_path, [f"m{i}" for i in range(12)])
        c = _make_consolidator(log)

        with patch.object(c, "_call_llm", AsyncMock(return_value=None)):
            await c._consolidate(KEY, include_history=True)

        meta = log.get_metadata(KEY)
        assert int(meta["consolidation_attempts_prompted"]) == int(
            meta["consolidation_attempts_count"]
        ), "a short tail must be prompted whole"
        assert log.consolidation_retry_state(KEY, 12)[0] == 1

        with history_mod.allow_on_loop_persist():
            for _ in range(3):
                log.append(KEY, "user", "new content the failing turns never saw")

        assert (
            log.consolidation_retry_state(KEY, log.consolidation_counts(KEY)[0])[0] == 0
        ), "a grown transcript is a different span for an unbounded attempt"

    @pytest.mark.asyncio
    async def test_an_over_budget_head_reaches_the_cap_in_a_growing_session(self, tmp_path) -> None:
        """The termination argument for an oversized head under sub-chunking.

        A head larger than one budget is sliced rather than abandoned whole, and
        each slice gets its own attempt budget. With the provider
        rejecting every attempt the cap abandons ONE slice and advances the
        durable sub-offset, so the next pass continues from the next slice and the
        head drains slice-by-slice instead of being lost in one write. The turns
        arriving behind the head still never reset a slice's budget — the
        bounded-within-message accounting keeps the cap across appends.
        """
        log = _log_with(
            tmp_path,
            ["x" * (_CONSOLIDATION_PROMPT_BUDGET_CHARS * 2)] + [f"tail-{i}" for i in range(3)],
        )
        c = _make_consolidator(log)

        # A 2x-budget head needs two slices. Run enough passes (cap per slice,
        # plus margin) that both slices reach the cap; each pass clears only the
        # deadline and appends a turn, exactly as a live session would.
        with patch.object(c, "_call_llm", AsyncMock(return_value=None)):
            for _ in range(_CONSOLIDATION_MAX_ATTEMPTS * 3):
                if log.get_metadata(KEY).get("last_consolidated"):
                    break
                with history_mod.allow_on_loop_persist():
                    log.update_metadata(KEY, {"consolidation_retry_at": 0.0})
                    log.append(KEY, "user", "another turn while the head is stuck")
                await c._consolidate(KEY, include_history=True)

        # The head is fully given up only once its LAST slice is abandoned, at
        # which point the message marker moves past it (and the sub-offset is
        # cleared). Reaching here means the loop terminated — the attempts were
        # not reset forever by the turns arriving behind the head.
        meta = log.get_metadata(KEY)
        assert meta.get("last_consolidated") == 1, (
            "the head drains slice-by-slice and the marker moves past it exactly "
            "once, after the final slice is abandoned"
        )
        assert not meta.get("consolidation_sub_offset"), (
            "the sub-offset is cleared once the marker moves past the whole head"
        )
        assert log.unconsolidated_count(KEY), "the tail behind the head must survive"

    @pytest.mark.asyncio
    async def test_an_over_budget_head_abandons_one_slice_at_a_time(self, tmp_path) -> None:
        """The cap abandons ONE slice, not the whole head.

        A head twice the budget is sliced in two. With every attempt rejected,
        the first slice reaches the cap and is abandoned: the durable sub-offset
        advances past it, the message marker stays put, and the next pass renders
        the SECOND slice. So one over-budget message costs at most one slice of
        loss per cap, never the whole message in a single write.
        """
        log = _log_with(
            tmp_path,
            ["x" * (_CONSOLIDATION_PROMPT_BUDGET_CHARS * 2)],
        )
        c = _make_consolidator(log)

        # Drive the first slice to its cap (every attempt rejected).
        with patch.object(c, "_call_llm", AsyncMock(return_value=None)):
            for _ in range(_CONSOLIDATION_MAX_ATTEMPTS):
                with history_mod.allow_on_loop_persist():
                    log.update_metadata(KEY, {"consolidation_retry_at": 0.0})
                await c._consolidate(KEY, include_history=True)

        meta = log.get_metadata(KEY)
        # The marker has NOT moved past the message — only the sub-offset advanced.
        assert meta.get("last_consolidated", 0) == 0, "the whole head was not abandoned"
        first_slice_end = int(meta["consolidation_sub_offset"])
        assert 0 < first_slice_end < _CONSOLIDATION_PROMPT_BUDGET_CHARS * 2, (
            "the sub-offset advanced past exactly the first slice"
        )
        # The abandoned-slice budget was cleared, so the next slice starts fresh.
        assert log.consolidation_retry_state(KEY, 1)[0] == 0

        # The next pass renders a slice STARTING where the first ended (the
        # durable sub-offset), still bounded — a 2x-budget head needs more than
        # two slices because the envelope is charged against each, so the second
        # slice is not necessarily the last. Capture its prompt and compare it to
        # the slice the helper produces from the sub-offset.
        from kiro_crew.history_consolidation import _slice_head_for_budget

        captured: dict = {}

        async def _capture(prompt, *, memory_store="", session_key=""):  # noqa: ANN001
            captured["prompt"] = prompt
            return None

        with patch.object(c, "_call_llm", AsyncMock(side_effect=_capture)):
            with history_mod.allow_on_loop_persist():
                log.update_metadata(KEY, {"consolidation_retry_at": 0.0})
            await c._consolidate(KEY, include_history=True)

        head = log.snapshot_for_consolidation(KEY)[0][0]
        expected_slice, start, _end, _last = _slice_head_for_budget(head, first_slice_end)
        assert start == first_slice_end, "the second slice begins at the durable sub-offset"
        assert expected_slice["content"] and expected_slice["content"] in captured["prompt"], (
            "the second pass resumes slicing from the durable sub-offset"
        )
        assert len(expected_slice["content"]) < _CONSOLIDATION_PROMPT_BUDGET_CHARS, (
            "a resumed slice's content is within one budget, not the whole head"
        )
        from kiro_crew.history import _fmt_message

        assert len(_fmt_message(expected_slice)) <= _CONSOLIDATION_PROMPT_BUDGET_CHARS, (
            "the rendered slice (content plus envelope) fits the budget"
        )


class TestSubChunkingDrainsAnOversizedHead:
    """A single message larger than the budget is extracted slice by
    slice over successive passes, with a durable sub-offset, instead of being
    sent whole (and rejected) or abandoned unread.
    """

    @pytest.mark.asyncio
    async def test_a_head_three_times_the_budget_is_drained_over_three_passes(
        self, tmp_path
    ) -> None:
        """The acceptance case: each pass's prompt is within the budget, and the
        message marker moves past the head only after the final slice.
        """
        log = _log_with(tmp_path, ["y" * (_CONSOLIDATION_PROMPT_BUDGET_CHARS * 3)])
        c = _make_consolidator(log)

        prompts: list[str] = []

        async def _ok(prompt, *, memory_store="", session_key=""):  # noqa: ANN001
            prompts.append(prompt)
            return {"history_entry": "e"}

        passes = 0
        with patch.object(c, "_call_llm", AsyncMock(side_effect=_ok)):
            while log.unconsolidated_count(KEY) and passes < 10:
                with history_mod.allow_on_loop_persist():
                    log.update_metadata(KEY, {"consolidation_retry_at": 0.0})
                before_marker = log.get_metadata(KEY).get("last_consolidated", 0)
                before_sub = log.get_metadata(KEY).get("consolidation_sub_offset", 0)
                await c._consolidate(KEY, include_history=True)
                passes += 1
                meta = log.get_metadata(KEY)
                marker = meta.get("last_consolidated", 0)
                still_pending = log.unconsolidated_count(KEY)
                if still_pending:
                    # A non-final slice: the marker did NOT move, the sub-offset
                    # advanced, and the slice prompted was within the budget.
                    assert marker == before_marker, "the marker must not move mid-message"
                    assert (
                        meta.get("consolidation_sub_offset", 0) > before_sub
                    ), "a non-final slice advances the durable sub-offset"

        # Drained in more than one pass (so slicing really happened) and the
        # marker moved past the one message exactly once, at the end.
        assert passes >= 3, "a 3x-budget head needs at least three slices"
        assert log.unconsolidated_count(KEY) == 0
        assert log.get_metadata(KEY).get("last_consolidated") == 1
        assert not log.get_metadata(KEY).get("consolidation_sub_offset")
        # Every slice prompted a budget-sized window: no single pass sent the
        # whole over-budget head.
        for p in prompts:
            assert len(p) < _CONSOLIDATION_PROMPT_BUDGET_CHARS * 3

    @pytest.mark.asyncio
    async def test_a_rotation_between_passes_discards_the_sub_offset(self, tmp_path) -> None:
        """A rotation (generation bump) invalidates a mid-message sub-offset, so
        the head is re-sliced from its start under the new generation rather than
        resumed at an offset describing superseded content.
        """
        log = _log_with(tmp_path, ["z" * (_CONSOLIDATION_PROMPT_BUDGET_CHARS * 3)])
        c = _make_consolidator(log)

        # One successful slice advances the durable sub-offset without moving the
        # message marker.
        with patch.object(
            c, "_call_llm", AsyncMock(return_value={"history_entry": "e"})
        ):
            with history_mod.allow_on_loop_persist():
                log.update_metadata(KEY, {"consolidation_retry_at": 0.0})
            await c._consolidate(KEY, include_history=True)

        meta = log.get_metadata(KEY)
        sub = int(meta.get("consolidation_sub_offset", 0))
        assert sub > 0, "the first slice left a durable sub-offset to resume from"
        gen_before = int(meta.get("rotation_generation", 0) or 0)

        # Simulate a rotation: bump the generation the way _maybe_rotate does.
        with history_mod.allow_on_loop_persist():
            log.update_metadata(KEY, {"rotation_generation": gen_before + 1})

        # The snapshot's sub-offset is now 0 (its stamp does not match the
        # live generation), so the next pass re-slices the head from its start.
        _rows, _total, gen_now, sub_now = log.snapshot_for_consolidation(KEY)
        assert gen_now == gen_before + 1
        assert sub_now == 0, "a rotation discards the mid-message sub-offset"

        captured: dict = {}

        async def _capture(prompt, *, memory_store="", session_key=""):  # noqa: ANN001
            captured["prompt"] = prompt
            return {"history_entry": "e"}

        with patch.object(c, "_call_llm", AsyncMock(side_effect=_capture)):
            with history_mod.allow_on_loop_persist():
                log.update_metadata(KEY, {"consolidation_retry_at": 0.0})
            await c._consolidate(KEY, include_history=True)

        head = log.snapshot_for_consolidation(KEY)[0][0]
        from kiro_crew.history_consolidation import _slice_head_for_budget

        first_slice_again, start, _end, _last = _slice_head_for_budget(head, 0)
        assert start == 0, "the head is re-sliced from its start, not resumed mid-way"
        assert first_slice_again["content"] in captured["prompt"]


class TestConsolidateNowDrainsTheTail:
    """The CLI has no sweep behind it: its process exits when the call returns.

    Every in-gateway entry point can stop after one bounded pass because the next
    turn, the idle sweep or a session-end hook fires the next one. ``kirocrew
    consolidate`` cannot, so a single pass would report a tail larger than the
    budget as consolidated while most of it was never read.
    """

    @pytest.mark.asyncio
    async def test_a_tail_larger_than_one_budget_is_drained(self, tmp_path) -> None:
        log = _log_with(tmp_path, ["x" * (_CONSOLIDATION_PROMPT_BUDGET_CHARS // 2)] * 8)
        c = _make_consolidator(log)
        call = AsyncMock(return_value={"history_entry": "e"})

        with patch.object(c, "_call_llm", call):
            assert await c.consolidate_now(KEY) is True

        assert log.unconsolidated_count(KEY) == 0
        assert call.await_count > 1, "fixture no longer exercises the split"

    @pytest.mark.asyncio
    async def test_a_pass_that_makes_no_progress_ends_the_loop(self, tmp_path) -> None:
        log = _log_with(tmp_path, ["x" * (_CONSOLIDATION_PROMPT_BUDGET_CHARS // 2)] * 8)
        c = _make_consolidator(log)
        stalled = AsyncMock(return_value=None)

        with patch.object(c, "_consolidate", stalled):
            assert await c.consolidate_now(KEY) is True

        assert stalled.await_count == 1, "repeating a pass that moved nothing is a spin"
        assert log.unconsolidated_count(KEY) == 8

    @pytest.mark.asyncio
    async def test_a_first_pass_refusal_is_reported_as_a_skip(self, tmp_path) -> None:
        log = _log_with(tmp_path, [f"m{i}" for i in range(4)])
        c = _make_consolidator(log)
        with history_mod.allow_on_loop_persist():
            log.update_metadata(
                KEY,
                {
                    "consolidation_attempts": 1,
                    "consolidation_retry_at": time.time() + 3600,
                },
            )

        with patch.object(c, "_call_llm", AsyncMock(return_value={"history_entry": "e"})):
            assert await c.consolidate_now(KEY) is False

        assert log.unconsolidated_count(KEY) == 4

    @pytest.mark.asyncio
    async def test_a_tail_that_turns_sensitive_mid_drain_is_never_prompted(self, tmp_path) -> None:
        """The tail a pass prompts is not the tail the pre-check cleared.

        A live session keeps appending while the drain runs, so a sensitive tool
        event can land between passes. The pre-check before the loop clears the
        transcript as it stands at that moment; the second pass prompts the
        transcript as it has since grown.
        """
        # Sized so one pass consumes several messages: the sensitive append has
        # to leave net progress behind it, or the loop exits on the no-progress
        # branch and never reaches a second pass at all.
        log = _log_with(tmp_path, ["x" * (_CONSOLIDATION_PROMPT_BUDGET_CHARS // 5)] * 12)
        c = _make_consolidator(log)
        real = c._consolidate
        prompts: list[str] = []
        leaked = False

        async def _leak_between_passes(key: str, include_history: bool = True):
            nonlocal leaked
            outcome = await real(key, include_history=include_history)
            if not leaked:
                leaked = True
                with history_mod.allow_on_loop_persist():
                    log.append(KEY, "tool", "cat /home/u/.ssh/id_ed25519")
            return outcome

        async def _record(_prompt, *, memory_store: str = "", session_key: str = "") -> dict:
            prompts.append(_prompt)
            return {"history_entry": "e"}

        with patch.object(c, "_call_llm", _record):
            with patch.object(c, "_consolidate", _leak_between_passes):
                assert await c.consolidate_now(KEY) is True

        assert len(prompts) == 1, "the drain prompted a span that had turned sensitive"
        assert not any(".ssh/" in p for p in prompts), "sensitive tail reached the provider"
        assert log.unconsolidated_count(KEY), "the unread remainder must stay unmarked"

    @pytest.mark.asyncio
    async def test_a_restriction_after_a_commit_marks_only_the_prompted_prefix(
        self, tmp_path
    ) -> None:
        """An output committed, then the transcript turned restricted: the span is
        marked so the sweep does not repeat it -- but only up to what was read."""
        import contextlib

        from kiro_crew.history import TranscriptWithheld

        log = _log_with(tmp_path, ["x" * (_CONSOLIDATION_PROMPT_BUDGET_CHARS // 3)] * 9)
        c = _make_consolidator(log)
        real = c._publication_hold_checked

        @contextlib.contextmanager
        def commit_then_restrict(key, commit_state=None):
            with real(key, commit_state) as state:
                yield state
            raise TranscriptWithheld("restricted after the first commit")

        async def _llm(_prompt, *, memory_store: str = "", session_key: str = "") -> dict:
            return {"history_entry": "e"}

        with (
            patch.object(c, "_call_llm", _llm),
            patch.object(c, "_publication_hold_checked", commit_then_restrict),
        ):
            await c._consolidate(KEY, include_history=True)

        remaining = log.unconsolidated_count(KEY)
        assert 0 < remaining < 9, "the unread remainder must stay unmarked"

    @pytest.mark.asyncio
    async def test_a_refusal_after_progress_is_not_a_skip(self, tmp_path) -> None:
        """Work happened; the caller reports the remainder from its own count."""
        log = _log_with(tmp_path, ["x" * (_CONSOLIDATION_PROMPT_BUDGET_CHARS // 2)] * 8)
        c = _make_consolidator(log)
        real = c._consolidate
        calls = {"n": 0}

        async def _one_then_refuse(key: str, include_history: bool = True):
            calls["n"] += 1
            if calls["n"] == 1:
                return await real(key, include_history=include_history)
            return history_mod._CONSOLIDATION_REFUSED

        with patch.object(c, "_call_llm", AsyncMock(return_value={"history_entry": "e"})):
            with patch.object(c, "_consolidate", _one_then_refuse):
                assert await c.consolidate_now(KEY) is True

        assert 0 < log.unconsolidated_count(KEY) < 8


class TestTheReceiptDescribesThePromptedSpan:
    @pytest.mark.asyncio
    async def test_the_member_receipt_covers_only_what_the_model_read(
        self, tmp_path, monkeypatch
    ) -> None:
        """A receipt over the unprompted tail retires messages the model never saw.

        The replay path marks consolidated up to the receipt's ``source_total``
        once the digest matches, so a receipt written over the whole snapshot
        advances the durable marker past the tail the moment the marker write is
        retried after a transient failure.
        """
        from kiro_crew.context import ContextBuilder

        log = _log_with(
            tmp_path,
            ["body-" + "x" * (_CONSOLIDATION_PROMPT_BUDGET_CHARS // 2) for _ in range(6)],
        )
        store = MagicMock()
        store.algorithm_version = "v2"
        store.consolidation_receipt = MagicMock(return_value=None)
        store.apply_consolidation = MagicMock(return_value={})
        memory = MagicMock()
        memory.read_preferences.return_value = ""
        memory.read_projects.return_value = ""
        monkeypatch.setattr("kiro_crew.context.store_of_session", lambda *_: "alice-store")
        monkeypatch.setattr(memory_stores, "memory_store_version", lambda *_: 2)
        monkeypatch.setattr(ContextBuilder, "ensure_store", AsyncMock(return_value=store))
        monkeypatch.setattr(ContextBuilder, "get_memory_for", lambda **_: memory)
        c = _make_consolidator(log, vector_store=store)
        before = log.unconsolidated_count(KEY)

        with patch.object(c, "_call_llm", AsyncMock(return_value={"history_entry": "e"})):
            await c._consolidate(KEY, include_history=True)

        kwargs = store.apply_consolidation.call_args.kwargs
        marked = before - log.unconsolidated_count(KEY)
        assert 0 < marked < before, "fixture must exercise a bounded prompt"
        assert kwargs["source_total"] == marked, "the receipt must not claim the whole snapshot"
        assert len(kwargs["messages"]) == marked
