"""Tests for file_send channel parameter feature.

Tests the api_slack_upload_file handler's channel routing:
- When channel is provided and tracked, upload goes to that channel
- When channel is provided but not tracked, request is denied (403)
- When channel is omitted, falls back to owner DM (existing behavior)
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers.files import api_channel_upload_file, api_slack_upload_file
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


@pytest.fixture
def outbox_pdf(tmp_path):
    """A non-raster document in the outbox — the extension the extraction
    sanitizer would rewrite to `.bin`, which is what the document verb exists
    to preserve. The delivered name comes from ``OutboundFile.path`` (the
    resolved file on disk), not from the request's ``filename`` field.
    """
    outbox = tmp_path / "outbox"
    outbox.mkdir(exist_ok=True)
    f = outbox / "report.pdf"
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


class TestChannelUploadEndpoint:
    """POST /api/channel/upload-file — the non-Slack parity leg of file_send.

    Destination comes exclusively from the caller's session map entry via the
    shared send ladder; these tests fake the ladder's answer and verify the
    handler's own obligations: skip-vs-error semantics, the shared admission
    gate, and the per-channel delivery calls.
    """

    def _app(self, state=None):
        if state is None:
            state = MagicMock(spec=DashboardState)
            state.sessions = MagicMock()
        app = web.Application()
        app["state"] = state
        app.router.add_post("/api/channel/upload-file", api_channel_upload_file)
        return app

    @staticmethod
    def _link(channel_type, channel_id="42", thread_id=None):
        from kiro_crew.messaging.link import ChannelLink

        return ChannelLink(channel_type=channel_type, channel_id=channel_id, thread_id=thread_id)

    @pytest.mark.asyncio
    async def test_a_restricted_session_gets_no_native_delivery(self, tmp_path, outbox_file):
        # The renderers' extraction path enforces the restricted ceiling
        # (incognito/temporary sessions ship no local file bytes); an explicit
        # file_send must not be the bypass. Same shared predicate, same skip
        # shape as every other "cannot deliver here" answer.
        from unittest.mock import AsyncMock

        transport = MagicMock(spec_set=["send_document"])
        transport.send_document = AsyncMock(return_value="123")
        app = self._app()
        with patch(
            "kiro_crew.dashboard.chat_runner._resolve_mirror_target",
            return_value=(self._link("telegram", "42"), transport),
        ), patch(
            "kiro_crew.messaging.upload_gate.uploads_restricted",
            new=AsyncMock(return_value=True),
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/channel/upload-file",
                    json={"file_path": str(outbox_file), "filename": "report.txt"},
                    headers={"X-Session-Key": "telegram:1"},
                )
                body = await resp.json()
        assert resp.status == 200
        assert body["delivered"] is False
        assert body["skipped"] == "restricted_session"
        transport.send_document.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_credential_bearing_filename_is_rejected_for_both_legs(self, tmp_path):
        # The Slack leg rejects a sensitive filename at its send site; the
        # channel leg must not be the bypass. Enforced in the SHARED gate so
        # neither leg can drift: checked before path resolution, so the name
        # never even selects a file.
        from unittest.mock import AsyncMock

        transport = MagicMock(spec_set=["send_document"])
        transport.send_document = AsyncMock(return_value="123")
        app = self._app()
        leaky_name = "AKIA" + "IOSFODNN7EXAMPLE" + ".txt"
        with patch(
            "kiro_crew.dashboard.chat_runner._resolve_mirror_target",
            return_value=(self._link("telegram", "42"), transport),
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/channel/upload-file",
                    json={"file_path": str(tmp_path / leaky_name), "filename": leaky_name},
                    headers={"X-Session-Key": "telegram:1"},
                )
                body = await resp.json()
        assert resp.status == 400
        assert "filename contains sensitive content" in body.get("error", "")
        transport.send_document.assert_not_called()

    @pytest.mark.asyncio
    async def test_outbound_text_is_redacted_in_display_form(self, tmp_path, outbox_file):
        # redact() scans literal bytes; a channel renderer strips markdown
        # delimiters at display, so AKIA**…** passes a literal scan and
        # displays as an intact key. The caption must go through
        # redact_for_display before delivery — same boundary rule as every
        # renderer sink.
        from unittest.mock import AsyncMock

        key = "AKIA" + "IOSFODNN7EXAMPLE"
        transport = MagicMock(spec_set=["send_document"])
        transport.send_document = AsyncMock(return_value="900")
        app = self._app()
        with patch(
            "kiro_crew.dashboard.chat_runner._resolve_mirror_target",
            return_value=(self._link("telegram", "42"), transport),
        ), patch(
            "kiro_crew.config.loader.outbox_dir", return_value=outbox_file.parent
        ), patch(
            "kiro_crew.config.loader.workspace_root", return_value=tmp_path
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/channel/upload-file",
                    json={
                        "file_path": str(outbox_file),
                        "filename": "report.txt",
                        "description": f"{key[:4]}**{key[4:]}**",
                    },
                    headers={"X-Session-Key": "telegram:1"},
                )
                body = await resp.json()
        assert resp.status == 200 and body["delivered"] is True
        _, kwargs = transport.send_document.call_args
        caption = kwargs["caption"]
        # What the channel DISPLAYS (delimiters stripped) must not reassemble
        # the key.
        assert key not in caption.replace("*", "").replace("`", "").replace("_", "")

    @pytest.mark.asyncio
    async def test_destination_resolution_runs_off_the_event_loop(self, tmp_path, outbox_file):
        # The ladder reloads governance profiles and reads the persisted
        # session map — synchronous filesystem work. Like the admission gate,
        # it must not run inline in the async handler
        # (no-blocking-call-on-event-loop).
        from unittest.mock import AsyncMock

        loop_thread = threading.get_ident()
        seen: dict = {}
        transport = MagicMock(spec_set=["send_document"])
        transport.send_document = AsyncMock(return_value="123")

        def _fake_resolver(state, session_key):
            seen["thread"] = threading.get_ident()
            return None  # skip path; the thread identity is the assertion

        app = self._app()
        with patch(
            "kiro_crew.dashboard.chat_runner._resolve_mirror_target",
            side_effect=_fake_resolver,
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/channel/upload-file",
                    json={"file_path": str(outbox_file), "filename": "report.txt"},
                    headers={"X-Session-Key": "telegram:1"},
                )
                assert (await resp.json())["skipped"] == "no_channel_destination"
        assert seen.get("thread") is not None, "resolver must have run"
        assert seen["thread"] != loop_thread, "resolver ran ON the event loop thread"
        assert asyncio.get_event_loop() is not None  # loop alive throughout

    @pytest.mark.asyncio
    async def test_no_session_key_is_a_skip_not_an_error(self, tmp_path):
        """Destination is resolved before the file is even read."""
        app = self._app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/channel/upload-file",
                json={"file_path": str(tmp_path / "missing.txt"), "filename": "missing.txt"},
            )
            body = await resp.json()
        assert resp.status == 200
        assert body == {"ok": True, "delivered": False, "skipped": "no_session"}

    @pytest.mark.asyncio
    async def test_no_destination_is_a_skip(self, tmp_path, outbox_file):
        app = self._app()
        with patch(
            "kiro_crew.dashboard.chat_runner._resolve_mirror_target",
            return_value=None,
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/channel/upload-file",
                    json={"file_path": str(outbox_file), "filename": "report.txt"},
                    headers={"X-Session-Key": "dashboard:chat-1"},
                )
                body = await resp.json()
        assert resp.status == 200
        assert body["delivered"] is False
        assert body["skipped"] == "no_channel_destination"

    @pytest.mark.asyncio
    async def test_telegram_destination_gets_send_document(self, tmp_path, outbox_file):
        from unittest.mock import AsyncMock

        transport = MagicMock(spec_set=["send_document"])
        transport.send_document = AsyncMock(return_value="123")
        app = self._app()
        with patch(
            "kiro_crew.dashboard.chat_runner._resolve_mirror_target",
            return_value=(self._link("telegram", "42", thread_id="7"), transport),
        ), patch(
            "kiro_crew.config.loader.outbox_dir", return_value=outbox_file.parent
        ), patch(
            "kiro_crew.config.loader.workspace_root", return_value=tmp_path
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/channel/upload-file",
                    json={
                        "file_path": str(outbox_file),
                        "filename": "report.txt",
                        "description": "weekly numbers",
                    },
                    headers={"X-Session-Key": "telegram:1"},
                )
                body = await resp.json()
        assert resp.status == 200
        assert body == {"ok": True, "delivered": True, "channel_type": "telegram"}
        transport.send_document.assert_awaited_once()
        args, kwargs = transport.send_document.call_args
        assert args[0] == "42"
        outbound = args[1]
        # The OutboundFile contract: the gated bytes ARE the payload.
        assert outbound.data == b"hello world"
        assert outbound.path == str(outbox_file)
        assert kwargs["caption"] == "weekly numbers"
        assert kwargs["thread_id"] == "7"

    @pytest.mark.asyncio
    async def test_discord_destination_gets_send_document(self, tmp_path, outbox_pdf):
        # Issue #6058: Discord was an explicit skip while its only upload verb
        # was the extraction one, whose sanitizer maps any non-raster mime to
        # `.bin` (report.pdf would arrive as report.bin). It now has the same
        # purpose-built name-preserving verb Telegram uses, so it delivers.
        from unittest.mock import AsyncMock

        transport = MagicMock(spec_set=["send_document", "send_message_with_files"])
        transport.send_document = AsyncMock(return_value="900")
        transport.send_message_with_files = AsyncMock(return_value="901")
        app = self._app()
        with patch(
            "kiro_crew.dashboard.chat_runner._resolve_mirror_target",
            return_value=(self._link("discord", "555"), transport),
        ), patch(
            "kiro_crew.config.loader.outbox_dir", return_value=outbox_pdf.parent
        ), patch(
            "kiro_crew.config.loader.workspace_root", return_value=tmp_path
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/channel/upload-file",
                    json={
                        "file_path": str(outbox_pdf),
                        "filename": "report.pdf",
                        "description": "weekly numbers",
                    },
                    headers={"X-Session-Key": "discord:1"},
                )
                body = await resp.json()
        assert resp.status == 200
        assert body == {"ok": True, "delivered": True, "channel_type": "discord"}
        transport.send_document.assert_awaited_once()
        # The extraction verb stays out of this path: it is the one whose
        # sanitizer would rename the attachment.
        transport.send_message_with_files.assert_not_called()
        args, kwargs = transport.send_document.call_args
        assert args[0] == "555"
        outbound = args[1]
        # The OutboundFile contract: the gated bytes ARE the payload.
        assert outbound.data == b"hello world"
        assert outbound.path == str(outbox_pdf)
        # `.pdf` survives end-to-end — the whole point of the separate verb.
        # `upload_filename` would have made this `report.bin`.
        from kiro_crew.messaging.outbound_files import upload_filename

        assert Path(outbound.path).name == "report.pdf"
        assert upload_filename(outbound, 0) == "report.bin"
        assert outbound.mime == "application/pdf"
        assert kwargs["caption"] == "weekly numbers"

    @pytest.mark.asyncio
    async def test_discord_outbound_text_is_redacted_in_display_form(self, tmp_path, outbox_file):
        # Same boundary rule as the Telegram leg: redact() scans literal bytes,
        # and Discord renders markdown, so AKIA**…** displays as an intact key.
        from unittest.mock import AsyncMock

        key = "AKIA" + "IOSFODNN7EXAMPLE"
        transport = MagicMock(spec_set=["send_document"])
        transport.send_document = AsyncMock(return_value="900")
        app = self._app()
        with patch(
            "kiro_crew.dashboard.chat_runner._resolve_mirror_target",
            return_value=(self._link("discord", "555"), transport),
        ), patch(
            "kiro_crew.config.loader.outbox_dir", return_value=outbox_file.parent
        ), patch(
            "kiro_crew.config.loader.workspace_root", return_value=tmp_path
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/channel/upload-file",
                    json={
                        "file_path": str(outbox_file),
                        "filename": "report.pdf",
                        "description": f"{key[:4]}**{key[4:]}**",
                    },
                    headers={"X-Session-Key": "discord:1"},
                )
                body = await resp.json()
        assert resp.status == 200 and body["delivered"] is True
        caption = transport.send_document.call_args[1]["caption"]
        assert key not in caption.replace("*", "").replace("`", "").replace("_", "")

    @pytest.mark.asyncio
    async def test_a_discord_transport_without_the_verb_is_still_a_skip(self, tmp_path, outbox_file):
        # The channel list is not the authority — the verb is. A transport that
        # predates it keeps the dashboard-link fallback rather than being routed
        # into the extraction upload whose sanitizer renames the attachment.
        from unittest.mock import AsyncMock

        transport = MagicMock(spec_set=["send_message_with_files"])
        transport.send_message_with_files = AsyncMock(return_value="900")
        app = self._app()
        with patch(
            "kiro_crew.dashboard.chat_runner._resolve_mirror_target",
            return_value=(self._link("discord", "555"), transport),
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/channel/upload-file",
                    json={"file_path": str(outbox_file), "filename": "report.txt"},
                    headers={"X-Session-Key": "discord:1"},
                )
                body = await resp.json()
        assert resp.status == 200
        assert body["delivered"] is False
        assert body["skipped"] == "channel_upload_unsupported:discord"
        transport.send_message_with_files.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_channel_without_an_upload_verb_is_a_skip(self, tmp_path, outbox_file):
        transport = MagicMock(spec_set=["send_message"])  # no upload verb
        app = self._app()
        with patch(
            "kiro_crew.dashboard.chat_runner._resolve_mirror_target",
            return_value=(self._link("teams", "t1"), transport),
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/channel/upload-file",
                    json={"file_path": str(outbox_file), "filename": "report.txt"},
                    headers={"X-Session-Key": "teams:1"},
                )
                body = await resp.json()
        assert resp.status == 200
        assert body["delivered"] is False
        assert body["skipped"] == "channel_upload_unsupported:teams"

    @pytest.mark.asyncio
    async def test_path_outside_allowed_roots_is_denied(self, tmp_path):
        from unittest.mock import AsyncMock

        stray = tmp_path / "stray.txt"
        stray.write_text("x", encoding="utf-8")
        outbox = tmp_path / "outbox"
        outbox.mkdir()
        workspace = tmp_path / "ws"
        workspace.mkdir()
        transport = MagicMock(spec_set=["send_document"])
        transport.send_document = AsyncMock()
        app = self._app()
        with patch(
            "kiro_crew.dashboard.chat_runner._resolve_mirror_target",
            return_value=(self._link("telegram"), transport),
        ), patch(
            "kiro_crew.config.loader.outbox_dir", return_value=outbox
        ), patch(
            "kiro_crew.config.loader.workspace_root", return_value=workspace
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/channel/upload-file",
                    json={"file_path": str(stray), "filename": "stray.txt"},
                    headers={"X-Session-Key": "telegram:1"},
                )
                body = await resp.json()
        assert resp.status == 403
        assert body.get("code") == "path_not_allowed"
        transport.send_document.assert_not_called()

    @pytest.mark.asyncio
    async def test_transport_failure_is_a_502_with_a_sanitized_error(self, tmp_path, outbox_file):
        from unittest.mock import AsyncMock

        transport = MagicMock(spec_set=["send_document"])
        transport.send_document = AsyncMock(side_effect=RuntimeError("boom"))
        app = self._app()
        with patch(
            "kiro_crew.dashboard.chat_runner._resolve_mirror_target",
            return_value=(self._link("telegram"), transport),
        ), patch(
            "kiro_crew.config.loader.outbox_dir", return_value=outbox_file.parent
        ), patch(
            "kiro_crew.config.loader.workspace_root", return_value=tmp_path
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/channel/upload-file",
                    json={"file_path": str(outbox_file), "filename": "report.txt"},
                    headers={"X-Session-Key": "telegram:1"},
                )
                body = await resp.json()
        assert resp.status == 502
        assert "boom" in body["error"]

    @pytest.mark.asyncio
    async def test_an_empty_message_id_is_a_502(self, tmp_path, outbox_file):
        from unittest.mock import AsyncMock

        transport = MagicMock(spec_set=["send_document"])
        transport.send_document = AsyncMock(return_value="")
        app = self._app()
        with patch(
            "kiro_crew.dashboard.chat_runner._resolve_mirror_target",
            return_value=(self._link("telegram"), transport),
        ), patch(
            "kiro_crew.config.loader.outbox_dir", return_value=outbox_file.parent
        ), patch(
            "kiro_crew.config.loader.workspace_root", return_value=tmp_path
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/channel/upload-file",
                    json={"file_path": str(outbox_file), "filename": "report.txt"},
                    headers={"X-Session-Key": "telegram:1"},
                )
                body = await resp.json()
        assert resp.status == 502
        assert body["error"] == "channel delivery failed"
