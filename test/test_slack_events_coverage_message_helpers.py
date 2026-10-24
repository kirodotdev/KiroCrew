"""Coverage for :mod:`kiro_crew.slack.events`: the voice, deletion and approval-mode helpers.

Voice-note transcription (the reaction bracket and the per-file transcriber), the
``message_deleted`` handler, and ``_resolve_approval_mode``.

Conventions follow ``test_slack_events_coverage.py``: a ``MagicMock`` stand-in for
``GatewayOrchestrator``, ``AsyncMock`` for the Slack client, and
``kiro_crew.slack.events.sel`` patched by the autouse fixtures from
``slack_events_coverage_helpers.py`` so no audit file is written.
"""

from __future__ import annotations

import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from slack_events_coverage_helpers import _fresh_trusted_bot_turn_ledger  # noqa: F401
from slack_events_coverage_helpers import _isolated_home_fixture  # noqa: F401
from slack_events_coverage_helpers import _mock_sel_fixture  # noqa: F401
from slack_events_coverage_helpers import _make_orch

from kiro_crew.slack import events as ev
from kiro_crew.slack import files as slack_files

# ---------------------------------------------------------------------------
# Voice transcription helpers
# ---------------------------------------------------------------------------


class TestTranscribeWithReaction:
    @pytest.mark.asyncio
    async def test_reaction_added_and_removed(self):
        orch = _make_orch()
        slack = AsyncMock()
        with patch(
            "kiro_crew.slack.events._transcribe_files",
            new_callable=AsyncMock,
            return_value=["hello"],
        ):
            out = await ev._transcribe_with_reaction(slack, "C1", "1.0", orch, [{}])
        assert out == ["hello"]
        slack.add_reaction.assert_awaited_once_with("C1", "1.0", "studio_microphone")
        slack.remove_reaction.assert_awaited_once_with("C1", "1.0", "studio_microphone")

    @pytest.mark.asyncio
    async def test_add_reaction_failure_skips_removal(self):
        orch = _make_orch()
        slack = AsyncMock()
        slack.add_reaction.side_effect = RuntimeError("no perms")
        with patch(
            "kiro_crew.slack.events._transcribe_files",
            new_callable=AsyncMock,
            return_value=[],
        ):
            assert await ev._transcribe_with_reaction(slack, "C1", "1.0", orch, [{}]) == []
        slack.remove_reaction.assert_not_called()

    @pytest.mark.asyncio
    async def test_remove_reaction_failure_is_swallowed(self):
        orch = _make_orch()
        slack = AsyncMock()
        slack.remove_reaction.side_effect = RuntimeError("gone")
        with patch(
            "kiro_crew.slack.events._transcribe_files",
            new_callable=AsyncMock,
            return_value=["x"],
        ):
            assert await ev._transcribe_with_reaction(slack, "C1", "1.0", orch, [{}]) == ["x"]

    @pytest.mark.asyncio
    async def test_reaction_removed_even_when_transcription_raises(self):
        orch = _make_orch()
        slack = AsyncMock()
        with patch(
            "kiro_crew.slack.events._transcribe_files",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ):
            with pytest.raises(RuntimeError):
                await ev._transcribe_with_reaction(slack, "C1", "1.0", orch, [{}])
        slack.remove_reaction.assert_awaited_once()


