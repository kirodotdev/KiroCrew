"""Slack notice seams across multiple answer chunks."""

from unittest.mock import Mock

import pytest
from test_slack_renderer import _RecSlack

from kiro_crew.messaging.notice_seam import NOTICE_SEAM_PREFIX_MAX, NoticeAnswerPrefix
from kiro_crew.messaging.renderer import _default_redactor
from kiro_crew.slack.renderer import SlackRenderer


@pytest.mark.asyncio
@pytest.mark.parametrize("thread_ts", ["t1", None])
async def test_notice_seam_survives_newline_chunk(thread_ts):
    rec = _RecSlack()
    renderer = SlackRenderer(rec, "C1", thread_ts, reactions_enabled=False)
    token = "opaque-notice-newline-token"
    await renderer.on_notice("Authorization: Bearer")
    await renderer.on_text_chunk("\n")
    await renderer.on_text_chunk(token + " and following prose survives.")
    await renderer.on_done("end_turn")
    assert token not in str(rec.calls)
    assert "following prose survives" in str(rec.calls)


def test_notice_seam_releases_predecessor_after_bounded_prefix():
    redactor = Mock(wraps=_default_redactor)
    seam = NoticeAnswerPrefix("Authorization: Bearer", redactor)
    assert seam.feed("\n") == "\n"
    token = "opaque-notice-newline-token"
    text = token + " and following prose survives."
    assert token not in seam.feed(text)
    padding = " " * (NOTICE_SEAM_PREFIX_MAX - len(text) - 1)
    assert seam.feed(padding) == padding
    assert seam._predecessor
    assert len(seam._prefix) == NOTICE_SEAM_PREFIX_MAX

    assert seam.feed(" ") == " "
    assert seam._predecessor == ""
    assert seam._prefix == ""
    redactor.reset_mock()
    assert seam.feed(text) is text
    redactor.assert_not_called()
    assert seam._prefix == ""


def test_notice_seam_grades_chunk_crossing_prefix_bound():
    seam = NoticeAnswerPrefix("Authorization: Bearer", _default_redactor)
    assert seam.feed("\n" * NOTICE_SEAM_PREFIX_MAX) == "\n" * NOTICE_SEAM_PREFIX_MAX
    token = "opaque-notice-newline-token"
    repaired = seam.feed(token + " and following prose survives.")
    assert token not in repaired
    assert "following prose survives" in repaired
    assert seam._predecessor == ""
    assert seam._prefix == ""


@pytest.mark.asyncio
async def test_notice_seam_refreshes_after_wait_closes_stream():
    rec = _RecSlack()
    renderer = SlackRenderer(rec, "C1", "t1", reactions_enabled=False)
    try:
        await renderer.on_text_chunk("The answer starts here. ")
        await renderer.on_tool_call("mcp", "wait")
        assert renderer._stream_ts is None
        await renderer.on_notice("Authorization: Bearer")
        token = "opaque-post-wait-token"
        await renderer.on_text_chunk(token + " and following prose survives.")
        await renderer.on_done("end_turn")
        assert token not in str(rec.calls)
        assert "following prose survives" in str(rec.calls)
        assert "[REDACTED: credential]" in str(rec.calls)
    finally:
        await renderer.close()


@pytest.mark.parametrize("chunk_sizes", [(4097,), (2048, 2049), (4097, 4097, 4097)])
def test_notice_seam_keeps_anchor_across_whitespace_budget(chunk_sizes):
    seam = NoticeAnswerPrefix("Authorization: Bearer", _default_redactor)
    for size in chunk_sizes:
        whitespace = "\n" * size
        assert seam.feed(whitespace) == whitespace
        assert len(seam._prefix) <= NOTICE_SEAM_PREFIX_MAX
        assert len(seam._predecessor) <= NOTICE_SEAM_PREFIX_MAX
    token = "opaque-after-whitespace-token"
    repaired = seam.feed(token + " and following prose survives.")
    assert token not in repaired
    assert "following prose survives" in repaired
    assert "[REDACTED: credential]" in repaired
    assert seam._prefix == ""
    assert seam._predecessor == ""


@pytest.mark.parametrize("marker", ["**", "__", "`", "~~", "||", "> ", "# "])
def test_notice_seam_keeps_anchor_across_formatting_budget(marker):
    seam = NoticeAnswerPrefix("Authorization: Bearer", _default_redactor)
    assert seam.feed("\n" * NOTICE_SEAM_PREFIX_MAX) == "\n" * NOTICE_SEAM_PREFIX_MAX
    assert seam.feed(marker) == marker
    assert len(seam._prefix) <= NOTICE_SEAM_PREFIX_MAX
    assert len(seam._predecessor) <= NOTICE_SEAM_PREFIX_MAX
    token = "opaque-after-formatting-token"
    assert token not in seam.feed(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("thread_ts", ["t1", None])
async def test_slack_notice_anchor_survives_formatting_budget(thread_ts):
    rec = _RecSlack()
    renderer = SlackRenderer(rec, "C1", thread_ts, reactions_enabled=False)
    try:
        await renderer.on_notice("Authorization: Bearer")
        for chunk in ("\n" * NOTICE_SEAM_PREFIX_MAX, "**", "opaque-format-token", "**"):
            await renderer.on_text_chunk(chunk)
        await renderer.on_done("end_turn")
        assert "opaque-format-token" not in str(rec.calls)
    finally:
        await renderer.close()
