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
from kiro_crew import history_consolidation as history_consolidation_mod
from kiro_crew import memory_stores
from kiro_crew.history import (
    _CONSOLIDATION_MAX_ATTEMPTS,
    _CONSOLIDATION_PROMPT_BUDGET_CHARS,
    ConversationLog,
    HistoryConsolidator,
    _consolidation_chunk,
    _fmt_message,
)
from kiro_crew.history_consolidation import (
    _head_exceeds_budget,
    _prompt_rows,
    _slice_head_for_budget,
)
from kiro_crew.image_refs import STRIPPED_IMAGE_MARKER

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
        assert not meta.get(
            "consolidation_sub_offset"
        ), "the sub-offset is cleared once the marker moves past the whole head"
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
        assert (
            0 < first_slice_end < _CONSOLIDATION_PROMPT_BUDGET_CHARS * 2
        ), "the sub-offset advanced past exactly the first slice"
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
        assert (
            expected_slice["content"] and expected_slice["content"] in captured["prompt"]
        ), "the second pass resumes slicing from the durable sub-offset"
        assert (
            len(expected_slice["content"]) < _CONSOLIDATION_PROMPT_BUDGET_CHARS
        ), "a resumed slice's content is within one budget, not the whole head"
        from kiro_crew.history import _fmt_message

        assert (
            len(_fmt_message(expected_slice)) <= _CONSOLIDATION_PROMPT_BUDGET_CHARS
        ), "the rendered slice (content plus envelope) fits the budget"


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
        with patch.object(c, "_call_llm", AsyncMock(return_value={"history_entry": "e"})):
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


class TestNoticeRowsDoNotSkipSlicing:
    """An oversized head next to a display-only row must still be sliced.

    ``_consolidation_chunk`` carries display-only rows (a ``notice`` such as an
    AutoNudge) inside the prefix even though no prompt renders them, so a chunk
    holding one oversized head plus a notice is two rows long. A slicing guard
    keyed on ``len(chunk)`` would skip slicing for that shape, prompt the whole
    head, and abandon the entire message at the attempt cap WITHOUT extraction —
    the data loss the sub-chunking path exists to prevent. The guard must key on
    the PROMPTED rows instead.
    """

    def _notice(self, content: str) -> dict:
        return {"ts": "2026-09-09T12:00:00", "role": "notice", "content": content, "tools": []}

    def test_a_notice_after_an_oversized_head_leaves_one_prompt_row(self) -> None:
        head = _msg("x" * (_CONSOLIDATION_PROMPT_BUDGET_CHARS * 3), "assistant")
        chunk = _consolidation_chunk([head, self._notice("AutoNudge: keep going")])
        # The chunk keeps the notice, so it is two rows long...
        assert len(chunk) == 2
        # ...but exactly one row is rendered into a prompt, and it is the head.
        rows = _prompt_rows(chunk)
        assert len(rows) == 1
        assert _head_exceeds_budget(rows[0])

    def test_a_notice_before_an_oversized_head_leaves_one_prompt_row(self) -> None:
        head = _msg("x" * (_CONSOLIDATION_PROMPT_BUDGET_CHARS * 3), "assistant")
        chunk = _consolidation_chunk([self._notice("AutoNudge: keep going"), head])
        assert len(chunk) == 2
        rows = _prompt_rows(chunk)
        assert len(rows) == 1
        assert _head_exceeds_budget(rows[0])


