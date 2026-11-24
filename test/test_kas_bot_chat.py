"""The KAS projection steps for a Bot's own chat (``acp.harness.kas_bot_chat``)."""

from __future__ import annotations

from types import SimpleNamespace

import kiro_crew.bot_chat_app_tools as tools
from kiro_crew.acp.harness.kas_bot_chat import bind_bot_chat_snapshot, settle_bot_chat_projection

KEY = "chat-bot-1"


def _wire(monkeypatch, *, keep=None, voided=()):
    recorded: list = []

    def _filter(key, servers, name_transform=None):
        return dict(servers) if keep is None else {a: servers[a] for a in keep}

    def _record(key, names, spelled=str):
        recorded.append((key, list(names), [spelled(n) for n in names]))
        return set(voided)

    monkeypatch.setattr(tools, "check_and_filter_projection", _filter)
    monkeypatch.setattr(tools, "record_mounted", _record)
    return recorded


def _agents():
    own = {"name": "conductor", "mcpServers": {"kirocrew-core": {}, "atlas:team": {}}}
    return [own, {"name": "other"}]


def test_an_unchanged_projection_keeps_the_agents_and_records_what_they_declare(monkeypatch):
    recorded = _wire(monkeypatch)
    agents = _agents()

    out = settle_bot_chat_projection(KEY, agents)

    assert out is agents
    assert recorded and recorded[0][0] == KEY
    assert recorded[0][2] == recorded[0][1]  # names already as offered stay as they are


def test_a_stripped_entry_is_dropped_from_the_first_agent_only(monkeypatch):
    _wire(monkeypatch, keep=["kirocrew-core"])
    agents = _agents()

    out = settle_bot_chat_projection(KEY, agents)

    assert out[0]["mcpServers"] == {"kirocrew-core": {}}
    assert out[1] is agents[1]
    assert agents[0]["mcpServers"] == {"kirocrew-core": {}, "atlas:team": {}}


def test_a_voided_app_loses_its_servers_but_platform_servers_stay(monkeypatch):
    _wire(monkeypatch, voided={"atlas"})

    out = settle_bot_chat_projection(KEY, _agents())

    assert out[0]["mcpServers"] == {"kirocrew-core": {}}


def test_no_agents_records_nothing_mounted_and_returns_no_agents(monkeypatch):
    recorded = _wire(monkeypatch, voided={"atlas"})

    assert settle_bot_chat_projection(KEY, []) == []
    assert recorded[0][1] == []


def test_a_failing_bind_is_logged_and_does_not_raise(monkeypatch):
    def _boom(*_a):
        raise RuntimeError("bind failed")

    monkeypatch.setattr(tools, "bind_snapshot_to_native_sid", _boom)

    bind_bot_chat_snapshot(SimpleNamespace(_session_key=KEY, session_id="sid-1"))