class TestTranscribeFiles:
    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- no test in the test_slack_events_coverage*.py modules calls monkeypatch.undo()
    def _uncapped_provider(self, monkeypatch):
        monkeypatch.setattr(ev, "load_stt_config", lambda: SimpleNamespace(timeout_secs=300))
        monkeypatch.setattr(ev, "batch_duration_cap_secs", lambda _cfg: None)

    @pytest.mark.asyncio
    async def test_non_audio_and_urlless_files_skipped(self):
        orch = _make_orch()
        files = [
            {"mimetype": "image/png", "url_private": "https://x.invalid/a.png"},
            {"mimetype": "audio/webm"},
        ]
        assert await ev._transcribe_files(orch, files) == []
        orch.slack.download_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_successful_transcription(self, _mock_sel):
        orch = _make_orch()
        files = [
            {
                "mimetype": "audio/webm",
                "url_private_download": "https://x.invalid/a.webm",
                "filetype": "webm",
                "name": "memo.webm",
            }
        ]
        with patch(
            "kiro_crew.slack.events.transcribe_audio",
            new_callable=AsyncMock,
            return_value="spoken words",
        ):
            assert await ev._transcribe_files(orch, files) == ["spoken words"]
        outcomes = [c.kwargs.get("outcome") for c in _mock_sel.log_api_access.call_args_list]
        assert "success" in outcomes

    @pytest.mark.asyncio
    async def test_empty_transcript_warns_and_returns_nothing(self, _mock_sel):
        orch = _make_orch()
        files = [
            {
                "mimetype": "audio/mp4",
                "url_private": "https://x.invalid/a.m4a",
                "filetype": "m 4 a!",
                "name": "quiet.m4a",
            }
        ]
        with patch(
            "kiro_crew.slack.events.transcribe_audio",
            new_callable=AsyncMock,
            return_value="",
        ):
            assert await ev._transcribe_files(orch, files) == []
        outcomes = [c.kwargs.get("outcome") for c in _mock_sel.log_api_access.call_args_list]
        assert "empty" in outcomes

    @pytest.mark.asyncio
    async def test_over_duration_memo_is_refused_before_transcription(self, _mock_sel):
        orch = _make_orch()
        files = [
            {
                "mimetype": "audio/webm",
                "url_private": "https://x.invalid/a.webm",
                "filetype": "webm",
                "name": "long.webm",
            }
        ]
        with (
            patch(
                "kiro_crew.slack.events.load_stt_config",
                return_value=SimpleNamespace(timeout_secs=900),
            ),
            patch("kiro_crew.slack.events.batch_duration_cap_secs", return_value=3600),
            patch(
                "kiro_crew.slack.events.audio_exceeds_secs",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch("kiro_crew.slack.events.transcribe_audio", new_callable=AsyncMock) as transcribe,
        ):
            result = await ev._transcribe_files(orch, files)

        assert result == ["[Audio attachment — exceeds the 60-minute transcription limit]"]
        # The note must be the pinned shared constant, not a drifted copy.
        assert result == [slack_files.VOICE_MEMO_TOO_LONG.format(minutes=60)]
        transcribe.assert_not_awaited()
        assert any(
            call.kwargs.get("error") == "audio_too_long"
            for call in _mock_sel.log_api_access.call_args_list
        )

    @pytest.mark.asyncio
    async def test_unverified_duration_is_refused_before_transcription(self, _mock_sel):
        orch = _make_orch()
        files = [
            {
                "mimetype": "audio/webm",
                "url_private": "https://x.invalid/a.webm",
                "filetype": "webm",
                "name": "unknown.webm",
            }
        ]
        with (
            patch(
                "kiro_crew.slack.events.load_stt_config",
                return_value=SimpleNamespace(timeout_secs=900),
            ),
            patch("kiro_crew.slack.events.batch_duration_cap_secs", return_value=3600),
            patch(
                "kiro_crew.slack.events.audio_exceeds_secs",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("kiro_crew.slack.events.transcribe_audio", new_callable=AsyncMock) as transcribe,
        ):
            result = await ev._transcribe_files(orch, files)

        assert result == ["[Audio attachment — duration could not be verified]"]
        # The note must be the pinned shared constant, not a drifted copy.
        assert result == [slack_files.VOICE_MEMO_DURATION_UNVERIFIED]
        transcribe.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_under_cap_memo_is_transcribed(self, _mock_sel):
        """A memo verified under the cap takes the healthy path: transcription
        is awaited and its transcript returned, with neither refusal note."""
        orch = _make_orch()
        files = [
            {
                "mimetype": "audio/webm",
                "url_private": "https://x.invalid/a.webm",
                "filetype": "webm",
                "name": "short.webm",
            }
        ]
        with (
            patch(
                "kiro_crew.slack.events.load_stt_config",
                return_value=SimpleNamespace(timeout_secs=900),
            ),
            patch("kiro_crew.slack.events.batch_duration_cap_secs", return_value=3600),
            patch(
                "kiro_crew.slack.events.audio_exceeds_secs",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "kiro_crew.slack.events.transcribe_audio",
                new_callable=AsyncMock,
                return_value="hello from the memo",
            ) as transcribe,
        ):
            result = await ev._transcribe_files(orch, files)

        transcribe.assert_awaited_once()
        assert result == ["hello from the memo"]
        assert not any(
            slack_files.VOICE_MEMO_TOO_LONG.format(minutes=60) in item
            or slack_files.VOICE_MEMO_DURATION_UNVERIFIED in item
            for item in result
        )

    @pytest.mark.asyncio
    async def test_download_failure_audits_error(self, _mock_sel):
        orch = _make_orch()
        orch.slack.download_file = AsyncMock(side_effect=RuntimeError("404"))
        files = [
            {
                "mimetype": "audio/webm",
                "url_private": "https://x.invalid/a.webm",
                "name": "bad.webm",
            }
        ]
        assert await ev._transcribe_files(orch, files) == []
        errors = [c.kwargs.get("error") for c in _mock_sel.log_api_access.call_args_list]
        assert "transcription_failed" in errors

    @pytest.mark.asyncio
    async def test_temp_unlink_failure_is_tolerated(self, tmp_path, monkeypatch):
        orch = _make_orch()
        files = [
            {
                "mimetype": "audio/webm",
                "url_private": "https://x.invalid/a.webm",
                "name": "memo.webm",
            }
        ]
        # This test deliberately makes unlink fail, so the downloaded file survives
        # the run. events_voice.py allocates it with tempfile.mkstemp(), which would put it
        # in the SYSTEM temp dir -- a real artifact left on the host, which the
        # blocking no-test-side-effects rule forbids. Pin the allocation into
        # tmp_path so the surviving file is inside pytest's own scratch dir.
        real_mkstemp = tempfile.mkstemp

        def _mkstemp_in_tmp(*args, **kwargs):
            kwargs["dir"] = str(tmp_path)
            return real_mkstemp(*args, **kwargs)

        monkeypatch.setattr(tempfile, "mkstemp", _mkstemp_in_tmp)
        with patch(
            "kiro_crew.slack.events.transcribe_audio",
            new_callable=AsyncMock,
            return_value="words",
        ):
            with patch("kiro_crew.slack.events.os.unlink", side_effect=OSError("locked")) as unlink:
                assert await ev._transcribe_files(orch, files) == ["words"]
        unlink.assert_called_once()
        # The surviving file is inside tmp_path, not the system temp dir.
        assert list(tmp_path.iterdir()), "the download should have landed in tmp_path"


# ---------------------------------------------------------------------------
# message_deleted
# ---------------------------------------------------------------------------


class TestHandleMessageDeleted:
    @pytest.mark.asyncio
    async def test_unauthorized_deleter_ignored(self, _mock_sel):
        orch = _make_orch()
        orch._pending_queue = {"1.0": [("1.0", "text", {})]}
        event = {"deleted_ts": "1.0", "channel": "C1", "previous_message": {"user": "U_BAD"}}
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=False) as allowed:
            await ev._handle_message_deleted(orch, event)
        allowed.assert_called_once_with("U_BAD")
        orch.sessions.cancel_queued.assert_not_called()
        assert orch._pending_queue == {"1.0": [("1.0", "text", {})]}
        _mock_sel.log_api_access.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_deleted_ts_ignored(self, _mock_sel):
        orch = _make_orch()
        orch._pending_queue = {"1.0": [("1.0", "text", {})]}
        event = {"channel": "C1", "previous_message": {"user": "U_OWNER", "thread_ts": "1.0"}}
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            await ev._handle_message_deleted(orch, event)
        orch.sessions.cancel_queued.assert_not_called()
        assert orch._pending_queue == {"1.0": [("1.0", "text", {})]}
        _mock_sel.log_api_access.assert_not_called()

    @pytest.mark.asyncio
    async def test_session_queue_cancellation_recorded(self, _mock_sel):
        orch = _make_orch()
        orch.sessions.cancel_queued = MagicMock(return_value=True)
        event = {
            "deleted_ts": "2.0",
            "channel": "C1",
            "previous_message": {"user": "U_OWNER", "thread_ts": "1.0"},
        }
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            await ev._handle_message_deleted(orch, event)
        orch.sessions.cancel_queued.assert_called_once_with("1.0", "2.0")
        assert "queued=True" in _mock_sel.log_api_access.call_args.kwargs["resources"]

    @pytest.mark.asyncio
    async def test_pending_queue_entry_removed_and_key_dropped(self):
        orch = _make_orch()
        orch._pending_queue = {"3.0": [("3.0", "text", {})]}
        event = {"deleted_ts": "3.0", "channel": "C1", "previous_message": {"user": "U_OWNER"}}
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            await ev._handle_message_deleted(orch, event)
        assert "3.0" not in orch._pending_queue

    @pytest.mark.asyncio
    async def test_pending_queue_keeps_surviving_entries(self):
        orch = _make_orch()
        orch._pending_queue = {"4.0": [("4.0", "gone", {}), ("5.0", "stays", {})]}
        event = {"deleted_ts": "4.0", "channel": "C1", "previous_message": {"user": "U_OWNER"}}
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            await ev._handle_message_deleted(orch, event)
        assert orch._pending_queue["4.0"] == [("5.0", "stays", {})]

    @pytest.mark.asyncio
    async def test_nothing_queued_still_audits(self, _mock_sel):
        orch = _make_orch()
        event = {"deleted_ts": "6.0", "channel": "C1", "previous_message": {"user": "U_OWNER"}}
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            await ev._handle_message_deleted(orch, event)
        assert "queued=False" in _mock_sel.log_api_access.call_args.kwargs["resources"]


# ---------------------------------------------------------------------------
# _resolve_approval_mode
# ---------------------------------------------------------------------------


class TestResolveApprovalMode:
    def test_yolo_forces_auto(self):
        orch = _make_orch()
        orch._approval_mode = "interactive"
        with patch("kiro_crew.slack.events.is_yolo_mode", return_value=True):
            assert ev._resolve_approval_mode(orch) == ev.APPROVAL_AUTO

    def test_cli_flag_wins_over_config(self):
        orch = _make_orch()
        orch._approval_mode = ev.APPROVAL_AUTO
        with patch("kiro_crew.slack.events.is_yolo_mode", return_value=False):
            assert ev._resolve_approval_mode(orch) == ev.APPROVAL_AUTO

    def test_anything_else_is_interactive(self):
        orch = _make_orch()
        orch._approval_mode = "reads"
        with patch("kiro_crew.slack.events.is_yolo_mode", return_value=False):
            assert ev._resolve_approval_mode(orch) == ev.APPROVAL_INTERACTIVE
