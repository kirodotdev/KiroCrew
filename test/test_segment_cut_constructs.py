"""Acceptance tests for never cutting a segment mid-construct.

The bug: a segment cut is a persistence boundary, and a tool call triggers one.
While background agents run, their tool calls interleave with the parent's token
stream, so the parent's reply is cut wherever a child happens to call a tool.
One reply then persists as several assistant rows split mid-word, and an options
marker whose closing bracket falls in the next row matches no render grammar --
the chips are lost and the tail reaches the user as literal prose.

Provenance cannot gate the cut. A child tool call reaches this path carrying no
``sub_session_id`` and no agent marker, byte-identical to the parent's own call,
which is why the fix asks WHERE the cut lands instead of who caused it.

The option line here is assembled from a fragment rather than written whole: a
complete ``[OPTIONS: ...]`` literal in a test file is a live UI control marker,
and the harness renders one it finds in transcript text.
"""

from __future__ import annotations

import pytest
from turn_harness import APPROVED, SlotSpec, TurnScript, run_turn

from kiro_crew.acp.types import (
    EVENT_COMPACTION_STATUS,
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    STOP_REASON_END_TURN,
    AcpEvent,
)
from kiro_crew.dashboard.chat_utils import open_construct_at_end

_OPEN = "[" + "OPTIONS:"


class TestOptionsLine:
    def test_unterminated_options_marker_defers_the_cut(self):
        text = "Board refreshed.\n\n" + _OPEN + " Let it finish | Kill it and relaun"
        assert open_construct_at_end(text) == "options-line"

    def test_closed_options_marker_is_safe_to_cut(self):
        text = "Board refreshed.\n\n" + _OPEN + " Let it finish | Kill it]"
        assert open_construct_at_end(text) == ""

    def test_a_realistic_mid_label_split_point_is_caught(self):
        """A prefix cut inside an option label, the shape a live wave produces."""
        text = (
            "an-out onward I'll use Kiro Crew's own path.\n\n"
            + _OPEN
            + " Let it finish, then post the cards | Kill it and relaun"
        )
        assert open_construct_at_end(text) == "options-line"

    def test_a_later_marker_is_the_one_that_counts(self):
        """A closed earlier marker must not mask an open later one."""
        text = _OPEN + " a | b]\n\nmore text\n\n" + _OPEN + " c | d"
        assert open_construct_at_end(text) == "options-line"

    def test_a_bracket_before_the_marker_does_not_close_it(self):
        text = "see [the docs](x)\n\n" + _OPEN + " a | b"
        assert open_construct_at_end(text) == "options-line"

    def test_a_lookalike_closer_counts_as_closed(self):
        # The shared partial-marker rule checks every MARKER_CLOSERS bracket,
        # not just ASCII ``]``; a marker closed only by a fullwidth ``]`` is
        # closed, so the cut is safe. A local ASCII-only check would miss this
        # and defer forever.
        assert open_construct_at_end("x\n\n[OPTIONS: 【重要】修复 | 跳过】") == ""


class TestCodeFence:
    def test_open_fence_defers_the_cut(self):
        assert open_construct_at_end("here:\n```python\nx = 1\n") == "code-fence"

    def test_closed_fence_is_safe_to_cut(self):
        assert open_construct_at_end("here:\n```python\nx = 1\n```\ndone.") == ""

    def test_inline_triple_backtick_in_prose_is_not_an_open_fence(self):
        # A bare ``` inside a prose line is not a fence opener (a fence opens
        # only at line start), so the odd backtick run must not defer the cut.
        # The old count-based rule read this as an open fence; the span-based
        # rule does not.
        assert open_construct_at_end("use the ``` marker to open a block") == ""

    def test_closed_fence_ending_at_eof_is_safe_to_cut(self):
        # A fence whose closer is the last line and ends exactly at the end of
        # the text (newline-terminated) is CLOSED, so the cut is safe. A check
        # that only asked whether the last fence span reaches len(text) would
        # wrongly read this closed fence as open.
        assert open_construct_at_end("here:\n```python\nx = 1\n```\n") == ""


class TestWidget:
    def test_unclosed_widget_defers_the_cut(self):
        assert open_construct_at_end('<mcwidget title="X">body') == "mcwidget"

    def test_closed_widget_is_safe_to_cut(self):
        assert open_construct_at_end('<mcwidget title="X">body</mcwidget>') == ""


