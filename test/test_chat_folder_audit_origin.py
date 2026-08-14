"""The folder endpoints must audit WHO moved a session — agent or human.

``/api/chat/folders`` and ``/api/chat/slots/{slot}/folder`` are now driven by
both the browser and the ``chat_folder_*`` MCP tools. An audit line that labels
every write ``dashboard`` cannot answer "did I file that session, or did the
agent?", which is the whole point of auditing a mutation.
"""

from __future__ import annotations

from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_folder_app, _make_state


class _RecordingSel:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def log_api_access(self, **kw: Any) -> None:
        self.events.append(kw)

    def __getattr__(self, _name: str) -> Any:  # pragma: no cover - unused legs
        return lambda *a, **k: None


@pytest.fixture
def recorded(monkeypatch: Any) -> _RecordingSel:
    rec = _RecordingSel()
    monkeypatch.setattr("kiro_crew.dashboard.chat_folders.sel", lambda: rec)
    return rec


async def _client(state: Any) -> TestClient:
    client = TestClient(TestServer(_make_folder_app(state)))
    await client.start_server()
    return client


class TestFolderAuditOrigin:
    @pytest.mark.asyncio
    async def test_browser_create_is_audited_as_dashboard(
        self, tmp_path: Any, monkeypatch: Any, recorded: _RecordingSel
    ) -> None:
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        client = await _client(state)
        try:
            resp = await client.post("/api/chat/folders", json={"name": "Browser"})
            assert resp.status == 201
        finally:
            await client.close()
        event = next(e for e in recorded.events if e["operation"] == "chat.folder_create")
        assert event["source"] == "dashboard"
        assert event["caller"] == "dashboard"

    @pytest.mark.asyncio
    async def test_mcp_create_is_audited_as_mcp(
        self, tmp_path: Any, monkeypatch: Any, recorded: _RecordingSel
    ) -> None:
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        client = await _client(state)
        try:
            resp = await client.post(
                "/api/chat/folders",
                json={"name": "Agent"},
                headers={"X-Internal-Secret": "s3cret"},
            )
            assert resp.status == 201
        finally:
            await client.close()
        event = next(e for e in recorded.events if e["operation"] == "chat.folder_create")
        assert event["source"] == "mcp"
        assert event["caller"] == "mcp"

    @pytest.mark.asyncio
    async def test_mcp_session_move_is_audited_as_mcp(
        self, tmp_path: Any, monkeypatch: Any, recorded: _RecordingSel
    ) -> None:
        """The mutation Raymond most needs attributed: who re-filed the session."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("myslot")
        slot.append("user", "hello")
        slot.drain()
        state._folders = [
            {"id": "f1", "name": "Test", "order": 0, "collapsed": False, "parent_id": ""}
        ]
        client = await _client(state)
        try:
            resp = await client.patch(
                "/api/chat/slots/myslot/folder",
                json={"folder_id": "f1"},
                headers={"X-Internal-Secret": "s3cret"},
            )
            assert resp.status == 200
        finally:
            await client.close()
        event = next(e for e in recorded.events if e["operation"] == "chat.slot_folder")
        assert event["source"] == "mcp"
        assert event["caller"] == "mcp"
        assert event["resources"] == "myslot"