class TestASliceRendersWithinTheBudget:
    """``_slice_head_for_budget`` must render within the budget, not just slice raw.

    ``_fmt_message`` runs ``strip_image_refs``, which replaces each image
    reference with a longer content-free marker. A slice sized against the raw
    content length can therefore render OVER the budget once that expansion
    applies — handing a provider the over-ceiling prompt the slicing avoids.
    """

    def test_an_image_ref_dense_head_slices_within_the_rendered_budget(self, monkeypatch) -> None:
        # Shrink the budget to a few KB so the image-ref-dense fixture stays
        # small. ``strip_image_refs`` (run by ``_fmt_message``) is superlinear in
        # the number of references, so a budget-sized fixture at the real 64 KB
        # ceiling means thousands of refs and a single render that runs for tens
        # of seconds — enough to trip the suite's per-test timeout. The slicing
        # behaviour under test is budget-relative, so a small budget exercises it
        # identically. ``_slice_head_for_budget`` and ``_head_exceeds_budget``
        # both read this module global at call time.
        budget = 2048
        monkeypatch.setattr(
            history_consolidation_mod,
            "_CONSOLIDATION_PROMPT_BUDGET_CHARS",
            budget,
        )
        ref = "![](/a.png)\n"
        assert len(STRIPPED_IMAGE_MARKER) > len(
            ref.rstrip("\n")
        ), "fixture only exercises expansion if the marker is longer than the ref"
        content = ref * (budget // len(ref) + 50)
        head = _msg(content, "assistant")
        # The raw content is over the budget, so this is a genuine oversized head.
        assert _head_exceeds_budget(head)
        sliced, start, end, _is_last = _slice_head_for_budget(head, 0)
        # The slice must make progress and render within the budget despite the
        # image-reference expansion. A raw slice sized at ``budget - envelope``
        # would render larger than the budget once each ref expands, so a slice
        # that renders within the budget proves the rendered-size shrink engaged.
        assert end > start
        assert len(_fmt_message(sliced)) <= budget


class TestSlicingStripsImageRefsOnceOverTheWholeHead:
    """Opus review: slicing raw ``content[start:end]`` can cut inside a fenced
    code block, flipping the fence parity the slice presents to
    ``strip_image_refs``. ``strip_image_refs`` masks fenced spans (a path inside
    a fence is documentation), so a slice that opens mid-fence reads as prose and
    an image reference the whole-head scan would MASK gets stripped — or, cut the
    other way, a real reference is left to be inlined into the prompt the
    sub-chunking path exists to keep image-free. Stripping once over the whole
    head before slicing removes the ambiguity.
    """

    def test_a_ref_inside_a_fence_does_not_leak_into_any_slice(self, monkeypatch) -> None:
        budget = 2048
        monkeypatch.setattr(
            history_consolidation_mod,
            "_CONSOLIDATION_PROMPT_BUDGET_CHARS",
            budget,
        )
        # A head whose stripped length spans several slices, with a real image
        # reference OUTSIDE any fence (so strip_image_refs replaces it) and a
        # fenced block that, if a slice cut inside it, would flip parity.
        body = "intro line\n" "```\n" + (
            "filler /not/stripped/in/code.png line\n" * 40
        ) + "```\n" + "![real](/leaked.png)\n" + ("tail text line\n" * 80)
        head = _msg(body, "assistant")
        assert _head_exceeds_budget(head)

        # Walk every slice the head produces; none may contain the raw image
        # reference that strip_image_refs replaces over the whole head.
        stripped_whole = history_consolidation_mod._consolidation_head_content(head)
        assert "/leaked.png" not in stripped_whole, "the ref outside the fence is stripped"

        sub = 0
        seen = ""
        for _ in range(50):
            sliced, start, end, is_last = _slice_head_for_budget(head, sub)
            # Every slice is drawn from the already-stripped content, so the raw
            # reference can never appear in a prompted slice.
            assert "/leaked.png" not in sliced["content"], "a slice leaked a raw image ref"
            seen += sliced["content"]
            if is_last:
                break
            sub = end
        # The slices reassemble the stripped whole (progress + no gaps/overlap).
        assert seen == stripped_whole

    def test_a_mid_fence_slice_is_rendered_without_a_second_strip(self) -> None:
        """GPT 6.1 (blocking, upheld): a slice is rendered with
        ``prestripped=True`` so ``_fmt_message`` does NOT run ``strip_image_refs``
        a second time. The head is stripped ONCE over its whole content, but a
        slice that begins inside a fenced block shows a different fence parity, so
        a re-strip would read the fenced block as prose and replace a path the
        whole-head pass deliberately preserved — silently corrupting the
        extraction prompt. The pre-strip removed the mid-fence AMBIGUITY in the
        slice's content; this test pins that the RENDER path does not reintroduce
        it.
        """
        from kiro_crew.image_refs import strip_image_refs

        png = "/illustrative/diagram.png"
        # An open fence, filler long enough to force a boundary before the path,
        # then the path still INSIDE the fence, then more filler and the close.
        body = (
            "```json\n"
            + ("x" * (_CONSOLIDATION_PROMPT_BUDGET_CHARS + 5000))
            + "\n"
            + f'value = "{png}"\n'
            + ("y" * 2000)
            + "\n```\n"
        )
        head = _msg(body, "assistant")
        assert _head_exceeds_budget(head)
        stripped_whole = history_consolidation_mod._consolidation_head_content(head)
        # Inside the fence, the path is documentation — the whole-head pass keeps it.
        assert png in stripped_whole

        # Walk to the slice that carries the path and render it both ways.
        sub = 0
        rendered_prestripped = None
        for _ in range(60):
            sliced, start, end, is_last = _slice_head_for_budget(head, sub)
            if png in sliced["content"]:
                rendered_prestripped = history_consolidation_mod._fmt_message(
                    sliced, prestripped=True
                )
                break
            if is_last:
                break
            sub = end
        assert rendered_prestripped is not None, "no slice carried the fenced path"
        # The fix (the real contract): the preserved path survives into the
        # rendered prompt because ``prestripped=True`` quotes the already-stripped
        # slice verbatim and does NOT run ``strip_image_refs`` a second time.
        assert png in rendered_prestripped
        # Pin that invariant directly rather than relying on the re-strip
        # happening to corrupt THIS slice (whether a mid-fence re-strip removes
        # the path depends on exactly where the boundary falls, which varies with
        # line endings across platforms — a brittle thing to assert). The slice
        # content is already stripped, so re-stripping it inside the render would
        # be the only way the path could change; assert the prestripped render is
        # exactly the render of the verbatim slice content.
        assert sliced["content"] in rendered_prestripped
        # And the whole-head content remains idempotent under re-strip.
        assert strip_image_refs(stripped_whole) == stripped_whole


class TestARotationDiscardsAStaleSubOffsetFromTheCap:
    """The attempt cap must read the sub-offset through its generation stamp.

    A rotation resets ``last_consolidated`` and bumps ``rotation_generation`` but
    leaves ``consolidation_sub_offset`` at its old nonzero value. Comparing the
    attempt's stamped sub-offset against that RAW value reads every failure on
    the new span as a different slice and resets the cap each time, so the
    abandon path is never reached. Reading it through
    ``_sub_offset_for_generation`` makes the comparison see the ``0`` the slicer
    uses for the new generation, so the cap holds across failures.
    """

    def test_a_stale_sub_offset_from_a_prior_generation_does_not_reset_the_cap(
        self, tmp_path
    ) -> None:
        log = ConversationLog(base_dir=tmp_path / "sessions")
        log.init()
        # Post-rotation: generation 2, marker reset to 0, but the raw sub-offset
        # still holds generation 1's 5000. The attempt was charged on the new
        # generation at sub-offset 0 (re-sliced from the head's start).
        meta = {
            "rotation_generation": 2,
            "last_consolidated": 0,
            "consolidation_sub_offset": 5000,
            "consolidation_sub_offset_generation": 1,
            "consolidation_attempts_generation": 2,
            "consolidation_attempts_offset": 0,
            "consolidation_attempts_sub_offset": 0,
        }
        # The generation-honoured read discards the stale offset...
        assert ConversationLog._sub_offset_for_generation(meta, 2) == 0
        # ...so the attempt still describes the current span and the cap holds.
        assert log._attempts_describe_current_span(meta, None) is True

    def test_an_advanced_sub_offset_in_the_current_generation_resets_the_cap(
        self, tmp_path
    ) -> None:
        log = ConversationLog(base_dir=tmp_path / "sessions")
        log.init()
        # Same generation, but an earlier slice succeeded and advanced the
        # durable sub-offset past where this attempt was charged — a genuinely
        # different slice, so the cap SHOULD reset.
        meta = {
            "rotation_generation": 2,
            "last_consolidated": 0,
            "consolidation_sub_offset": 5000,
            "consolidation_sub_offset_generation": 2,
            "consolidation_attempts_generation": 2,
            "consolidation_attempts_offset": 0,
            "consolidation_attempts_sub_offset": 0,
        }
        assert ConversationLog._sub_offset_for_generation(meta, 2) == 5000
        assert log._attempts_describe_current_span(meta, None) is False


class TestAPerHeadSliceCeilingBoundsTheCost:
    """A single over-budget head is sliced a bounded number of times — about
    ``_CONSOLIDATION_MAX_HEAD_SLICES`` for near-span lines, up to roughly twice
    that for lines that advance well under a span — before a genuinely
    multi-slice remainder is retired, so one pathological message cannot spawn
    an unbounded run of sequential consolidation LLM calls that holds up every
    message behind it. A head whose remainder fits one more slice is never
    retired; it drains in full. The retired remainder is FAIL-SAFE: the marker
    is advanced past the whole head WITH a logged reason and the user-facing
    abandon notice, never silently.
    """

    @pytest.mark.asyncio
    async def test_a_ten_megabyte_head_makes_at_most_the_ceiling_of_llm_calls(
        self, tmp_path
    ) -> None:
        ceiling = history_consolidation_mod._CONSOLIDATION_MAX_HEAD_SLICES
        # A head far larger than ceiling * budget: without the ceiling it would
        # slice into ~160 pieces (10 MB / 64 KB). Each line is kept short so the
        # boundary search cuts cleanly on a newline.
        big = ("a" * 4096 + "\n") * (10 * 1024 * 1024 // 4097)
        log = _log_with(tmp_path, [big])
        c = _make_consolidator(log)

        notices: list[dict] = []
        c.on_abandoned = lambda key, count, reason, char_count=0, last_slice=False, retired=False: notices.append(
            {
                "key": key,
                "count": count,
                "reason": reason,
                "char_count": char_count,
                "last_slice": last_slice,
                "retired": retired,
            }
        )

        llm = AsyncMock(return_value={"history_entry": "e"})
        passes = 0
        with patch.object(c, "_call_llm", llm):
            # Drive passes until the head is retired (marker moved) or we exceed
            # a hard bound that proves the ceiling failed.
            while log.unconsolidated_count(KEY) and passes < ceiling + 5:
                with history_mod.allow_on_loop_persist():
                    log.update_metadata(KEY, {"consolidation_retry_at": 0.0})
                await c._consolidate(KEY, include_history=True)
                passes += 1

        # The head is retired, not re-sliced forever.
        assert log.unconsolidated_count(KEY) == 0, "the head must be retired, not looped"
        meta = log.get_metadata(KEY)
        assert meta.get("last_consolidated") == 1, "the marker moved past the whole head"
        assert not meta.get("consolidation_sub_offset"), "the sub-offset was cleared on retire"
        # The worst-case pin: the per-head cost is a small constant, not the
        # ~160 an unbounded slicer would make. The ceiling is N whole spans of
        # the durable sub-offset; a near-span head reaches it after N slices and
        # retires its multi-slice remainder on the next pass (N + 1 prompts),
        # and content that advances well under a span per cut takes up to roughly
        # 2 * N + 1 — bounded either way.
        assert llm.call_count <= 2 * ceiling + 1, (
            f"made {llm.call_count} LLM calls; the per-head cost must stay a "
            f"small constant (<= {2 * ceiling + 1}), far below the ~160 an "
            f"unbounded slicer makes"
        )

    @pytest.mark.asyncio
    async def test_the_retired_remainder_fires_a_fail_safe_abandon_notice(self, tmp_path) -> None:
        ceiling = history_consolidation_mod._CONSOLIDATION_MAX_HEAD_SLICES
        big = ("b" * 4096 + "\n") * (10 * 1024 * 1024 // 4097)
        log = _log_with(tmp_path, [big])
        c = _make_consolidator(log)

        notices: list[dict] = []
        c.on_abandoned = lambda key, count, reason, char_count=0, last_slice=False, retired=False: notices.append(
            {
                "reason": reason,
                "char_count": char_count,
                "last_slice": last_slice,
                "retired": retired,
            }
        )

        with patch.object(c, "_call_llm", AsyncMock(return_value={"history_entry": "e"})):
            passes = 0
            while log.unconsolidated_count(KEY) and passes < ceiling + 5:
                with history_mod.allow_on_loop_persist():
                    log.update_metadata(KEY, {"consolidation_retry_at": 0.0})
                await c._consolidate(KEY, include_history=True)
                passes += 1

        # Exactly one retire notice, and it is a FAIL-SAFE record: it names the
        # dropped remainder (char_count > 0), says the message is now fully
        # consolidated (last_slice), gives the ceiling as the reason, and marks
        # the drop as a DELIBERATE retire (retired=True) rather than a failure —
        # the remainder is NEVER marked silently.
        retire = [n for n in notices if "ceiling" in n["reason"]]
        assert len(retire) == 1, f"expected one retire notice, got {notices}"
        assert retire[0]["char_count"] > 0, "the notice names the dropped remainder size"
        assert retire[0]["last_slice"] is True, "the marker moved past the whole message"
        assert retire[0]["retired"] is True, "a ceiling retire is deliberate, not a failure"

    @pytest.mark.asyncio
    async def test_a_head_within_the_ceiling_is_fully_extracted_without_retiring(
        self, tmp_path
    ) -> None:
        """A head needing fewer than the ceiling of slices drains normally: no
        retire notice, every slice extracted, the marker moving only at the end.
        """
        # Three budgets' worth: well under the ceiling, so it drains in full.
        log = _log_with(tmp_path, ["c" * (_CONSOLIDATION_PROMPT_BUDGET_CHARS * 3)])
        c = _make_consolidator(log)

        notices: list[dict] = []
        c.on_abandoned = lambda key, count, reason, char_count=0, last_slice=False: notices.append(
            {"reason": reason}
        )

        with patch.object(c, "_call_llm", AsyncMock(return_value={"history_entry": "e"})):
            passes = 0
            while log.unconsolidated_count(KEY) and passes < 10:
                with history_mod.allow_on_loop_persist():
                    log.update_metadata(KEY, {"consolidation_retry_at": 0.0})
                await c._consolidate(KEY, include_history=True)
                passes += 1

        assert log.unconsolidated_count(KEY) == 0
        assert log.get_metadata(KEY).get("last_consolidated") == 1
        assert not any(
            "ceiling" in n["reason"] for n in notices
        ), "a head under the ceiling must not be retired"

    @pytest.mark.asyncio
    async def test_a_head_whose_remainder_fits_one_more_slice_is_never_retired(
        self, tmp_path
    ) -> None:
        """The retire fires only when MORE THAN one slice of content remains.

        A head that drains in exactly the ceiling of slices — or one more — keeps
        its final short slice: there is no span that would fit in one more slice
        yet is dropped unread. This pins the retire condition's lower edge, so a
        head whose remainder is a single slice is prompted in full rather than
        marked consolidated unread.
        """
        ceiling = history_consolidation_mod._CONSOLIDATION_MAX_HEAD_SLICES

        # A base64-style blob with NO whitespace: _slice_boundary falls back to
        # the bare cut, so every slice advances exactly one span and the content
        # drains in a predictable number of slices. Size it to just over the
        # ceiling of whole spans (ceiling + 0.4), the exact "remainder fits one
        # more slice" shape the retire gate must let drain in full.
        env = len(history_consolidation_mod._fmt_message({"role": "user", "content": ""}))
        span = max(1, _CONSOLIDATION_PROMPT_BUDGET_CHARS - env)
        blob = "A" * int(span * (ceiling + 0.4))
        log = _log_with(tmp_path, [blob])
        c = _make_consolidator(log)

        notices: list[dict] = []
        c.on_abandoned = lambda key, count, reason, char_count=0, last_slice=False, retired=False: notices.append(
            {"reason": reason, "retired": retired}
        )

        llm = AsyncMock(return_value={"history_entry": "e"})
        with patch.object(c, "_call_llm", llm):
            passes = 0
            while log.unconsolidated_count(KEY) and passes < ceiling + 5:
                with history_mod.allow_on_loop_persist():
                    log.update_metadata(KEY, {"consolidation_retry_at": 0.0})
                await c._consolidate(KEY, include_history=True)
                passes += 1

        # The whole head was read: the marker moved past it and NO retire fired.
        assert log.unconsolidated_count(KEY) == 0, "the head must drain, not loop"
        assert log.get_metadata(KEY).get("last_consolidated") == 1
        assert not any(
            n["retired"] for n in notices
        ), "a head whose remainder fits one more slice must NOT be retired"
        # It drained in ceiling + 1 slices (the final short slice IS prompted),
        # not retired at the ceiling with a lost tail.
        assert llm.call_count == ceiling + 1, (
            f"a {ceiling + 0.4:.1f}-span head should drain in {ceiling + 1} slices; "
            f"made {llm.call_count}"
        )
