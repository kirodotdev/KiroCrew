"""A markdown link in a steer acknowledgement's summary stays inside the frame.

kiro-cli's ``[STEERING steer-<id>: <summary>]`` carries the model's own words, and
a summary that names the PR it acted on as ``[PR #7](url)`` holds a ``]`` before
the frame's own. A reader that ends the frame at that first ``]`` hands the rest of
the frame, ``(url) for it]``, on as text. Each reader below takes the frame's end
from ``messaging.steer_ack``: the ``]`` that balances the frame's ``[``, or the
first ``]`` when the summary never balances.
"""

from __future__ import annotations

import pytest
from discord_helpers import FakeClient as DiscordFakeClient
from discord_helpers import _drop_live_config_snapshot  # noqa: F401
from telegram_helpers import FakeClient as TelegramFakeClient

from conftest import assert_rejected_without_backtracking
from kiro_crew.dashboard.system_notices import is_speech_row
from kiro_crew.discord.renderer import DiscordRenderer
from kiro_crew.discord.renderer import _strip_steering as discord_strip
from kiro_crew.discord.transport import DISCORD_CAPABILITIES
from kiro_crew.messaging import driver
from kiro_crew.messaging.steer_ack import find_steering_acks, strip_steering_acks
from kiro_crew.preview_text import strip_markdown_preview
from kiro_crew.telegram.renderer import TelegramRenderer
from kiro_crew.telegram.renderer import _strip_steering as telegram_strip
from kiro_crew.telegram.renderer_display import _neutralize_md
from kiro_crew.telegram.transport import TELEGRAM_CAPABILITIES

SUMMARY = "opened [PR #7](https://github.com/o/r/pull/7) for it"
ACK = f"[STEERING steer-ab12: {SUMMARY}]"
REPLY = f"Done. {ACK} tail"
#: The reply cut right after the link's "]", before the frame's own has arrived.
CUT_AT_LINK = REPLY.index("](") + 1


def _drain(*chunks: str) -> tuple[str, list[str]]:
    parser = driver._SteeringMarkerFilter()
    frames: list[tuple[str, str]] = []
    for chunk in chunks:
        frames.extend(parser.feed(chunk))
    frames.extend(parser.flush())
    return (
        "".join(payload for kind, payload in frames if kind == "text"),
        [payload for kind, payload in frames if kind == "steer"],
    )


class TestTheFrameEnd:
    def test_a_link_in_the_summary_is_part_of_the_frame(self):
        (ack,) = find_steering_acks(REPLY)
        assert REPLY[ack.start : ack.end] == ACK
        assert ack.summary.strip() == SUMMARY

    def test_a_summary_that_never_balances_ends_at_the_first_close(self):
        text = "a [STEERING steer-1: typed [ by mistake] b"
        assert strip_steering_acks(text) == "a  b"
        assert find_steering_acks(text, unbalanced=False) == []

    def test_an_unclosed_frame_is_the_texts_own(self):
        text = "a [STEERING steer-1: opened [PR #7"
        assert strip_steering_acks(text) == text

    @pytest.mark.parametrize(
        "frame",
        [
            "[STEERING steer-ab12: noted]",
            "[STEERING steer-ab12]",
            "[steering\tsteer-a180-ae7f :line one\nline two]",
            "[STEERING steer-ab12 ]",
            "[STEERING steer-: empty id]",
            "[STEERING steer-abz: not hex]",
            "[STEERING is the feature I mean]",
            "[STEERINGsteer-ab12: no gap]",
        ],
    )
    def test_the_frame_reader_agrees_with_the_marker_grammar(self, frame):
        match = driver._STEER_MARKER_RE.match(frame)
        acks = find_steering_acks(frame)
        assert [(a.start, a.end, a.summary) for a in acks] == (
            [(0, len(frame), match.group(1) or "")] if match else []
        )

    @pytest.mark.parametrize(
        "pump",
        [
            lambda n: "[STEERING steer-a: " * n + "]",
            lambda n: "[STEERING steer-a: [" * n + "]",
            lambda n: "[STEERING steer-a: " * n,
            lambda n: "[steering\tsteer--:a" * n + "]",
        ],
        ids=[
            "heads-then-close",
            "openers-then-close",
            "heads-unclosed",
            "dash-id-heads-then-close",
        ],
    )
    def test_a_run_of_heads_is_read_in_linear_time(self, pump):
        assert_rejected_without_backtracking(lambda text: strip_steering_acks(text), pump)


