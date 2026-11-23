"""An MCP config change a running session cannot load is named in its chat.

``POST /api/mcp/custom`` and ``PUT /api/mcp/custom/{name}`` rewrite the agent spec,
but only kiro-cli (file watcher), KAS (re-sent projection) and Strands (relaunch)
apply that to a running session. Every other session keeps the set it started
with, so its MCP report lists the server in ``restart_to_load`` and its chat gets
one notice row. Nothing is restarted.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner

from kiro_crew.acp.mcp_session_report import McpSessionReport
from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.chat_runner import _ChatSlot

_STDIO = {"command": "npx", "args": ["-y", "@acme/weather-mcp"]}


def _rebuild_that_writes(**kwargs: Any) -> Path:
    """A rebuild that wrote the spec, reported the way the real one does."""
    wrote_out = kwargs.get("_wrote_out")
    if wrote_out is not None:
        wrote_out.append(True)
    return Path("/dev/null")


def _rebuild_that_declines(**_kwargs: Any) -> Path:
    """A rebuild that wrote nothing."""
    return Path("/dev/null")


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """MCP config paths in *tmp_path*, the spec rebuild faked and SEL captured."""
    import kiro_crew.agent as agent_mod
    from kiro_crew.dashboard.handlers import mcp as mcp_mod
    from kiro_crew.dashboard.handlers import mcp_custom

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("KIROCREW_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(mcp_mod, "_KIROCREW_MCP_JSON", tmp_path / "kirocrew.mcp.json")
    monkeypatch.setattr(mcp_mod, "_GLOBAL_MCP_JSON", tmp_path / "kiro.mcp.json")
    monkeypatch.setattr(mcp_mod, "_MCP_LOCK_PATH", tmp_path / "kiro.mcp.lock")
    rebuild = MagicMock(side_effect=_rebuild_that_writes)
    monkeypatch.setattr(agent_mod, "rebuild_agent_config", rebuild)
    monkeypatch.setattr(mcp_custom, "sel", lambda: MagicMock())
    return SimpleNamespace(rebuild=rebuild)


class _Provider:
    """A running session's provider: its report and how it applies an MCP change."""

    def __init__(self, *, hot_reload: bool = False, reinject: bool = False) -> None:
        self.report = McpSessionReport()
        self.report.begin_session([{"name": "kirocrew-core"}])
        self.mcp_config_hot_reload = hot_reload
        self.mcp_config_wire_reinject = reinject
        self.mcp_config_relaunch_reload = False
        self.session_id = "sess-1"
        self.marked = 0

    def mcp_session_report(self) -> McpSessionReport:
        return self.report

    def mark_mcp_config_changed(self) -> None:
        self.marked += 1


def _restart_to_load(provider: _Provider) -> list[str]:
    payload = provider.report.payload()
    assert payload is not None
    return payload["restart_to_load"]


