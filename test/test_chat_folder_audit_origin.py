"""The folder endpoints must audit WHO moved a session — agent or human.

``/api/chat/folders`` and ``/api/chat/slots/{slot}/folder`` are driven by both
the browser and the ``chat_folder_*`` MCP tools. An audit line that labels
every write ``dashboard`` cannot answer "did I file that session, or did the
agent?", which is the whole point of auditing a mutation.

Since #3503 the audit splits interface from identity: ``source`` stays in
SEL's closed interface vocabulary (``dashboard`` / ``mcp``) so operator
queries like ``source == "mcp"`` keep matching every MCP-driven event
uniformly, while ``caller`` carries the internal caller's own declared
identity (``X-Internal-Caller``, validated against
``_KNOWN_INTERNAL_CALLERS``) rather than a value inferred from the secret's
presence — inferring from the secret was correct only while exactly one
internal caller existed, and would silently mislabel writes the moment a
second one is added. An authenticated internal request with a missing or
unrecognized caller header audits as ``caller="unknown-internal"`` with a
warning: loud, attributable, and never trusted verbatim into the log.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_folder_app, _make_state

from kiro_crew.dashboard.chat_folders import _KNOWN_INTERNAL_CALLERS


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
    async def test_caller_header_without_secret_is_still_dashboard(
        self, tmp_path: Any, monkeypatch: Any, recorded: _RecordingSel
    ) -> None:
        """The caller header alone grants nothing — attribution, not authorization.

        A browser (or anything else that never presented ``X-Internal-Secret``)
        cannot promote its own audit label to an internal component's by naming
        one: the secret check runs first, and this request audits exactly like
        any other browser write.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        client = await _client(state)
        try:
            resp = await client.post(
                "/api/chat/folders",
                json={"name": "Sneaky"},
                headers={"X-Internal-Caller": "kirocrew-dashboard"},
            )
            assert resp.status == 201
        finally:
            await client.close()
        event = next(e for e in recorded.events if e["operation"] == "chat.folder_create")
        assert event["source"] == "dashboard"
        assert event["caller"] == "dashboard"

    @pytest.mark.asyncio
    async def test_mcp_create_is_audited_as_declared_caller(
        self, tmp_path: Any, monkeypatch: Any, recorded: _RecordingSel
    ) -> None:
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        client = await _client(state)
        try:
            resp = await client.post(
                "/api/chat/folders",
                json={"name": "Agent"},
                headers={
                    "X-Internal-Secret": "s3cret",
                    "X-Internal-Caller": "kirocrew-dashboard",
                },
            )
            assert resp.status == 201
        finally:
            await client.close()
        event = next(e for e in recorded.events if e["operation"] == "chat.folder_create")
        assert event["source"] == "mcp"
        assert event["caller"] == "kirocrew-dashboard"

    @pytest.mark.asyncio
    async def test_mcp_session_move_is_audited_as_declared_caller(
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
                headers={
                    "X-Internal-Secret": "s3cret",
                    "X-Internal-Caller": "kirocrew-dashboard",
                },
            )
            assert resp.status == 200
        finally:
            await client.close()
        event = next(e for e in recorded.events if e["operation"] == "chat.slot_folder")
        assert event["source"] == "mcp"
        assert event["caller"] == "kirocrew-dashboard"
        assert event["resources"] == "myslot"

    @pytest.mark.asyncio
    async def test_internal_write_without_caller_header_is_unknown_internal(
        self, tmp_path: Any, monkeypatch: Any, recorded: _RecordingSel, caplog: Any
    ) -> None:
        """Secret present, no caller header: audit loudly, never guess.

        This is the exact latent bug of #3503 made visible — an internal
        caller that never declared itself used to inherit the ``mcp`` label
        silently; now it shows up as ``unknown-internal`` plus a warning that
        names the fix (add the caller to the known set, with a test).
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        client = await _client(state)
        try:
            with caplog.at_level(logging.WARNING, logger="kiro_crew.dashboard.chat_folders"):
                resp = await client.post(
                    "/api/chat/folders",
                    json={"name": "Mystery"},
                    headers={"X-Internal-Secret": "s3cret"},
                )
                assert resp.status == 201
        finally:
            await client.close()
        event = next(e for e in recorded.events if e["operation"] == "chat.folder_create")
        assert event["source"] == "mcp"
        assert event["caller"] == "unknown-internal"
        assert any("X-Internal-Caller" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_internal_write_with_unrecognized_caller_is_unknown_internal(
        self, tmp_path: Any, monkeypatch: Any, recorded: _RecordingSel, caplog: Any
    ) -> None:
        """An arbitrary caller string is never trusted verbatim into the audit log."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        client = await _client(state)
        try:
            with caplog.at_level(logging.WARNING, logger="kiro_crew.dashboard.chat_folders"):
                resp = await client.post(
                    "/api/chat/folders",
                    json={"name": "Rogue"},
                    headers={
                        "X-Internal-Secret": "s3cret",
                        "X-Internal-Caller": "not-a-known-component",
                    },
                )
                assert resp.status == 201
        finally:
            await client.close()
        event = next(e for e in recorded.events if e["operation"] == "chat.folder_create")
        assert event["source"] == "mcp"
        assert event["caller"] == "unknown-internal"
        assert any("not-a-known-component" in r.getMessage() for r in caplog.records)


class TestKnownCallerRatchet:
    def test_known_internal_callers_exact_list(self) -> None:
        """RATCHET: adding an internal caller is a conscious, reviewed edit.

        The known set is the entire defense #3503 asks for — a second internal
        caller must fail loudly (``unknown-internal`` + warning) until someone
        adds it HERE, alongside its own audit test. Widening this assertion is
        that conscious edit.
        """
        assert _KNOWN_INTERNAL_CALLERS == frozenset({"kirocrew-dashboard"})

    def test_dashboard_server_name_is_a_known_caller(self) -> None:
        """The cross-module contract pin: the name the dashboard MCP server
        declares (via ``run_mcp_stdio_loop`` → ``X-Internal-Caller``) must be a
        name ``_audit_origin`` recognizes — renaming either side without the
        other silently downgrades every agent folder write to
        ``unknown-internal``."""
        from kiro_crew.mcp_dashboard import SERVER_NAME

        assert SERVER_NAME in _KNOWN_INTERNAL_CALLERS
