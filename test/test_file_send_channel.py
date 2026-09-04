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
        """Create state with a sessions mock that returns slack link data.

        The slot is explicitly UNRESTRICTED. These are all ordinary
        (non-incognito) dashboard sessions, and the leg's restricted-session
        ceiling reads the live slot for a ``dashboard:`` key — an auto-attribute
        mock answers that with a truthy stand-in and would deny the upload for a
        reason none of these cases is about.
        """
        state = MagicMock()
        state.slack_client = slack
        state.get_slot.return_value.is_restricted = False
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
        # `spec_set` names that verb alone: reaching for any other upload verb
        # raises here instead of being caught by an after-the-fact assertion.
        from unittest.mock import AsyncMock

        transport = MagicMock(spec_set=["send_document"])
        transport.send_document = AsyncMock(return_value="900")
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
        # The channel list is not the authority — the verb is. A Discord
        # transport that predates `send_document` keeps the dashboard-link
        # fallback rather than failing the request on a missing attribute.
        transport = MagicMock(spec_set=["send_message"])
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
        transport.send_message.assert_not_called()

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


class TestSlackUploadAuthorizationRungs:
    """The two ceilings the Slack leg shares with the channel leg (issue #7290).

    Slack is deliberately absent from ``channel_transports``, so it reaches
    neither the send ladder's ``channels`` governance vet nor the ceiling the
    ladder's caller applies. Both are direct calls in the oracle. Pinned here:
    that they are reached, that a denial is a REFUSAL the caller sees rather than
    a silent skip, that they run before any destination work, and that a caller
    with no session of its own is not muted by them.
    """

    @staticmethod
    def _state(slack, *, restricted=False):
        state = MagicMock()
        state.slack_client = slack
        state.get_slot.return_value.is_restricted = restricted
        return state

    @staticmethod
    def _decision(permitted):
        return MagicMock(permitted=permitted, rule="", layer="", reason="")

    async def _post(self, app, payload, *, headers=None):
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/slack/upload-file", json=payload, headers=headers)
            return resp, await resp.json()

    @pytest.mark.asyncio
    async def test_a_restricted_session_is_denied_the_slack_upload(
        self, tmp_path, outbox_file
    ):
        # An incognito/temporary session refuses to write a transcript, read
        # memory or save a title; shipping its local file bytes into a Slack
        # channel or DM is the same disclosure with a different destination.
        slack = MagicMock()
        slack.upload_file = AsyncMock()
        app = _make_app(slack, tmp_path, state=self._state(slack, restricted=True))

        with patch(
            "kiro_crew.config.loader.outbox_dir", return_value=outbox_file.parent
        ), patch(
            "kiro_crew.config.loader.workspace_root", return_value=tmp_path
        ), patch(
            "kiro_crew.dashboard.handlers.files.is_tracked_channel", return_value=True
        ):
            resp, body = await self._post(
                app,
                {
                    "file_path": str(outbox_file),
                    "filename": "report.txt",
                    "channel": "C0TRACKED123",
                },
                headers={"X-Session-Key": "dashboard:chat-1"},
            )

        assert resp.status == 403
        assert body["code"] == "restricted_session"
        slack.upload_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_governance_denied_channels_scope_denies_the_slack_leg(
        self, tmp_path, outbox_file
    ):
        # The vet the channel leg inherits from its send ladder. A profile that
        # denies the ``channels`` scope refused a Telegram upload and allowed a
        # Slack one; one denial now answers for both.
        slack = MagicMock()
        slack.upload_file = AsyncMock()
        app = _make_app(slack, tmp_path, state=self._state(slack))
        sel = MagicMock()

        with patch(
            "kiro_crew.config.loader.outbox_dir", return_value=outbox_file.parent
        ), patch(
            "kiro_crew.config.loader.workspace_root", return_value=tmp_path
        ), patch(
            "kiro_crew.dashboard.handlers.files.is_tracked_channel", return_value=True
        ), patch(
            "kiro_crew.dashboard.handlers.files._sel", return_value=sel
        ), patch(
            "kiro_crew.dashboard.upload_destination.vet_and_audit",
            return_value=self._decision(False),
        ):
            resp, body = await self._post(
                app,
                {
                    "file_path": str(outbox_file),
                    "filename": "report.txt",
                    "channel": "C0TRACKED123",
                },
                headers={"X-Session-Key": "dashboard:chat-1"},
            )

        assert resp.status == 403
        assert body["code"] == "channels_governance_denied"
        slack.upload_file.assert_not_called()
        # The audit lane an operator greps for refused sends, same shape as every
        # other refusal on this leg.
        denied = [
            r for r in (c.kwargs for c in sel.log_tool_invocation.call_args_list)
            if r.get("outcome") == "denied"
        ]
        assert [r["error"] for r in denied] == ["channels_governance_denied"]
        assert denied[0]["downstream_service"] == "slack"

    @pytest.mark.asyncio
    async def test_the_vet_names_the_slack_transport_and_the_channels_scope(
        self, tmp_path, outbox_file
    ):
        # The scope/item pair is the authorization content of the rung: vetting
        # anything but the transport the file actually leaves over would evaluate
        # one channel's rule against another's egress.
        slack = MagicMock()
        slack.upload_file = AsyncMock()
        app = _make_app(slack, tmp_path, state=self._state(slack))

        with patch(
            "kiro_crew.config.loader.outbox_dir", return_value=outbox_file.parent
        ), patch(
            "kiro_crew.config.loader.workspace_root", return_value=tmp_path
        ), patch(
            "kiro_crew.dashboard.handlers.files.is_tracked_channel", return_value=True
        ), patch(
            "kiro_crew.dashboard.upload_destination.vet_and_audit",
            return_value=self._decision(True),
        ) as vet:
            resp, _ = await self._post(
                app,
                {
                    "file_path": str(outbox_file),
                    "filename": "report.txt",
                    "thread_ts": "999.888",
                    "channel": "C0TRACKED123",
                },
                headers={"X-Session-Key": "dashboard:chat-1"},
            )

        assert resp.status == 200
        assert vet.call_args[0] == ("channels", "slack")
        assert vet.call_args[1]["session_key"] == "dashboard:chat-1"
        # Fail-closed: a degraded evaluation on an egress chokepoint must deny.
        assert vet.call_args[1]["fail_closed"] is True

    @pytest.mark.asyncio
    async def test_a_degraded_governance_evaluation_denies(self, tmp_path, outbox_file):
        # Fail-closed at the point the error is caught: an unusable gate answer
        # must not read as permission on the broadest-audience leg.
        slack = MagicMock()
        slack.upload_file = AsyncMock()
        app = _make_app(slack, tmp_path, state=self._state(slack))

        with patch(
            "kiro_crew.config.loader.outbox_dir", return_value=outbox_file.parent
        ), patch(
            "kiro_crew.config.loader.workspace_root", return_value=tmp_path
        ), patch(
            "kiro_crew.dashboard.handlers.files.is_tracked_channel", return_value=True
        ), patch(
            "kiro_crew.dashboard.upload_destination.vet_and_audit",
            side_effect=RuntimeError("profile store unreadable"),
        ):
            resp, body = await self._post(
                app,
                {
                    "file_path": str(outbox_file),
                    "filename": "report.txt",
                    "channel": "C0TRACKED123",
                },
                headers={"X-Session-Key": "dashboard:chat-1"},
            )

        assert resp.status == 403
        assert body["code"] == "channels_governance_denied"
        slack.upload_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_sessionless_owner_dm_caller_is_not_muted(self, tmp_path, outbox_file):
        # The owner-DM fallback serves callers with no session of their own (a
        # cron, the heartbeat, an out-of-band host action). Neither ceiling may
        # mute them on an ungoverned host: governance runs for real here, and the
        # ceiling has no slot and no channel privacy mode to read.
        slack = MagicMock()
        slack.upload_file = AsyncMock()
        slack.open_dm = AsyncMock(return_value="D_OWNER_DM")
        app = _make_app(slack, tmp_path, state=self._state(slack))

        with patch(
            "kiro_crew.config.loader.outbox_dir", return_value=outbox_file.parent
        ), patch(
            "kiro_crew.config.loader.workspace_root", return_value=tmp_path
        ), patch(
            "kiro_crew.config.loader.KiroCrewConfig.load"
        ) as mock_cfg:
            mock_cfg.return_value.load_credentials.return_value = {
                "KIROCREW_OWNER_ID": "U_OWNER"
            }
            resp, body = await self._post(
                app,
                {"file_path": str(outbox_file), "filename": "report.txt", "channel": ""},
            )

        assert resp.status == 200
        assert body.get("ok") is True
        assert slack.upload_file.call_args[0][0] == "D_OWNER_DM"

    @pytest.mark.asyncio
    async def test_a_sessionless_caller_is_vetted_under_the_host_sentinel(
        self, tmp_path, outbox_file
    ):
        # An EMPTY key classifies as ``unknown`` and matches no profile at all, so
        # vetting under it would make host-side governance inert on this leg.
        # ``_host`` is the bind target operators actually attach it to.
        from kiro_crew.platform.governance_profiles import HOST_SESSION_KEY

        slack = MagicMock()
        slack.upload_file = AsyncMock()
        slack.open_dm = AsyncMock(return_value="D_OWNER_DM")
        app = _make_app(slack, tmp_path, state=self._state(slack))

        with patch(
            "kiro_crew.config.loader.outbox_dir", return_value=outbox_file.parent
        ), patch(
            "kiro_crew.config.loader.workspace_root", return_value=tmp_path
        ), patch(
            "kiro_crew.config.loader.KiroCrewConfig.load"
        ) as mock_cfg, patch(
            "kiro_crew.dashboard.upload_destination.vet_and_audit",
            return_value=self._decision(True),
        ) as vet:
            mock_cfg.return_value.load_credentials.return_value = {
                "KIROCREW_OWNER_ID": "U_OWNER"
            }
            resp, _ = await self._post(
                app,
                {"file_path": str(outbox_file), "filename": "report.txt", "channel": ""},
            )

        assert resp.status == 200
        assert vet.call_args[1]["session_key"] == HOST_SESSION_KEY

    @pytest.mark.asyncio
    async def test_both_ceilings_precede_any_destination_work(self, tmp_path, outbox_file):
        # A caller that may not egress here never opens an owner DM and never
        # reads the session map, so a denial leaks nothing about where the file
        # would have gone — the same ordering rule the admission gate follows.
        slack = MagicMock()
        slack.upload_file = AsyncMock()
        slack.open_dm = AsyncMock(return_value="D_OWNER_DM")
        state = self._state(slack)
        state.sessions = MagicMock()
        state.sessions.get_slack_link = MagicMock(return_value=("111.222", "D0SLOTDM01"))
        app = _make_app(slack, tmp_path, state=state)

        with patch(
            "kiro_crew.config.loader.outbox_dir", return_value=outbox_file.parent
        ), patch(
            "kiro_crew.config.loader.workspace_root", return_value=tmp_path
        ), patch(
            "kiro_crew.dashboard.upload_destination.vet_and_audit",
            return_value=self._decision(False),
        ):
            resp, _ = await self._post(
                app,
                {"file_path": str(outbox_file), "filename": "report.txt", "channel": ""},
                headers={"X-Session-Key": "dashboard:chat-1"},
            )

        assert resp.status == 403
        state.sessions.get_slack_link.assert_not_called()
        slack.open_dm.assert_not_called()


