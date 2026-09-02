"""Focused coverage for the shared hardened agent name-to-model scan."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from conftest import requires_symlinks
from kiro_crew.agent_discovery import agent_model_map


def _scan(agents_dir):
    return agent_model_map(
        agents_dir=agents_dir,
        operation="test_agent_model_map",
        source="unknown",
    )


def test_maps_declared_name_and_stem_with_model_coercion(tmp_path):
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "bot-file.json").write_text(
        json.dumps({"name": "bot", "model": "opus-5"}), encoding="utf-8"
    )
    (agents_dir / "foreign.json").write_text(
        json.dumps({"name": {"id": "foreign"}, "model": {"id": "model"}}),
        encoding="utf-8",
    )
    (agents_dir / "model-less.json").write_text(json.dumps({"name": "inherits"}), encoding="utf-8")

    result = _scan(agents_dir)

    assert result == {
        "bot": "opus-5",
        "bot-file": "opus-5",
        "foreign": "",
        "inherits": "",
        "model-less": "",
    }


def test_refused_specs_are_skipped_and_scan_continues(tmp_path, monkeypatch):
    from kiro_crew import hooks

    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    monkeypatch.setattr(hooks, "MAX_FILE_BYTES", 96)
    (agents_dir / "good.json").write_text(
        json.dumps({"name": "good", "model": "m1"}), encoding="utf-8"
    )
    (agents_dir / "large.json").write_text(
        json.dumps({"name": "large", "model": "leaked", "pad": "x" * 512}),
        encoding="utf-8",
    )
    (agents_dir / "array.json").write_text("[]", encoding="utf-8")
    (agents_dir / "binary.json").write_bytes(b"\xff\xfe")
    (agents_dir / "._good.json").write_text("{}", encoding="utf-8")

    assert _scan(agents_dir) == {"good": "m1"}


def test_all_refused_specs_emit_one_systematic_warning(tmp_path, caplog):
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "array.json").write_text("[]", encoding="utf-8")
    (agents_dir / "broken.json").write_text("{", encoding="utf-8")

    assert _scan(agents_dir) == {}

    warnings = [
        record
        for record in caplog.records
        if "all were unreadable or rejected" in record.getMessage()
    ]
    assert len(warnings) == 1


@requires_symlinks
def test_sensitive_denial_preserves_caller_attribution(tmp_path, monkeypatch):
    from kiro_crew import agent_discovery

    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    target = tmp_path / "sensitive.json"
    target.write_text(json.dumps({"name": "secret", "model": "leaked"}), encoding="utf-8")
    (agents_dir / "evil.json").symlink_to(target)
    security_log = MagicMock()
    monkeypatch.setattr(
        agent_discovery,
        "is_sensitive_path",
        lambda path: str(target) in str(path),
    )
    monkeypatch.setattr(agent_discovery, "_sel", lambda: security_log)

    result = agent_model_map(
        agents_dir=agents_dir,
        operation="resolve_agent_model",
        source="unknown",
    )

    assert result == {}
    security_log.log_api_access.assert_called_once_with(
        caller="agent_discovery",
        operation="resolve_agent_model",
        outcome="denied",
        source="unknown",
        resources=str(target.resolve()),
        error="sensitive path rejected",
    )


def test_chat_restore_supplies_its_directory_and_attribution(tmp_path, monkeypatch):
    from kiro_crew.dashboard import chat_persistence

    seen = {}

    def _map(**kwargs):
        seen.update(kwargs)
        return {"bot": "m1"}

    monkeypatch.setattr(chat_persistence, "kiro_agents_dir_path", lambda: tmp_path)
    monkeypatch.setattr(chat_persistence, "agent_model_map", _map)

    assert chat_persistence._build_kiro_model_map() == {"bot": "m1"}
    assert seen == {
        "agents_dir": tmp_path,
        "operation": "chat_persistence",
        "source": "unknown",
    }


def test_chat_restore_keeps_a_model_less_spec_unpinned(tmp_path, monkeypatch):
    from kiro_crew.dashboard import chat_persistence

    spec = tmp_path / "bot-file.json"
    spec.write_text(json.dumps({"name": "bot"}), encoding="utf-8")
    monkeypatch.setattr(chat_persistence, "kiro_agents_dir_path", lambda: tmp_path)

    model_map = chat_persistence._build_kiro_model_map()

    # Both restore lookup spellings stay falsy, so the downstream slot keeps
    # inheriting its crew/global model instead of persisting an "auto" override.
    assert model_map == {"bot": "", "bot-file": ""}


def test_session_resolver_stops_at_first_match_and_preserves_glob_order(tmp_path, monkeypatch):
    from kiro_crew import session

    first = tmp_path / "z-first.json"
    first.write_text(json.dumps({"name": "bot", "model": "m1"}), encoding="utf-8")
    later = tmp_path / "a-later.json"
    later.write_text(json.dumps({"name": "bot", "model": "m2"}), encoding="utf-8")
    real_reader = session._read_agent_spec
    reads = []

    def _reader(path, **attribution):
        reads.append((path, attribution))
        return real_reader(path, **attribution)

    monkeypatch.setattr(session, "kiro_agents_dir_path", lambda: tmp_path)
    monkeypatch.setattr(session, "_read_agent_spec", _reader)
    monkeypatch.setattr(type(tmp_path), "glob", lambda self, pattern: iter((first, later)))
    session.SessionManager._agent_model_cache = {}

    assert session.SessionManager._resolve_agent_model("bot") == "m1"
    assert reads == [
        (
            first,
            {"operation": "resolve_agent_model", "source": "unknown"},
        )
    ]


def test_chat_restore_degrades_an_unexpected_scan_failure(monkeypatch):
    from kiro_crew.dashboard import chat_persistence

    monkeypatch.setattr(
        chat_persistence,
        "agent_model_map",
        MagicMock(side_effect=RuntimeError("scan failed")),
    )

    assert chat_persistence._build_kiro_model_map() == {}
