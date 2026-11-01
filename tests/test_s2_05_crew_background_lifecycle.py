"""S2-05: Crew background-model handoff at session boundaries.

A background role is established only by Crew's Saved Agent projection, never
inferred from a session id. Keep Crew's session lifetime and scheduler owned by
Crew; BYO is responsible only for the model it actually starts.
"""
from __future__ import annotations

import json
from io import StringIO
from pathlib import Path

from kiro_byo.acp_server import AcpServer, Session
from kiro_byo.agent_projection import AgentProjection
from kiro_byo.protocol import type_selection_id
from test_mode_type_semantics import config_with_models


class EmptyMcp:
    servers = {}
    bindings = {}
    startup_durations_ms = {}
    startup_failures = {}
    catalog_cache_hits = set()

    def start_background(self):
        pass

    def close(self):
        pass

    def signature(self):
        return ""


def _frames(out):
    return [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]


def _crew_params(cwd: Path, *, role="kirocrew-lite", model="auto"):
    return {
        "cwd": str(cwd), "mcpServers": [],
        "_meta": {"kiro": {
            "modelId": model,
            "modeId": role,
            "customAgents": [{"id": role, "prompt": "background worker", "tools": []}],
        }},
    }


def _server(monkeypatch, tmp_path):
    monkeypatch.setenv("KIRO_BYO_HOME", str(tmp_path / "byo-home"))
    monkeypatch.setattr(AcpServer, "_config", staticmethod(config_with_models))
    monkeypatch.setattr(AcpServer, "_mcp_from_params", staticmethod(lambda params: EmptyMcp()))
    out = StringIO()
    return AcpServer(stdin=StringIO(), stdout=out), out


def test_new_background_auto_uses_one_concrete_model(monkeypatch, tmp_path):
    server, out = _server(monkeypatch, tmp_path)
    try:
        server._handle_session_new_request(1, _crew_params(tmp_path))
        frame = next(f for f in _frames(out) if f.get("id") == 1)
        assert "result" in frame, frame
        sid = frame["result"]["sessionId"]
        session = server._get_session(sid)
        assert session.agent.agent_id == "kirocrew-lite"
        assert session.selection_kind == "concrete"
        assert session.selection_source == "background_auto"
        assert session.selection_requested == "auto"
        assert session.model in {m for m, _ in config_with_models().public_models}
        assert not session.model.startswith("kiro-byo-type:")
        assert not any(row["modelId"].startswith("kiro-byo-type:") for row in frame["result"]["models"]["availableModels"])
    finally:
        server._executor.shutdown(wait=True)


def test_background_explicit_model_is_host_authoritative(monkeypatch, tmp_path):
    server, out = _server(monkeypatch, tmp_path)
    try:
        server._handle_session_new_request(2, _crew_params(tmp_path, model="DeepSeek-V4-Pro"))
        frame = next(f for f in _frames(out) if f.get("id") == 2)
        sid = frame["result"]["sessionId"]
        session = server._get_session(sid)
        assert session.model == "DeepSeek-V4-Pro"
        assert session.selection_source == "host_session_new"
    finally:
        server._executor.shutdown(wait=True)


def test_regular_chat_auto_keeps_tiga_type(monkeypatch, tmp_path):
    server, out = _server(monkeypatch, tmp_path)
    try:
        params = {"cwd": str(tmp_path), "mcpServers": [], "_meta": {"kiro": {"modelId": "auto"}}}
        server._handle_session_new_request(3, params)
        frame = next(f for f in _frames(out) if f.get("id") == 3)
        sid = frame["result"]["sessionId"]
        session = server._get_session(sid)
        assert session.selection_kind == "type"
        assert session.model == type_selection_id("tiga", "multi")
    finally:
        server._executor.shutdown(wait=True)


def test_restore_legacy_background_type_is_normalized(monkeypatch, tmp_path):
    server, _ = _server(monkeypatch, tmp_path)
    try:
        session = Session(
            "saved-bg", str(tmp_path), type_selection_id("tiga", "multi"),
            selection_kind="type", agent=AgentProjection(agent_id="kirocrew-lite", tools_declared=True),
        )
        server._persist_session(session)
        restored, _ = server._restore_session(
            "saved-bg", {"cwd": str(tmp_path), "mcpServers": [],
                "_meta": {"kiro": {"modeId": "kirocrew-lite", "customAgents": [
                    {"id": "kirocrew-lite", "prompt": "background", "tools": []}]}}},
            replay=False,
        )
        assert restored.selection_kind == "concrete"
        assert restored.selection_source == "background_auto_restore"
        assert restored.model in {m for m, _ in config_with_models().public_models}
    finally:
        server._executor.shutdown(wait=True)


def test_restore_concrete_background_choice_not_overwritten(monkeypatch, tmp_path):
    server, _ = _server(monkeypatch, tmp_path)
    try:
        session = Session(
            "saved-concrete", str(tmp_path), "DeepSeek-V4-Pro",
            selection_kind="concrete", agent=AgentProjection(agent_id="kirocrew-lite", tools_declared=True),
        )
        server._persist_session(session)
        restored, _ = server._restore_session(
            "saved-concrete", {"cwd": str(tmp_path), "mcpServers": [],
                "_meta": {"kiro": {"modeId": "kirocrew-lite", "customAgents": [
                    {"id": "kirocrew-lite", "prompt": "background", "tools": []}]}}},
            replay=False,
        )
        assert restored.model == "DeepSeek-V4-Pro"
        assert restored.selection_source != "background_auto_restore"
    finally:
        server._executor.shutdown(wait=True)
