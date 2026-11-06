"""Raw config readers merge config.local.json over config.json, as the loader does.

``config.local.json`` is an overlay whose keys win; the loader and Settings report the
merged value. A reader that parses the files itself must use the same merge, or a
setting placed only in the overlay is shown but not used.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


def read_merged_config_files(*paths: Path) -> dict:
    from kiro_crew.config.loader import read_merged_config_files as read

    return read(*paths)


def _write(path: Path, document: object) -> Path:
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


class TestTheSharedRead:
    def test_the_overlay_wins_key_by_key(self, tmp_path: Path) -> None:
        base = _write(tmp_path / "config.json", {"a": {"x": 1, "y": 1}, "b": 1})
        local = _write(tmp_path / "config.local.json", {"a": {"y": 2}})

        assert read_merged_config_files(base, local) == {"a": {"x": 1, "y": 2}, "b": 1}

    def test_a_null_overlay_section_replaces_the_base_section(self, tmp_path: Path) -> None:
        base = _write(tmp_path / "config.json", {"dashboard": {"terminal": {"enabled": False}}})
        local = _write(tmp_path / "config.local.json", {"dashboard": None})

        assert read_merged_config_files(base, local) == {"dashboard": None}

    @pytest.mark.parametrize(
        "bad", ["{not json", "[1, 2]", "", pytest.param("[" * 100_000, id="nested-too-deep")]
    )
    def test_an_unusable_file_contributes_nothing(self, tmp_path: Path, bad: str) -> None:
        base = _write(tmp_path / "config.json", {"b": 1})
        (tmp_path / "config.local.json").write_text(bad, encoding="utf-8")

        assert read_merged_config_files(base, tmp_path / "config.local.json") == {"b": 1}

    def test_absent_files_give_an_empty_document(self, tmp_path: Path) -> None:
        assert read_merged_config_files(tmp_path / "config.json", tmp_path / "nope.json") == {}


def test_the_knowledge_pool_reads_the_overlay(tmp_path: Path, monkeypatch) -> None:
    from kiro_crew.knowledge import llm_pool

    monkeypatch.setattr(llm_pool, "config_dir", lambda: tmp_path)
    _write(tmp_path / "config.json", {"knowledge": {"a": 1}, "agent": {"model": "base"}})
    _write(tmp_path / "config.local.json", {"agent": {"model": "local"}})

    data = llm_pool._read_config()

    assert data["agent"]["model"] == "local"
    assert data["knowledge"] == {"a": 1}


def test_the_terminal_settings_read_the_overlay(tmp_path: Path, monkeypatch) -> None:
    from kiro_crew.dashboard.handlers import terminal_session

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    _write(tmp_path / "config.json", {"dashboard": {"terminal": {"enabled": True}}})
    _write(tmp_path / "config.local.json", {"dashboard": {"terminal": {"enabled": False}}})

    assert terminal_session._read_terminal_config() == {"enabled": False}


def test_the_voice_fallback_reads_the_overlay(tmp_path: Path, monkeypatch) -> None:
    import dataclasses

    from kiro_crew.slack import handler as handler_mod

    # The live voice state is process-wide: every field is restored by monkeypatch,
    # patched in place because the handler and its owners hold the same object.
    for field in dataclasses.fields(handler_mod._vc):
        monkeypatch.setattr(handler_mod._vc, field.name, getattr(handler_mod._vc, field.name))
    base = _write(tmp_path / "config.json", {"voice_reply": {"enabled": False}})
    local = _write(tmp_path / "config.local.json", {"voice_reply": {"auto_speak": True}})
    monkeypatch.setattr(handler_mod, "config_path", lambda: base)
    monkeypatch.setattr(handler_mod, "config_local_path", lambda: local)

    handler_mod.load_voice_reply_config(None)

    assert handler_mod._vc.auto_speak is True
