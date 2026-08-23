"""Tests for Slack client abstraction."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from conftest import MockSlackClient
from kiro_crew.slack import client as slack_client
from kiro_crew.slack.client import RealSlackClient


class _RecordingBody:
    """Streams one fixed body in a single chunk, like aiohttp's content reader."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    async def iter_chunked(self, size: int) -> Any:
        if self._body:
            yield self._body


class _RecordingResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self.content = _RecordingBody(body)

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise AssertionError(f"unexpected raise_for_status at {self.status}")

    async def __aenter__(self) -> "_RecordingResponse":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class _RecordingSession:
    """Captures the ClientSession/get kwargs the downloader chose."""

    def __init__(self, *, status: int = 200, body: bytes = b"") -> None:
        self._status = status
        self._body = body
        self.init_kwargs: dict[str, Any] = {}
        self.get_kwargs: dict[str, Any] = {}

    def __call__(self, **kwargs: Any) -> "_RecordingSession":
        """Stand in for the ClientSession CONSTRUCTOR too, so the session-level
        kwargs (the timeout) are observable and not silently dropped."""
        self.init_kwargs = kwargs
        return self

    def get(self, url: str, **kwargs: Any) -> _RecordingResponse:
        self.get_kwargs = kwargs
        return _RecordingResponse(self._status, self._body)

    async def __aenter__(self) -> "_RecordingSession":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class TestMockSlackClient:
    @pytest.mark.asyncio
    async def test_post_returns_ts(self):
        c = MockSlackClient()
        ts = await c.post_message("C1", "hi")
        assert "." in ts

    @pytest.mark.asyncio
    async def test_post_increments_ts(self):
        c = MockSlackClient()
        ts1 = await c.post_message("C1", "a")
        ts2 = await c.post_message("C1", "b")
        assert ts1 != ts2

    @pytest.mark.asyncio
    async def test_actions_recorded(self):
        c = MockSlackClient()
        await c.post_message("C1", "hello", "thread1")
        await c.add_reaction("C1", "ts1", "eyes")
        assert len(c.actions) == 2
        assert c.actions[0][0] == "post"
        assert c.actions[1][0] == "react"
        assert c.actions[1][1]["emoji"] == "eyes"

    @pytest.mark.asyncio
    async def test_update_and_delete(self):
        c = MockSlackClient()
        ts = await c.post_message("C1", "draft")
        await c.update_message("C1", ts, "final")
        await c.delete_message("C1", ts)
        assert c.actions[-2][0] == "update"
        assert c.actions[-1][0] == "delete"

    @pytest.mark.asyncio
    async def test_post_blocks(self):
        c = MockSlackClient()
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "hi"}}]
        ts = await c.post_blocks("C1", blocks, "fallback", "thread1")
        assert "." in ts
        assert c.actions[-1][0] == "blocks"
        assert c.actions[-1][1]["blocks"] == blocks

    @pytest.mark.asyncio
    async def test_upload_records_all_params(self):
        """upload_file still works as the Slack transport for file_send."""
        c = MockSlackClient()
        await c.upload_file("C1", "1234.5678", "/tmp/f.csv", "f.csv", "Title")
        rec = c.actions[-1][1]
        assert rec["file"] == "/tmp/f.csv"
        assert rec["title"] == "Title"
        assert rec["thread_ts"] == "1234.5678"

    @pytest.mark.asyncio
    async def test_upload_thread_ts_empty_by_default(self):
        c = MockSlackClient()
        await c.upload_file("C1", "", "/tmp/f.txt", "f.txt", "f.txt")
        assert c.actions[-1][1]["thread_ts"] == ""


class TestFileDownloadGuards:
    """``download_file`` is the one outbound request that carries the bot token.

    Its URL comes from the inbound event envelope, not from us, so the host and
    the redirect policy are what make attaching a bot-level workspace credential
    safe at all. Mirrors ``discord/client.py::download_attachment``.
    """

    def _client(self) -> RealSlackClient:
        client = RealSlackClient.__new__(RealSlackClient)
        client._web = SimpleNamespace(token="xoxb-secret")
        return client

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "url",
        [
            "https://evil.example.com/x.png",
            "http://files.slack.com/x.png",  # plaintext
            "https://files.slack.com.evil.example/x.png",  # suffix-confusion
            "https://files.slack.com:8443/x.png",  # non-default port
            "https://notslack.com/x.png",
            "file:///etc/passwd",
            "",
        ],
    )
    async def test_a_non_slack_url_is_refused_before_any_request(
        self, url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        opened: list[str] = []

        def _boom(*args: object, **kwargs: object) -> None:
            opened.append("session")
            raise AssertionError("must not open a session for a refused URL")

        monkeypatch.setattr(slack_client.aiohttp, "ClientSession", _boom)
        with pytest.raises(ValueError):
            await self._client().download_file(url, str(tmp_path / "out"))
        assert opened == []

    @pytest.mark.asyncio
    async def test_a_redirect_is_refused_rather_than_followed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """aiohttp replays an explicitly set Authorization header across a
        redirect, so following one would bounce the bot token to the redirect
        target and the host check would have been true only of the first hop."""
        session = _RecordingSession(status=302)
        monkeypatch.setattr(slack_client.aiohttp, "ClientSession", session)
        with pytest.raises(ValueError):
            await self._client().download_file(
                "https://files.slack.com/x.png", str(tmp_path / "out")
            )
        assert session.get_kwargs["allow_redirects"] is False
        assert not (tmp_path / "out").exists()

    @pytest.mark.asyncio
    async def test_an_allowed_url_downloads_with_the_token_and_a_timeout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = _RecordingSession(status=200, body=b"PNGDATA")
        monkeypatch.setattr(slack_client.aiohttp, "ClientSession", session)
        dest = tmp_path / "out"
        await self._client().download_file("https://files.slack.com/x.png", str(dest))
        assert dest.read_bytes() == b"PNGDATA"
        assert session.get_kwargs["headers"]["Authorization"] == "Bearer xoxb-secret"
        # An unbounded (or 5-minute-default) download holds an ingest slot and a
        # temp file for as long as the peer keeps the socket open.
        assert session.init_kwargs["timeout"].total == slack_client._FILE_DOWNLOAD_TIMEOUT_SECS

    @pytest.mark.asyncio
    async def test_a_subdomain_under_the_slack_domain_is_allowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Slack serves files from several hosts under its own domain and varies
        them by install shape, so the check is a suffix match, not one host."""
        session = _RecordingSession(status=200, body=b"X")
        monkeypatch.setattr(slack_client.aiohttp, "ClientSession", session)
        dest = tmp_path / "out"
        await self._client().download_file("https://files-edge.slack.com/x.png", str(dest))
        assert dest.read_bytes() == b"X"
