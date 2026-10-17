"""Telegram renderer: how a long answer is cut into messages, and what each piece holds.

Covers table-aware splitting with rich budget selection (renderer.py), the rotation seam
that must never hand a reader a credential split across two bubbles, and the threaded
notice that follows an answer the seal redacted.
"""

from __future__ import annotations

import asyncio
import html
import re
from typing import Any

import pytest
from telegram_helpers import _drop_live_config_snapshot  # noqa: F401
from telegram_helpers import FakeClient

from conftest import CREDENTIAL_STRADDLE_SHAPES
from kiro_crew.messaging.display_safety import (
    canonicalize_display,
    joins_to_a_credential,
    severs_a_credential,
)
from kiro_crew.telegram.client import TELEGRAM_MAX_TEXT
from kiro_crew.telegram.renderer import (
    TelegramRenderer,
    _default_redactor,
    _delivered_form,
    _has_table,
    _md_to_telegram_html,
    _rendered_len,
    _split_markdown_bounded,
    _split_markdown_table_aware,
    _split_table_rows,
    safe_split_offset,
)
from kiro_crew.telegram.transport import TELEGRAM_CAPABILITIES


class TestTableAwareSplitting:
    def _renderer(self, cli: FakeClient) -> TelegramRenderer:
        return TelegramRenderer(cli, 55, TELEGRAM_CAPABILITIES, session_key="telegram:1:0")  # type: ignore[arg-type]

    def _table(self, rows: int, fill: str = "x", width: int = 80) -> str:
        head = "| id | data |\n| --- | --- |\n"
        return head + "".join(f"| {i:05d} | {fill * width} |\n" for i in range(rows))

    def test_a_table_that_fits_one_rich_message_is_never_split(self) -> None:
        # A table that overflows the HTML budget must not be cut row-wise, which
        # strands header-less body rows on the literal-pipe path. Sized against
        # the rich budget it is one segment, one sendRichMessage, one rendered
        # table.
        cli = FakeClient()
        r = self._renderer(cli)
        table = self._table(120)
        assert (
            r._limit() < len(table) <= r._rich_limit()
        ), "precondition: overflows the HTML budget, fits the rich budget"

        async def _go() -> None:
            await r.on_turn_start()
            await r.on_text_chunk(table)
            await r.on_done()

        asyncio.run(_go())

        assert len(cli.rich_sent) == 1, "one logical table -> one rich message"
        md = cli.rich_sent[0][0]
        assert "| 00000 |" in md and "| 00119 |" in md, "no row lost"
        assert _has_table(md)

    def test_a_table_over_the_rich_budget_splits_into_table_detected_chunks(self) -> None:
        # When even the rich budget overflows, cuts land at row boundaries and
        # every continuation repeats the header + separator, so EVERY chunk is
        # table-detected and renders rich -- never a ragged pipe-text tail.
        cli = FakeClient()
        r = self._renderer(cli)
        table = self._table(300, fill="y", width=120)
        assert len(table) > r._rich_limit(), "precondition: overflows the rich budget"

        async def _go() -> None:
            await r.on_turn_start()
            await r.on_text_chunk(table)
            await r.on_done()

        asyncio.run(_go())

        assert len(cli.rich_sent) >= 2, "an over-rich-budget table needs several rich sends"
        for md, _, _ in cli.rich_sent:
            assert _has_table(md), "every chunk carries the header, so it seals rich"
            assert len(md) <= r._rich_limit()
        joined = "\n".join(md for md, _, _ in cli.rich_sent)
        for i in (0, 150, 299):
            assert f"| {i:05d} |" in joined, "no row lost across the chunks"

    def test_an_oversize_table_degrades_uniformly_when_rich_is_unavailable(self) -> None:
        # A segment sized against the rich budget can be several HTML messages
        # long. When the rich send fails it must be re-split and shipped whole
        # -- truncation would silently drop rows -- and header repetition keeps
        # every chunk on the <pre> path, so degradation is uniform.
        cli = FakeClient()
        cli.rich_fails = True
        r = self._renderer(cli)
        table = self._table(120, fill="k", width=90)
        assert r._limit() < len(table) <= r._rich_limit()

        async def _go() -> None:
            await r.on_turn_start()
            await r.on_text_chunk(table)
            await r.on_done()

        asyncio.run(_go())

        assert cli.rich_sent == []
        bodies = [e[1] for e in cli.edits if "<pre>" in e[1]]
        bodies += [s[0] for s in cli.sent if "<pre>" in s[0]]
        assert len(bodies) >= 2, "the oversize segment ships as several HTML messages"
        joined = "\n".join(bodies)
        for i in (0, 60, 119):
            assert f"| {i:05d} |" in joined, "no row lost to truncation"
        for b in bodies:
            assert len(b) <= TELEGRAM_MAX_TEXT, "each chunk respects the hard cap"

    def test_split_table_rows_repeats_the_header_on_every_chunk(self) -> None:
        rows = ["| a | b |", "| --- | --- |"] + [f"| {i} | {'z' * 50} |" for i in range(40)]
        chunks = _split_table_rows(rows, 600)
        assert len(chunks) > 1
        for c in chunks:
            assert c.startswith("| a | b |\n| --- | --- |\n")
            assert _has_table(c)
            assert len(c) <= 600
        body = "\n".join(chunks)
        for i in range(40):
            assert body.count(f"| {i} | ") == 1, "each row appears exactly once"

    def test_split_table_rows_cannot_cut_inside_a_single_oversize_row(self) -> None:
        # There is no sub-row boundary that keeps the table valid, so the row
        # splitter returns it oversize; the degraded ladder is what bounds it.
        rows = ["| a |", "| --- |", "| " + "w" * 900 + " |"]
        chunks = _split_table_rows(rows, 400)
        assert len(chunks) == 1
        assert _has_table(chunks[0])

    def test_a_single_monster_row_is_cut_inside_rather_than_truncated(self) -> None:
        # A row bigger than one message has no valid row-boundary cut. The
        # degraded ladder must cut INSIDE it (losing table framing for that row
        # only) rather than shipping an oversize chunk the client backstop
        # would truncate -- the tail of the row has to reach the user.
        cli = FakeClient()
        cli.rich_fails = True
        r = self._renderer(cli)
        marker_head, marker_tail = "ROWSTART", "ROWEND"
        row = f"| {marker_head} {'v' * 6000} {marker_tail} |"
        table = f"| a |\n| --- |\n{row}\n"

        async def _go() -> None:
            await r.on_turn_start()
            await r.on_text_chunk(table)
            await r.on_done()

        asyncio.run(_go())

        bodies = [t for _, t, _ in cli.edits] + [t for t, _ in cli.sent]
        joined = "".join(bodies)
        assert marker_head in joined and marker_tail in joined, "both row ends survive"
        assert joined.count("v" * 100) * 100 >= 5900, "the row body ships whole"
        for b in bodies:
            assert len(b) <= TELEGRAM_MAX_TEXT, "no chunk relies on backstop truncation"

    def test_table_aware_split_budgets_prose_against_the_html_cap(self) -> None:
        # Prose around a table seals through the HTML path, so it must keep the
        # rendered budget even while the table beside it rides the rich budget.
        prose = ("lorem ipsum dolor sit amet " * 90).strip()
        table = "| a | b |\n| --- | --- |\n" + "\n".join(
            f"| {i} | {'q' * 100} |" for i in range(40)
        )
        chunks = _split_markdown_table_aware(prose + "\n\n" + table, 1000, len(table) + 10)
        table_chunks = [c for c in chunks if _has_table(c)]
        assert len(table_chunks) == 1, "the table run stays atomic"
        assert table_chunks[0] == table
        prose_chunks = [c for c in chunks if not _has_table(c)]
        assert prose_chunks, "the prose still ships"
        assert all(len(_md_to_telegram_html(c)) <= 1000 for c in prose_chunks)

    def test_table_aware_split_falls_back_to_the_bounded_splitter_for_fences(self) -> None:
        # Fence-bearing text keeps the fence-aware splitter: deciding where a
        # fence ends means growing a second CommonMark parser, and a pipe
        # pattern inside a fence is not a table anyway.
        text = "```\n| a | b |\n| --- | --- |\n" + "x\n" * 500 + "```"
        assert _split_markdown_table_aware(text, 800, 32000) == _split_markdown_bounded(text, 800)

    def test_non_table_content_splits_exactly_as_before(self) -> None:
        # Regression guard for the shared sizing path: replies without a table
        # must take the identical bounded split they always did, sealing each
        # chunk to the same HTML the bounded splitter implies. Markup makes the
        # sealed HTML distinguishable from plaintext live-stream frames.
        text = "para **one**. " * 150 + "\n\n" + "para _two_! " * 250
        assert not _has_table(text)
        cli = FakeClient()
        r = self._renderer(cli)

        async def _go() -> None:
            await r.on_turn_start()
            await r.on_text_chunk(text)
            await r.on_done()

        asyncio.run(_go())

        assert cli.rich_sent == [], "no table -> the rich path is never touched"
        # The seal strips the segment before rendering (pre-existing behavior),
        # so normalize both sides the same way for the comparison.
        expected = [
            _md_to_telegram_html(c.strip())
            for c in _split_markdown_bounded(text, r._rendered_limit())
        ]
        assert len(expected) > 1, "precondition: long enough to actually rotate"
        finals = [t for _, t, _ in cli.edits if "<b>" in t or "<i>" in t]
        finals += [t for t, _ in cli.sent if "<b>" in t or "<i>" in t]
        assert sorted(finals) == sorted(expected), "sealed bodies match the bounded split"

    def test_an_escape_heavy_degraded_table_is_never_truncated(self) -> None:
        # html.escape inflation inside <pre> is multiplicative, so a degraded
        # split that budgets SOURCE chars ships oversize chunks the client
        # backstop truncates -- silent row loss. The re-split must measure the
        # RENDERED form: every shipped chunk fits the cap and every row lands.
        cli = FakeClient()
        cli.rich_fails = True
        r = self._renderer(cli)
        table = self._table(100, fill="<&>", width=25)
        assert r._limit() < len(table) <= r._rich_limit()

        async def _go() -> None:
            await r.on_turn_start()
            await r.on_text_chunk(table)
            await r.on_done()

        asyncio.run(_go())

        bodies = [t for _, t, _ in cli.edits if "<pre>" in t]
        bodies += [t for t, _ in cli.sent if "<pre>" in t]
        assert len(bodies) >= 2
        for b in bodies:
            assert len(b) <= r._rendered_limit(), "every RENDERED chunk fits the cap"
        joined = "\n".join(bodies)
        for i in range(100):
            assert f"| {i:05d} |" in joined, "no row lost to escape inflation"

    def test_a_row_streamed_after_an_over_budget_rotation_stays_its_own_row(self) -> None:
        # The block splitter joins lines without the buffer's trailing newline.
        # The retained tail keeps streaming, so dropping it would glue the next
        # streamed row onto the previous one and corrupt the table mid-stream.
        cli = FakeClient()
        r = self._renderer(cli)
        table = self._table(300, fill="y", width=120)
        assert len(table) > r._rich_limit()
        assert table.endswith("\n")

        async def _go() -> None:
            await r.on_turn_start()
            await r.on_text_chunk(table)
            await r.on_text_chunk("| 99999 | sentinel |\n")
            await r.on_done()

        asyncio.run(_go())

        lines = [ln for md, _, _ in cli.rich_sent for ln in md.split("\n")]
        assert "| 99999 | sentinel |" in lines, "the streamed row survives as its own line"
        assert not any("||" in ln.replace("| |", "") for ln in lines), "no glued rows"

    def test_a_partial_row_at_rotation_time_is_not_stranded_as_prose(self) -> None:
        # GFM rows need no outer pipe, so a row whose first pipe has not
        # streamed yet reads as prose to the block parser. A rotation firing at
        # that instant must keep the unterminated line with the streaming tail;
        # emitting it as a prose chunk strands it -- and the rows after it --
        # outside the table.
        cli = FakeClient()
        r = self._renderer(cli)
        head = "id | data\n--- | ---\n"
        rows = "".join(f"{i:05d} | {'y' * 120}\n" for i in range(300))
        assert len(head + rows) > r._rich_limit()

        async def _go() -> None:
            await r.on_turn_start()
            # First delivery ends mid-row, BEFORE the row's first pipe.
            await r.on_text_chunk(head + rows + "99999")
            await r.on_text_chunk(" | sentinel\n")
            await r.on_done()

        asyncio.run(_go())

        assert len(cli.rich_sent) >= 2
        lines = [ln for md, _, _ in cli.rich_sent for ln in md.split("\n")]
        assert "99999 | sentinel" in lines, "the partial row finishes inside the table"
        for md, _, _ in cli.rich_sent:
            assert _has_table(md), "every chunk stays table-detected"


