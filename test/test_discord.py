"""Unit tests for the Discord channel on the messaging-transport abstraction.

Covers: command parsing (commands.py), text chunking + [OPTIONS:] extraction +
button components (renderer.py), deny-by-default auth + DM-only guard +
capabilities + inbound normalization (transport.py), streaming render +
finalization (renderer.py), the interactive approval decider, and the dispatch
turn + interaction routing (transport_dispatch.py). Mirrors test_telegram.py.

The family is split by the module under test. This file holds the command
parsing, the renderer's chunking and option helpers, the gateway client and the
transport. The streaming render, finalization and the approval decider are in
``test_discord_renderer.py``; the dispatch turn is in
``test_discord_dispatcher.py`` and ``test_discord_mirroring.py`` (the
conversation as its own mirror, and its crew log); interaction routing and the
command surfaces are in ``test_discord_interactions.py``. The shared fakes are
in ``discord_helpers.py``; this module re-exports the ones other suites import
from it.
"""

from __future__ import annotations

import asyncio
import os
import threading
from types import SimpleNamespace
from typing import Any

import discord_helpers
import pytest
from discord_helpers import (  # noqa: F401
    _PNG,
    FakeClient,
    FakeCtx,
    FakeProvider,
    FakeSessions,
    MultipartFake,
    _dispatcher,
    _drop_live_config_snapshot,
    _Ev,
    _inbound,
    _prime_live,
)

from conftest import assert_rejected_without_backtracking
from kiro_crew.discord import renderer as discord_renderer
from kiro_crew.discord.attachments import process_discord_attachments
from kiro_crew.discord.client import (
    _INTENT_DIRECT_MESSAGES,
    _INTENT_GUILD_MESSAGES,
    _INTENT_MESSAGE_CONTENT,
    DISCORD_CHUNK_LIMIT,
    DISCORD_MAX_TEXT,
    DiscordClient,
    DiscordInbound,
    _find_button_label,
)
from kiro_crew.discord.commands import parse_command, parse_mid_turn_override
from kiro_crew.discord.renderer import (
    DiscordRenderer,
    _extract_options,
    _strip_steering,
    build_option_components,
)
from kiro_crew.discord.transport import (
    DISCORD_CAPABILITIES,
    DiscordInboundMessage,
    DiscordTransport,
)
from kiro_crew.messaging import driver as messaging_driver
from kiro_crew.messaging.attachments import cleanup
from kiro_crew.messaging.split import split_markdown_safe
from kiro_crew.messaging.transport import InboundMessage

_cfg = discord_helpers._cfg  # assigned, not imported: a parameter here shadows it


# ── commands.py ──────────────────────────────────────────────────────────


class TestParseCommand:
    def test_new_aliases(self) -> None:
        assert parse_command("!new") == "new"
        assert parse_command("!start") == "new"
        assert parse_command("/new") == "new"  # Telegram muscle memory

    def test_compact(self) -> None:
        assert parse_command("!compact") == "compact"
        assert parse_command("/compact") == "compact"

    def test_stop_aliases(self) -> None:
        assert parse_command("!stop") == "stop"
        assert parse_command("!cancel") == "stop"

    def test_link_unlink_help(self) -> None:
        assert parse_command("!link") == "link"
        assert parse_command("!unlink") == "unlink"
        assert parse_command("!help") == "help"

    def test_case_and_whitespace(self) -> None:
        assert parse_command("  !NEW  ") == "new"

    def test_plain_text_is_not_a_command(self) -> None:
        assert parse_command("hello there") is None
        assert parse_command("!unknown") is None
        assert parse_command("") is None

    def test_command_with_trailing_words_still_matches(self) -> None:
        assert parse_command("!new please") == "new"


class TestMidTurnOverride:
    def test_queue_override(self) -> None:
        assert parse_mid_turn_override("!queue do it later") == (
            "queue",
            "do it later",
        )

    def test_steer_override(self) -> None:
        assert parse_mid_turn_override("!steer focus on X") == (
            "steer",
            "focus on X",
        )

    def test_slash_aliases(self) -> None:
        assert parse_mid_turn_override("/steer now") == ("steer", "now")

    def test_bare_directive_is_content(self) -> None:
        assert parse_mid_turn_override("!queue") == (None, "!queue")

    def test_plain_text_passthrough(self) -> None:
        assert parse_mid_turn_override("hello") == (None, "hello")


# ── renderer.py helpers ──────────────────────────────────────────────────

_ORACLE_CODE = "x = 1\n"
_ORACLE_PROSE = "Ordinary prose about how chat surfaces render markdown.\n"

#: Fence shapes swept by BOTH renderer oracles -- the strip/append symmetry one
#: and the whitespace-fidelity one. Shared so neither can drift onto a corpus the
#: other never sees. They cover the information a per-chunk fence walk cannot
#: recover from the source: 3/4/5-backtick openers, authored inner bare ``` lines,
#: literal backticks in prose, 4-space-indented lookalikes, info strings, and a
#: run of blank lines at the tail.
_FENCE_SHAPES = [
    "```py\n" + _ORACLE_CODE * 40,  # open 3-backtick fence
    "```py\n" + _ORACLE_CODE * 40 + "```\n",  # closed 3-backtick fence
    "````md\n" + _ORACLE_CODE * 40,  # open 4-backtick fence
    "````md\n" + ("```py\n" + _ORACLE_CODE + "```\n") * 25,  # inner bare closers
    "````md\n" + ("```py\n" + _ORACLE_CODE + "```\n") * 25 + "````\n",  # …then closed
    "`````\n" + ("```\n" + _ORACLE_CODE + "```\n") * 25,  # 5-backtick outer
    _ORACLE_PROSE * 12,  # no fence at all
    "You type ``` to open a block.\n" + _ORACLE_PROSE * 12,  # literal in prose
    "You type ``` inline.\n\n```py\n" + _ORACLE_CODE * 30,  # literal, then open
    _ORACLE_PROSE * 6 + "    ```\n" + _ORACLE_CODE * 20,  # indented lookalike
    "   ```py\n" + _ORACLE_CODE * 40,  # 3-space indent still opens
    "```a`b\n" + _ORACLE_CODE * 40,  # backtick in info string == inline code
    "```py\n" + _ORACLE_CODE * 20 + "```\n\n```sh\nls\n" + _ORACLE_CODE * 20,  # two fences
    "````md\n" + _ORACLE_CODE * 20 + "```\n" + _ORACLE_CODE * 20 + "\n\n\n",  # ws tail
    # Blank code lines INSIDE a fence with more code after them -- the shape the
    # remainder would delete, swept at every limit so the cut lands on each
    # newline of the run in turn.
    "```py\n" + _ORACLE_CODE * 20 + "\n\n" + _ORACLE_CODE * 20,
    "```py\n" + (_ORACLE_CODE + "\n\n\n") * 12,  # 4-newline runs throughout
]


