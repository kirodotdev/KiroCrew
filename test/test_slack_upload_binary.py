"""Tests for binary file upload via api_slack_upload_file endpoint."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers import api_slack_upload_file


def _make_app(slack_client=None) -> web.Application:
    app = web.Application()
    state = MagicMock()
    state.slack_client = slack_client
    app["state"] = state
    app.router.add_post("/api/slack/upload-file", api_slack_upload_file)
    return app


@pytest.fixture
def mock_sel():
    with patch("kiro_crew.dashboard.handlers.files._sel") as m:
        instance = MagicMock()
        m.return_value = instance
        yield instance


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    with patch("kiro_crew.config.loader.workspace_root", return_value=ws), \
         patch("kiro_crew.config.loader.outbox_dir", return_value=tmp_path / "outbox"):
        (tmp_path / "outbox").mkdir()
        yield ws


class TestSlackUploadBinary:
    @pytest.mark.asyncio
    async def test_pdf_upload_accepted(self, workspace, mock_sel):
        """PDF file (binary, in BINARY_MIME_ALLOWLIST) should be uploaded successfully."""
        pdf = workspace / "report.pdf"
        pdf.write_bytes(b"%PDF-1.4\n" + b"\x00\xff" * 50)

        slack = MagicMock()
        slack.upload_file = AsyncMock()

        with patch("kiro_crew.dashboard.handlers.files.is_tracked_channel", return_value=True):
            async with TestClient(TestServer(_make_app(slack_client=slack))) as client:
                resp = await client.post("/api/slack/upload-file", json={
                    "file_path": str(pdf),
                    "filename": "report.pdf",
                    "thread_ts": "1234567890.123456",
                    "channel": "C0TEST12345",
                })
                assert resp.status == 200, f"Expected 200, got {resp.status}: {await resp.text()}"
                slack.upload_file.assert_called_once()

    @pytest.mark.asyncio
    async def test_exe_binary_rejected(self, workspace, mock_sel):
        """EXE file (binary, NOT in BINARY_MIME_ALLOWLIST) should be rejected."""
        exe = workspace / "malware.exe"
        exe.write_bytes(b"\x4d\x5a\x90\x00" + b"\xff" * 50)

        slack = MagicMock()
        slack.upload_file = AsyncMock()

        async with TestClient(TestServer(_make_app(slack_client=slack))) as client:
            resp = await client.post("/api/slack/upload-file", json={
                "file_path": str(exe),
                "filename": "malware.exe",
                "thread_ts": "1234567890.123456",
            })
            assert resp.status == 400
            data = await resp.json()
            assert "not allowed" in data["error"].lower() or "not supported" in data["error"].lower()
            slack.upload_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_text_with_secrets_still_rejected(self, workspace, mock_sel):
        """Text file containing credentials should still be rejected (redact catches it)."""
        txt = workspace / "secrets.txt"
        txt.write_text("aws_secret_access_key=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")

        slack = MagicMock()
        slack.upload_file = AsyncMock()

        async with TestClient(TestServer(_make_app(slack_client=slack))) as client:
            resp = await client.post("/api/slack/upload-file", json={
                "file_path": str(txt),
                "filename": "secrets.txt",
                "thread_ts": "1234567890.123456",
            })
            assert resp.status == 400
            slack.upload_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_binary_with_embedded_credentials_rejected(self, workspace, mock_sel):
        """Binary file containing embedded credential patterns should be rejected."""
        pdf = workspace / "sneaky.pdf"
        # PDF with embedded AWS key pattern in binary content
        pdf.write_bytes(
            b"%PDF-1.4\n\x00\xff"
            b"AKIAIOSFODNN7EXAMPLE"
            b"\x00\xff" * 20
        )

        slack = MagicMock()
        slack.upload_file = AsyncMock()

        with patch("kiro_crew.dashboard.handlers.files.is_tracked_channel", return_value=True):
            async with TestClient(TestServer(_make_app(slack_client=slack))) as client:
                resp = await client.post("/api/slack/upload-file", json={
                    "file_path": str(pdf),
                    "filename": "sneaky.pdf",
                    "thread_ts": "1234567890.123456",
                    "channel": "C0TEST12345",
                })
                assert resp.status == 400
                data = await resp.json()
                assert "credential" in data["error"].lower()
                slack.upload_file.assert_not_called()


class TestFileSendErrorRedaction:
    """A Slack upload failure is redacted before it reaches the response body or
    the ``file_send`` audit record."""

    @pytest.mark.asyncio
    async def test_upload_failure_is_redacted_in_body_and_audit(self, workspace, mock_sel):
        pdf = workspace / "report.pdf"
        pdf.write_bytes(b"%PDF-1.4\n" + b"\x00\xff" * 50)
        token = "ghp_" + "a" * 36

        slack = MagicMock()
        slack.upload_file = AsyncMock(side_effect=RuntimeError("upload failed: Bearer " + token))

        with patch("kiro_crew.dashboard.handlers.files.is_tracked_channel", return_value=True):
            async with TestClient(TestServer(_make_app(slack_client=slack))) as client:
                resp = await client.post(
                    "/api/slack/upload-file",
                    json={
                        "file_path": str(pdf),
                        "filename": "report.pdf",
                        "thread_ts": "1234567890.123456",
                        "channel": "C0TEST12345",
                    },
                )
                assert resp.status == 500
                body_error = (await resp.json())["error"]
        assert "upload failed" in body_error
        assert token not in body_error
        audited = [
            c.kwargs
            for c in mock_sel.log_tool_invocation.call_args_list
            if c.kwargs.get("outcome") == "error"
        ]
        assert [a["error"] for a in audited] == [body_error]

    def test_helpers_are_imported(self):
        from kiro_crew.dashboard.handlers import files as files_mod

        token = "ghp_" + "a" * 36
        redacted, _count = files_mod.redact_credentials("Bearer " + token)
        assert token not in redacted
        assert callable(files_mod.redact_exfiltration_urls)
