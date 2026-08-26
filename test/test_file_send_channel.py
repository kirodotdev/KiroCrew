"""Tests for file_send channel parameter feature.

Tests the api_slack_upload_file handler's channel routing:
- When channel is provided and tracked, upload goes to that channel
- When channel is provided but not tracked, request is denied (403)
- When channel is omitted, falls back to owner DM (existing behavior)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers.files import api_slack_upload_file
from kiro_crew.dashboard.state import DashboardState


def _make_app(slack_client, tmp_path, state=None):
    """Minimal app with the upload-file route and a mock Slack client."""
    app = web.Application()
    if state is None:
        state = MagicMock(spec=DashboardState)
        state.slack_client = slack_client
    app["state"] = state
    app.router.add_post("/api/slack/upload-file", api_slack_upload_file)
    return app


@pytest.fixture
def outbox_file(tmp_path):
    """Create a valid UTF-8 file inside a fake outbox directory."""
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    f = outbox / "report.txt"
    f.write_text("hello world", encoding="utf-8")
    return f


class TestFileUploadChannel:
    @pytest.mark.asyncio
    async def test_upload_to_tracked_channel(self, tmp_path, outbox_file):
        """When channel is provided and tracked, file uploads to that channel."""
        slack = MagicMock()
        slack.upload_file = AsyncMock()
        app = _make_app(slack, tmp_path)

        with patch(
            "kiro_crew.config.loader.outbox_dir",
            return_value=outbox_file.parent,
        ), patch(
            "kiro_crew.config.loader.workspace_root",
            return_value=tmp_path,
        ), patch(
            "kiro_crew.dashboard.handlers.files.is_tracked_channel",
            return_value=True,
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/slack/upload-file",
                    json={
                        "file_path": str(outbox_file),
                        "filename": "report.txt",
                        "thread_ts": "",
                        "channel": "C0TRACKED123",
                    },
                )
                body = await resp.json()

        assert resp.status == 200
        assert body.get("ok") is True
        # Verify upload went to the specified channel, not owner DM
        slack.upload_file.assert_called_once()
        call_args = slack.upload_file.call_args
        assert call_args[0][0] == "C0TRACKED123"

    @pytest.mark.asyncio
    async def test_upload_to_untracked_channel_denied(self, tmp_path, outbox_file):
        """When channel is provided but NOT tracked, returns 403."""
        slack = MagicMock()
        slack.upload_file = AsyncMock()
        app = _make_app(slack, tmp_path)

        with patch(
            "kiro_crew.config.loader.outbox_dir",
            return_value=outbox_file.parent,
        ), patch(
            "kiro_crew.config.loader.workspace_root",
            return_value=tmp_path,
        ), patch(
            "kiro_crew.dashboard.handlers.files.is_tracked_channel",
            return_value=False,
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/slack/upload-file",
                    json={
                        "file_path": str(outbox_file),
                        "filename": "report.txt",
                        "thread_ts": "",
                        "channel": "C0UNTRACKED9",
                    },
                )
                body = await resp.json()

        assert resp.status == 403
        assert "not in tracked channels" in body.get("error", "")
        slack.upload_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_upload_without_channel_uses_owner_dm(self, tmp_path, outbox_file):
        """When channel is omitted, falls back to owner DM."""
        slack = MagicMock()
        slack.upload_file = AsyncMock()
        slack.open_dm = AsyncMock(return_value="D_OWNER_DM")
        app = _make_app(slack, tmp_path)

        with patch(
            "kiro_crew.config.loader.outbox_dir",
            return_value=outbox_file.parent,
        ), patch(
            "kiro_crew.config.loader.workspace_root",
            return_value=tmp_path,
        ), patch(
            "kiro_crew.config.loader.KiroCrewConfig.load",
        ) as mock_cfg:
            mock_cfg.return_value.load_credentials.return_value = {
                "KIROCREW_OWNER_ID": "U_OWNER"
            }
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/slack/upload-file",
                    json={
                        "file_path": str(outbox_file),
                        "filename": "report.txt",
                        "thread_ts": "",
                        "channel": "",
                    },
                )
                body = await resp.json()

        assert resp.status == 200
        assert body.get("ok") is True
        slack.upload_file.assert_called_once()
        call_args = slack.upload_file.call_args
        assert call_args[0][0] == "D_OWNER_DM"

    @pytest.mark.asyncio
    async def test_upload_with_invalid_channel_returns_400(self, tmp_path, outbox_file):
        """When channel exceeds max length, returns 400."""
        slack = MagicMock()
        slack.upload_file = AsyncMock()
        app = _make_app(slack, tmp_path)

        with patch(
            "kiro_crew.config.loader.outbox_dir",
            return_value=outbox_file.parent,
        ), patch(
            "kiro_crew.config.loader.workspace_root",
            return_value=tmp_path,
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/slack/upload-file",
                    json={
                        "file_path": str(outbox_file),
                        "filename": "report.txt",
                        "thread_ts": "",
                        "channel": "C" * 600,
                    },
                )
                body = await resp.json()

        assert resp.status == 400
        assert "invalid channel value" in body.get("error", "")
        slack.upload_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_path_outside_allowed_roots_denied_with_code(self, tmp_path):
        """A file_path outside both the outbox and the workspace root returns 403
        with a machine-readable code and a message naming the allowed roots."""
        slack = MagicMock()
        slack.upload_file = AsyncMock()
        app = _make_app(slack, tmp_path)

        outside = tmp_path / "elsewhere" / "secret.txt"
        outside.parent.mkdir()
        outside.write_text("data", encoding="utf-8")

        with patch(
            "kiro_crew.config.loader.outbox_dir",
            return_value=tmp_path / "outbox",
        ), patch(
            "kiro_crew.config.loader.workspace_root",
            return_value=tmp_path / "workspace",
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/slack/upload-file",
                    json={
                        "file_path": str(outside),
                        "filename": "secret.txt",
                        "thread_ts": "",
                    },
                )
                body = await resp.json()

        assert resp.status == 403
        assert body.get("code") == "path_not_allowed"
        assert "outbox directory or the workspace root" in body.get("error", "")
        # The caller-supplied path must not be reflected back in the body.
        assert str(outside) not in body.get("error", "")
        slack.upload_file.assert_not_called()


class TestFileUploadBinary:
    """Behaviour: binary files in BINARY_MIME_ALLOWLIST upload to Slack without UTF-8 decode."""

    @pytest.mark.asyncio
    async def test_binary_audio_uploaded_to_slack(self, tmp_path):
        """Happy path: WAV file (binary, in allowlist) uploads successfully."""
        outbox = tmp_path / "outbox"
        outbox.mkdir()
        wav = outbox / "clip.wav"
        wav.write_bytes(b"\x00" * 100)  # non-UTF-8 binary content

        slack = MagicMock()
        slack.upload_file = AsyncMock()
        app = _make_app(slack, tmp_path)

        with patch(
            "kiro_crew.config.loader.outbox_dir",
            return_value=outbox,
        ), patch(
            "kiro_crew.config.loader.workspace_root",
            return_value=tmp_path,
        ), patch(
            "kiro_crew.dashboard.handlers.files._sel",
            return_value=MagicMock(),
        ), patch(
            "kiro_crew.dashboard.handlers.files.is_tracked_channel",
            return_value=True,
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/slack/upload-file",
                    json={
                        "file_path": str(wav),
                        "filename": "clip.wav",
                        "thread_ts": "123.456",
                        "channel": "C0TEST123",
                    },
                )
                assert resp.status == 200
                slack.upload_file.assert_called_once()

    @pytest.mark.asyncio
    async def test_binary_disallowed_mime_rejected(self, tmp_path):
        """Unhappy path: binary EXE file (not in allowlist) rejected with 400."""
        outbox = tmp_path / "outbox"
        outbox.mkdir()
        exe = outbox / "payload.exe"
        exe.write_bytes(b"\x4d\x5a\x90\x00" * 20)  # non-UTF-8 PE header

        slack = MagicMock()
        slack.upload_file = AsyncMock()
        app = _make_app(slack, tmp_path)

        with patch(
            "kiro_crew.config.loader.outbox_dir",
            return_value=outbox,
        ), patch(
            "kiro_crew.config.loader.workspace_root",
            return_value=tmp_path,
        ), patch(
            "kiro_crew.dashboard.handlers.files._sel",
            return_value=MagicMock(),
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/slack/upload-file",
                    json={
                        "file_path": str(exe),
                        "filename": "payload.exe",
                        "thread_ts": "123.456",
                    },
                )
                assert resp.status == 400
                data = await resp.json()
                assert "not allowed" in data["error"].lower() or "not supported" in data["error"].lower()
                slack.upload_file.assert_not_called()


class TestFileUploadSlotThreading:
    """Behaviour: file_send resolves thread_ts from session_map when not explicitly provided."""

    def _make_state_with_link(self, slack, thread_ts=None, channel=None):
        """Create state with a sessions mock that returns slack link data."""
        state = MagicMock()
        state.slack_client = slack
        sessions = MagicMock()
        sessions.get_slack_link = MagicMock(
            return_value=(thread_ts, channel)
        )
        state.sessions = sessions
        return state

    @pytest.mark.asyncio
    async def test_slot_thread_ts_used_when_body_empty(self, tmp_path):
        """T1: Session-map-sourced channel bypasses tracking check."""
        outbox = tmp_path / "outbox"
        outbox.mkdir()
        f = outbox / "note.txt"
        f.write_text("hello", encoding="utf-8")

        slack = MagicMock()
        slack.upload_file = AsyncMock()
        state = self._make_state_with_link(
            slack, thread_ts="111.222", channel="D0SLOTDM01"
        )
        app = _make_app(slack, tmp_path, state=state)

        with patch(
            "kiro_crew.config.loader.outbox_dir", return_value=outbox
        ), patch(
            "kiro_crew.config.loader.workspace_root", return_value=tmp_path
        ), patch(
            "kiro_crew.dashboard.handlers.files.is_tracked_channel", return_value=False
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/slack/upload-file",
                    json={
                        "file_path": str(f),
                        "filename": "note.txt",
                        "thread_ts": "",
                        "channel": "",
                    },
                    headers={"X-Session-Key": "dashboard:chat-1"},
                )
                assert resp.status == 200
                slack.upload_file.assert_called_once()
                call_args = slack.upload_file.call_args
                # Channel from session map — bypasses tracking check
                assert call_args[0][0] == "D0SLOTDM01"
                # thread_ts from session map
                assert call_args[0][1] == "111.222"

    @pytest.mark.asyncio
    async def test_session_map_dm_channel_bypasses_tracking(self, tmp_path):
        """T4: DM channel from session map bypasses is_tracked_channel gate."""
        outbox = tmp_path / "outbox"
        outbox.mkdir()
        f = outbox / "clip.wav"
        f.write_bytes(b"\x00" * 100)

        slack = MagicMock()
        slack.upload_file = AsyncMock()
        state = self._make_state_with_link(
            slack, thread_ts="1779958875.862869", channel="D0AMUTELUCA"
        )
        app = _make_app(slack, tmp_path, state=state)

        with patch(
            "kiro_crew.config.loader.outbox_dir", return_value=outbox
        ), patch(
            "kiro_crew.config.loader.workspace_root", return_value=tmp_path
        ), patch(
            "kiro_crew.dashboard.handlers.files.is_tracked_channel", return_value=False
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/slack/upload-file",
                    json={
                        "file_path": str(f),
                        "filename": "clip.wav",
                        "thread_ts": "",
                        "channel": "",
                    },
                    headers={"X-Session-Key": "dashboard:1779958875.862869"},
                )
                assert resp.status == 200
                slack.upload_file.assert_called_once()
                call_args = slack.upload_file.call_args
                # DM channel from session map — NOT rejected by tracking check
                assert call_args[0][0] == "D0AMUTELUCA"
                # thread_ts from session map
                assert call_args[0][1] == "1779958875.862869"

    @pytest.mark.asyncio
    async def test_no_slot_thread_falls_back_to_owner_dm(self, tmp_path):
        """T2: Session has no slack link → falls back to owner DM top-level."""
        outbox = tmp_path / "outbox"
        outbox.mkdir()
        f = outbox / "report.txt"
        f.write_text("data", encoding="utf-8")

        slack = MagicMock()
        slack.upload_file = AsyncMock()
        slack.open_dm = AsyncMock(return_value="D_OWNER_DM")
        state = self._make_state_with_link(slack, thread_ts=None, channel=None)
        app = _make_app(slack, tmp_path, state=state)

        with patch(
            "kiro_crew.config.loader.outbox_dir", return_value=outbox
        ), patch(
            "kiro_crew.config.loader.workspace_root", return_value=tmp_path
        ), patch(
            "kiro_crew.config.loader.KiroCrewConfig.load"
        ) as mock_cfg:
            mock_cfg.return_value.load_credentials.return_value = {
                "KIROCREW_OWNER_ID": "U_OWNER"
            }
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/slack/upload-file",
                    json={
                        "file_path": str(f),
                        "filename": "report.txt",
                        "thread_ts": "",
                        "channel": "",
                    },
                    headers={"X-Session-Key": "dashboard:chat-1"},
                )
                assert resp.status == 200
                slack.upload_file.assert_called_once()
                call_args = slack.upload_file.call_args
                assert call_args[0][0] == "D_OWNER_DM"
                # No thread_ts — top-level
                assert call_args[0][1] == ""

    @pytest.mark.asyncio
    async def test_explicit_thread_ts_takes_priority_over_slot(self, tmp_path):
        """T3: Explicit thread_ts in body → takes priority over session map."""
        outbox = tmp_path / "outbox"
        outbox.mkdir()
        f = outbox / "log.txt"
        f.write_text("log data", encoding="utf-8")

        slack = MagicMock()
        slack.upload_file = AsyncMock()
        state = self._make_state_with_link(
            slack, thread_ts="111.222", channel="C0SLOTCHAN"
        )
        app = _make_app(slack, tmp_path, state=state)

        with patch(
            "kiro_crew.config.loader.outbox_dir", return_value=outbox
        ), patch(
            "kiro_crew.config.loader.workspace_root", return_value=tmp_path
        ), patch(
            "kiro_crew.dashboard.handlers.files.is_tracked_channel", return_value=True
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/slack/upload-file",
                    json={
                        "file_path": str(f),
                        "filename": "log.txt",
                        "thread_ts": "999.888",
                        "channel": "C0EXPLICIT",
                    },
                    headers={"X-Session-Key": "dashboard:chat-1"},
                )
                assert resp.status == 200
                slack.upload_file.assert_called_once()
                call_args = slack.upload_file.call_args
                # Explicit channel wins
                assert call_args[0][0] == "C0EXPLICIT"
                # Explicit thread_ts wins
                assert call_args[0][1] == "999.888"

    @pytest.mark.asyncio
    async def test_explicit_channel_does_not_inherit_unrelated_thread_ts(self, tmp_path):
        """T6: explicit channel differing from the session-map link's channel must
        NOT inherit the link's thread_ts (it belongs to a different channel)."""
        outbox = tmp_path / "outbox"
        outbox.mkdir()
        f = outbox / "note.txt"
        f.write_text("hello", encoding="utf-8")

        slack = MagicMock()
        slack.upload_file = AsyncMock()
        state = self._make_state_with_link(
            slack, thread_ts="111.222", channel="D0SLOTDM01"
        )
        app = _make_app(slack, tmp_path, state=state)

        with patch(
            "kiro_crew.config.loader.outbox_dir", return_value=outbox
        ), patch(
            "kiro_crew.config.loader.workspace_root", return_value=tmp_path
        ), patch(
            "kiro_crew.dashboard.handlers.files.is_tracked_channel", return_value=True
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/slack/upload-file",
                    json={
                        "file_path": str(f),
                        "filename": "note.txt",
                        "thread_ts": "",
                        "channel": "C0OTHER",
                    },
                    headers={"X-Session-Key": "dashboard:chat-1"},
                )
                assert resp.status == 200
                slack.upload_file.assert_called_once()
                call_args = slack.upload_file.call_args
                # Explicit channel honoured
                assert call_args[0][0] == "C0OTHER"
                # thread_ts NOT inherited from the unrelated session-map link
                assert call_args[0][1] == ""

    @pytest.mark.asyncio
    async def test_session_map_non_dm_untracked_channel_rejected(self, tmp_path):
        """T5: Non-DM channel from session map that isn't tracked gets rejected (defense-in-depth)."""
        outbox = tmp_path / "outbox"
        outbox.mkdir()
        f = outbox / "note.txt"
        f.write_text("hello", encoding="utf-8")

        slack = MagicMock()
        slack.upload_file = AsyncMock()
        state = self._make_state_with_link(
            slack, thread_ts="111.222", channel="C0ROGUE999"
        )
        app = _make_app(slack, tmp_path, state=state)

        with patch(
            "kiro_crew.config.loader.outbox_dir", return_value=outbox
        ), patch(
            "kiro_crew.config.loader.workspace_root", return_value=tmp_path
        ), patch(
            "kiro_crew.dashboard.handlers.files.is_tracked_channel", return_value=False
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/slack/upload-file",
                    json={
                        "file_path": str(f),
                        "filename": "note.txt",
                        "thread_ts": "",
                        "channel": "",
                    },
                    headers={"X-Session-Key": "dashboard:chat-1"},
                )
                assert resp.status == 403
                body = await resp.json()
                assert "not authorized" in body.get("error", "")
                slack.upload_file.assert_not_called()
