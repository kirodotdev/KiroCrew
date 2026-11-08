"""A credential-graded rotation cut inside an open fence seals the fence on both sides.

When the splitter's own boundary severs a key, Telegram and Discord re-cut the
segment at an offset the credential grade chooses and send two SOURCE slices. That
offset can land inside a fenced block. The head is then closed with the fence's own
closer and the remainder reopened with its opener, as the splitter does at its own
cuts. The repaired pair is graded again before anything is sent, and on Discord the
re-cut segment extracts no uploads, because the tier analysis that allowed them read
a different boundary.

The renderer tests force the re-cut deterministically: the first sever check (the
splitter's chunks) answers "severs" and the offset search answers a fixed offset
inside the fence, so the graded path runs on every platform with no key fixture.
"""

from __future__ import annotations

import asyncio
import html
import re
from typing import Any

import pytest
from discord_helpers import FakeClient as DiscordFakeClient
from telegram_helpers import FakeClient as TelegramFakeClient

from kiro_crew.discord import renderer as discord_renderer
from kiro_crew.discord.renderer import DiscordRenderer
from kiro_crew.discord.transport import DISCORD_CAPABILITIES
from kiro_crew.messaging.split import fence_line_safe_offset, rebalance_fenced_cuts
from kiro_crew.telegram import renderer as telegram_renderer
from kiro_crew.telegram.renderer import TelegramRenderer
from kiro_crew.telegram.transport import TELEGRAM_CAPABILITIES

_CODE = "".join(f"line {i:03d}\n" for i in range(60))
_SOURCE = "Here is the code:\n\n```python\n" + _CODE + "```\n\nAfter the block."


class TestRebalanceFencedCuts:
    def test_a_cut_outside_any_fence_is_left_alone(self) -> None:
        pieces = ["plain prose\n", "more prose"]
        assert rebalance_fenced_cuts(pieces) == pieces

    def test_a_cut_inside_a_fence_closes_and_reopens_it(self) -> None:
        head, tail = "intro\n```py\nx = 1\n", "y = 2\n```\nend"
        assert rebalance_fenced_cuts([head, tail]) == [
            "intro\n```py\nx = 1\n```",
            "```py\ny = 2\n```\nend",
        ]

    def test_a_mid_line_cut_closes_on_its_own_line(self) -> None:
        sealed, rest = rebalance_fenced_cuts(["```\nabc", "def\n```"])
        assert sealed == "```\nabc\n```"
        assert rest == "```\ndef\n```"

    def test_the_opener_keeps_its_run_length_and_info_string(self) -> None:
        sealed, rest = rebalance_fenced_cuts(["````diff\n+a\n```\n", "+b\n````"])
        # A ``` line inside a ```` block is content, so the cut is still inside it.
        assert sealed.endswith("\n````")
        assert rest.startswith("````diff\n")

    def test_the_last_piece_is_never_closed(self) -> None:
        pieces = rebalance_fenced_cuts(["a\n", "```\nstill streaming"])
        assert pieces == ["a\n", "```\nstill streaming"]

    def test_a_line_split_by_the_cut_has_not_opened_a_fence_yet(self) -> None:
        assert rebalance_fenced_cuts(["text ``", "`py\ncode"]) == ["text ``", "`py\ncode"]

    def test_every_cut_of_a_three_way_split_is_repaired(self) -> None:
        a, b, c = rebalance_fenced_cuts(["```\n1\n", "2\n", "3\n```"])
        assert a == "```\n1\n```"
        assert b == "```\n2\n```"
        assert c == "```\n3\n```"


class TestFenceLineSafeOffset:
    def test_a_cut_through_a_closer_moves_to_the_closer_line(self) -> None:
        text = "```\ncode\n```\nafter prose\n"
        at = fence_line_safe_offset(text, text.index("```\nafter") + 2)
        assert at == text.index("```\nafter")
        head, tail = rebalance_fenced_cuts([text[:at], text[at:]])
        assert head == "```\ncode\n```"
        assert tail == "```\n```\nafter prose\n"

    def test_a_cut_after_a_nested_delimiters_backticks_moves_to_its_line(self) -> None:
        text = "````\nintro\n```python\nx = 1\n````\n"
        start = text.index("```python")
        assert fence_line_safe_offset(text, start + 3) == start

    def test_a_mid_line_cut_before_a_backtick_run_moves_back(self) -> None:
        text = "```\nsee ```inner\nmore\n```\n"
        start = text.index("see")
        assert fence_line_safe_offset(text, text.index("```inner")) == start

    def test_an_ordinary_mid_line_cut_is_left_alone(self) -> None:
        text = "```\nabcdef\n```\n"
        assert fence_line_safe_offset(text, text.index("def")) == text.index("def")

    def test_a_cut_on_the_first_line_can_become_no_cut(self) -> None:
        assert fence_line_safe_offset("``` x", 2) == 0