@pytest.fixture
def rows(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Every row appended through ``append_and_surface``."""
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(
        chat_runner,
        "append_and_surface",
        lambda state, slot, role, text, cls="", **kw: seen.append(
            {"slot": slot.key, "role": role, "text": text, "cls": cls, "meta": kw.get("meta")}
        ),
    )
    return seen


def _state(providers: dict[str, _Provider]) -> SimpleNamespace:
    """A dashboard with one chat per entry, each running its session on *providers*' entry."""
    slots = {key: _ChatSlot(key) for key in providers}
    by_session = {chat_runner.effective_session_key(slots[k]): p for k, p in providers.items()}
    return SimpleNamespace(
        owner_id="",
        _slots=slots,
        sessions=SimpleNamespace(
            get_provider=by_session.get,
            active_providers=lambda: list(providers.values()),
            warm_providers=lambda: [],
        ),
        broadcast_ws=lambda *a, **k: None,
    )


async def _client(state: SimpleNamespace) -> TestClient:
    from kiro_crew.dashboard.handlers import mcp_custom as mod

    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/mcp/custom", mod.api_mcp_custom_add)
    app.router.add_put("/api/mcp/custom/{name}", mod.api_mcp_custom_update)
    client = TestClient(TestServer(as_owner(app)))
    await client.start_server()
    return client


@pytest.mark.asyncio
class TestRestartToLoad:
    async def test_an_enabled_add_marks_and_tells_a_session_that_cannot_load_it(
        self, sandbox, rows
    ):
        claude = _Provider()
        client = await _client(_state({"chat-claude": claude}))
        try:
            resp = await client.post(
                "/api/mcp/custom", json={"servers": {"weather": _STDIO}, "enable": True}
            )
            assert resp.status == 200
            assert (await resp.json())["agent_spec_rewritten"] is True
        finally:
            await client.close()
        assert _restart_to_load(claude) == ["weather"]
        payload = claude.report.payload()
        assert payload is not None and payload["reload_pending"] == ["weather"]
        [row] = rows
        assert row["slot"] == "chat-claude"
        assert row["role"] == "notice"
        assert row["cls"] == "msg msg-warn"
        assert row["meta"] == {"kind": "mcp_restart_to_load", "servers": ["weather"]}
        assert row["text"].startswith("⚠ The MCP server weather was added or changed")
        assert row["text"].endswith("Start a new chat to use it.")

    async def test_a_session_that_applies_it_live_is_left_alone(self, sandbox, rows):
        kiro, kas = _Provider(hot_reload=True), _Provider(reinject=True)
        client = await _client(_state({"chat-kiro": kiro, "chat-kas": kas}))
        try:
            await client.post(
                "/api/mcp/custom", json={"servers": {"weather": _STDIO}, "enable": True}
            )
        finally:
            await client.close()
        assert rows == []
        assert _restart_to_load(kiro) == []
        assert _restart_to_load(kas) == []
        assert kas.marked == 1, "KAS is marked to re-send its projection at its next turn"
        assert kiro.marked == 0, "kiro-cli watches its agent file on its own"

    async def test_a_disabled_add_and_an_unwritten_spec_tell_no_one(self, sandbox, rows):
        claude = _Provider()
        client = await _client(_state({"chat-claude": claude}))
        try:
            await client.post("/api/mcp/custom", json={"servers": {"weather": _STDIO}})
            sandbox.rebuild.side_effect = _rebuild_that_declines
            await client.post(
                "/api/mcp/custom", json={"servers": {"notes": _STDIO}, "enable": True}
            )
        finally:
            await client.close()
        assert rows == []
        assert _restart_to_load(claude) == []

    async def test_an_edit_of_an_enabled_server_is_told_once_until_a_new_session(
        self, sandbox, rows
    ):
        codex = _Provider()
        client = await _client(_state({"chat-codex": codex}))
        try:
            await client.post(
                "/api/mcp/custom", json={"servers": {"weather": _STDIO}, "enable": True}
            )
            resp = await client.put("/api/mcp/custom/weather", json={"spec": _STDIO})
            assert resp.status == 200
        finally:
            await client.close()
        assert len(rows) == 1, "a server already listed is not told again"
        assert _restart_to_load(codex) == ["weather"]
        codex.report.begin_session([{"name": "kirocrew-core"}])
        assert _restart_to_load(codex) == []


@pytest.mark.asyncio
async def test_an_unchanged_save_tells_no_one(sandbox, rows: list) -> None:
    codex = _Provider()
    client = await _client(_state({"chat-codex": codex}))
    try:
        await client.post("/api/mcp/custom", json={"servers": {"weather": _STDIO}, "enable": True})
        codex.report.begin_session([{"name": "kirocrew-core"}])
        rows.clear()
        assert (await client.put("/api/mcp/custom/weather", json={"spec": _STDIO})).status == 200
        assert rows == [], "a save that changes nothing is not news"
        changed = {**_STDIO, "args": ["-y", "@acme/weather-mcp@2"]}
        assert (await client.put("/api/mcp/custom/weather", json={"spec": changed})).status == 200
    finally:
        await client.close()
    assert [row["meta"]["servers"] for row in rows] == [["weather"]]


def test_several_servers_share_one_row(rows: list) -> None:
    pi = _Provider()
    state: Any = _state({"chat-pi": pi})
    marked = chat_runner.notice_mcp_restart_to_load(state, ["a", "b"])
    assert marked == 1
    [row] = rows
    assert row["text"].startswith("⚠ The MCP servers a, b were added or changed")
    assert row["meta"]["servers"] == ["a", "b"]
    assert pi.report.problem_summary() == "", "a pending load is not a server problem"


def test_the_pending_list_is_bounded_and_counts_what_it_leaves_out() -> None:
    from kiro_crew.acp.mcp_session_report import STATUS_CAP

    report = _Provider().report
    names = [f"s{n}" for n in range(STATUS_CAP + 3)]
    admitted = report.mark_restart_to_load(names)
    assert len(admitted) == STATUS_CAP
    assert report.mark_restart_to_load(["late"]) == ()
    payload = report.payload()
    assert payload is not None
    assert len(payload["reload_pending"]) == STATUS_CAP
    assert payload["states_omitted"] == 4, "overflow adds up across writes"


def test_an_overflow_only_write_still_republishes_the_report(rows: list) -> None:
    from kiro_crew.acp.mcp_session_report import STATUS_CAP

    pi = _Provider()
    state: Any = _state({"chat-pi": pi})
    chat_runner.notice_mcp_restart_to_load(state, [f"s{n}" for n in range(STATUS_CAP)])
    assert chat_runner.notice_mcp_restart_to_load(state, ["late"]) == 0
    assert len(rows) == 1, "an overflow-only write posts no row"
    published = state._slots["chat-pi"].mcp_report_payload()
    assert published["states_omitted"] == 1