class TestRedactionNotice:
    """A rewritten answer is followed by one threaded notice; clean answers are not.

    Telegram redacts at the seal against the rendered form, so a raw secret fed
    to the renderer lands as a placeholder — the tally counts each landed frame.
    Shared wording is pinned in ``test_credential_redaction_notice.py``.
    """

    _SECRET_URI = "postgresql://user:SuperSecret123@db.example.com:5432/prod"

    def test_redacted_answer_is_followed_by_one_notice(self) -> None:
        cli = FakeClient()
        r = TelegramRenderer(cli, 55, TELEGRAM_CAPABILITIES, session_key="telegram:1:0")  # type: ignore[arg-type]

        async def _go() -> None:
            await r.on_text_chunk(f"Run: psql {self._SECRET_URI}")
            await r.on_done()

        asyncio.run(_go())
        outbound = [t for t, _kb in cli.sent] + [t for _mid, t, _kb in cli.edits]
        assert not any("SuperSecret123" in t for t in outbound)
        notices = [t for t, _kb in cli.sent if "Security notice" in t]
        assert len(notices) == 1
        assert "SuperSecret123" not in notices[0]
        assert cli.sent[-1][0] == notices[0], "the notice lands below the answer"

    def test_clean_answer_sends_no_notice(self) -> None:
        cli = FakeClient()
        r = TelegramRenderer(cli, 55, TELEGRAM_CAPABILITIES, session_key="telegram:1:0")  # type: ignore[arg-type]

        async def _go() -> None:
            await r.on_text_chunk("All green, deploy finished.")
            await r.on_done()

        asyncio.run(_go())
        assert not any("Security notice" in t for t, _kb in cli.sent)

    def test_notice_send_failure_does_not_fail_a_delivered_turn(self) -> None:
        class _NoticeFailsClient(FakeClient):
            async def send_message(self, chat_id: int, text: str, **kw: Any) -> int:
                if "Security notice" in text:
                    raise RuntimeError("telegram down after the answer")
                return await super().send_message(chat_id, text, **kw)

        cli = _NoticeFailsClient()
        r = TelegramRenderer(cli, 55, TELEGRAM_CAPABILITIES, session_key="telegram:1:0")  # type: ignore[arg-type]

        async def _go() -> None:
            await r.on_text_chunk(f"Run: psql {self._SECRET_URI}")
            await r.on_done()  # must not raise: the answer above already landed

        asyncio.run(_go())
        delivered = [t for t, _kb in cli.sent] + [t for _mid, t, _kb in cli.edits]
        assert any("[REDACTED: credential]" in t for t in delivered)


