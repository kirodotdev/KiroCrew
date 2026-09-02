"""Tests for STT transcription reaction indicator in slack/events.py."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
class TestSttReactionIndicator:
    """Verify studio_microphone reaction is added/removed during transcription."""

    async def test_reaction_added_and_removed_on_success(self):
        """Reaction is added before transcription and removed after."""
        from kiro_crew.slack.events import _transcribe_with_reaction

        slack = AsyncMock()
        orch = AsyncMock()

        with patch(
            "kiro_crew.slack.events._transcribe_files",
            new_callable=AsyncMock,
            return_value=["hello world"],
        ):
            result = await _transcribe_with_reaction(
                slack,
                "C1",
                "ts1",
                orch,
                [{"mimetype": "audio/mp4"}],
            )

        slack.add_reaction.assert_awaited_once_with("C1", "ts1", "studio_microphone")
        slack.remove_reaction.assert_awaited_once_with(
            "C1",
            "ts1",
            "studio_microphone",
        )
        assert result == ["hello world"]

    async def test_reaction_removed_even_on_transcription_failure(self):
        """Reaction is removed in finally block even if transcription raises."""
        from kiro_crew.slack.events import _transcribe_with_reaction

        slack = AsyncMock()
        orch = AsyncMock()

        with patch(
            "kiro_crew.slack.events._transcribe_files",
            new_callable=AsyncMock,
            side_effect=RuntimeError("crash"),
        ):
            with pytest.raises(RuntimeError, match="crash"):
                await _transcribe_with_reaction(
                    slack,
                    "C1",
                    "ts1",
                    orch,
                    [{"mimetype": "audio/mp4"}],
                )

        slack.add_reaction.assert_awaited_once()
        slack.remove_reaction.assert_awaited_once()

    async def test_no_removal_if_add_reaction_fails(self):
        """If add_reaction fails, removal is skipped (flag stays False)."""
        from kiro_crew.slack.events import _transcribe_with_reaction

        slack = AsyncMock()
        slack.add_reaction = AsyncMock(side_effect=Exception("no perms"))
        orch = AsyncMock()

        with patch(
            "kiro_crew.slack.events._transcribe_files",
            new_callable=AsyncMock,
            return_value=[],
        ):
            result = await _transcribe_with_reaction(
                slack,
                "C1",
                "ts1",
                orch,
                [{"mimetype": "audio/mp4"}],
            )

        slack.add_reaction.assert_awaited_once()
        slack.remove_reaction.assert_not_awaited()
        assert result == []