class TestDestinationOracleEquivalence:
    """The rungs the destination oracle must keep answering exactly as the two
    inline ladders did (issue #6060).

    The classes above already pin the seven Slack destination OUTCOMES and the
    channel leg's skip-vs-error semantics, and they run unchanged against the
    oracle. What they never asserted is what a fold could silently change while
    leaving those outcomes intact: the SEL record each refusal writes, the
    fail-closed direction when the tracking probe RAISES, and which callers get
    to consult the session map at all. Those are pinned here.
    """

    @staticmethod
    def _state(slack, *, thread_ts=None, channel=None):
        state = MagicMock()
        state.slack_client = slack
        # Unrestricted slot: these cases are about destination resolution, and an
        # auto-attribute mock reads as a restricted session, which the ceiling
        # ahead of resolution would answer first.
        state.get_slot.return_value.is_restricted = False
        sessions = MagicMock()
        sessions.get_slack_link = MagicMock(return_value=(thread_ts, channel))
        state.sessions = sessions
        return state

    @staticmethod
    def _records(sel):
        """The kwargs of every SEL tool-invocation the request wrote."""
        return [c.kwargs for c in sel.log_tool_invocation.call_args_list]

    async def _post_slack(self, app, payload, *, headers=None):
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/slack/upload-file", json=payload, headers=headers)
            return resp, await resp.json()

    @pytest.mark.asyncio
    async def test_an_untracked_channel_refusal_keeps_its_audit_record(
        self, tmp_path, outbox_file
    ):
        """A refused destination writes ONE denied record naming the channel,
        with ``downstream_service`` set — the audit lane an operator greps to
        find refused sends. The client response still does not name it."""
        slack = MagicMock()
        slack.upload_file = AsyncMock()
        app = _make_app(slack, tmp_path)
        sel = MagicMock()

        with patch(
            "kiro_crew.config.loader.outbox_dir", return_value=outbox_file.parent
        ), patch(
            "kiro_crew.config.loader.workspace_root", return_value=tmp_path
        ), patch(
            "kiro_crew.dashboard.handlers.files.is_tracked_channel", return_value=False
        ), patch(
            "kiro_crew.dashboard.handlers.files._sel", return_value=sel
        ):
            resp, body = await self._post_slack(
                app,
                {
                    "file_path": str(outbox_file),
                    "filename": "report.txt",
                    "channel": "C0UNTRACKED9",
                },
            )

        assert resp.status == 403
        assert body["code"] == "channel_not_tracked"
        assert "C0UNTRACKED9" not in body.get("error", "")
        denials = [r for r in self._records(sel) if r.get("outcome") == "denied"]
        assert denials == [
            {
                "session_key": "api",
                "source": "api",
                "tool_name": "file_send",
                "tool_kind": "slack",
                "outcome": "denied",
                "downstream_service": "slack",
                "error": "channel_not_tracked: C0UNTRACKED9",
            }
        ]

    @pytest.mark.asyncio
    async def test_a_skipped_send_carries_no_downstream_service(self, tmp_path, outbox_file):
        """A skip is not a refusal: the shipped skip records carry no
        ``downstream_service``, so an audit reader can tell "nowhere to send" from
        "refused to send there"."""
        slack = MagicMock()
        slack.upload_file = AsyncMock()
        app = _make_app(slack, tmp_path)
        sel = MagicMock()

        with patch(
            "kiro_crew.config.loader.outbox_dir", return_value=outbox_file.parent
        ), patch(
            "kiro_crew.config.loader.workspace_root", return_value=tmp_path
        ), patch(
            "kiro_crew.config.loader.KiroCrewConfig.load"
        ) as mock_cfg, patch(
            "kiro_crew.dashboard.handlers.files._sel", return_value=sel
        ):
            mock_cfg.return_value.load_credentials.return_value = {}
            resp, body = await self._post_slack(
                app,
                {"file_path": str(outbox_file), "filename": "report.txt", "channel": ""},
            )

        assert resp.status == 200
        assert body == {"ok": True, "skipped": "no_channel"}
        skips = [r for r in self._records(sel) if r.get("outcome") == "skipped"]
        assert skips == [
            {
                "session_key": "api",
                "source": "api",
                "tool_name": "file_send",
                "tool_kind": "slack",
                "outcome": "skipped",
                "error": "no_channel",
            }
        ]
        slack.upload_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_raising_tracking_probe_denies_a_named_channel(self, tmp_path, outbox_file):
        """Deny-by-default extends to uncertainty: a tracking check that RAISED
        has not authorized anybody, so the named channel is refused rather than
        treated as tracked."""
        slack = MagicMock()
        slack.upload_file = AsyncMock()
        app = _make_app(slack, tmp_path)

        with patch(
            "kiro_crew.config.loader.outbox_dir", return_value=outbox_file.parent
        ), patch(
            "kiro_crew.config.loader.workspace_root", return_value=tmp_path
        ), patch(
            "kiro_crew.dashboard.handlers.files.is_tracked_channel",
            side_effect=RuntimeError("config unreadable"),
        ):
            resp, body = await self._post_slack(
                app,
                {
                    "file_path": str(outbox_file),
                    "filename": "report.txt",
                    "channel": "C0TRACKED123",
                },
            )

        assert resp.status == 403
        assert "not in tracked channels" in body.get("error", "")
        slack.upload_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_raising_tracking_probe_denies_a_non_dm_session_map_channel(
        self, tmp_path, outbox_file
    ):
        """Same fail-closed direction on the session-map branch, where the
        D-prefix short-circuit does NOT apply: a raising probe refuses the
        channel instead of letting the trusted-link path carry it."""
        slack = MagicMock()
        slack.upload_file = AsyncMock()
        state = self._state(slack, thread_ts="111.222", channel="C0ROGUE999")
        app = _make_app(slack, tmp_path, state=state)

        with patch(
            "kiro_crew.config.loader.outbox_dir", return_value=outbox_file.parent
        ), patch(
            "kiro_crew.config.loader.workspace_root", return_value=tmp_path
        ), patch(
            "kiro_crew.dashboard.handlers.files.is_tracked_channel",
            side_effect=RuntimeError("config unreadable"),
        ):
            resp, body = await self._post_slack(
                app,
                {"file_path": str(outbox_file), "filename": "report.txt", "channel": ""},
                headers={"X-Session-Key": "dashboard:chat-1"},
            )

        assert resp.status == 403
        assert "not authorized" in body.get("error", "")
        slack.upload_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_dm_from_the_session_map_never_consults_the_tracking_probe(
        self, tmp_path, outbox_file
    ):
        """The D-prefix short-circuit is not just an alternative to tracking, it
        is evaluated FIRST: a system-created DM link uploads even when the
        tracking probe would raise."""
        slack = MagicMock()
        slack.upload_file = AsyncMock()
        state = self._state(slack, thread_ts="111.222", channel="D0SLOTDM01")
        app = _make_app(slack, tmp_path, state=state)
        probe = MagicMock(side_effect=RuntimeError("must not be consulted"))

        with patch(
            "kiro_crew.config.loader.outbox_dir", return_value=outbox_file.parent
        ), patch(
            "kiro_crew.config.loader.workspace_root", return_value=tmp_path
        ), patch(
            "kiro_crew.dashboard.handlers.files.is_tracked_channel", new=probe
        ):
            resp, _ = await self._post_slack(
                app,
                {"file_path": str(outbox_file), "filename": "report.txt", "channel": ""},
                headers={"X-Session-Key": "dashboard:chat-1"},
            )

        assert resp.status == 200
        probe.assert_not_called()
        assert slack.upload_file.call_args[0][0] == "D0SLOTDM01"

    @pytest.mark.asyncio
    async def test_a_non_linkable_caller_never_reads_the_session_map(
        self, tmp_path, outbox_file
    ):
        """Only a ``dashboard:`` or channel-native key owns a Slack link. A
        ``cron:``/``subagent:``-style key must not inherit one: the lookup is
        never made, and the send falls back to the owner DM."""
        slack = MagicMock()
        slack.upload_file = AsyncMock()
        slack.open_dm = AsyncMock(return_value="D_OWNER_DM")
        state = self._state(slack, thread_ts="111.222", channel="C0SOMEONE_ELSE")
        app = _make_app(slack, tmp_path, state=state)

        with patch(
            "kiro_crew.config.loader.outbox_dir", return_value=outbox_file.parent
        ), patch(
            "kiro_crew.config.loader.workspace_root", return_value=tmp_path
        ), patch(
            "kiro_crew.config.loader.KiroCrewConfig.load"
        ) as mock_cfg:
            mock_cfg.return_value.load_credentials.return_value = {
                "KIROCREW_OWNER_ID": "U_OWNER"
            }
            resp, _ = await self._post_slack(
                app,
                {"file_path": str(outbox_file), "filename": "report.txt", "channel": ""},
                headers={"X-Session-Key": "cron:nightly"},
            )

        assert resp.status == 200
        state.sessions.get_slack_link.assert_not_called()
        assert slack.upload_file.call_args[0][0] == "D_OWNER_DM"
        assert slack.upload_file.call_args[0][1] == ""

    @pytest.mark.asyncio
    async def test_the_admission_gate_runs_before_any_destination_work(self, tmp_path):
        """Ordering the two legs share: a file that cannot ship is refused before
        the oracle is consulted, so a rejected upload cannot open a DM or read
        the session map as a side effect."""
        slack = MagicMock()
        slack.upload_file = AsyncMock()
        slack.open_dm = AsyncMock(return_value="D_OWNER_DM")
        state = self._state(slack, thread_ts="111.222", channel="D0SLOTDM01")
        app = _make_app(slack, tmp_path, state=state)
        outside = tmp_path / "elsewhere" / "secret.txt"
        outside.parent.mkdir()
        outside.write_text("data", encoding="utf-8")

        with patch(
            "kiro_crew.config.loader.outbox_dir", return_value=tmp_path / "outbox"
        ), patch(
            "kiro_crew.config.loader.workspace_root", return_value=tmp_path / "workspace"
        ):
            resp, body = await self._post_slack(
                app,
                {"file_path": str(outside), "filename": "secret.txt"},
                headers={"X-Session-Key": "dashboard:chat-1"},
            )

        assert resp.status == 403 and body["code"] == "path_not_allowed"
        state.sessions.get_slack_link.assert_not_called()
        slack.open_dm.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_unreadable_credential_store_is_a_skip_not_a_500(
        self, tmp_path, outbox_file
    ):
        """The owner-DM fallback is best-effort: a credential read that RAISES
        leaves the caller with no destination, which is a skip. The request must
        not surface a 500 (nor an exception naming the credential store) just
        because no channel could be resolved."""
        slack = MagicMock()
        slack.upload_file = AsyncMock()
        slack.open_dm = AsyncMock(return_value="D_OWNER_DM")
        app = _make_app(slack, tmp_path)

        with patch(
            "kiro_crew.config.loader.outbox_dir", return_value=outbox_file.parent
        ), patch(
            "kiro_crew.config.loader.workspace_root", return_value=tmp_path
        ), patch(
            "kiro_crew.config.loader.KiroCrewConfig.load",
            side_effect=RuntimeError("credential store unreadable"),
        ), patch(
            # An unreadable config also breaks the governance evaluation this leg
            # runs first, and THAT rung is fail-closed — so without pinning it
            # permitted, its 403 would answer before the owner-DM fallback this
            # case is about ever ran.
            "kiro_crew.dashboard.upload_destination.vet_and_audit",
            return_value=MagicMock(permitted=True, rule="", layer="", reason=""),
        ):
            resp, body = await self._post_slack(
                app,
                {"file_path": str(outbox_file), "filename": "report.txt", "channel": ""},
            )

        assert resp.status == 200
        assert body == {"ok": True, "skipped": "no_channel"}
        slack.open_dm.assert_not_called()
        slack.upload_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_both_legs_resolve_through_the_one_oracle_module(self):
        """The point of the change: neither endpoint carries its own resolver any
        more. Pinned structurally so a future edit that re-inlines a ladder in
        one leg fails here instead of in review."""
        import inspect

        from kiro_crew.dashboard import upload_destination
        from kiro_crew.dashboard.handlers import files

        def _body(func):
            """The function's code, with its docstring dropped — the docstrings
            NAME these rungs to say where they live, and a text scan would flag
            exactly the sentence documenting the move."""
            return inspect.getsource(func).replace(func.__doc__ or "\0", "")

        slack_src = _body(files.api_slack_upload_file)
        channel_src = _body(files.api_channel_upload_file)
        assert "upload_destination.resolve_slack(" in slack_src
        assert "upload_destination.resolve_channel(" in channel_src
        # The rungs themselves live in the oracle, not in either handler.
        for rung in ("get_slack_link(", "open_dm(", "_resolve_mirror_target", "may_send_to("):
            assert rung not in slack_src, f"{rung} is back in the Slack handler"
            assert rung not in channel_src, f"{rung} is back in the channel handler"
        assert "is_tracked_channel(" not in slack_src, "the tracking check is back in the handler"
        oracle_src = inspect.getsource(upload_destination)
        for rung in ("get_slack_link(", "open_dm(", "_resolve_mirror_target", "tracked_probe("):
            assert rung in oracle_src
