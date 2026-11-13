"""The timing footer rides in the streamed answer, or is its own message, never both.

A streamed turn with no ``[OPTIONS]`` hands the footer to ``chat.stopStream``'s
``blocks``, so it lands inside the answer message. Every other turn keeps the separate
footer post: an OPTIONS turn (the next turn strikes its buttons with a ``chat.update``
on that ts), a non-streamed reply, a refused footer seal, and, on the native path, a
turn whose sealed stream is overwritten by ``chat.update`` (a redaction or a demotion).
Each finished turn shows exactly one footer. Both paths: the transport renderer
(``slack/renderer.py``) and the native handler (``handler_runtime/stream.py`` +
``delivery.py``).
"""

from __future__ import annotations

import asyncio
import json

import pytest
from slack_handler_composition_contract_helpers import (  # noqa: F401 -- fixture by name
    _complete,
    _Slack,
    _text,
    _turn,
    world_fixture,
)
from test_slack_renderer import _Provider, _RecSlack

from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK, AcpEvent
from kiro_crew.messaging import TurnDriver
from kiro_crew.slack.renderer import SlackRenderer

_FOOTER = "Finished in"


def _has_footer(payload: object) -> bool:
    return _FOOTER in json.dumps(payload, default=str)


# ── transport path (slack/renderer.py) ──────────────────────────────────────


class _NoFooterSealSlack(_RecSlack):
    """``chat.stopStream`` refuses the footer, as after the 10-minute stream window."""

    async def stop_stream(self, channel, ts, final_text=None, blocks=None):
        await super().stop_stream(channel, ts, final_text, blocks)
        return blocks is None


class _RaisingFooterSealSlack(_RecSlack):
    async def stop_stream(self, channel, ts, final_text=None, blocks=None):
        await super().stop_stream(channel, ts, final_text, blocks)
        if blocks is not None:
            raise RuntimeError("message_not_in_streaming_state")
        return True


class _NoStreamSlack(_RecSlack):
    async def start_stream(self, channel, thread_ts, **kw):
        self.calls.append(("start_stream", {}))
        return None


def _drive(rec: _RecSlack, text: str) -> SlackRenderer:
    renderer = SlackRenderer(rec, "C1", "t1", reactions_enabled=False)
    provider = _Provider(
        [
            AcpEvent(kind=EVENT_TEXT_CHUNK, text=text),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
        ]
    )
    asyncio.run(TurnDriver(provider, renderer, approval_mode="auto").run("hi"))
    return renderer


def _transport_footers(rec: _RecSlack) -> tuple[list[dict], list[dict]]:
    """(stop_stream calls carrying the footer, separate footer posts)."""
    inline = [kw for m, kw in rec.calls if m == "stop_stream" and _has_footer(kw["blocks"])]
    posted = [kw for m, kw in rec.calls if m == "post_blocks" and _has_footer(kw["blocks"])]
    return inline, posted


class TestTransportFooter:
    def test_a_streamed_reply_carries_the_footer_in_its_seal(self):
        rec = _RecSlack()
        _drive(rec, "Hello world")
        inline, posted = _transport_footers(rec)
        assert len(inline) == 1 and inline[0]["final_text"] == "Hello world", rec.calls
        assert posted == [], rec.calls

    def test_an_options_reply_keeps_the_footer_its_own_message(self):
        rec = _RecSlack()
        renderer = _drive(rec, "Pick one [OPTIONS: Yes | No]")
        inline, posted = _transport_footers(rec)
        assert inline == [] and len(posted) == 1, rec.calls
        # The buttons ride in that post, and it is the message the next turn strikes.
        assert "Yes" in json.dumps(posted[0]["blocks"])
        assert renderer.posted_options is not None
        assert list(renderer.posted_options.blocks) == posted[0]["blocks"]

    def test_a_non_streamed_reply_posts_the_footer(self):
        rec = _NoStreamSlack()
        _drive(rec, "Hello world")
        inline, posted = _transport_footers(rec)
        assert inline == [] and len(posted) == 1, rec.calls
        assert not any(m == "stop_stream" for m, _ in rec.calls), rec.calls

    @pytest.mark.parametrize("slack_cls", [_NoFooterSealSlack, _RaisingFooterSealSlack])
    def test_a_refused_footer_seal_seals_bare_and_posts_the_footer_once(self, slack_cls):
        rec = slack_cls()
        _drive(rec, "Hello world")
        stops = [kw for m, kw in rec.calls if m == "stop_stream"]
        # The footer seal, then a bare one so the stream still closes.
        assert [s["blocks"] is not None for s in stops] == [True, False], stops
        posted = [kw for m, kw in rec.calls if m == "post_blocks" and _has_footer(kw["blocks"])]
        assert len(posted) == 1, rec.calls

    def test_a_redacted_reply_still_shows_one_footer(self):
        # The transport path never overwrites a sealed stream (its redaction notice is
        # a separate post), so the footer stays in the seal.
        rec = _RecSlack()
        _drive(rec, "key AKIAIOSFODNN7EXAMPLE here")
        inline, posted = _transport_footers(rec)
        assert len(inline) + len(posted) == 1, rec.calls
        assert not any(m == "update_message" for m, _ in rec.calls), rec.calls