class TestRotationSeamCredentialSafety:
    """A rotation must not hand the reader a key by putting two bubbles in a row.

    The length cut lands on the RAW buffer and every bubble is redacted ALONE, so a
    credential the model wrote with markup across the cut matches nothing in either
    bubble -- and the reader's client renders the markup away and reads the halves
    as one key, one bubble under the other.

    Telegram budgets the cut against the RENDERED HTML, not the source, so the cut
    offset is MEASURED here rather than assumed: a fixture that places the key at
    the source cap sees the splitter cut on its own budget somewhere else, the key
    lands whole inside one bubble, and the test passes without ever exercising the
    hazard.
    """

    _CAP = 400

    def _renderer(self, monkeypatch: pytest.MonkeyPatch) -> tuple[TelegramRenderer, FakeClient]:
        cli = FakeClient()
        r = TelegramRenderer(cli, 55, TELEGRAM_CAPABILITIES, session_key="telegram:1:0")  # type: ignore[arg-type]
        monkeypatch.setattr(r, "_limit", lambda: self._CAP)
        monkeypatch.setattr(r, "_rendered_limit", lambda: self._CAP)
        return r, cli

    def _straddling_source(self, head: str, tail: str, filler: str = "a") -> str:
        """A source whose first splitter boundary falls strictly inside the key.

        One unbroken run of *filler*, so the splitter has no newline to prefer and
        cuts on the budget. The placement is corrected against the boundary the
        splitter actually chooses, which differs from the source offset by each
        shape's own escape and link inflation.
        """
        key = head + tail
        at = self._CAP - len(head)
        for _ in range(6):
            src = filler * at + key + filler * (self._CAP // 2)
            boundary = len(_split_markdown_bounded(src, self._CAP)[0])
            if at < boundary < at + len(key):
                return src
            at -= boundary - at - len(head)
            assert at > 0, "the boundary cannot be placed inside this shape"
        raise AssertionError(f"boundary never landed inside {key!r}")

    @staticmethod
    def _on_screen(frame: str) -> str:
        """What the reader sees of one bubble: Telegram's HTML, rendered.

        The seal ships HTML, so the raw frame keeps a key apart with the very tags
        that vanish on screen -- ``AKIA</a>IOSFODNN7EXAMPLE`` matches nothing while
        the reader reads one key straight through it.
        """
        return html.unescape(re.sub(r"<[^>]+>", "", frame))

    def _assert_no_key_on_screen(self, frames: list[str]) -> None:
        shown = [self._on_screen(f) for f in frames]
        for reading in (
            canonicalize_display("".join(shown)),
            "".join(canonicalize_display(f) for f in shown),
        ):
            assert _default_redactor(reading) == reading, f"key readable across frames: {shown}"

    @pytest.mark.parametrize(("head", "tail"), CREDENTIAL_STRADDLE_SHAPES)
    def test_a_straddled_credential_never_reaches_two_bubbles(
        self, monkeypatch: pytest.MonkeyPatch, head: str, tail: str
    ) -> None:
        rejoined = (
            canonicalize_display(head + tail),
            canonicalize_display(head) + canonicalize_display(tail),
        )
        assert any(_default_redactor(r) != r for r in rejoined), "fixture is not a straddle"

        r, cli = self._renderer(monkeypatch)
        r._buf = [self._straddling_source(head, tail)]

        async def _go() -> None:
            await r._rotate_on_length()
            await r._seal_current(extract_uploads=False)

        asyncio.run(_go())
        frames = [text for text, _kb in cli.sent]
        assert frames, "nothing was delivered at all"
        self._assert_no_key_on_screen(frames)

    def test_an_innocent_body_still_rotates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Control: the grading refuses boundaries, it does not stop rotating."""
        r, cli = self._renderer(monkeypatch)
        r._buf = ["word " * 400]
        asyncio.run(r._rotate_on_length())
        assert cli.sent, "an innocent body was withheld"

    def test_a_boundary_is_graded_on_the_text_the_seal_delivers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Whitespace between the halves is gone by the time the reader sees them.

        The seal delivers ``_segment_text().strip()``, and both ``_strip_steering``
        and ``_strip_hr`` end in a strip of their own. Graded raw, these two chunks
        are separated by a newline and an indent and no credential pattern matches --
        none tolerates whitespace. Delivered, the indent is gone and the two bubbles
        sit flush together.
        """
        r, cli = self._renderer(monkeypatch)
        r._buf = ["a" * (self._CAP - 8) + "AKIAIOSF" + "\n    ODNN7EXAMPLE" + " tail" * 40]

        async def _go() -> None:
            await r._rotate_on_length()
            await r._seal_current(extract_uploads=False)

        asyncio.run(_go())
        frames = [text for text, _kb in cli.sent]
        assert len(frames) >= 2, f"fixture did not rotate into separate bubbles: {len(frames)}"
        self._assert_no_key_on_screen(frames)

    def test_the_fallback_offset_is_one_the_caller_can_take(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The search must grade the DELIVERED form, or the segment deadlocks.

        Graded raw, the budget offset looks safe here -- a newline and an indent sit
        between the halves and no credential pattern tolerates whitespace -- so the
        exponential back-off never runs and the first sample is returned. A caller
        that then re-graded in delivered form would reject it, and because the search
        is deterministic it would get the same answer on every later rotation: the
        segment would never go out at all.
        """
        raw = "a" * (self._CAP - 8) + "AKIAIOSF" + "\n    ODNN7EXAMPLE" + " tail" * 40
        raw_offset = safe_split_offset(raw, self._CAP, _default_redactor)
        assert not joins_to_a_credential(
            raw[:raw_offset], raw[raw_offset:], _default_redactor
        ), "fixture no longer exercises the raw-vs-delivered gap"
        assert joins_to_a_credential(
            _delivered_form(raw[:raw_offset]),
            _delivered_form(raw[raw_offset:]),
            _default_redactor,
        ), "fixture no longer exercises the raw-vs-delivered gap"

        shown = safe_split_offset(raw, self._CAP, _default_redactor, _delivered_form)
        assert shown, "the delivered-form search withheld instead of stepping back"
        assert shown != raw_offset, "the delivered-form search returned the raw answer"
        assert not joins_to_a_credential(
            _delivered_form(raw[:shown]), _delivered_form(raw[shown:]), _default_redactor
        ), "the offset the search returned still severs a key once delivered"

        r, cli = self._renderer(monkeypatch)
        r._buf = [raw]
        asyncio.run(r._rotate_on_length())
        assert cli.sent, "the rotation withheld on an offset it could have taken"

    def _three_piece_rule_source(self) -> str:
        """A prefix the splitter cuts into three pieces, raw-clean, shown-severing.

        The rules are LONG on purpose. A short ``---`` leaves the middle fragment
        packed into the same chunk as the last one, where the blank line between
        them survives canonicalising and no key forms; a rule sized against the
        budget is what puts the middle fragment in a chunk of its OWN, and
        ``_strip_hr`` erases the rule on delivery so that chunk shows the fragment
        alone. Measured, not assumed -- the precondition is asserted below.
        """
        bar = "-" * 120
        pad = "a" * 330
        return pad + "\nAKIAIOS\n\n" + bar + "\n\nFODNN7\n\n" + bar + "\n\nEXAMPLE\n" + "b" * 330

    def test_the_held_image_prefix_is_graded_as_a_sequence(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The upload-hold branch seals its whole prefix; it must grade it first.

        That branch keeps the image reference and everything after it in the live
        tail and seals every chunk before it, then returns -- ahead of the length
        path's own delivered-form sequence grade. The two gradings that do run are
        blind to this shape: the splitter reads the RAW pieces, where the rules
        still stand between the fragments, and the seam repair reads the delivered
        form but against ONE predecessor only. A key whose fragments sit across
        three pieces separated by rules is clean in both, and flush on screen.
        """
        prefix = self._three_piece_rule_source()
        chunks = _split_markdown_bounded(prefix, self._CAP)
        assert len(chunks) >= 3, f"fixture did not reach three pieces: {len(chunks)}"
        assert not severs_a_credential(
            chunks, _default_redactor
        ), "fixture no longer hides the key from the raw grade"
        assert severs_a_credential(
            chunks, _default_redactor, _delivered_form
        ), "fixture no longer severs a key once delivered"

        r, cli = self._renderer(monkeypatch)
        monkeypatch.setattr(r, "_uploads_enabled", lambda: True)
        r._buf = [prefix + "\n![shot](/tmp/shot.png)"]

        asyncio.run(r._rotate_on_length())

        assert r._buf and r._buf[0].lstrip().startswith(
            "!["
        ), f"the upload-hold branch was not taken: {r._buf!r}"
        frames = [text for text, _kb in cli.sent]
        self._assert_no_key_on_screen(frames)

    def test_a_markup_span_covering_a_whole_piece_is_caught(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Piece length is no defence: canonicalising DROPS a link's target.

        No neighbouring PAIR of these three pieces reveals anything -- the link needs
        its closing bracket, which is in the third -- while the full join collapses
        the url to its label and puts that label against ``AKIA``.
        """
        r, cli = self._renderer(monkeypatch)
        r._buf = [
            "a" * (self._CAP - 4) + "AKIA[IOSFODNN7EXAMPLE](http://q/" + "b" * self._CAP + ") rest"
        ]

        async def _go() -> None:
            await r._rotate_on_length()
            await r._seal_current(extract_uploads=False)

        asyncio.run(_go())
        frames = [text for text, _kb in cli.sent]
        assert frames, "nothing was delivered at all"
        self._assert_no_key_on_screen(frames)

    def test_a_safe_head_that_renders_over_the_cap_is_shrunk_not_abandoned(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Escaping inflates, so the source-budget offset can still render too long.

        The offset is bounded by the SOURCE budget while the cap applies to the
        rendered HTML, so a head that severs nothing can still be over-cap. The
        rotation shrinks the budget by the inflation it measured and looks again,
        rather than holding the whole buffer -- a deferral delivers nothing at all.

        Its own budgets, because the shared ``_CAP`` equals ``_MIN_SPLIT_LIMIT``:
        with no room above the splitter's floor there is nowhere to shrink to, and a
        buffer that inflates past the cap at the floor is genuinely indivisible.
        """
        limit, cap = 800, 2400  # 5x escape inflation leaves room above the 400 floor
        cli = FakeClient()
        r = TelegramRenderer(cli, 55, TELEGRAM_CAPABILITIES, session_key="telegram:1:0")  # type: ignore[arg-type]
        monkeypatch.setattr(r, "_limit", lambda: limit)
        monkeypatch.setattr(r, "_rendered_limit", lambda: cap)

        # The key straddles a NEWLINE, which the splitter prefers as a break, so the
        # severing boundary needs no arithmetic. The leading `&` run is what makes a
        # head bounded by the source budget render past the cap.
        src = "&" * 600 + "AKIAIOSF" + "\n" + "ODNN7EXAMPLE" + "a" * 2000
        assert _rendered_len(src[:limit]) > cap, "fixture head does not inflate past the cap"
        r._buf = [src]

        asyncio.run(r._rotate_on_length())
        rotated = [text for text, _kb in cli.sent]
        assert rotated, "the rotation gave up instead of shrinking to a safe cut"
        for frame in rotated:
            assert len(frame) <= cap, f"a rotated frame rendered past the cap: {len(frame)}"

        asyncio.run(r._seal_current(extract_uploads=False))
        self._assert_no_key_on_screen([text for text, _kb in cli.sent])

    def test_a_retained_buffer_holds_only_source_text(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Whatever is kept live must be a SLICE of the source, never rejoined chunks.

        ``_split_markdown`` closes an open fence at the seal and reopens it in the
        next chunk, so rejoining chunks would leave literal backticks the model never
        wrote in the buffer the user is eventually sent.
        """
        src = (
            "a" * (self._CAP - 8)
            + "AKIAIOSF"
            + "ODNN7EXAMPLE"
            + "\n\n```py\n"
            + "x = 1\n" * 40
            + "```\n\ntail "
            + "c" * self._CAP
        )
        r, _cli = self._renderer(monkeypatch)
        r._buf = [src]
        asyncio.run(r._rotate_on_length())
        retained = "".join(r._buf)
        assert retained in src, "retained buffer is not a slice of the source"