class TestTheDriver:
    def test_the_whole_text_strip_removes_the_frame_link_and_all(self):
        assert driver.strip_steering_markers(REPLY) == "Done.  tail"

    def test_the_channel_replay_removes_the_frame_link_and_all(self):
        assert driver.sanitize_channel_replay_text(REPLY) == "Done.  tail"

    @pytest.mark.parametrize("cut", range(1, len(REPLY)))
    def test_the_stream_filter_holds_the_frame_across_any_chunk_cut(self, cut):
        assert _drain(REPLY[:cut], REPLY[cut:]) == ("Done.  tail", [SUMMARY])

    @pytest.mark.parametrize(
        ("pad", "held"),
        [(16_384 - 80, True), (16_384 + 1, False)],
        ids=["under-the-bound", "over-the-bound"],
    )
    def test_the_hold_on_an_unbalanced_summary_ends_at_the_size_bound(self, pad, held):
        """Fed with no flush: under the bound the frame and the text after it wait
        for the frame's own "]"; past it the frame ends at its first "]"."""
        tail = "b" * pad
        parser = driver._SteeringMarkerFilter()
        frames = parser.feed("a [STEERING steer-1: typed [ by mistake] " + tail)
        text = "".join(payload for kind, payload in frames if kind == "text")
        steers = [payload for kind, payload in frames if kind == "steer"]
        if held:
            assert (text, steers) == ("a ", [])
        else:
            assert (text, steers) == ("a  " + tail, ["typed [ by mistake"])

    def test_the_stream_filter_ends_an_unbalanced_summary_at_flush(self):
        assert _drain("a [STEERING steer-1: typed [ by", " mistake] b") == (
            "a  b",
            ["typed [ by mistake"],
        )


class TestTheQuietTurnCheck:
    def test_a_reply_of_only_a_linked_ack_is_not_speech(self):
        assert not is_speech_row("assistant", f"\n\n{ACK}", {})


class TestTheSidebarPreview:
    def test_the_preview_drops_the_frame_link_and_all(self):
        assert strip_markdown_preview(REPLY) == "Done. tail"


async def _rotate(renderer, buffered: str) -> tuple[list[str], str]:
    consumed: list[str] = []

    async def on_steer_consumed(summary: str = "") -> None:
        consumed.append(summary)

    renderer.on_steer_consumed = on_steer_consumed
    renderer._buf = [buffered]
    await renderer._rotate_at_markers()
    return consumed, "".join(renderer._buf)


class TestTheDiscordRenderer:
    def test_the_strip_removes_the_frame_link_and_all(self):
        assert discord_strip(REPLY) == "Done.  tail"

    @pytest.mark.asyncio
    async def test_a_rotation_takes_the_whole_frame(self):
        r = DiscordRenderer(DiscordFakeClient(), "chan1", DISCORD_CAPABILITIES, session_key="sk")  # type: ignore[arg-type]
        assert await _rotate(r, REPLY) == ([SUMMARY], " tail")

    @pytest.mark.asyncio
    async def test_a_rotation_waits_for_the_frames_own_close(self):
        r = DiscordRenderer(DiscordFakeClient(), "chan1", DISCORD_CAPABILITIES, session_key="sk")  # type: ignore[arg-type]
        assert await _rotate(r, REPLY[:CUT_AT_LINK]) == ([], REPLY[:CUT_AT_LINK])


class TestTheTelegramRenderer:
    def test_the_strip_removes_the_frame_link_and_all(self):
        assert telegram_strip(REPLY) == "Done.  tail"

    @pytest.mark.asyncio
    async def test_a_rotation_takes_the_whole_frame(self):
        r = TelegramRenderer(TelegramFakeClient(), 55, TELEGRAM_CAPABILITIES, session_key="telegram:1:0")  # type: ignore[arg-type]
        assert await _rotate(r, REPLY) == ([_neutralize_md(SUMMARY)], " tail")

    @pytest.mark.asyncio
    async def test_a_rotation_waits_for_the_frames_own_close(self):
        r = TelegramRenderer(TelegramFakeClient(), 55, TELEGRAM_CAPABILITIES, session_key="telegram:1:0")  # type: ignore[arg-type]
        assert await _rotate(r, REPLY[:CUT_AT_LINK]) == ([], REPLY[:CUT_AT_LINK])
