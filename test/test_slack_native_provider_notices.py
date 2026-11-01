"""Provider notices through the native Slack turn, with the existing delivery gates."""

from __future__ import annotations

import pytest
from slack_handler_composition_contract_helpers import (  # noqa: F401 -- fixture requested by name
    _complete,
    _Sessions,
    _text,
    _turn,
    world_fixture,
)

from kiro_crew.config.loader import ACTIVATION_REVIEW
from kiro_crew.history import ConversationLog
from kiro_crew.providers.base import EVENT_NOTICE, LLMEvent


@pytest.mark.asyncio
@pytest.mark.parametrize("severity,prefix", [("warning", "⚠️"), ("error", "⛔"), ("future", "ℹ️")])
async def test_native_notice_is_display_safe_and_separate_from_answer(
    world, tmp_path, severity, prefix
):
    notice = LLMEvent(
        kind=EVENT_NOTICE,
        title="Provider warning AKIA**IOSFODNN7EXAMPLE**",
        text="<!channel> " + "detail " * 800,
        notice_severity=severity,
    )
    slack, _ = await _turn(
        world,
        [notice, _text("The answer."), _complete()],
        conversation_log=ConversationLog(tmp_path / "history"),
    )
    assert not any(row[0] == "raised" for row in world.rows), world.text()
    posts = [data["text"] for kind, data in slack.actions if kind == "post"]
    assert posts[0].startswith(prefix + " Provider warning")
    assert len(posts) >= 2
    assert all(len(text) <= 3900 for text in posts)
    assert "IOSFODNN7EXAMPLE" not in "".join(posts)
    assert "<!channel>" not in "".join(posts)
    assert "[REDACTED: credential]" in "".join(posts)
    saved = [row[1]["reply"] for row in world.rows if row[0] == "history.save_turn"]
    assert saved == ["The answer."]
    assert not any(row[0] == "raised" for row in world.rows)


@pytest.mark.asyncio
@pytest.mark.parametrize("suppression", ["review", "muted"])
async def test_native_notice_respects_existing_delivery_suppression(
    world, monkeypatch, suppression
):
    monkeypatch.setattr(
        _Sessions, "is_slack_paused", lambda self, key: suppression == "muted", raising=False
    )
    notice = LLMEvent(kind=EVENT_NOTICE, title="Private provider notice", notice_severity="warning")
    slack, _ = await _turn(
        world,
        [notice, _text("The answer."), _complete()],
        channel_activation=ACTIVATION_REVIEW if suppression == "review" else None,
    )
    assert "Private provider notice" not in str(slack.actions)
    assert any(row[0] == "sessions.record_success" for row in world.rows)
    assert not any(row[0] == "raised" for row in world.rows)


@pytest.mark.asyncio
async def test_native_notice_send_failure_does_not_lose_answer(world, tmp_path, caplog):
    notice = LLMEvent(kind=EVENT_NOTICE, title="Provider warning", notice_severity="warning")
    await _turn(
        world,
        [notice, _text("The answer."), _complete()],
        fail=("post_message",),
        conversation_log=ConversationLog(tmp_path / "history"),
    )
    assert "Provider notice delivery failed on slack" in caplog.text
    saved = [row[1]["reply"] for row in world.rows if row[0] == "history.save_turn"]
    assert saved == ["The answer."]
    assert any(row[0] == "sessions.record_success" for row in world.rows)
    assert not any(row[0] == "raised" for row in world.rows)


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapper", ["", "**", "`"])
@pytest.mark.parametrize("stream", [True, False])
async def test_native_notice_bearer_join_never_delivers_the_answer_token(world, wrapper, stream):
    token = "opaque-notice-join-token"
    slack, _ = await _turn(
        world,
        [
            LLMEvent(kind=EVENT_NOTICE, title="Authorization: Bearer"),
            _text(f"{wrapper}{token}{wrapper} and following prose survives."),
            _complete(),
        ],
        stream=stream,
    )
    assert not any(row[0] == "raised" for row in world.rows), world.text()
    delivered = str(slack.actions)
    assert token not in delivered
    assert "following prose survives" in delivered
    assert "[REDACTED: credential]" in delivered


@pytest.mark.asyncio
async def test_native_notice_below_open_stream_does_not_rewrite_that_stream(world):
    slack, _ = await _turn(
        world,
        [
            _text("The answer starts here. "),
            LLMEvent(kind=EVENT_NOTICE, title="Authorization: Bearer"),
            _text("ordinary-answer-word and following prose survives."),
            _complete(),
        ],
    )
    assert not any(row[0] == "raised" for row in world.rows), world.text()
    delivered = str(slack.actions)
    assert "ordinary-answer-word" in delivered
    assert "[REDACTED: credential]" not in delivered


@pytest.mark.asyncio
async def test_native_fresh_continuation_uses_notice_below_open_answer(monkeypatch):
    from unittest.mock import AsyncMock

    from kiro_crew.slack import handler

    slack = AsyncMock()
    monkeypatch.setattr(
        handler,
        "split_message",
        lambda text: [
            "The answer above.",
            "opaque-notice-join-token and following prose survives.",
        ],
    )
    await handler._safe_final_update_with_notice(
        slack,
        "C1",
        "answer-ts",
        "unused",
        "thread-ts",
        notice_tail="Authorization: Bearer",
    )
    assert slack.update_message.await_args.args[2] == "The answer above."
    posted = slack.post_message.await_args.args[1]
    assert "opaque-notice-join-token" not in posted
    assert "following prose survives" in posted


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [True, False])
@pytest.mark.parametrize("formatting", [False, True])
async def test_native_notice_seam_survives_newline_chunk(world, stream, formatting):
    token = "opaque-notice-newline-token"
    slack, _ = await _turn(
        world,
        [
            LLMEvent(kind=EVENT_NOTICE, title="Authorization: Bearer"),
            *(
                [
                    _text("\n" * 4096),
                    _text("**"),
                    _text(token),
                    _text("** and following prose survives."),
                ]
                if formatting
                else [_text("\n"), _text(token + " and following prose survives.")]
            ),
            _complete(),
        ],
        stream=stream,
    )
    assert not any(row[0] == "raised" for row in world.rows), world.text()
    assert token not in str(slack.actions)
    assert "following prose survives" in str(slack.actions)


@pytest.mark.asyncio
async def test_native_notice_seam_refreshes_after_wait_closes_stream(world):
    from kiro_crew.providers.base import EVENT_TOOL_CALL

    token = "opaque-post-wait-token"
    slack, _ = await _turn(
        world,
        [
            _text("The answer starts here. "),
            LLMEvent(kind=EVENT_TOOL_CALL, title="wait", tool_kind="mcp"),
            LLMEvent(kind=EVENT_NOTICE, title="Authorization: Bearer"),
            _text(token + " and following prose survives."),
            _complete(),
        ],
    )
    assert not any(row[0] == "raised" for row in world.rows), world.text()
    assert any(kind == "stop_stream" for kind, _ in slack.actions)
    assert token not in str(slack.actions)
    assert "following prose survives" in str(slack.actions)
    assert "[REDACTED: credential]" in str(slack.actions)