def _force_recut(monkeypatch: pytest.MonkeyPatch, module: Any, at: int) -> dict[str, int]:
    """First sever check says "severs"; every later one says no. Offset is fixed."""
    calls = {"severs": 0}

    def _severs(chunks, *_a, **_k) -> bool:
        calls["severs"] += 1
        return calls["severs"] == 1

    monkeypatch.setattr(module, "severs_a_credential", _severs)
    monkeypatch.setattr(module, "safe_split_offset", lambda text, budget, *_a, **_k: at)
    return calls


#: Inside the fenced block, at a line boundary well past its opener.
_CUT = _SOURCE.index("line 020\n")


class TestTelegramRecutInsideAFence:
    _CAP = 400

    def _renderer(self, monkeypatch: pytest.MonkeyPatch) -> tuple[TelegramRenderer, Any]:
        cli = TelegramFakeClient()
        r = TelegramRenderer(cli, 55, TELEGRAM_CAPABILITIES, session_key="telegram:1:0")  # type: ignore[arg-type]
        monkeypatch.setattr(r, "_limit", lambda: self._CAP)
        monkeypatch.setattr(r, "_rendered_limit", lambda: self._CAP)
        return r, cli

    def test_the_sealed_head_and_the_reopened_tail_both_render_as_code(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        r, cli = self._renderer(monkeypatch)
        calls = _force_recut(monkeypatch, telegram_renderer, _CUT)
        r._buf = [_SOURCE]

        async def _go() -> None:
            await r._rotate_on_length()
            await r._seal_current(extract_uploads=False)

        asyncio.run(_go())
        frames = [text for text, _kb in cli.sent]
        assert calls["severs"] >= 2, "the repaired pair was not graded again"
        assert len(frames) >= 2, f"fixture did not rotate: {frames}"
        for frame in frames:
            assert frame.count("<pre") == frame.count("</pre>"), frame
            shown = html.unescape(re.sub(r"<[^>]+>", "", frame))
            assert "```" not in shown, f"a fence delimiter shows as text: {shown!r}"
        assert "line 019" in frames[0] and "<pre" in frames[0]
        assert "line 020" not in frames[0]

    def test_a_repaired_pair_that_severs_is_withheld(self, monkeypatch: pytest.MonkeyPatch) -> None:
        r, cli = self._renderer(monkeypatch)
        monkeypatch.setattr(telegram_renderer, "severs_a_credential", lambda *_a, **_k: True)
        monkeypatch.setattr(
            telegram_renderer, "safe_split_offset", lambda text, budget, *_a, **_k: _CUT
        )
        r._buf = [_SOURCE]
        asyncio.run(r._rotate_on_length())
        assert cli.sent == [], "a pair the second grade rejects must not go out"
        assert "".join(r._buf) == _SOURCE


class TestDiscordRecutInsideAFence:
    _LIMIT = 400

    def _renderer(self, monkeypatch: pytest.MonkeyPatch) -> tuple[DiscordRenderer, Any]:
        cli = DiscordFakeClient()
        r = DiscordRenderer(cli, "chan1", DISCORD_CAPABILITIES, session_key="sk")  # type: ignore[arg-type]
        monkeypatch.setattr(r, "_limit", lambda: self._LIMIT)
        monkeypatch.setattr("kiro_crew.discord.renderer._EDIT_THROTTLE_S", 1e9)
        return r, cli

    @pytest.mark.asyncio
    async def test_every_frame_carries_whole_fences(self, monkeypatch: pytest.MonkeyPatch) -> None:
        r, cli = self._renderer(monkeypatch)
        calls = _force_recut(monkeypatch, discord_renderer, _CUT)
        r._buf = [_SOURCE]
        await r._rotate_on_length()
        await r._seal_current(extract_uploads=False)

        frames = [text for text, _ in cli.sent]
        assert calls["severs"] >= 2, "the repaired pair was not graded again"
        assert len(frames) >= 2, f"fixture did not rotate: {frames}"
        for frame in frames:
            fence_lines = [ln for ln in frame.splitlines() if ln.startswith("```")]
            assert len(fence_lines) % 2 == 0, f"unbalanced fence in {frame!r}"
        assert frames[1].lstrip().startswith("```python")

    @pytest.mark.asyncio
    async def test_a_recut_segment_extracts_no_uploads(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        r, _cli = self._renderer(monkeypatch)
        _force_recut(monkeypatch, discord_renderer, _CUT)
        r._segment_uploads_safe = True
        r._buf = [_SOURCE]
        await r._rotate_on_length()
        assert r._segment_uploads_safe is False
