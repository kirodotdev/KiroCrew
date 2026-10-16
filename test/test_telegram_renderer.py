"""Telegram renderer: streaming, finalization and the interactive approval decider.

Covers ``TelegramRenderer`` from the first chunk to the sealed answer (renderer.py):
streaming edits, draft and rich-message paths, finalization, failure text and steering
markers; and ``TelegramApprovalDecider``, the inline-keyboard approval prompt and the
nonce that refuses a stale button.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

import pytest
from telegram_helpers import _drop_live_config_snapshot  # noqa: F401
from telegram_helpers import FakeClient

from kiro_crew.messaging import driver as messaging_driver
from kiro_crew.messaging.renderer import (
    DONE,
    STEER_CONSUMED,
    TEXT_CHUNK,
    TOOL_CALL,
    OutputEvent,
    session_provenance_tag,
)
from kiro_crew.telegram import renderer as telegram_renderer
from kiro_crew.telegram.renderer import (
    TelegramApprovalDecider,
    TelegramRenderer,
    _has_table,
    _md_to_telegram_html,
    _row_cell_count,
    _seal_table_fallback,
    _strip_steering,
)
from kiro_crew.telegram.transport import TELEGRAM_CAPABILITIES


class TestRenderer:
    def test_the_approval_prompt_names_the_tool_the_request_is_about(self) -> None:
        # `_last_tool` is the last tool_call the renderer saw and is never
        # cleared, so a permission arriving without its own titled tool_call
        # would ask the operator to approve the PREVIOUS tool.
        cli = FakeClient()
        r = TelegramRenderer(cli, 55, TELEGRAM_CAPABILITIES, session_key="telegram:1:0")  # type: ignore[arg-type]

        async def _go() -> None:
            await r.on_turn_start()
            await r.on_tool_call("t1", "fs_read")
            await r.on_prompt_choice([], request_id="rq1", tool_title="execute_bash")

        asyncio.run(_go())
        prompt = cli.sent[-1][0]
        assert "execute_bash" in prompt
        assert "fs_read" not in prompt

    def test_the_approval_prompt_falls_back_to_the_last_tool(self) -> None:
        # Non-vacuity: without a title on the event the remembered name is still
        # better than "this tool", so the fallback must survive.
        cli = FakeClient()
        r = TelegramRenderer(cli, 55, TELEGRAM_CAPABILITIES, session_key="telegram:1:0")  # type: ignore[arg-type]

        async def _go() -> None:
            await r.on_turn_start()
            await r.on_tool_call("t1", "fs_read")
            await r.on_prompt_choice([], request_id="rq2")

        asyncio.run(_go())
        assert "fs_read" in cli.sent[-1][0]

    def test_a_streamed_table_reply_still_goes_out_as_a_rich_message(self) -> None:
        # THE regression this feature exists to prevent. A normal agent reply
        # streams, so _stream_live has already sent a plaintext bubble and set
        # _stream_mid by the time the segment is sealed. Gating the rich path on
        # "_stream_mid is None" therefore skipped every real reply and the table
        # reached the user as literal pipes -- the feature was dead in the only
        # case that matters. Assert the rich send happens even though a bubble
        # was streamed, and that the superseded bubble is deleted so the user is
        # left with exactly one message.
        table = "Here you go:\n\n| a | b |\n| --- | --- |\n| 1 | 2 |"
        cli = FakeClient()
        r = TelegramRenderer(cli, 55, TELEGRAM_CAPABILITIES, session_key="telegram:1:0")  # type: ignore[arg-type]

        async def _go() -> None:
            await r.on_turn_start()
            await r.on_text_chunk(table)
            await r._stream_live(force=True)  # force the live bubble to exist
            assert r._stream_mid is not None, "precondition: a bubble streamed"
            await r._seal_current()

        asyncio.run(_go())

        assert len(cli.rich_sent) == 1, "table must be sealed via sendRichMessage"
        assert "| a | b |" in cli.rich_sent[0][0], "raw markdown table is passed through"
        assert cli.deleted, "the superseded plaintext bubble must be deleted"
        assert r._stream_mid is None, "stream id cleared after the bubble is dropped"

    def test_a_table_reply_falls_back_to_html_when_rich_is_unavailable(self) -> None:
        # If sendRichMessage fails, the streamed bubble must survive and be
        # sealed the legacy way. Losing the answer is far worse than a table
        # that degrades to literal pipes, so the delete must NOT have happened.
        table = "| a | b |\n| --- | --- |\n| 1 | 2 |"
        cli = FakeClient()
        cli.rich_fails = True
        r = TelegramRenderer(cli, 55, TELEGRAM_CAPABILITIES, session_key="telegram:1:0")  # type: ignore[arg-type]

        async def _go() -> None:
            await r.on_turn_start()
            await r.on_text_chunk(table)
            await r._stream_live(force=True)
            await r._seal_current()

        asyncio.run(_go())

        assert cli.rich_sent == [], "rich send reported failure"
        assert not cli.deleted, "a failed rich send must never delete the answer"
        assert cli.edits, "the streamed bubble is still sealed via the HTML path"

    def test_a_reply_without_a_table_never_touches_the_rich_api(self) -> None:
        # Rich Messages are reserved for content the HTML subset cannot express.
        # An ordinary reply must take the unchanged HTML path, so the new code
        # adds zero API calls to the common case.
        cli = FakeClient()
        r = TelegramRenderer(cli, 55, TELEGRAM_CAPABILITIES, session_key="telegram:1:0")  # type: ignore[arg-type]

        async def _go() -> None:
            await r.on_turn_start()
            await r.on_text_chunk("just a plain **bold** answer, no table here")
            await r._stream_live(force=True)
            await r._seal_current()

        asyncio.run(_go())

        assert cli.rich_sent == [], "no table -> no rich send"
        assert not cli.deleted, "no table -> the streamed bubble is edited, not replaced"

    def test_table_detection_needs_a_separator_row(self) -> None:
        # A line of pipes alone is not a table (prose, or a code sample), and
        # sending it as rich would reflow it. Only a header + separator pair is.
        assert _has_table("| a | b |\n| --- | --- |\n| 1 | 2 |")
        assert _has_table("| a | b |\n|:---|---:|\n| 1 | 2 |")  # alignment colons
        assert not _has_table("pipes | in | prose are not a table")
        assert not _has_table("| a | b |\nno separator row follows")

    def test_table_detection_accepts_gfm_tables_without_outer_pipes(self) -> None:
        # GFM does not require leading/trailing pipes. Anchoring detection on a
        # leading `|` silently missed these and shipped them as literal pipes.
        assert _has_table("a | b\n--- | ---\n1 | 2")
        assert _has_table("a | b\n---|---\n1 | 2")
        assert _has_table("  a | b\n  --- | ---\n  1 | 2")  # indented
        # A pipe-bearing sentence above a horizontal rule is NOT a table: the
        # separator row has no pipe, so it stays on the ordinary HTML path.
        assert not _has_table("cost | benefit analysis\n---------------------")

    def test_table_detection_requires_matching_cell_counts(self) -> None:
        # THE bug: a header row glued to leading prose has one cell too many, so
        # no GFM parser sees a table. Claiming one anyway sends the block down
        # the rich path, where the server renders it as a single paragraph --
        # newlines collapsed, every pipe literal. Counting cells is what keeps
        # that content on the monospace path instead.
        assert not _has_table("Here you go:| a | b |\n| --- | --- |\n| 1 | 2 |")
        assert not _has_table("a | b | c\n--- | ---\n1 | 2")  # malformed, 3 vs 2
        assert _has_table("| a |\n| --- |\n| 1 |")  # single column is still a table
        assert _has_table("| a\\|b | c |\n| --- | --- |\n| 1 | 2 |")  # escaped pipe
        # A one-cell separator must not promote the sentence above it to a table.
        assert not _has_table("just prose\n|---|")

    def test_table_detection_counts_cells_by_escape_parity(self) -> None:
        # `\|` is cell content, but `\\` is a literal backslash that leaves the
        # NEXT pipe a real boundary. A fixed-width lookbehind cannot tell those
        # apart: it reads the second backslash of an even run as an escape, merges
        # two cells, and can make a malformed header match its delimiter -- which
        # would route it to the rich path and flatten it.
        assert _row_cell_count(r"| a\|b | c |") == 2, "escaped pipe stays inside its cell"
        assert _row_cell_count("| a\\\\ | b |") == 2, "even backslash run does not escape"
        assert _row_cell_count(r"| a\\\|b | c |") == 2, "odd run after a pair escapes again"
        # The header below is 3 cells against a 2-cell delimiter once parity is
        # honoured, so it must NOT be claimed as a table.
        assert not _has_table("a\\\\ | b | c\n| --- | --- |\n| 1 | 2 |")

    def test_delimiter_cells_follow_the_observed_server_rule(self) -> None:
        # Every case here was checked against the live API by reading the echoed
        # `rich_message.blocks`, because a spec reading and the server disagree.
        # An EMPTY delimiter cell is rejected by the server (`paragraph`), so it
        # must not take the rich path.
        assert not _has_table("| a | b |\n| --- | |\n| 1 | 2 |")
        # A BROKEN dash run is accepted by the server (`table`), so demanding a
        # contiguous run would degrade a table it renders fine into monospace.
        assert _has_table("| a | b |\n| - - | --- |\n| 1 | 2 |")
        # Ordinary spellings, also confirmed as `table`.
        assert _has_table("| a | b |\n| --- | --- |\n| 1 | 2 |")
        assert _has_table("| a | b |\n| - | - |\n| 1 | 2 |")

    def test_a_glued_table_is_sealed_verbatim_instead_of_reflowed(self) -> None:
        # A header row sharing a line with prose has one cell too many, so no GFM
        # parser sees a table. Sending it as rich would render one paragraph with
        # every newline collapsed. Whether the extra cell is prose or a delimiter
        # the author got wrong is NOT decidable from the text, so nothing is
        # rewritten: the monospace seal reproduces the block as written.
        glued = "Here is the table you asked for:| a | b |\n| --- | --- |\n| 1 | 2 |"
        cli = FakeClient()
        r = TelegramRenderer(cli, 55, TELEGRAM_CAPABILITIES, session_key="telegram:1:0")  # type: ignore[arg-type]

        async def _go() -> None:
            await r.on_turn_start()
            await r.on_text_chunk(glued)
            await r._stream_live(force=True)
            await r._seal_current()

        asyncio.run(_go())

        assert cli.rich_sent == [], "non-conforming pipe markup must not take the rich path"
        assert not cli.deleted, "the streamed bubble is edited, not replaced"
        sealed = cli.edits[-1][1]
        assert "<pre>" in sealed and "</pre>" in sealed, "the pipe block is sealed monospace"
        for row in ("| a | b |", "| --- | --- |", "| 1 | 2 |"):
            assert row in sealed, f"{row} must survive verbatim"

    def test_a_degraded_table_is_sealed_monospace_not_as_ragged_pipes(self) -> None:
        # When rich is unavailable the table still has to go out, but sealing it
        # through the normal HTML path reflows it into ragged escaped pipes.
        # <pre> keeps the columns aligned, so the degraded case stays readable.
        cli = FakeClient()
        cli.rich_fails = True
        r = TelegramRenderer(cli, 55, TELEGRAM_CAPABILITIES, session_key="telegram:1:0")  # type: ignore[arg-type]

        async def _go() -> None:
            await r.on_turn_start()
            await r.on_text_chunk("| a | b |\n| --- | --- |\n| 1 | 2 |")
            await r._stream_live(force=True)
            await r._seal_current()

        asyncio.run(_go())

        sealed = cli.edits[-1][1]
        assert "<pre>" in sealed and "</pre>" in sealed
        assert "| a | b |" in sealed, "the table text survives inside the pre block"

    def test_the_degraded_path_keeps_prose_formatting_around_the_table(self) -> None:
        # Regression: wrapping the WHOLE segment in <pre> made a prose+table
        # reply render worse than before the feature existed -- every bold, link
        # and inline-code span was lost and `**` markers showed up literally.
        # On a server without Rich Messages this is the permanent path, so it
        # must never be a downgrade. Only the table run may be monospace.
        text = "Here is the **summary**:\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n\nSee `docs` too."
        out = _seal_table_fallback(text)

        assert "<b>summary</b>" in out, "prose keeps its bold"
        assert "<code>docs</code>" in out, "prose keeps its inline code"
        assert "**" not in out, "no literal markdown markers leak to the user"
        # The table is the only monospace run, and it is intact.
        assert out.count("<pre>") == 1
        table_block = out.split("<pre>")[1].split("</pre>")[0]
        assert "| a | b |" in table_block and "| 1 | 2 |" in table_block
        assert "summary" not in table_block, "prose must not be swallowed into the pre"

    def test_the_degraded_path_handles_a_table_with_no_surrounding_prose(self) -> None:
        # The bare-table case must not emit stray empty prose fragments.
        out = _seal_table_fallback("| a | b |\n| --- | --- |\n| 1 | 2 |")
        assert out.startswith("<pre>") and out.endswith("</pre>")
        assert out.count("<pre>") == 1

    def test_a_degraded_mixed_reply_keeps_its_prose_formatting_end_to_end(self) -> None:
        # Pins the SEAL PATH, not just the helper: _seal_current must route the
        # failed-rich case through the mixed-segment renderer. Asserting only on
        # _seal_table_fallback() left the call site free to wrap the whole
        # segment in <pre> again, which is exactly the regression to prevent.
        cli = FakeClient()
        cli.rich_fails = True
        r = TelegramRenderer(cli, 55, TELEGRAM_CAPABILITIES, session_key="telegram:1:0")  # type: ignore[arg-type]

        async def _go() -> None:
            await r.on_turn_start()
            await r.on_text_chunk("The **key** result:\n\n| a | b |\n| --- | --- |\n| 1 | 2 |")
            await r._stream_live(force=True)
            await r._seal_current()

        asyncio.run(_go())

        sealed = cli.edits[-1][1]
        assert "<b>key</b>" in sealed, "prose bold survives the degraded seal"
        assert "**" not in sealed, "no literal markdown markers reach the user"
        assert "<pre>" in sealed, "the table run is still monospace"
        assert "key" not in sealed.split("<pre>")[1], "prose not swallowed into the pre"

    def test_a_near_limit_degraded_reply_is_not_truncated_by_pre_overhead(self) -> None:
        # The segment was sized against the PLAIN html render, and <pre> wrapping
        # only adds characters, so on a near-limit reply the wrapped form can
        # spill past the rendered ceiling and have its tail cut by _cap_text().
        # Losing the end of the answer is worse than losing column alignment, so
        # the plain render must win once the wrapped form would overflow.
        cli = FakeClient()
        cli.rich_fails = True
        r = TelegramRenderer(cli, 55, TELEGRAM_CAPABILITIES, session_key="telegram:1:0")  # type: ignore[arg-type]

        # Many small tables: source stays under the plaintext rotate threshold
        # while each table adds <pre></pre> overhead to the wrapped form.
        one = "| a | b |\n| - | - |\n| 1 | 2 |\n\n"
        text = one * (r._limit() // len(one) - 1)
        assert (
            len(_seal_table_fallback(text)) > r._rendered_limit()
        ), "precondition: the wrapped form must overflow for this to test anything"
        assert (
            len(_md_to_telegram_html(text)) <= r._rendered_limit()
        ), "precondition: the plain render must fit"

        async def _go() -> None:
            await r.on_turn_start()
            await r.on_text_chunk(text)
            await r._seal_current()

        asyncio.run(_go())

        sealed = cli.final_text()
        assert len(sealed) <= r._rendered_limit(), "degraded seal respects the rendered cap"
        assert "<pre>" not in sealed, "overflowing wrap is dropped for the plain render"

    def test_fenced_table_markup_costs_a_rich_send_but_never_wrong_output(self) -> None:
        # Detection is deliberately fence-agnostic: a fenced sample of table
        # markup does take the rich path, but Rich Markdown parses the fence
        # itself and renders it as the code block it is. The cost is one extra
        # send, not wrong output -- and it buys not maintaining a second
        # CommonMark fence parser beside the HTML renderer's own.
        fenced = "Example:\n\n```\n| a | b |\n| --- | --- |\n| 1 | 2 |\n```\n\ndone"
        assert _has_table(fenced), "detection does not screen fences"
        # A real table after a closed fence is of course still found.
        assert _has_table(fenced + "\n\n| x | y |\n| --- | --- |\n| 1 | 2 |")

    def test_the_degraded_path_never_splits_a_code_fence(self) -> None:
        # The safety net lives on the FALLBACK side: a fenced segment renders
        # whole via the normal path, so no fence can be torn in half.
        fenced = "Example:\n\n```\n| a | b |\n| --- | --- |\n| 1 | 2 |\n```\n\ndone"
        out = _seal_table_fallback(fenced)
        assert out == _md_to_telegram_html(fenced), "rendered whole, unsplit"
        assert "&#x60;" not in out and "```" not in out, "no literal fence markers leak"

    def test_replacing_a_streamed_bubble_does_not_ping_the_user_twice(self) -> None:
        # send-rich-then-delete means two messages exist briefly and Telegram
        # notifies for each. The bubble already pinged, so the replacement must
        # be silent -- otherwise every table reply buzzes twice where main
        # buzzed once, a regression introduced by replacing instead of editing.
        cli = FakeClient()
        r = TelegramRenderer(cli, 71, TELEGRAM_CAPABILITIES, session_key="telegram:1:0")  # type: ignore[arg-type]

        async def _go() -> None:
            await r.on_turn_start()
            await r.on_text_chunk("| a | b |\n| --- | --- |\n| 1 | 2 |")
            await r._stream_live()  # streams the bubble -> user is pinged
            await r._seal_current()

        asyncio.run(_go())
        assert cli.rich_sent, "rich send happened"
        assert cli.rich_silent == [True], "the replacement is silent"

    def test_a_table_that_never_streamed_still_notifies(self) -> None:
        # No bubble streamed -> no earlier ping, so the rich send is the user's
        # ONLY notification. Suppressing it here would deliver the answer
        # silently and the reply would go unnoticed.
        cli = FakeClient()
        r = TelegramRenderer(cli, 72, TELEGRAM_CAPABILITIES, session_key="telegram:1:0")  # type: ignore[arg-type]

        async def _go() -> None:
            await r.on_turn_start()
            # Throttle out the live frame -- the "segment never streamed"
            # case named in _seal_current's docstring.
            # flake-ok: pre-existing real-clock stamp that throttles out the live frame, moved verbatim; tracked for a de-flake pass
            r._last_edit = time.monotonic()
            await r.on_text_chunk("| a | b |\n| --- | --- |\n| 1 | 2 |")
            assert r._stream_mid is None, "precondition: nothing streamed"
            await r._seal_current()

        asyncio.run(_go())
        assert cli.rich_sent, "rich send happened"
        assert cli.rich_silent == [False], "the only message must notify"

    def test_a_fenced_reply_is_never_split_by_the_degraded_path(self) -> None:
        # Splitting renders the pieces with separate _md_to_telegram_html calls,
        # so a cut inside a fence tears the block and leaks its delimiters.
        # Deciding where a fence starts/ends reliably means reimplementing
        # CommonMark as a second parser; declining to split removes the class.
        # These are the exact shapes that each defeated a hand-rolled parser:
        cases = [
            # plain fenced table markup
            "Example:\n\n```\n| a | b |\n| --- | --- |\n| 1 | 2 |\n```\n\ndone",
            # a ~~~ line inside a ``` block (mismatched delimiter)
            "How:\n\n```\n~~~\n| a | b |\n| --- | --- |\n```\n\ndone",
            # an info string on an inner delimiter
            "How:\n\n```\n```python\n| a | b |\n| --- | --- |\n```\n\ndone",
            # a longer opener closed only by a shorter run
            "````\n```\n| a | b |\n| --- | --- |\n",
            # a block fenced with tildes only -- the guard must cover both
            # delimiter characters, not just backticks
            "Example:\n\n~~~\n| a | b |\n| --- | --- |\n| 1 | 2 |\n~~~\n\ndone",
        ]
        for text in cases:
            out = _seal_table_fallback(text)
            assert out == _md_to_telegram_html(
                text
            ), f"a fenced segment must render whole, unsplit: {text!r}"

    def test_the_degraded_path_still_aligns_tables_with_no_fence_present(self) -> None:
        # The no-split rule must not disable the feature for ordinary replies.
        text = "Here:\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n\n**after**"
        out = _seal_table_fallback(text)
        assert "<pre>" in out, "a fence-free table still gets monospace alignment"
        assert "<b>after</b>" in out, "prose around it keeps its formatting"

    def test_strip_steering_complete_and_unclosed(self) -> None:
        # Complete marker is removed anywhere in the text. The id is hex because
        # that is the grammar `messaging.driver` accepts -- the old "steer-x"
        # fixture was never a frame the driver would have taken.
        out = _strip_steering("BANANA [STEERING steer-ab12: rephrase] tail")
        assert "STEERING" not in out and out.startswith("BANANA") and out.endswith("tail")
        # UNCLOSED trailing marker (still streaming, no closing "]") is also
        # removed, so the live draft never previews text that on_done strips.
        assert _strip_steering("BANANA\n\n[STEERING steer-abc: interpreted as wanting") == "BANANA"
        # No marker -> unchanged.
        assert _strip_steering("just text") == "just text"

    def test_prose_that_merely_opens_with_the_sentinel_stays(self) -> None:
        """Opening with the sentinel is not being a marker.

        ``messaging.driver`` already rules that -- it requires ``steer-<id>`` --
        and so does the dashboard's own parser. A bare ``[STEERING`` class deleted
        ordinary prose from the delivered message, and because the class does not
        stop at a line end it ran on to whatever ``]`` came next: here a Markdown
        link two lines down, taking the text in between with it.
        """
        one_line = "Read the [STEERING] section, then [docs](x) for more."
        assert _strip_steering(one_line) == one_line
        across_lines = "[STEERING is the feature I mean\n\nsee the [docs](x) for it"
        assert _strip_steering(across_lines) == across_lines

    def test_a_dashed_steer_id_is_one_frame_to_both_patterns(self) -> None:
        """``messaging.driver`` accepts ``[0-9a-f-]+`` for the id, so a dashed id
        is a real frame -- and the two patterns here have to agree about it.

        ``_rotate_at_markers`` reads the summary at the offset the MARKER pattern
        chose, so an id class the marker accepts and the summary does not leaves
        the steer chip with no summary at all, which is the only new information
        that chip carries.
        """
        text = "[STEERING steer-a180-ae7f: checked the job id] tail"
        marker = telegram_renderer._STEER_MARKER_RE.search(text)
        assert marker is not None
        summary = telegram_renderer._STEER_SUMMARY_RE.match(text, marker.start())
        assert summary is not None and summary.group(1) == "checked the job id"
        assert _strip_steering(text) == "tail"

    def test_the_renderer_patterns_agree_with_the_driver_on_a_frame_corpus(self) -> None:
        """``messaging.driver`` is the authority on this grammar, so the renderer
        must not recognise a frame the driver rejects, or reject one it takes."""
        frames = [
            "[STEERING steer-ab12: switching to the job id]",
            "[STEERING steer-a180-ae7f: checked]",
            "[STEERING steer-ab12: switching to the job id\nand re-running it]",
            "[STEERING steer-ab12]",
        ]
        for frame in frames:
            assert messaging_driver._STEER_MARKER_RE.match(frame) is not None, frame
            assert telegram_renderer._STEER_MARKER_RE.fullmatch(frame) is not None, frame
            assert _strip_steering(f"before {frame} after") == "before  after"
        not_frames = [
            "[STEERING]",
            "[STEERING is the feature I mean]",
            "[STEERING steer-: nothing]",
        ]
        for frame in not_frames:
            assert messaging_driver._STEER_MARKER_RE.match(frame) is None, frame
            assert telegram_renderer._STEER_MARKER_RE.search(frame) is None, frame

    def _drive(self, events: list[OutputEvent]) -> FakeClient:
        cli = FakeClient()
        r = TelegramRenderer(cli, 55, TELEGRAM_CAPABILITIES, session_key="telegram:1:0")  # type: ignore[arg-type]

        async def _go() -> None:
            await r.on_turn_start()
            for ev in events:
                await r.dispatch(ev)

        asyncio.run(_go())
        return cli

    def test_rotation_keeps_an_open_fence_open_in_the_retained_tail(self) -> None:
        # Regression: mid-stream the source fence is still open (the model has
        # not emitted its closing ``` yet). _split_markdown balances each chunk
        # with a synthetic closer, which is right for sealed chunks but wrong for
        # the tail we keep streaming into: later tokens would land after that
        # closer, render outside <pre>, and the real closing fence would show up
        # literally.
        cli = FakeClient()
        r = TelegramRenderer(cli, 55, TELEGRAM_CAPABILITIES, session_key="telegram:1:0")  # type: ignore[arg-type]
        open_src = "intro\n\n```python\n" + "\n".join(
            f'    x{i} = a["k{i}"] & b<c>' for i in range(150)
        )
        assert open_src.count("```") % 2 == 1, "fixture must leave the fence open"
        r._buf = [open_src]
        asyncio.run(r._rotate_on_length())
        tail = "".join(r._buf)
        assert not tail.rstrip().endswith(
            "```"
        ), f"synthetic closer retained in streaming tail: {tail[-40:]!r}"

    def test_rotation_preserves_a_real_closing_fence(self) -> None:
        # Control for the above: when the source fence IS closed, the tail must
        # keep its genuine closer.
        cli = FakeClient()
        r = TelegramRenderer(cli, 55, TELEGRAM_CAPABILITIES, session_key="telegram:1:0")  # type: ignore[arg-type]
        closed_src = (
            "intro\n\n```python\n"
            + "\n".join(f'    y{i} = a["k{i}"] & b<c>' for i in range(150))
            + "\n```"
        )
        assert closed_src.count("```") % 2 == 0
        r._buf = [closed_src]
        asyncio.run(r._rotate_on_length())
        assert "".join(r._buf).rstrip().endswith("```")

    def test_streaming_strips_options_and_renders_keyboard(self) -> None:
        cli = self._drive(
            [
                OutputEvent(kind=TOOL_CALL, tool_call_id="t", title="fs_read"),
                OutputEvent(kind=TEXT_CHUNK, text="Hello. "),
                OutputEvent(kind=TEXT_CHUNK, text="Pick.\n\n[OPTIONS: A | B]"),
                OutputEvent(kind=DONE, stop_reason=""),
            ]
        )
        # Edit-streaming: the finished answer is the last edit, carrying the
        # [OPTIONS:] keyboard; the raw [OPTIONS:] directive is stripped from text.
        final_text = cli.final_text()
        final_kb = cli.final_markup()
        assert final_text == "Hello. Pick."  # [OPTIONS:] stripped
        labels = [b["text"] for row in final_kb["inline_keyboard"] for b in row]
        data = [b["callback_data"] for row in final_kb["inline_keyboard"] for b in row]
        tag = session_provenance_tag("telegram:1:0")
        assert labels == ["A", "B"]
        assert data == [f"opt:0:{tag}", f"opt:1:{tag}"]

    def test_streams_live_via_send_then_edit(self) -> None:
        # Edit-streaming (OpenClaw-style): send one real message, then edit it in
        # place as text arrives. No draft (the ghost/vanish source). The final
        # formatted content is the last edit; the initial send seeds the bubble.
        cli = self._drive(
            [
                OutputEvent(kind=TEXT_CHUNK, text="one "),
                OutputEvent(kind=TEXT_CHUNK, text="two "),
                OutputEvent(kind=TEXT_CHUNK, text="three"),
                OutputEvent(kind=DONE, stop_reason=""),
            ]
        )
        assert cli.drafts == []  # no draft preview -> no ghost bubble
        assert cli.sent  # a real message was sent (live seed)
        assert cli.final_text() == "one two three"  # final formatted content

    def test_tool_footer_surfaces_immediately_on_tool_call(self) -> None:
        # A mid-turn tool call surfaces a transient "🔧 {tool}…" footer on the
        # live bubble immediately (force bypasses the edit throttle), so long
        # agentic turns show activity instead of a dead typing indicator.
        cli = self._drive(
            [
                OutputEvent(kind=TEXT_CHUNK, text="Checking the logs. "),
                OutputEvent(kind=TOOL_CALL, tool_call_id="t1", title="grep"),
                OutputEvent(kind=DONE, stop_reason=""),
            ]
        )
        frames = [t for t, _ in cli.sent] + [t for _, t, _ in cli.edits]
        assert any("🔧 grep…" in f for f in frames)  # footer shown live

    def test_tool_footer_cleared_by_text_and_absent_from_final(self) -> None:
        # The footer is transient: cleared the moment text resumes, and never
        # part of the sealed/final message (seals read _segment_text, which the
        # footer is deliberately kept out of).
        cli = self._drive(
            [
                OutputEvent(kind=TOOL_CALL, tool_call_id="t1", title="fs_read"),
                OutputEvent(kind=TEXT_CHUNK, text="Found it. "),
                OutputEvent(kind=TOOL_CALL, tool_call_id="t2", title="shell"),
                OutputEvent(kind=TEXT_CHUNK, text="All done."),
                OutputEvent(kind=DONE, stop_reason=""),
            ]
        )
        frames = [t for t, _ in cli.sent] + [t for _, t, _ in cli.edits]
        assert any("🔧 fs_read…" in f for f in frames)
        assert any("🔧 shell…" in f for f in frames)
        assert "🔧" not in cli.final_text()  # final is clean
        assert cli.final_text() == "Found it. All done."

    def test_strips_steering_marker_from_output(self) -> None:
        # kiro-cli's inline "[STEERING steer-<id>: …]" ack marker must never leak
        # raw into any posted message. An end-of-stream marker (no continuation
        # text after it) produces NO extra message — just the sealed answer.
        cli = self._drive(
            [
                OutputEvent(kind=TEXT_CHUNK, text="UTC is 09:03. BANANA\n\n"),
                OutputEvent(
                    kind=TEXT_CHUNK,
                    text="[STEERING steer-f0783769b5c44c5a9b40de895109315f: "
                    "stop, only say BANANA]",
                ),
                OutputEvent(kind=DONE, stop_reason=""),
            ]
        )
        texts = [t for t, _ in cli.sent]
        assert texts == ["UTC is 09:03. BANANA"]  # sealed clean, no ack tail
        assert "STEERING" not in " ".join(texts)

    def test_steer_seals_at_marker_into_new_bubble(self) -> None:
        # Seal-on-steer: the [STEERING] marker (kiro-cli's in-stream injection
        # point) seals the pre-steer text as its own message; the steered
        # continuation opens a FRESH message headed by a chip. The chip prefers
        # the SUMMARY embedded in the marker (dashboard parity) over the user's
        # own words — those are already on screen as the user's message.
        cli = FakeClient()
        r = TelegramRenderer(cli, 55, TELEGRAM_CAPABILITIES, session_key="telegram:1:0")  # type: ignore[arg-type]
        r.note_steer("stop and say BANANA")  # the dispatcher records the user's words

        async def _go() -> None:
            await r.on_turn_start()
            await r.dispatch(OutputEvent(kind=TEXT_CHUNK, text="Root at 86% used. "))
            await r.dispatch(OutputEvent(kind=STEER_CONSUMED, text="stop"))
            await r.dispatch(OutputEvent(kind=TEXT_CHUNK, text="BANANA"))
            await r.dispatch(OutputEvent(kind=DONE, stop_reason=""))

        asyncio.run(_go())
        texts = [t for t, _ in cli.sent]
        assert len(texts) == 2  # pre-steer bubble sealed + steered continuation
        assert texts[0] == "Root at 86% used."  # frozen pre-steer bubble
        assert "STEERING" not in texts[0] and "STEERING" not in texts[1]
        # The chip heads the new bubble with the MARKER's summary (not the quote).
        assert texts[1].startswith("<blockquote>↪️ stop</blockquote>")
        assert "BANANA" in texts[1] and "used.BANANA" not in texts[1]  # no leak

    def test_end_of_stream_marker_posts_no_tail_bubble(self) -> None:
        # Marker at the very END of the stream (kiri-cli folded the steer but
        # emitted no post-steer text): NO tail message at all. The answer already
        # covered the steer and the user's message carries the reaction receipt —
        # any trailing ack bubble (quote OR summary) is pure noise.
        cli = FakeClient()
        r = TelegramRenderer(cli, 55, TELEGRAM_CAPABILITIES, session_key="telegram:1:0")  # type: ignore[arg-type]
        r.note_steer("顺便看看今天悉尼什么天气")

        async def _go() -> None:
            await r.on_turn_start()
            await r.dispatch(OutputEvent(kind=TEXT_CHUNK, text="目录总结。天气:多云。"))
            await r.dispatch(
                OutputEvent(
                    kind=TEXT_CHUNK,
                    text="\n\n[STEERING steer-a180ae7f: 已并行查询悉尼天气,一并答复。]",
                )
            )
            await r.dispatch(OutputEvent(kind=DONE, stop_reason=""))

        asyncio.run(_go())
        texts = [t for t, _ in cli.sent]
        assert len(texts) == 1  # only the sealed answer — no ack tail
        assert "STEERING" not in texts[0]
        assert "已并行查询" not in texts[0] and "顺便看看" not in texts[0]

    def test_options_keyboard_survives_length_rotation(self) -> None:
        # Codex finding: [OPTIONS:] must be extracted BEFORE length rotation.
        # A body long enough to rotate must still attach the keyboard to the
        # FINAL message — not strand the options text in a sealed segment and
        # fall into the "…" placeholder branch.
        cli = self._drive(
            [
                OutputEvent(kind=TEXT_CHUNK, text=("line\n" * 1200)),  # > limit
                OutputEvent(kind=TEXT_CHUNK, text="Pick one.\n\n[OPTIONS: A | B]"),
                OutputEvent(kind=DONE, stop_reason=""),
            ]
        )
        kb = cli.final_markup()
        assert kb is not None
        labels = [b["text"] for row in kb["inline_keyboard"] for b in row]
        assert labels == ["A", "B"]
        # The options directive never leaks into any posted text.
        all_text = " ".join(t for t, _ in cli.sent) + " ".join(t for _, t, _ in cli.edits)
        assert "[OPTIONS" not in all_text

    def test_long_options_before_streamed_steer_ack_become_keyboard(self) -> None:
        cli = self._drive(
            [
                OutputEvent(
                    kind=TEXT_CHUNK,
                    text=("x" * 7680)
                    + "\n\n[OPTIONS: Alpha | Bravo | Charlie]"
                    + "\n\n[STEERING steer-7e6a4a0d",
                ),
                OutputEvent(kind=TEXT_CHUNK, text="94314d2db: acknowledged]"),
                OutputEvent(kind=DONE, stop_reason=""),
            ]
        )

        markups = [m for _, m in cli.sent if m] + [m for _, _, m in cli.edits if m]
        labels = [b["text"] for row in markups[0]["inline_keyboard"] for b in row]
        data = [b["callback_data"] for row in markups[0]["inline_keyboard"] for b in row]
        tag = session_provenance_tag("telegram:1:0")
        assert labels == ["Alpha", "Bravo", "Charlie"]
        assert data == [f"opt:{index}:{tag}" for index in range(3)]
        visible = "\n".join([t for t, _ in cli.sent] + [t for _, t, _ in cli.edits])
        assert "[OPTIONS" not in visible
        assert "[STEERING" not in visible
        assert "steer-7e6a4a0d" not in visible
        assert "94314d2db" not in visible

    def test_tool_only_message_not_orphaned_at_steer_boundary(self) -> None:
        # Codex finding: a tool call BEFORE any assistant text creates a live
        # "🔧 tool…" message. If a steer marker then arrives with no pre-marker
        # text, nothing is sealed — the renderer must KEEP that message id so
        # the steered continuation replaces the transient footer in place,
        # instead of orphaning a permanent tool-footer bubble.
        cli = self._drive(
            [
                OutputEvent(kind=TOOL_CALL, tool_call_id="t1", title="grep"),
                OutputEvent(
                    kind=TEXT_CHUNK,
                    text="[STEERING steer-abc123: checked] steered answer.",
                ),
                OutputEvent(kind=DONE, stop_reason=""),
            ]
        )
        # Exactly ONE message ever sent (the tool-footer one, reused).
        assert len(cli.sent) == 1
        assert cli.final_text() is not None
        assert "steered answer" in cli.final_text()
        assert "🔧" not in cli.final_text()

    def test_options_only_response_keeps_keyboard(self) -> None:
        # Codex finding: a response that is ONLY "[OPTIONS: A | B]" leaves an
        # empty body after extraction — the placeholder must still carry the
        # keyboard, not silently drop the user's choices.
        cli = self._drive(
            [
                OutputEvent(kind=TEXT_CHUNK, text="[OPTIONS: A | B]"),
                OutputEvent(kind=DONE, stop_reason=""),
            ]
        )
        markups = [m for _, m in cli.sent if m] + [m for _, _, m in cli.edits if m]
        assert len(markups) == 1
        labels = [b["text"] for row in markups[0]["inline_keyboard"] for b in row]
        assert labels == ["A", "B"]

    def test_complete_options_straddling_length_cut_stays_intact(self) -> None:
        # Codex finding: a COMPLETE trailing [OPTIONS: A | B] whose text
        # crosses the length-rotation boundary must be detached whole before
        # _split_markdown — a bare split would put "[OPTIO" in one message and
        # "NS: A | B]" in the next, leaking protocol text and losing the
        # keyboard. Body sized so the directive itself straddles the cut.
        body = "x" * 3730  # just under the ~3840 limit; directive crosses it
        cli = self._drive(
            [
                OutputEvent(kind=TEXT_CHUNK, text=body),
                OutputEvent(
                    kind=TEXT_CHUNK,
                    text="\n\nPick one below please.\n\n[OPTIONS: A | B]",
                ),
                OutputEvent(kind=DONE, stop_reason=""),
            ]
        )
        # The final segment never live-streamed (rotation happened at the very
        # end), so the seal SENDS a fresh message — find the keyboard across
        # both sends and edits rather than via final_markup().
        markups = [m for _, m in cli.sent if m] + [m for _, _, m in cli.edits if m]
        assert len(markups) == 1
        labels = [b["text"] for row in markups[0]["inline_keyboard"] for b in row]
        assert labels == ["A", "B"]
        # The options directive never leaks — whole or split — into any text.
        all_text = " ".join(t for t, _ in cli.sent) + " ".join(t for _, t, _ in cli.edits)
        assert "[OPTIO" not in all_text and "NS: A | B]" not in all_text

    def test_double_marker_chunk_keeps_first_chip(self) -> None:
        # Codex finding: one chunk carrying TWO complete [STEERING] markers must
        # not overwrite the first steer's chip — its continuation seals WITH the
        # chip before the second rotation.
        cli = self._drive(
            [
                OutputEvent(
                    kind=TEXT_CHUNK,
                    text=(
                        "base answer. "
                        "[STEERING steer-aaa111: first ack] first continuation. "
                        "[STEERING steer-bbb222: second ack] second continuation."
                    ),
                ),
                OutputEvent(kind=DONE, stop_reason=""),
            ]
        )
        texts = [t for t, _ in cli.sent]
        assert len(texts) == 3  # base + two steered continuations
        assert "first ack" in texts[1] and "first continuation" in texts[1]
        assert "second ack" in texts[2] and "second continuation" in texts[2]
        assert "STEERING" not in " ".join(texts)

    def test_hr_inside_code_fence_preserved(self) -> None:
        # Codex finding: _strip_hr must not delete a standalone "---" INSIDE a
        # fenced code block (e.g. a YAML document separator).
        cli = self._drive(
            [
                OutputEvent(
                    kind=TEXT_CHUNK,
                    text="Config:\n```yaml\na: 1\n---\nb: 2\n```\ndone",
                ),
                OutputEvent(kind=DONE, stop_reason=""),
            ]
        )
        assert "---" in cli.final_text()  # YAML separator survives
        # A bare HR OUTSIDE a fence is still stripped.
        cli2 = self._drive(
            [
                OutputEvent(kind=TEXT_CHUNK, text="above\n\n---\n\nbelow"),
                OutputEvent(kind=DONE, stop_reason=""),
            ]
        )
        assert "---" not in cli2.final_text()

    def test_live_frames_never_leak_options_markup(self) -> None:
        # Codex finding: a live frame streamed from a chunk that already carries
        # the complete [OPTIONS:] directive must hold it back, not show it.
        cli = self._drive(
            [
                OutputEvent(kind=TEXT_CHUNK, text="Pick one.\n\n[OPTIONS: A | B]"),
                OutputEvent(kind=TEXT_CHUNK, text=""),
                OutputEvent(kind=DONE, stop_reason=""),
            ]
        )
        frames = [t for t, _ in cli.sent] + [t for _, t, _ in cli.edits]
        assert all("[OPTIONS" not in f for f in frames)
        labels = [b["text"] for row in cli.final_markup()["inline_keyboard"] for b in row]
        assert labels == ["A", "B"]

    def test_length_rotation_keeps_fences_balanced(self) -> None:
        # Codex finding: length rotation must not cut a fenced code block in
        # half — every sealed message carries balanced fences (via
        # _split_markdown's close-and-reopen).
        big_code = "```python\n" + ("x = 1\n" * 1500) + "```"
        cli = self._drive(
            [
                OutputEvent(kind=TEXT_CHUNK, text=big_code),
                OutputEvent(kind=DONE, stop_reason=""),
            ]
        )
        assert len(cli.sent) >= 2  # rotated at least once
        # Sealed HTML frames render the code as <pre> (fence recognized), and
        # no posted frame carries an odd number of literal fences.
        finals = [t for t, _ in cli.sent]
        assert any("<pre>" in t for t in finals)
        for t in finals:
            assert t.count("```") % 2 == 0

    def test_steer_summary_respects_transport_limit(self) -> None:
        # Codex finding: the no-marker steer summary is prepended BEFORE length
        # rotation, so the final message can never exceed Telegram's cap.
        cli = FakeClient()
        r = TelegramRenderer(cli, 55, TELEGRAM_CAPABILITIES, session_key="telegram:1:0")  # type: ignore[arg-type]
        for i in range(10):
            r.note_steer(f"steer number {i} " + "y" * 100)

        async def _go() -> None:
            await r.on_turn_start()
            await r.dispatch(OutputEvent(kind=TEXT_CHUNK, text="body " * 780))
            await r.dispatch(OutputEvent(kind=DONE, stop_reason=""))

        asyncio.run(_go())
        for t, _ in cli.sent:
            assert len(t) <= 4096
        for _, t, _ in cli.edits:
            assert len(t) <= 4096

    def test_oversized_pre_marker_segment_rotates_before_seal(self) -> None:
        # Codex finding: a chunk can deliver over-limit text AND a complete
        # [STEERING] marker together. The pre-marker segment must length-rotate
        # before sealing, or the client truncates it at Telegram's cap.
        big = "word " * 1300  # ~6500 chars > limit
        cli = self._drive(
            [
                OutputEvent(
                    kind=TEXT_CHUNK,
                    text=big + "[STEERING steer-abc123: ack] tail answer.",
                ),
                OutputEvent(kind=DONE, stop_reason=""),
            ]
        )
        finals = [t for t, _ in cli.sent]
        assert len(finals) >= 3  # pre-marker rotated into >=2 + continuation
        for t in finals:
            assert len(t) <= 4096  # nothing exceeds the transport cap
        joined = " ".join(finals)
        assert joined.count("word") >= 1290  # no pre-marker content lost
        assert "tail answer" in finals[-1]

    def test_partial_directive_never_split_by_length_rotation(self) -> None:
        # Codex finding: an INCOMPLETE trailing directive crossing the length
        # limit must be detached before splitting and reattached to the tail —
        # never cut in half (which would leak fragments and lose the rotation).
        big = "text " * 1300
        cli = self._drive(
            [
                OutputEvent(kind=TEXT_CHUNK, text=big + "[STEERING steer-ddd444: par"),
                OutputEvent(kind=TEXT_CHUNK, text="tial ack] steered tail."),
                OutputEvent(kind=DONE, stop_reason=""),
            ]
        )
        finals = [t for t, _ in cli.sent]
        joined = " ".join(finals)
        assert "STEERING" not in joined  # directive never leaked, whole or split
        assert "steered tail" in joined  # rotation still happened
        frames = [t for _, t, _ in cli.edits]
        assert all("[STEERING" not in f for f in frames)  # nor on live frames

    def test_seal_resends_when_live_message_deleted(self) -> None:
        # Codex finding: if the user deletes the streamed message mid-turn,
        # both seal edits fail — the final answer (and keyboard) must be
        # re-SENT as a fresh message, never silently lost.
        class _EditsFailClient(FakeClient):
            async def edit_message(self, *a: Any, **kw: Any) -> bool:
                await super().edit_message(*a, **kw)
                return False  # message gone — every edit fails

        cli = _EditsFailClient()
        r = TelegramRenderer(cli, 55, TELEGRAM_CAPABILITIES, session_key="telegram:1:0")  # type: ignore[arg-type]

        async def _go() -> None:
            await r.on_turn_start()
            await r.dispatch(OutputEvent(kind=TEXT_CHUNK, text="final answer"))
            await r.dispatch(OutputEvent(kind=TEXT_CHUNK, text="\n\n[OPTIONS: A | B]"))
            await r.dispatch(OutputEvent(kind=DONE, stop_reason=""))

        asyncio.run(_go())
        # Last SENT message carries the final content + keyboard.
        final_text, final_kb = cli.sent[-1]
        assert "final answer" in final_text
        labels = [b["text"] for row in final_kb["inline_keyboard"] for b in row]
        assert labels == ["A", "B"]

    @staticmethod
    def _parts_seen(fails: Any) -> list[str]:
        # A three-part reply; ``fails(text, kwargs)`` says which sends fail.
        class _Client(FakeClient):
            async def send_message(self, chat_id: int, text: str, **kw: Any) -> Any:
                if fails(text, kw):
                    return None
                return await super().send_message(chat_id, text, **kw)

        cli = _Client()
        r = TelegramRenderer(cli, 55, TELEGRAM_CAPABILITIES, session_key="telegram:1:0")  # type: ignore[arg-type]
        reply = "\n\n".join(
            (
                "Part one. " + "alpha " * 500,
                "Part two. " + "bravo " * 500,
                "Part three. " + "charlie " * 400,
            )
        )

        async def _go() -> None:
            await r.on_turn_start()
            await r.dispatch(OutputEvent(kind=TEXT_CHUNK, text=reply))
            await r.dispatch(OutputEvent(kind=DONE, stop_reason=""))

        asyncio.run(_go())
        names = {"alpha": "1", "bravo": "2", "charlie": "3"}
        seen = []
        for text, _kb in cli.sent:
            seen.append(next((n for w, n in names.items() if w in text), text))
        return seen

    def test_a_part_that_cannot_be_delivered_leaves_a_notice_in_its_place(self) -> None:
        # Every send of the middle part fails. The later part still goes out, so
        # a notice stands where the part is missing and the reply does not read
        # as complete.
        seen = self._parts_seen(lambda text, kw: "bravo" in text)
        assert seen == ["1", "Part of this reply may not have been delivered.", "3"]

    def test_a_reply_whose_parts_all_land_has_no_notice(self) -> None:
        assert self._parts_seen(lambda text, kw: False) == ["1", "2", "3"]

    def test_a_part_delivered_by_its_plain_fallback_has_no_notice(self) -> None:
        seen = self._parts_seen(lambda text, kw: "bravo" in text and kw.get("parse_mode"))
        assert seen == ["1", "2", "3"]

    @staticmethod
    def _overflowing_table() -> str:
        # A pipe table that fits one segment while it streams but whose degraded
        # render is split into several messages when it is sealed.
        rows = "".join(f"| row {i} | value {i} |\n" for i in range(400))
        return "| name | value |\n| --- | --- |\n" + rows

    @staticmethod
    def _sealed_over_dead_edits(
        table: bool, fails: Any, lost: str | None = None
    ) -> tuple[FakeClient, list[int], int, list[str]]:
        # Seals one streamed answer, or a pipe table whose degraded render splits,
        # while every edit fails although the live frame still exists (the per-chat
        # edit budget, or two timeouts). ``fails(text, kwargs)`` says which sends
        # fail once the live frame has landed, and ``lost`` ("first", "second" or
        # "last") names one part of the table whose every send fails, its notice
        # still going out. Returns the client, the id of each send that landed (the
        # live frame first), the number of parts the reply is sealed in, and the
        # sends and deletes in the order they happened.
        notice = "Part of this reply may not have been delivered."
        ids: list[int] = []
        order: list[str] = []
        lost_at = 0  # the number of the part whose sends all fail, 0 for none

        class _Client(FakeClient):
            async def edit_message(self, *a: Any, **kw: Any) -> bool:
                await super().edit_message(*a, **kw)
                return False

            async def send_message(self, chat_id: int, text: str, **kw: Any) -> Any:
                # The live frame plus the parts landed so far: the part now being
                # sent is number ``len(ids)``.
                part_lost = len(ids) == lost_at and text != notice
                if ids and (fails(text, kw) or part_lost):
                    return None
                ids.append(await super().send_message(chat_id, text, **kw))
                order.append(f"send {ids[-1]}")
                return ids[-1]

            async def delete_message(self, chat_id: int, message_id: int) -> None:
                order.append(f"delete {message_id}")
                await super().delete_message(chat_id, message_id)

        cli = _Client()
        cli.rich_fails = True
        r = TelegramRenderer(cli, 55, TELEGRAM_CAPABILITIES, session_key="telegram:1:0")  # type: ignore[arg-type]
        text = TestRenderer._overflowing_table()
        parts = 1
        if table:
            assert len(text) <= r._rich_limit(), "precondition: one segment while streaming"
            assert (
                len(_md_to_telegram_html(text)) > r._rendered_limit()
            ), "precondition: the degraded seal must split, so its first chunk takes the frame"
            parts = len(r._degraded_table_chunks(text))
        if lost:
            assert parts >= 3, "precondition: a first chunk, a middle chunk and a tail"
            lost_at = {"first": 1, "second": 2, "last": parts}[lost]

        async def _answer() -> None:
            await r.on_turn_start()
            await r.dispatch(OutputEvent(kind=TEXT_CHUNK, text="first half"))
            await r.dispatch(OutputEvent(kind=TEXT_CHUNK, text=", second half"))
            await r.dispatch(OutputEvent(kind=DONE, stop_reason=""))

        async def _degraded_table() -> None:
            await r.on_turn_start()
            await r.on_text_chunk(text)  # the live frame is sent
            await r._seal_current()

        asyncio.run(_degraded_table() if table else _answer())
        return cli, ids, parts, order

    @pytest.mark.parametrize("table", [False, True], ids=["answer", "degraded table"])
    @pytest.mark.parametrize("html_send_fails", [False, True], ids=["html send", "plain send"])
    def test_a_resent_answer_replaces_the_live_frame_it_could_not_edit(
        self, table: bool, html_send_fails: bool
    ) -> None:
        # The answer is sent as a new message, and the live frame above it, an older
        # and shorter draft, is deleted, as the rich path deletes the bubble it
        # supersedes. "degraded table" is the first chunk of an overflowing table,
        # which re-uses the live frame through its own ladder; either send of a
        # ladder (the HTML one, then the plain one) can be the one that lands. The
        # frame goes last: until every part has landed it is the only copy of what
        # the reader saw.
        cli, ids, parts, order = self._sealed_over_dead_edits(
            table, lambda text, kw: html_send_fails and bool(kw.get("parse_mode"))
        )
        assert len(ids) == 1 + parts, "the live frame, then each part as a new message"
        assert cli.deleted == [ids[0]]
        assert order == [f"send {i}" for i in ids] + [f"delete {ids[0]}"]

    @pytest.mark.parametrize("table", [False, True], ids=["answer", "degraded table"])
    def test_a_failed_resend_keeps_the_live_frame(self, table: bool) -> None:
        # When the fallback send fails too, the live frame is the only copy of the
        # answer the reader has, so it stays, and a notice stands where each part is
        # missing.
        notice = "Part of this reply may not have been delivered."
        cli, _ids, parts, _order = self._sealed_over_dead_edits(
            table, lambda text, kw: text != notice
        )
        assert cli.deleted == []
        assert [text for text, _kb in cli.sent[1:]] == [notice] * parts

    @pytest.mark.parametrize("lost", ["first", "second", "last"])
    def test_a_lost_part_keeps_the_live_frame(self, lost: str) -> None:
        # Each part of an overflowing table is a send of its own, and the pressure
        # that failed the edits can fail any one of them. The frame holds rows the
        # reader has already seen, and the parts that landed may not carry them, so
        # one lost part keeps it, wherever that part falls: the first chunk, a middle
        # chunk (the first one to go out below the frame) or the tail (sealed by
        # ``_seal_text``). A notice stands where the part is missing; the rest land.
        notice = "Part of this reply may not have been delivered."
        cli, ids, parts, _order = self._sealed_over_dead_edits(True, lambda text, kw: False, lost)
        at = {"first": 1, "second": 2, "last": parts}[lost]
        sent = [text for text, _kb in cli.sent[1:]]
        assert len(sent) == parts, "every part either landed or left a notice"
        assert [n for n, text in enumerate(sent, 1) if text == notice] == [at]
        assert len(ids) == 1 + parts
        assert cli.deleted == []

    def test_a_chunk_edited_into_the_live_frame_leaves_it_alone(self) -> None:
        # With working edits, the first chunk of an overflowing table lands in the
        # live frame itself and the rest go out below it. The frame now carries that
        # chunk, so nothing replaces it, and deleting it would delete the chunk.
        cli = FakeClient()
        cli.rich_fails = True
        r = TelegramRenderer(cli, 55, TELEGRAM_CAPABILITIES, session_key="telegram:1:0")  # type: ignore[arg-type]
        text = self._overflowing_table()
        parts = len(r._degraded_table_chunks(text))
        assert parts >= 3, "precondition: the table splits, so its first chunk takes the frame"

        async def _go() -> None:
            await r.on_turn_start()
            await r.on_text_chunk(text)  # the live frame is sent
            await r._seal_current()

        asyncio.run(_go())
        assert len(cli.edits) == 1, "the first chunk is edited into the live frame"
        assert len(cli.sent) == parts, "the live frame, then every other part as a new message"
        assert cli.deleted == []

    def test_error_done_renders_error_when_no_text(self) -> None:
        cli = self._drive([OutputEvent(kind=DONE, stop_reason="error")])
        assert "Error" in cli.sent[-1][0]

    def test_close_is_idempotent_after_done(self) -> None:
        cli = FakeClient()
        r = TelegramRenderer(cli, 55, TELEGRAM_CAPABILITIES)  # type: ignore[arg-type]

        async def _go() -> int:
            await r.on_turn_start()
            await r.dispatch(OutputEvent(kind=TEXT_CHUNK, text="hi"))
            await r.dispatch(OutputEvent(kind=DONE, stop_reason=""))
            n = len(cli.sent)
            await r.close()  # should no-op
            return len(cli.sent) - n

        assert asyncio.run(_go()) == 0

    def test_close_with_failure_reason_replaces_generic_placeholder(self) -> None:
        # A permanent failure's sanitized reason must reach the user instead of
        # the misleading "please try again" text.
        cli = FakeClient()
        r = TelegramRenderer(cli, 55, TELEGRAM_CAPABILITIES)  # type: ignore[arg-type]
        reason = "⚠️ Your account does not have access to model 'x'. Pick one in the picker."

        async def _go() -> None:
            await r.on_turn_start()
            await r.close(failure_reason=reason)

        asyncio.run(_go())
        assert cli.sent[-1][0] == reason
        assert "please try again" not in cli.sent[-1][0]

    def test_close_without_reason_keeps_generic_error_text(self) -> None:
        cli = FakeClient()
        r = TelegramRenderer(cli, 55, TELEGRAM_CAPABILITIES)  # type: ignore[arg-type]

        async def _go() -> None:
            await r.on_turn_start()
            await r.close()

        asyncio.run(_go())
        assert "please try again" in cli.sent[-1][0]

    def test_close_reason_ignored_after_normal_done(self) -> None:
        # A reason arriving after on_done already finalized must not post
        # anything: close() stays a strict no-op post-finalization.
        cli = FakeClient()
        r = TelegramRenderer(cli, 55, TELEGRAM_CAPABILITIES)  # type: ignore[arg-type]

        async def _go() -> int:
            await r.on_turn_start()
            await r.dispatch(OutputEvent(kind=TEXT_CHUNK, text="hi"))
            await r.dispatch(OutputEvent(kind=DONE, stop_reason=""))
            n = len(cli.sent)
            await r.close(failure_reason="⚠️ too late")
            return len(cli.sent) - n

        assert asyncio.run(_go()) == 0


class TestApprovalDecider:
    def test_resolve_pending(self) -> None:
        async def _go() -> bool:
            d = TelegramApprovalDecider(session_key="telegram:1:0")
            TelegramApprovalDecider.arm("telegram:1:0:rq7", "n1")
            task = asyncio.ensure_future(d(SimpleNamespace(request_id="rq7")))
            # flake-ok: pre-existing 20 ms yield barrier before the resolve, moved verbatim; tracked for a de-flake pass
            # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing 20 ms yield barrier before the resolve, moved verbatim; tracked for a de-flake pass
            await asyncio.sleep(0.02)
            TelegramApprovalDecider.resolve_global("telegram:1:0:rq7", True, nonce="n1")
            # flake-ok: pre-existing unbounded await of the decider task, moved verbatim; tracked for a de-flake pass
            return await task

        assert asyncio.run(_go()) is True

    def test_resolve_unknown_key_returns_false(self) -> None:
        assert TelegramApprovalDecider.resolve_global("no-such-key", True, nonce="n1") is False

    def test_a_stale_keyboard_cannot_approve_a_live_prompt(self) -> None:
        """Request ids restart at 1 per provider process.

        So a button left in a Telegram chat from a previous run names an id that is
        live again for a DIFFERENT tool. The nonce is what refuses it.
        """

        async def _go() -> bool:
            d = TelegramApprovalDecider(session_key="telegram:1:0")
            TelegramApprovalDecider.arm("telegram:1:0:rq7", "fresh")
            task = asyncio.ensure_future(d(SimpleNamespace(request_id="rq7")))
            # flake-ok: pre-existing 20 ms yield barrier before the resolve, moved verbatim; tracked for a de-flake pass
            # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing 20 ms yield barrier before the resolve, moved verbatim; tracked for a de-flake pass
            await asyncio.sleep(0.02)
            stale = TelegramApprovalDecider.resolve_global(
                "telegram:1:0:rq7", True, nonce="from-a-previous-run"
            )
            assert stale is False, "a stale press must resolve nothing"
            # The prompt is still waiting: the stale press neither approved nor
            # consumed it.
            TelegramApprovalDecider.resolve_global("telegram:1:0:rq7", False, nonce="fresh")
            # flake-ok: pre-existing unbounded await of the decider task, moved verbatim; tracked for a de-flake pass
            return await task

        assert asyncio.run(_go()) is False

    def test_an_unarmed_prompt_fails_closed(self) -> None:
        """No nonce armed means no widget this process minted, so nothing to answer."""

        async def _go() -> bool:
            d = TelegramApprovalDecider(session_key="telegram:1:0")
            task = asyncio.ensure_future(d(SimpleNamespace(request_id="rq8")))
            # flake-ok: pre-existing 20 ms yield barrier before the resolve, moved verbatim; tracked for a de-flake pass
            # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing 20 ms yield barrier before the resolve, moved verbatim; tracked for a de-flake pass
            await asyncio.sleep(0.02)
            assert TelegramApprovalDecider.resolve_global("telegram:1:0:rq8", True) is False
            TelegramApprovalDecider._REGISTRY["telegram:1:0:rq8"].set_result(False)
            # flake-ok: pre-existing unbounded await of the decider task, moved verbatim; tracked for a de-flake pass
            return await task

        assert asyncio.run(_go()) is False