# ── native path (handler_runtime/stream.py + delivery.py) ───────────────────


def _native_footers(slack: _Slack) -> tuple[list[dict], list[dict]]:
    inline = [p for k, p in slack.actions if k == "stop_stream" and _has_footer(p.get("blocks"))]
    posted = [p for k, p in slack.actions if k == "blocks" and _has_footer(p)]
    return inline, posted


class TestNativeFooter:
    @pytest.mark.asyncio
    async def test_a_streamed_reply_carries_the_footer_in_its_seal(self, world):
        slack, _ = await _turn(world, [_text("Hello world"), _complete()])
        inline, posted = _native_footers(slack)
        assert len(inline) == 1 and posted == [], slack.actions

    @pytest.mark.asyncio
    async def test_an_options_reply_keeps_the_footer_its_own_message(self, world):
        slack, _ = await _turn(world, [_text("Pick one [OPTIONS: Yes | No]"), _complete()])
        inline, posted = _native_footers(slack)
        assert inline == [] and len(posted) == 1, slack.actions
        assert "Yes" in json.dumps(posted[0]["blocks"])

    @pytest.mark.asyncio
    async def test_a_non_streamed_reply_posts_the_footer(self, world):
        slack, _ = await _turn(world, [_text("Hello world"), _complete()], stream=False)
        inline, posted = _native_footers(slack)
        assert inline == [] and len(posted) == 1, slack.actions

    @pytest.mark.asyncio
    async def test_a_raising_footer_seal_seals_bare_and_posts_the_footer_once(self, world):
        slack, _ = await _turn(world, [_text("Hello world"), _complete()], fail=("stop_stream",))
        stops = [p for k, p in slack.actions if k == "stop_stream"]
        assert len(stops) == 1 and "blocks" not in stops[0], slack.actions
        inline, posted = _native_footers(slack)
        assert inline == [] and len(posted) == 1, slack.actions

    @pytest.mark.asyncio
    async def test_a_refused_footer_seal_seals_bare_and_posts_the_footer_once(self, world):
        recording = _Slack.stop_stream

        async def _refusing(self, channel, ts, final_text=None, blocks=None):
            await recording(self, channel, ts, final_text, blocks)
            return blocks is None

        world.monkeypatch.setattr(_Slack, "stop_stream", _refusing)
        slack, _ = await _turn(world, [_text("Hello world"), _complete()])
        stops = [p for k, p in slack.actions if k == "stop_stream"]
        assert ["blocks" in s for s in stops] == [True, False], slack.actions
        _, posted = _native_footers(slack)
        assert len(posted) == 1, slack.actions

    @pytest.mark.asyncio
    async def test_a_redaction_overwrite_keeps_the_footer_its_own_message(self, world):
        events = [_text("key AKIAIOSFODNN7EXAMPLE and more"), _complete()]
        slack, _ = await _turn(world, events)
        assert any(k == "update" for k, _ in slack.actions), slack.actions
        inline, posted = _native_footers(slack)
        assert inline == [] and len(posted) == 1, slack.actions

    @pytest.mark.asyncio
    async def test_a_stream_demoted_at_the_seal_keeps_the_footer_its_own_message(self, world):
        # The held tail's append and its retry are refused and the rotation fails, so
        # the stream demotes inside the seal and a chat.update delivers the answer
        # onto the message the seal closed.
        slack, _ = await _turn(
            world,
            [_text("First sentence. "), _text("tail end"), _complete()],
            fail=(("append_stream", 2), ("append_stream", 3), ("start_stream", 2)),
        )
        assert any(k == "update" for k, _ in slack.actions), slack.actions
        inline, posted = _native_footers(slack)
        assert inline == [] and len(posted) == 1, slack.actions