class TestPlainText:
    def test_plain_prose_is_always_safe(self):
        assert open_construct_at_end("All 30 briefs are on disk.") == ""

    def test_empty_text_is_safe(self):
        assert open_construct_at_end("") == ""

    def test_mid_word_plain_prose_is_still_safe(self):
        """Deliberate: a mid-word cut in prose is cosmetic, not destructive.

        Widening the predicate to every word boundary would defer almost every
        cut and reorder the whole transcript, which costs more than it buys.
        Only constructs that render as a UNIT are protected.
        """
        assert open_construct_at_end("Rather than kill") == ""


class TestCompactionBoundary:
    """Through the real ``_run_chat``: a mid-turn compaction reset must not drop
    text a segment cut was deferred over."""

    @staticmethod
    async def _turn(*middle: AcpEvent):
        return await run_turn(
            TurnScript(
                events=[
                    AcpEvent(kind=EVENT_TEXT_CHUNK, text="here:\n```python\nx = 1\n"),
                    AcpEvent(
                        kind=EVENT_TOOL_CALL, tool_call_id="t1", title="read_file", tool_kind="read"
                    ),
                    *middle,
                    AcpEvent(kind=EVENT_COMPACTION_STATUS, text="started"),
                    AcpEvent(kind=EVENT_COMPACTION_STATUS, text="completed"),
                    AcpEvent(kind=EVENT_TEXT_CHUNK, text="after"),
                    AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
                ]
            ),
            slot=SlotSpec(rows=[("user", "earlier"), ("assistant", "earlier answer")]),
        )

    @pytest.mark.asyncio
    async def test_a_deferred_segment_whose_fence_closed_is_persisted(self):
        record = await self._turn(AcpEvent(kind=EVENT_TEXT_CHUNK, text="```\ndone."))
        assert record.stop_reason == STOP_REASON_END_TURN
        persisted = [str(row.get("content")) for row in record.rows("assistant")]
        assert any("x = 1" in text and "done." in text for text in persisted), persisted

    @pytest.mark.asyncio
    async def test_a_deferred_segment_still_open_is_persisted(self):
        record = await self._turn(AcpEvent(kind=EVENT_TEXT_CHUNK, text="y = 2\n"))
        persisted = [str(row.get("content")) for row in record.rows("assistant")]
        assert any("x = 1" in text and "y = 2" in text for text in persisted), persisted


class TestPermissionRequestCut:
    """Through the real ``_run_chat``: an approval prompt that lands inside an
    open construct must not split it, the same as a tool-call cut."""

    @staticmethod
    async def _turn(first: str, second: str):
        return await run_turn(
            TurnScript(
                events=[
                    AcpEvent(kind=EVENT_TEXT_CHUNK, text=first),
                    AcpEvent(
                        kind=EVENT_PERMISSION_REQUEST,
                        request_id="req-1",
                        title="fs_write",
                        tool_kind="edit",
                    ),
                    AcpEvent(kind=EVENT_TEXT_CHUNK, text=second),
                    AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
                ],
                answers={"req-1": APPROVED},
            ),
        )

    @pytest.mark.asyncio
    async def test_a_prompt_inside_an_open_fence_does_not_split_it(self):
        record = await self._turn("here:\n```python\nx = 1\n", "y = 2\n```\ndone.")
        assert record.stop_reason == STOP_REASON_END_TURN
        persisted = [str(row.get("content")) for row in record.rows("assistant")]
        assert any("x = 1" in text and "y = 2" in text for text in persisted), persisted
        assert not any("x = 1" in text and "y = 2" not in text for text in persisted), persisted
        # The approval card sits between the deferred chunks; none may strand
        # beside it as a duplicate streaming bubble in the live window.
        assert [r for r in record.window if r.get("role") == "chunk"] == []

    @pytest.mark.asyncio
    async def test_a_prompt_after_plain_prose_still_cuts_the_segment(self):
        record = await self._turn("Writing the file now.", "Written.")
        persisted = [str(row.get("content")) for row in record.rows("assistant")]
        cut = [t for t in persisted if "Writing the file now." in t and "Written." not in t]
        assert cut, persisted