class TestRotationSplitting:
    """The regression corpus, repointed onto the shared splitter.

    Discord owns no splitter: ``_rotate_on_length`` consumes
    ``split_markdown_safe``, so every shape below is that module's behavior as a
    Discord user reads it. The module's own contracts -- fence grammar, budget,
    prefix stability, lossless reassembly -- are pinned in
    ``test_messaging_split.py`` and are deliberately not restated here. These
    tests pin the INTEGRATION: which chunks the renderer seals, which one it
    keeps live, and that it adds and removes nothing on the way.
    """

    def _renderer(
        self, monkeypatch: pytest.MonkeyPatch, limit: int
    ) -> tuple[DiscordRenderer, FakeClient]:
        cli = FakeClient()
        r = DiscordRenderer(cli, "chan1", DISCORD_CAPABILITIES, session_key="sk")  # type: ignore[arg-type]
        monkeypatch.setattr(r, "_limit", lambda: limit)
        # A live frame is throttled and re-rendered by design, so it carries no
        # stability promise and would interleave with the sealed frames. Holding
        # it back leaves ``cli.sent`` as exactly the sealed chunks, in order,
        # which is the channel every assertion below reads.
        monkeypatch.setattr("kiro_crew.discord.renderer._EDIT_THROTTLE_S", 1e9)
        return r, cli

    async def _rotate(
        self, monkeypatch: pytest.MonkeyPatch, src: str, limit: int
    ) -> tuple[list[str], str]:
        """The sealed frames and the retained live buffer for *src* at *limit*."""
        r, cli = self._renderer(monkeypatch, limit)
        r._buf = [src]
        await r._rotate_on_length()
        return [t for t, _ in cli.sent], "".join(r._buf)

    @pytest.mark.asyncio
    async def test_pathological_rotation_work_is_offloaded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.discord import renderer as renderer_module

        r, _ = self._renderer(monkeypatch, 100)
        source = "`" * 5_000
        r._buf = [source]
        offloads: list[tuple[Any, tuple[Any, ...], dict[str, Any]]] = []

        def _capture(text: str, limit: int) -> tuple[list[str], bool]:
            return [text], False

        async def _offload(func: Any, /, *args: Any, **kwargs: Any) -> Any:
            offloads.append((func, args, kwargs))
            return func(*args, **kwargs)

        monkeypatch.setattr(renderer_module, "split_markdown_safe_with_tier", _capture)
        monkeypatch.setattr(renderer_module.asyncio, "to_thread", _offload)

        await r._rotate_on_length()

        assert offloads == [
            (renderer_module.protected_ref_spans, (source,), {}),
            (_capture, (source, 100), {}),
            # The rotation grades the pair the splitter never sees -- the sealed
            # chunks plus the tail it retains -- and that read is over
            # attacker-influenced text, so it is offloaded like the two above it.
            (
                renderer_module.severs_a_credential,
                ([source], renderer_module._redact_all, renderer_module._delivered_form),
                {},
            ),
        ]

    @pytest.mark.asyncio
    async def test_a_segment_under_the_cap_is_not_rotated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert await self._rotate(monkeypatch, "hello", 100) == ([], "hello")
        assert await self._rotate(monkeypatch, "", 100) == ([], "")

    @pytest.mark.asyncio
    async def test_a_lone_chunk_is_handed_back_untouched(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Nothing appended and nothing to undo, including for the shapes the
        # deleted tail-closer strip would have to reason about.
        for text in ["```py\nx = 1\n", "type ``` here", "plain prose"]:
            assert await self._rotate(monkeypatch, text, 1900) == ([], text)

    @pytest.mark.asyncio
    async def test_a_paragraph_boundary_is_preferred(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sealed, tail = await self._rotate(monkeypatch, "para one\n\npara two\n\npara three", 20)
        assert sealed == ["para one"]  # cut at the paragraph break, blank line trimmed
        assert tail == "para two\n\npara three"

    @pytest.mark.asyncio
    async def test_every_sealed_frame_closes_its_own_fence(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sealed, tail = await self._rotate(monkeypatch, "```py\n" + "x = 1\n" * 50 + "```", 120)
        assert len(sealed) > 1
        for frame in sealed:
            # Self-contained: the language tag is carried into every
            # continuation and a matching closer ends it.
            assert frame.startswith("```py\n"), frame[:12]
            assert frame.endswith("\n```"), frame[-8:]
            assert frame.count("```") % 2 == 0
        assert tail.endswith("```")  # the source's own closer, not an invented one

    @pytest.mark.asyncio
    async def test_the_retained_tail_is_left_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The final chunk keeps a still-arriving fence OPEN, by contract.

        This is what replaced a whole append-then-strip protocol. The private
        splitter sealed every chunk including the last and reported, through a
        returned flag, that it had done so; the rotation then undid it on the
        retained tail. The shared splitter never seals the final chunk, so there
        is no flag to read and nothing to strip -- and no way for an append and
        a strip to disagree, which is what every defect in that cluster was.
        """
        src = "```py\n" + "x = 1\n" * 50  # the model's closing ``` has not arrived
        sealed, tail = await self._rotate(monkeypatch, src, 120)
        assert len(sealed) > 1
        for frame in sealed:
            assert frame.endswith("\n```")
        assert tail.startswith("```py\n")  # the authored opener, not a bare ```
        assert not tail.rstrip().endswith("```")
        assert src.endswith(tail[len("```py\n") :])  # the tail IS the source's own tail

    @pytest.mark.asyncio
    async def test_a_seal_ending_in_escape_degrades_uploads(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Escape opener UNMATCHED across the seam: the sealed prefix ends in an
        odd backslash run, so a marker on the live tail's first line is escaped
        (literal) in the full text but real when the tail is scanned alone.

        The seam-aware classifier asks the extraction reader at the tail's own
        resume point: the two readings disagree, so the rotation fails closed.
        Left un-degraded, the semantic seal would upload a source-literal file.
        """
        r, _ = self._renderer(monkeypatch, 60)
        # Cut lands right after a lone backslash; the tail opens with markup the
        # backslash escapes in the full text.
        r._buf = ["y" * 59 + "\\" + "![c](/tmp/c.png) tail"]
        await r._rotate_on_length()
        assert r._segment_uploads_safe is False

    @pytest.mark.asyncio
    async def test_an_escaped_trailing_space_is_not_escape_debt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Escape opener MATCHED (spent) at the seam: a backslash escaping a real
        trailing space straddles nothing the tail resumes.

        The extraction reader judges both readings the same, so the classifier
        sees no flip and uploads stay eligible. A whole-head escape count on the
        rstripped sealed chunk would have faked debt here; consulting the reader
        at the tail's resume point does not.
        """
        r, _ = self._renderer(monkeypatch, 60)
        assert r._segment_uploads_safe is True
        r._buf = ["x" * 58 + "\\ short tail here"]
        await r._rotate_on_length()
        assert r._segment_uploads_safe is True

    @pytest.mark.asyncio
    async def test_a_clean_seal_keeps_uploads_eligible(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A seam with no open literalness context of any kind is neutral: both
        readings agree, so uploads stay eligible."""
        r, _ = self._renderer(monkeypatch, 60)
        assert r._segment_uploads_safe is True
        r._buf = ["x" * 58 + " short tail here"]
        await r._rotate_on_length()
        assert r._segment_uploads_safe is True

    @pytest.mark.asyncio
    async def test_a_backtick_balanced_within_the_seam_block_is_not_debt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A backtick BALANCED within the seam's own blank-line block is not
        debt: the block closes its inline-code span before the seam, so the
        prefix opens nothing the tail inherits.

        Check 1 masks the seam's own block with the reader's segmentation. A
        block whose backticks pair leaves no surviving opener, so uploads stay
        eligible -- degrading here would only cost a genuinely-real image later.
        """
        r, _ = self._renderer(monkeypatch, 60)
        assert r._segment_uploads_safe is True
        # An earlier paragraph carries a lone backtick, but the seam's OWN block
        # closes its inline-code span (`code`) before the over-limit filler is
        # cut, so nothing is left open at the seam.
        r._buf = ["a ` char here\n\nthen `code` and " + "y" * 70 + "\n"]
        await r._rotate_on_length()
        assert r._segment_uploads_safe is True

    @pytest.mark.asyncio
    async def test_an_open_inline_code_opener_in_the_seam_block_degrades(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An inline-code opener still OPEN in the seam's own block degrades,
        fail-closed.

        The seam's block ends inside an unclosed inline-code span. Whether a
        closing backtick arrives later on the tail is unknown at rotation, and if
        it does the tail-alone read promotes a marker the full text keeps
        literal -- so the classifier fails closed on the open opener in the
        seam's own block rather than gambling on the closer never arriving.
        """
        r, _ = self._renderer(monkeypatch, 60)
        assert r._segment_uploads_safe is True
        # The seam's own block opens an inline-code span and does not close it
        # before the over-limit line is cut.
        r._buf = ["intro\n\nopen `code span " + "y" * 70 + "\n"]
        await r._rotate_on_length()
        assert r._segment_uploads_safe is False

    @pytest.mark.asyncio
    async def test_two_unmatched_backticks_in_separate_paragraphs_degrade(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two unmatched backticks in SEPARATE paragraphs, then a literal local
        image across the seam, must degrade (GPT finding on the whole-region
        mask).

        A whole-prefix mask pairs the two lone backticks across the blank line
        and sees no debt, so a later ``![b](...)`` that the full text keeps
        literal -- because the second paragraph's inline-code span still covers
        it -- is uploaded once the tail is scanned alone. Block-bounding the mask
        to the seam's own block (the reader's own segmentation) sees the second
        paragraph's opener still open and degrades. Fail closed.
        """
        r, _ = self._renderer(monkeypatch, 60)
        assert r._segment_uploads_safe is True
        # Para 1 opens a lone backtick; blank line; para 2 opens another and its
        # over-limit line is cut with the span still open -- a later
        # ``![b](/tmp/b.png)`` on the tail, closed by a trailing backtick, is
        # literal in the full text but real in the tail alone.
        r._buf = [
            "first `para with a lone tick\n\n"
            "second `para " + "y" * 70 + " and ![b](/tmp/b.png) done`\n"
        ]
        await r._rotate_on_length()
        assert r._segment_uploads_safe is False

    @pytest.mark.asyncio
    async def test_a_matched_inline_code_pair_across_the_seam_stays_eligible(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Inline-code opener MATCHED before the seam: a balanced pair leaves no
        open context for the tail to resume, so uploads stay eligible."""
        r, _ = self._renderer(monkeypatch, 60)
        assert r._segment_uploads_safe is True
        r._buf = ["a `code` here " + "y" * 70 + "\n"]
        await r._rotate_on_length()
        assert r._segment_uploads_safe is True

    @pytest.mark.asyncio
    async def test_an_open_fence_across_the_seam_does_not_disable_uploads(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fence opener across the seam via a REOPENER tail: the splitter builds
        ``tail = reopener + remainder`` with a synthetic ``"```lang\\n"`` the
        source never had, so ``split_source.endswith(tail)`` is False.

        The seam then sits inside an open fence -- literal in both readings and
        already owned by the fence-aware per-chunk span scan -- so the classifier
        is skipped and uploads stay eligible. A naive concatenation check would
        have fabricated a closing-then-reopening fence and faked debt.
        """
        r, _ = self._renderer(monkeypatch, 60)
        assert r._segment_uploads_safe is True
        # A long code fence, still open, over the limit -- no orphaned ref.
        r._buf = ["```py\n" + "x = 1\n" * 40]
        await r._rotate_on_length()
        assert r._segment_uploads_safe is True

    @pytest.mark.asyncio
    async def test_an_over_limit_indented_fenced_block_stays_eligible(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fenced block whose lines are themselves indented, crossing the
        limit, must not disable uploads.

        The reopener tail makes ``split_source.endswith(tail)`` False, so the
        seam classifier is skipped: every seam is inside the open fence, literal
        in both readings. Otherwise the reopened fence's indent would have faked
        indentation debt and disabled uploads for the whole segment.
        """
        r, _ = self._renderer(monkeypatch, 60)
        assert r._segment_uploads_safe is True
        r._buf = ["```py\n" + "    indented_code = 1\n" * 6]
        await r._rotate_on_length()
        assert r._segment_uploads_safe is True

    @pytest.mark.asyncio
    async def test_a_midline_cut_in_indented_code_degrades_uploads(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Indentation opener UNMATCHED across the seam (head indented, tail
        not): a four-space indented logical line dirty-cut MID-LINE leaves the
        tail continuing that literal-code context WITHOUT its indent.

        The full text reads a marker on that line literal (indented code); the
        de-indented tail reads it real. The classifications differ -- the seam
        classifier sees the flip and fails closed. This is the original
        local-path leak repro: left un-degraded, the semantic seal would upload
        a source-literal file.
        """
        r, _ = self._renderer(monkeypatch, 60)
        assert r._segment_uploads_safe is True
        # One over-limit indented-code line, no ref yet; it is cut mid-line and
        # the tail resumes the same logical line without the four-space indent.
        r._buf = ["    " + "y" * 90 + "\n"]
        await r._rotate_on_length()
        assert r._segment_uploads_safe is False

    @pytest.mark.asyncio
    async def test_a_midline_cut_without_indent_keeps_uploads_eligible(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Indentation MATCHED (absent in both): a non-indented logical line cut
        mid-line opens no literal-code context.

        A marker on its tail is genuinely real in the full text too, so both
        readings agree and there is no degrade.
        """
        r, _ = self._renderer(monkeypatch, 60)
        assert r._segment_uploads_safe is True
        r._buf = ["z" * 90 + "\n"]
        await r._rotate_on_length()
        assert r._segment_uploads_safe is True

    @pytest.mark.asyncio
    async def test_a_midline_cut_before_a_tab_led_tail_degrades_uploads(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Indentation opener UNMATCHED the OTHER way (head not indented, tail
        tab-led): the mirror flip.

        ``_safe_cut`` admits a mid-line boundary right before a leading ``\\t``
        (a tab is not a delimiter lead), so the retained tail BEGINS tab-led and
        reads as indented code at offset 0 -- while the source line's own start
        is not indented. The full text reads a marker on that line REAL, the
        tail-alone reading reads it LITERAL: the seal drops the image and ships
        the raw local path to Discord as display text. The seam classifier fails
        closed on the mismatch.
        """
        r, _ = self._renderer(monkeypatch, 60)
        assert r._segment_uploads_safe is True
        # Over-limit single logical line; the mid-line cut lands so the tail
        # begins with a tab (>= four expanded columns) while the source line
        # itself is not indented -- a literalness flip.
        r._buf = ["z" * 59 + "\t  more code on the same over-limit logical line\n"]
        await r._rotate_on_length()
        assert r._segment_uploads_safe is False

    @pytest.mark.asyncio
    async def test_an_over_limit_fenced_block_does_not_fake_seam_debt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fenced block crossing the limit must not disable uploads: the
        reopener tail makes the seam classifier skip (seam inside an open
        fence, owned by the fence-aware per-chunk span scan)."""
        r, _ = self._renderer(monkeypatch, 60)
        assert r._segment_uploads_safe is True
        # An indented fenced code block, over the limit, carrying no orphaned
        # reference -- every seam is inside the open fence.
        r._buf = ["```py\n" + "    indented_code = 1\n" * 6]
        await r._rotate_on_length()
        assert r._segment_uploads_safe is True

    @pytest.mark.asyncio
    async def test_the_original_local_path_leak_is_not_uploaded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end: the indentation-seam leak (the class all three review
        blocks descended from) does not surface a workspace path as an upload.

        A four-space indented over-limit line is cut mid-line; a later image
        marker arrives on the de-indented tail. The full text keeps it literal
        (indented code), so the file must NOT be extracted -- degrading routes
        the seal to redacted display text, where the marker stays literal, and
        no OutboundFile is produced.
        """
        r, cli = self._renderer(monkeypatch, 60)
        assert r._segment_uploads_safe is True
        # Rotate on the indented over-limit line -> degrade.
        r._buf = ["    " + "y" * 90 + "\n"]
        await r._rotate_on_length()
        assert r._segment_uploads_safe is False
        # The later marker arrives on the tail; the segment stays degraded, so
        # the seal extracts nothing (no local file uploaded).
        r._buf.append("![c](/tmp/c.png)\n")
        await r._seal_current(extract_uploads=True)
        assert cli.uploaded_files == []

    @pytest.mark.asyncio
    async def test_an_inline_code_span_opened_before_the_seam_degrades(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A `` ``code `` inline-code span opened in the sealed prefix, whose
        closer sits DEEP in the live tail, degrades the segment.

        The rotation seals a prefix that opens a two-backtick inline span; the
        tail is later sealed WITHOUT that opener, so a marker the full text kept
        literal because the span covered it reads as real once the tail is
        scanned alone. The ref-set at the rotation seam cannot see it -- the tail
        still carries the closing delimiter at that instant -- so the classifier
        catches it as an inline-code opener with no closer before the seam. Fail
        closed: the whole segment degrades.
        """
        r, _ = self._renderer(monkeypatch, 60)
        assert r._segment_uploads_safe is True
        # `` ``code `` opens a two-backtick inline span; the over-limit filler
        # forces a rotation whose sealed prefix ends inside that open span,
        # while the closer only arrives far down the live tail.
        r._buf = ["``code\n" + "x" * 80 + "\n" + "y" * 80 + "\ntail and ![b](/tmp/b.png)\n``"]
        await r._rotate_on_length()
        assert r._segment_uploads_safe is False

    @pytest.mark.asyncio
    async def test_fence_grammar_seams_survive_a_rotation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only a real fence line opens a block, and its run length is kept.

        A ``` counted as a substring reads prose about fencing as code and
        inverts every later decision; a closer shorter than its opener closes
        nothing. Each row is (opener line, the tail's reopener, the closer a
        sealed frame carries) -- an empty reopener means the source opened no
        fence at all, so no frame may carry a closer either.
        """
        code = "x = 1\n" * 60
        for opener, reopen, closer in [
            ("```py\n", "```py\n", "\n```"),
            ("````md\n", "````md\n", "\n````"),
            ("`````\n", "`````\n", "\n`````"),
            ("   ```py\n", "   ```py\n", "\n```"),  # <=3 spaces of indent still opens
            ("    ```py\n", "", ""),  # 4+ is an indented code line
            ("```a`b\n", "", ""),  # a backtick in the info string is inline code
            ("You type ``` inline.\n", "", ""),  # mid-line, so it opens nothing
        ]:
            sealed, tail = await self._rotate(monkeypatch, opener + code, 120)
            where = repr(opener)
            assert len(sealed) > 1, where
            assert tail.startswith(reopen), (where, tail[:14])
            for frame in sealed:
                assert frame.endswith(closer or "x = 1"), (where, frame[-10:])

    @pytest.mark.asyncio
    async def test_an_authored_inner_fence_line_survives_a_rotation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A 4-backtick block documenting 3-backtick ones keeps its own ``` lines.

        Per CommonMark the inner bare ``` closes nothing, so the source fence is
        still open at the cut and every continuation reopens the 4-backtick one.
        A per-chunk walk run after a BARE ``` reopen loses the opener's run
        length, reads the authored inner closer as closing the block, and the
        strip that followed then deleted the author's own line.
        """
        src = "````markdown\n" + "Nest a block:\n\n```py\nx = 1\n```\n\n" * 90
        sealed, tail = await self._rotate(monkeypatch, src, 1800)
        assert len(sealed) >= 1
        assert tail.startswith("````markdown\n")
        assert src.endswith(tail[len("````markdown\n") :])  # a suffix, not a shortened copy
        authored = src.count("\n```\n")
        assert sum(f.count("\n```\n") for f in sealed) + tail.count("\n```\n") >= authored

    @pytest.mark.asyncio
    async def test_indentation_inside_a_fence_reaches_the_user_verbatim(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Stripping leading whitespace silently re-indents split code. Inside a
        # fence the reopener heads every continuation, so an indented code line
        # never starts a frame and survives the renderer's own strip too.
        src = "```py\n" + "    indented = 1\n" * 40
        sealed, tail = await self._rotate(monkeypatch, src, 200)
        assert len(sealed) > 1
        for frame in sealed + [tail]:
            for line in frame.split("\n"):
                if "indented" in line:
                    assert line == "    indented = 1", repr(line)

    @pytest.mark.asyncio
    async def test_blank_code_lines_survive_a_rotation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Blank lines are CONTENT inside a fence, and no cut absorbs a run.

        ``lstrip("\\n")`` on a boundary remainder deleted whole runs of them and
        pulled the next code line up a row. The shared splitter absorbs no line
        separator at all, so the run reaches the retained tail intact.
        """
        # A remainder of nothing but newlines.
        _, tail = await self._rotate(monkeypatch, "```py\n" + "x = 1\n" * 299 + "\n\n", 1800)
        assert tail.endswith("\n\n\n")
        # The same run straddling the cut, with more code AFTER it.
        src = "```py\n" + "x = 1\n" * 6 + "\n\n" + "y = 2\n" * 6
        sealed, tail = await self._rotate(monkeypatch, src, 60)
        assert len(sealed) >= 1
        joined = "".join(sealed) + tail
        assert "x = 1y = 2" not in joined  # no code line shifted up onto another
        assert "\n\n" in joined  # the blank code lines are still there

    @pytest.mark.asyncio
    async def test_a_continuation_gains_no_leading_blank_line(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The opposite failure mode of preserving a run: where the source had a
        # single separator and no blank line, the continuation must start with
        # content and no invented gap.
        sealed, tail = await self._rotate(monkeypatch, "a" * 30 + "\n" + "b" * 30, 40)
        assert sealed == ["a" * 30]
        assert tail == "b" * 30

    @pytest.mark.timeout(30)
    @pytest.mark.asyncio
    async def test_a_rotation_terminates_on_pathological_input(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Input no cut fits cleanly must finish rather than spin.

        An all-newline tail, a bare backtick run, and a budget too small to hold
        a fence's own scaffolding are the three shapes with no clean cut
        anywhere. Chunks legitimately go over budget in the last of them; what
        matters is that the call returns and makes progress.

        The run is five times the limit: termination does not depend on its
        length, but the cost does. Every cut of a bare run is two characters
        wide, and the sever grade reads each boundary against the whole text,
        so the work grows with the square of the run -- 5000 backticks took
        about 4 s alone and timed out on a loaded coverage shard.
        """
        for src, limit in [
            ("\n\n", 1),
            ("`" * 500, 100),
            ("```a-very-long-info-string-indeed\n" + "code\n" * 20, 12),
        ]:
            sealed, tail = await self._rotate(monkeypatch, src, limit)
            assert sealed or tail, repr(src[:14])

    @pytest.mark.asyncio
    async def test_an_overlimit_chunk_never_reaches_the_api_whole(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The splitter's one documented budget exception, bounded again here.

        A logical line that admits no cut clean on both sides is placed WHOLE
        and its chunk carries the fence scaffolding on top of the limit. The 100
        characters ``_limit`` holds back absorb ordinary scaffolding, but an
        opener this long does not fit in them, so the chunk passes Discord's
        hard cap -- where ``send_message`` truncates and drops the tail
        INCLUDING the synthetic closer, leaving an unterminated code block
        missing content and no signal that anything went.
        """
        opener = "`" * 260  # scaffolding wider than _limit's headroom
        line = "x" + "`" * 1699  # content: not a bare run, and no cut is clean
        src = opener + "\n" + line + "\n" + "tail\n" * 40
        limit = 1800  # DISCORD_CAPABILITIES' own _limit()
        assert any(len(c) > DISCORD_MAX_TEXT for c in split_markdown_safe(src, limit))

        sealed, tail = await self._rotate(monkeypatch, src, limit)
        assert sealed
        for frame in sealed:
            assert len(frame) <= DISCORD_MAX_TEXT, len(frame)
        # Sliced, not truncated: every authored character still reaches the user.
        it = iter("".join(("".join(sealed) + tail).split()))
        assert all(c in it for c in "".join(src.split()))

        # The same guard covers the final seal, which is the other payload the
        # API sees whole.
        r, cli = self._renderer(monkeypatch, limit)
        await r.on_text_chunk(src)
        await r.on_done()
        for text, _ in cli.sent:
            assert len(text) <= DISCORD_MAX_TEXT, len(text)
        for _mid, text, _components in cli.edits:
            assert len(text) <= DISCORD_MAX_TEXT, len(text)

    @pytest.mark.asyncio
    async def test_streaming_a_source_seals_what_splitting_it_whole_would(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Prefix stability, end to end: incremental == one-shot.

        Splitting is greedy left-to-right, so re-splitting a longer prefix of
        the same stream reproduces every chunk but the last byte-for-byte. That
        is what lets this renderer POST a sealed chunk and keep only the final
        one live -- Discord has no affordance for un-posting a message, so a cut
        that moved once more text arrived would be unrecoverable. Streaming the
        source in slices must therefore land exactly the messages one shot does.
        """
        src = _FENCE_SHAPES[3]  # a 4-backtick block full of inner 3-backtick ones
        limit = 200
        r, cli = self._renderer(monkeypatch, limit)
        posted: list[str] = []
        for start in range(0, len(src), 37):
            await r.on_text_chunk(src[start : start + 37])
            grown = [t for t, _ in cli.sent]
            assert grown[: len(posted)] == posted, "a message already posted moved"
            posted = grown
        whole = split_markdown_safe(src, limit)
        assert posted == [_strip_steering(c) for c in whole[:-1]]
        assert "".join(r._buf) == whole[-1]

    @pytest.mark.asyncio
    async def test_the_renderer_seals_the_splitter_chunks_unmodified(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Swept oracle: a rotation IS the splitter's output, verbatim.

        The renderer must not carry its own splitter and then undo part of it, since
        every defect in that cluster was the append and the strip disagreeing on
        one shape. There is nothing left to disagree about, and this pins that:
        each chunk but the last is sealed exactly once, in order, and the last is
        retained as the live buffer byte-for-byte. The only transform in the
        identity is ``_strip_steering``, the renderer's pre-existing normalizer
        (which also collapses runs of 3+ newlines, so THAT is the visible
        blank-line cap, not the splitter's) -- any re-added append or strip fails
        it, whatever shape motivated it.

        Shapes cover what a per-chunk fence walk cannot recover from the source:
        3/4/5-backtick openers, authored inner bare ``` lines, literal backticks
        in prose, 4-space-indented lookalikes, info strings, and blank-line runs.
        """
        rotated = frames = 0
        for src in _FENCE_SHAPES:
            assert src and "[OPTIONS" not in src and "[STEERING" not in src  # no detach
            for limit in list(range(40, 201, 7)) + [1900]:
                chunks = split_markdown_safe(src, limit)
                sealed, tail = await self._rotate(monkeypatch, src, limit)
                where = f"shape={src[:14]!r} limit={limit}"
                rotated += len(chunks) > 1
                frames += len(sealed)
                assert tail == chunks[-1], f"live buffer is not the final chunk: {where}"
                assert sealed == [
                    _strip_steering(c) for c in chunks[:-1]
                ], f"sealed frames are not the splitter's own chunks: {where}"
                # Nothing authored is dropped: every non-whitespace source
                # character still appears, in order, across the frames plus the
                # tail. Synthetic backticks only ever ADD.
                it = iter("".join(("".join(sealed) + tail).split()))
                assert all(
                    c in it for c in "".join(src.split())
                ), f"authored characters deleted: {where}"
        # The sweep must not go vacuous.
        assert rotated > 300 and frames > 1000, (rotated, frames)


class TestOptionComponents:
    def test_empty_returns_none(self) -> None:
        assert build_option_components([]) is None

    def test_builds_rows_of_five(self) -> None:
        comps = build_option_components([f"opt{i}" for i in range(7)])
        assert comps is not None
        assert len(comps) == 2  # 5 + 2
        assert len(comps[0]["components"]) == 5
        assert len(comps[1]["components"]) == 2
        assert comps[0]["components"][0]["custom_id"] == "opt:0"

    def test_label_capped_at_80(self) -> None:
        comps = build_option_components(["x" * 200])
        assert comps is not None
        assert len(comps[0]["components"][0]["label"]) == 80

    def test_caps_at_25_options(self) -> None:
        comps = build_option_components([f"o{i}" for i in range(30)])
        assert comps is not None
        total = sum(len(r["components"]) for r in comps)
        assert total == 25

    def test_origin_tag_suffixes_every_custom_id(self) -> None:
        """The provenance tag rides the custom_id; bare ids are the legacy shape.

        ``opt:<i>:<tag>`` is what the press-side gate parses back out, so the
        two halves meet exactly here.
        """
        comps = build_option_components(["a", "b"], "deadbeefcafe")
        assert comps is not None
        ids = [b["custom_id"] for row in comps for b in row["components"]]
        assert ids == ["opt:0:deadbeefcafe", "opt:1:deadbeefcafe"]


class TestExtractOptions:
    def test_no_options(self) -> None:
        assert _extract_options("plain body") == ("plain body", [])

    def test_extracts_trailing_options(self) -> None:
        body, opts = _extract_options("Pick one\n[OPTIONS: A | B | C]")
        assert body == "Pick one"
        assert opts == ["A", "B", "C"]

    def test_holds_back_streaming_partial(self) -> None:
        body, opts = _extract_options("Pick one\n[OPTIONS: A | B")
        assert body == "Pick one"
        assert opts == []

    def test_unterminated_options_tag_is_not_redos(self) -> None:
        # Regression (py/polynomial-redos): a plain greedy ``.*`` body could
        # consume a "[" that ALSO starts the outer "[OPTIONS:" literal, so over
        # text with many "[OPTIONS:" prefixes search() re-explored the body from
        # each position — polynomial. The tempered body
        # (?:[^[]|\[(?!OPTIONS:))* forbids only a re-occurring "[OPTIONS:", so
        # the body is unambiguous (linear). A whitespace-padded unterminated tag
        # and many repeated "[OPTIONS:" prefixes (the real pump) must both be
        # rejected in CPU time linear in the pump -- see
        # conftest.assert_rejected_without_backtracking for why this is not a
        # 1.0 s wall-clock bound.
        def reject(text: str) -> None:
            body, opts = _extract_options(text)
            assert opts == []

        assert_rejected_without_backtracking(reject, lambda n: "[OPTIONS:" + ("\t" * n) + "x")
        assert_rejected_without_backtracking(reject, lambda n: "[OPTIONS:" * n + "x")


class TestStripSteering:
    def test_removes_complete_marker(self) -> None:
        assert _strip_steering("before [STEERING steer-ab12: do X] after") == (
            "before  after".replace("  ", " ")
        ) or "STEERING" not in _strip_steering("before [STEERING steer-ab12: do X] after")

    def test_removes_unclosed_trailing_marker(self) -> None:
        out = _strip_steering("body text [STEERING steer-ab12: still stream")
        assert "STEERING" not in out
        assert out.startswith("body text")

    def test_an_unclosed_marker_cannot_span_table_rows(self) -> None:
        text = "[STEERING steer-deadbeef |\n| --- | --- |"
        assert _strip_steering(text) == text

    def test_removes_a_marker_whose_summary_wrapped(self) -> None:
        """kiro-cli's rephrase is free to wrap, and the frame is still a frame.

        Every other reader of this frame says so: ``messaging.driver`` matches it
        with ``re.DOTALL``, ``constants._STEERING_TAIL_PREFIX_RE`` closes the same
        grammar's prefix with ``re.DOTALL``, and the dashboard's parser spells the
        summary ``[\\s\\S]*?``. A class that stopped at the first line end left the
        marker in the delivered Discord message.
        """
        text = "before [STEERING steer-ab12: switching to the job id\nand re-running it] after"
        out = _strip_steering(text)
        assert "STEERING" not in out
        assert out.startswith("before") and out.endswith("after")

    def test_the_chip_summary_survives_a_wrapped_marker(self) -> None:
        """``_rotate_at_markers`` reads the summary at the offset the marker
        pattern chose, so the two must agree on the same frame: a summary the
        marker matched but this one did not leaves the steer chip blank."""
        text = "[STEERING steer-ab12: switching to the job id\nand re-running it]"
        marker = discord_renderer._STEER_MARKER_RE.search(text)
        assert marker is not None
        summary = discord_renderer._STEER_SUMMARY_RE.match(text, marker.start())
        assert summary is not None
        assert summary.group(1) == "switching to the job id\nand re-running it"

    def test_a_dashed_steer_id_is_one_frame_to_both_patterns(self) -> None:
        """``messaging.driver`` accepts ``[0-9a-f-]+`` for the id, so a dashed id
        is a real frame; the two patterns here have to agree about it."""
        text = "[STEERING steer-a180-ae7f: checked] tail"
        marker = discord_renderer._STEER_MARKER_RE.search(text)
        assert marker is not None
        summary = discord_renderer._STEER_SUMMARY_RE.match(text, marker.start())
        assert summary is not None and summary.group(1) == "checked"
        assert _strip_steering(text).strip() == "tail"

    def test_prose_that_merely_opens_with_the_sentinel_stays(self) -> None:
        """The counterpart to allowing newlines, and the reason it is safe.

        ``messaging.driver`` already rules that opening with the sentinel is not
        being a marker. Without the id requirement, a class that spans lines would
        swallow from ``[STEERING`` to any later ``]`` -- here a Markdown link two
        lines down.
        """
        text = "[STEERING is the feature I mean\n\nsee the [docs](x) for it"
        assert _strip_steering(text) == text

    def test_the_grammar_agrees_with_the_messaging_driver(self) -> None:
        """One frame, two readers: a corpus both must classify the same way.

        This renderer is defence for callers that bypass ``TurnDriver``, so the
        two spellings answer the same question about the same bytes; a divergence
        is how one surface starts delivering what the other removes.
        """
        frames = [
            "[STEERING steer-ab12: checked]",
            "[STEERING steer-a180ae7f: 已并行查询悉尼天气,一并答复。]",
            "[STEERING steer-a180-ae7f: checked]",
            "[STEERING steer-ab12: line one\nline two]",
            "[STEERING steer-ab12]",
        ]
        not_frames = [
            "[STEERING is the feature I mean]",
            "[STEERING steer-: empty id]",
            "[STEERING steer-zzzz: not hex]",
        ]
        for text in frames:
            assert discord_renderer._STEER_MARKER_RE.fullmatch(text), text
            assert messaging_driver._STEER_MARKER_RE.match(text), text
        for text in not_frames:
            assert discord_renderer._STEER_MARKER_RE.fullmatch(text) is None, text
            assert messaging_driver._STEER_MARKER_RE.match(text) is None, text


class TestFindButtonLabel:
    def test_recovers_label(self) -> None:
        components = [
            {
                "type": 1,
                "components": [
                    {"type": 2, "custom_id": "opt:0", "label": "First"},
                    {"type": 2, "custom_id": "opt:1", "label": "Second"},
                ],
            }
        ]
        assert _find_button_label(components, "opt:1") == "Second"
        assert _find_button_label(components, "opt:9") == ""


# ── client.py Gateway + attachment download ──────────────────────────────


class TestGatewayAttachmentNormalization:
    @pytest.mark.asyncio
    async def test_message_create_copies_attachments(self) -> None:
        captured: list[DiscordInbound] = []

        async def _capture(inbound: DiscordInbound) -> None:
            captured.append(inbound)

        client = DiscordClient(token="test", on_message=_capture)
        raw_attachment = {
            "filename": "photo.png",
            "content_type": "image/png",
            "size": len(_PNG),
            "url": "https://cdn.discordapp.com/attachments/c/m/photo.png",
        }
        client._on_dispatch(
            "MESSAGE_CREATE",
            {
                "channel_id": "c1",
                "id": "m1",
                "content": "caption",
                "author": {"id": "u1", "username": "user"},
                "attachments": [raw_attachment],
            },
        )
        tasks = tuple(client._handler_tasks)
        assert tasks
        await asyncio.gather(*tasks)

        assert captured[0].text == "caption"
        assert captured[0].attachments == [raw_attachment]

    @pytest.mark.asyncio
    async def test_download_file_operations_run_off_loop(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        loop_thread = threading.get_ident()
        operation_threads: dict[str, list[int]] = {
            "open": [],
            "write": [],
            "close": [],
        }
        real_open = open

        class _TrackedFile:
            def __init__(self, inner: Any) -> None:
                self._inner = inner

            def write(self, chunk: bytes) -> int:
                operation_threads["write"].append(threading.get_ident())
                return self._inner.write(chunk)

            def close(self) -> None:
                operation_threads["close"].append(threading.get_ident())
                self._inner.close()

        def _tracked_open(*args: Any, **kwargs: Any) -> _TrackedFile:
            operation_threads["open"].append(threading.get_ident())
            return _TrackedFile(real_open(*args, **kwargs))

        class _Content:
            async def iter_chunked(self, size: int) -> Any:
                assert size == 8192
                yield b"first"
                yield b"second"

        class _Response:
            status = 200
            content = _Content()

            async def __aenter__(self) -> "_Response":
                return self

            async def __aexit__(self, *args: Any) -> None:
                return None

            def raise_for_status(self) -> None:
                return None

        class _Session:
            def get(self, *args: Any, **kwargs: Any) -> _Response:
                return _Response()

        async def _ensure_session() -> _Session:
            return _Session()

        client = DiscordClient(token="test")
        monkeypatch.setattr(client, "_ensure_session", _ensure_session)
        monkeypatch.setattr("builtins.open", _tracked_open)
        dest = tmp_path / "download.bin"

        await client.download_attachment(
            "https://cdn.discordapp.com/attachments/c/m/download.bin",
            str(dest),
        )

        assert dest.read_bytes() == b"firstsecond"
        assert all(operation_threads.values())
        assert all(
            thread != loop_thread for threads in operation_threads.values() for thread in threads
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "url",
        [
            "https://example.com/file.png",
            "https://cdn.discordapp.com.evil.example/file.png",
            "http://cdn.discordapp.com/file.png",
            "https://media.discordapp.net:444/file.png",
        ],
    )
    async def test_download_refuses_non_discord_origin(self, tmp_path: Any, url: str) -> None:
        client = DiscordClient(token="test")
        with pytest.raises(ValueError, match="Discord attachment URL"):
            await client.download_attachment(url, str(tmp_path / "out"))
        assert client._session is None


class TestDiscordAttachmentAdapter:
    @pytest.mark.asyncio
    async def test_audio_is_returned_for_transcription(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = FakeClient()
        url = "https://cdn.discordapp.com/attachments/c/m/voice.ogg"
        client.attachment_bodies[url] = b"OggS" + b"\x00" * 32
        transcribed: list[str] = []

        async def _transcribe(path: str, _cfg: object) -> str:
            assert os.path.exists(path)
            transcribed.append(path)
            return "spoken words"

        monkeypatch.setattr("kiro_crew.transcribe.is_available", lambda: True)
        monkeypatch.setattr("kiro_crew.transcribe.batch_duration_cap_secs", lambda _cfg: None)
        monkeypatch.setattr("kiro_crew.transcribe.transcribe_audio", _transcribe)

        result = await process_discord_attachments(
            client,  # type: ignore[arg-type]
            [
                {
                    "filename": "voice.ogg",
                    "content_type": "audio/ogg",
                    "size": 36,
                    "url": url,
                }
            ],
        )

        assert transcribed == result.audio_paths
        assert any("spoken words" in block for block in result.text_blocks)
        cleanup(result.temp_paths)

    @pytest.mark.asyncio
    async def test_stt_availability_check_runs_off_loop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        loop_thread = threading.get_ident()
        observed: list[int] = []
        client = FakeClient()
        url = "https://cdn.discordapp.com/attachments/c/m/voice.ogg"
        client.attachment_bodies[url] = b"OggS" + b"\x00" * 32

        def _available() -> bool:
            observed.append(threading.get_ident())
            return False

        monkeypatch.setattr("kiro_crew.transcribe.is_available", _available)
        result = await process_discord_attachments(
            client,  # type: ignore[arg-type]
            [
                {
                    "filename": "voice.ogg",
                    "content_type": "audio/ogg",
                    "size": 36,
                    "url": url,
                }
            ],
        )

        assert observed and loop_thread not in observed
        assert result.rejections == ["[Audio attachment — transcription is unavailable]"]
        cleanup(result.temp_paths)


class _FakeWs:
    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.payloads.append(payload)


class TestGatewayIntents:
    @pytest.mark.asyncio
    async def test_dm_only_requests_no_privileged_intent(self) -> None:
        client = DiscordClient(token="test", enable_guild_threads=False)
        ws = _FakeWs()
        await client._identify(ws)
        assert ws.payloads[0]["d"]["intents"] == _INTENT_DIRECT_MESSAGES

    @pytest.mark.asyncio
    async def test_thread_mode_requests_guild_messages_and_content(self) -> None:
        client = DiscordClient(token="test", enable_guild_threads=True)
        ws = _FakeWs()
        await client._identify(ws)
        intents = ws.payloads[0]["d"]["intents"]
        assert intents & _INTENT_DIRECT_MESSAGES
        assert intents & _INTENT_GUILD_MESSAGES
        assert intents & _INTENT_MESSAGE_CONTENT


# ── transport.py ─────────────────────────────────────────────────────────


class TestTransportAuth:
    def test_empty_allowlist_denies_everyone(self) -> None:
        t = DiscordTransport(FakeClient())  # type: ignore[arg-type]
        msg = InboundMessage(channel_type="discord", user_id="123", conversation_id="c1", text="hi")
        assert t.authorize(msg) is False

    def test_allowed_user_passes(self) -> None:
        t = DiscordTransport(FakeClient(), allowed_user_ids=["123"])  # type: ignore[arg-type]
        msg = InboundMessage(channel_type="discord", user_id="123", conversation_id="c1", text="hi")
        assert t.authorize(msg) is True

    def test_unlisted_user_denied(self) -> None:
        t = DiscordTransport(FakeClient(), allowed_user_ids=["123"])  # type: ignore[arg-type]
        msg = InboundMessage(channel_type="discord", user_id="456", conversation_id="c1", text="hi")
        assert t.authorize(msg) is False

    def test_empty_user_id_denied(self) -> None:
        t = DiscordTransport(FakeClient(), allowed_user_ids=["123"])  # type: ignore[arg-type]
        msg = InboundMessage(channel_type="discord", user_id="", conversation_id="c1", text="hi")
        assert t.authorize(msg) is False

    def test_capabilities(self) -> None:
        assert DISCORD_CAPABILITIES.max_message_chars == DISCORD_CHUNK_LIMIT
        assert DISCORD_CAPABILITIES.streaming is True
        assert DISCORD_CAPABILITIES.edit is True
        assert DISCORD_CAPABILITIES.reactions is True
        assert DISCORD_CAPABILITIES.files_inbound is True
        assert DISCORD_CAPABILITIES.files_outbound is True  # seal-time upload path
        assert DISCORD_CAPABILITIES.threads is True


class TestPublicInjectionSurface:
    """Locks the out-of-band injection contract used by AutoNudge + REST.

    The AutoNudge fire path and POST /api/autonudge reach the dispatcher only
    through ``transport.dispatcher`` and call only ``is_authorized`` /
    ``current_session_key`` / ``handle_message``. If any of these are renamed,
    these tests fail loudly — before a refactor can silently retire live
    monitoring loops at fire time.
    """

    def test_transport_dispatcher_exposes_bound_dispatcher(self) -> None:
        d, _cli, _sess = _dispatcher({"42"})
        t = DiscordTransport(FakeClient(), dispatch=d.handle_message)  # type: ignore[arg-type]
        assert t.dispatcher is d

    def test_transport_dispatcher_none_when_unwired(self) -> None:
        t = DiscordTransport(FakeClient())  # type: ignore[arg-type]
        assert t.dispatcher is None

    def test_is_authorized_deny_by_default(self) -> None:
        d, _cli, _sess = _dispatcher(set())
        assert d.is_authorized("42") is False
        assert d.is_authorized("") is False

    def test_is_authorized_allowlisted_user(self) -> None:
        d, _cli, _sess = _dispatcher({"42"})
        assert d.is_authorized("42") is True
        assert d.is_authorized("99") is False

    def test_current_session_key_matches_inbound_derivation(self) -> None:
        d, _cli, _sess = _dispatcher({"42"}, default_agent="kirocrew")
        # Must agree with the private derivation the inbound path uses — the
        # generation guard compares a stored loop key against this value.
        assert d.current_session_key("42") == d._session_key("42")
        assert d.current_session_key("42").startswith("discord:")


class TestConfiguredTargets:
    @pytest.mark.asyncio
    async def test_resolves_allowlisted_dm(self) -> None:
        client = FakeClient()
        transport = DiscordTransport(client, allowed_user_ids=["u1"])  # type: ignore[arg-type]

        assert await transport.resolve_configured_target("user:u1") == ("dm-u1", None)

    @pytest.mark.asyncio
    async def test_resolves_allowlisted_confirmed_thread(self) -> None:
        client = FakeClient()
        client.thread_channels.add("t1")
        transport = DiscordTransport(client, allowed_thread_ids=["t1"])  # type: ignore[arg-type]

        assert await transport.resolve_configured_target("thread:t1") == ("t1", None)

    @pytest.mark.asyncio
    async def test_denies_allowlisted_normal_guild_channel(self) -> None:
        client = FakeClient()
        transport = DiscordTransport(client, allowed_thread_ids=["c1"])  # type: ignore[arg-type]

        assert await transport.resolve_configured_target("thread:c1") is None


class TestTransportReceive:
    def _transport(
        self,
        allowed: list[str],
        allowed_threads: list[str] | None = None,
        allowed_channels: list[str] | None = None,
    ) -> tuple[DiscordTransport, list[InboundMessage], FakeClient]:
        dispatched: list[InboundMessage] = []

        async def _dispatch(m: InboundMessage) -> None:
            dispatched.append(m)

        client = FakeClient()
        client.thread_channels.update(allowed_threads or [])
        t = DiscordTransport(
            client,  # type: ignore[arg-type]
            allowed_user_ids=allowed,
            allowed_thread_ids=allowed_threads or [],
            allowed_channel_ids=allowed_channels or [],
            dispatch=_dispatch,
        )
        return t, dispatched, client

    @pytest.mark.asyncio
    async def test_authorized_dm_dispatches(self) -> None:
        t, dispatched, _ = self._transport(["u1"])
        await t.receive(
            DiscordInbound(channel_id="c1", user_id="u1", text="hello", message_id="m1")
        )
        assert len(dispatched) == 1
        msg = dispatched[0]
        assert isinstance(msg, DiscordInboundMessage)
        assert msg.conversation_id == "c1"
        assert msg.message_id == "m1"

    @pytest.mark.asyncio
    async def test_allowed_user_in_unapproved_thread_is_audited(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        events: list[dict[str, Any]] = []
        monkeypatch.setattr(
            "kiro_crew.discord.transport.sel",
            lambda: SimpleNamespace(log_api_access=lambda **kwargs: events.append(kwargs)),
        )
        t, dispatched, _ = self._transport(["u1"], ["t1"])
        await t.receive(DiscordInbound(channel_id="c1", user_id="u1", text="hello", guild_id="g1"))
        assert dispatched == []
        assert [event["outcome"] for event in events] == ["denied_unapproved_thread"]

    @pytest.mark.asyncio
    async def test_unrelated_guild_chatter_is_dropped_without_audit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        events: list[dict[str, Any]] = []
        monkeypatch.setattr(
            "kiro_crew.discord.transport.sel",
            lambda: SimpleNamespace(log_api_access=lambda **kwargs: events.append(kwargs)),
        )
        t, dispatched, _ = self._transport(["u1"], ["t1"])
        await t.receive(DiscordInbound(channel_id="c1", user_id="u2", text="hello", guild_id="g1"))
        assert dispatched == []
        assert events == []

    @pytest.mark.asyncio
    async def test_allowlisted_thread_dispatches_for_allowed_user(self) -> None:
        t, dispatched, _ = self._transport(["u1"], ["t1"])
        await t.receive(DiscordInbound(channel_id="t1", user_id="u1", text="hello", guild_id="g1"))
        assert len(dispatched) == 1
        assert dispatched[0].thread_id == "t1"

    @pytest.mark.asyncio
    async def test_allowlisted_channel_creates_thread_before_dispatch(self) -> None:
        t, dispatched, client = self._transport(["u1"], allowed_channels=["c1"])
        await t.receive(
            DiscordInbound(
                channel_id="c1",
                user_id="u1",
                text="Plan the release",
                message_id="m1",
                guild_id="g1",
            )
        )

        assert client.created_threads == [("c1", "m1", "Plan the release")]
        assert len(dispatched) == 1
        assert dispatched[0].conversation_id == "thread-m1"
        assert dispatched[0].thread_id == "thread-m1"

    @pytest.mark.asyncio
    async def test_followup_message_in_auto_created_thread_dispatches(self) -> None:
        """The thread the transport just created must be immediately valid for
        the same user's next message, not just for button interactions -- a
        frozen ``_allowed_threads`` would silently strand every reply."""
        t, dispatched, _ = self._transport(["u1"], allowed_channels=["c1"])
        await t.receive(
            DiscordInbound(
                channel_id="c1",
                user_id="u1",
                text="Plan the release",
                message_id="m1",
                guild_id="g1",
            )
        )
        assert len(dispatched) == 1
        created_thread_id = dispatched[0].conversation_id

        await t.receive(
            DiscordInbound(
                channel_id=created_thread_id,
                user_id="u1",
                text="Here's a follow-up",
                message_id="m2",
                guild_id="g1",
            )
        )

        assert len(dispatched) == 2
        assert dispatched[1].conversation_id == created_thread_id
        assert dispatched[1].thread_id == created_thread_id
        assert dispatched[1].text == "Here's a follow-up"

    @pytest.mark.asyncio
    async def test_channels_governance_denial_blocks_thread_creation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A runtime channels-governance deny must stop the REST thread-create
        call itself -- not just the turn that would have followed it -- since
        creating the thread is a visible, irreversible side effect."""

        async def _denied(_channel_type: str) -> bool:
            return False

        monkeypatch.setattr("kiro_crew.discord.transport.channel_inbound_permitted", _denied)
        t, dispatched, client = self._transport(["u1"], allowed_channels=["c1"])
        await t.receive(
            DiscordInbound(
                channel_id="c1",
                user_id="u1",
                text="Plan the release",
                message_id="m1",
                guild_id="g1",
            )
        )

        assert client.created_threads == []
        assert dispatched == []

    @pytest.mark.asyncio
    async def test_allowlisted_channel_rejects_unapproved_user_without_thread(self) -> None:
        t, dispatched, client = self._transport(["u1"], allowed_channels=["c1"])
        await t.receive(
            DiscordInbound(
                channel_id="c1",
                user_id="u2",
                text="hello",
                message_id="m1",
                guild_id="g1",
            )
        )

        assert client.created_threads == []
        assert dispatched == []

    @pytest.mark.asyncio
    async def test_normal_channel_denied_even_if_id_is_allowlisted(self) -> None:
        t, dispatched, client = self._transport(["u1"], ["c1"])
        client.thread_channels.clear()
        await t.receive(DiscordInbound(channel_id="c1", user_id="u1", text="hello", guild_id="g1"))
        assert dispatched == []

    @pytest.mark.asyncio
    async def test_allowlisted_thread_denies_unapproved_user(self) -> None:
        t, dispatched, _ = self._transport(["u1"], ["t1"])
        await t.receive(DiscordInbound(channel_id="t1", user_id="u2", text="hello", guild_id="g1"))
        assert dispatched == []

    @pytest.mark.asyncio
    async def test_unauthorized_user_dropped(self) -> None:
        t, dispatched, _ = self._transport(["u1"])
        await t.receive(DiscordInbound(channel_id="c1", user_id="u2", text="hello"))
        assert dispatched == []

    @pytest.mark.asyncio
    async def test_empty_text_dropped(self) -> None:
        t, dispatched, _ = self._transport(["u1"])
        await t.receive(DiscordInbound(channel_id="c1", user_id="u1", text=""))
        assert dispatched == []

    @pytest.mark.asyncio
    async def test_attachment_only_message_dispatches(self) -> None:
        t, dispatched, _ = self._transport(["u1"])
        attachment = {
            "filename": "photo.png",
            "content_type": "image/png",
            "size": len(_PNG),
            "url": "https://cdn.discordapp.com/attachments/c/m/photo.png",
        }
        await t.receive(
            DiscordInbound(
                channel_id="c1",
                user_id="u1",
                text="",
                attachments=[attachment],
            )
        )
        assert len(dispatched) == 1
        assert dispatched[0].text == ""
        assert dispatched[0].attachments == [attachment]

    @pytest.mark.asyncio
    async def test_non_inbound_envelope_ignored(self) -> None:
        t, dispatched, _ = self._transport(["u1"])
        await t.receive({"random": "dict"})
        assert dispatched == []

    @pytest.mark.asyncio
    async def test_resolve_conversation_creates_dm_channel(self) -> None:
        t, _, _ = self._transport(["u1"])
        assert await t.resolve_conversation("u1") == "dm-u1"
