"""Three apps/ JSON readers ride out a Windows sharing violation.

On Windows a read raises ``PermissionError`` (``WinError 32``) while a concurrent
tmp-file-plus-rename writer holds the file open. Each reader below would fold that
into "no data" (a key missing from ``list_keys``, an agent config read as absent, an
app enabled with no crons) unless the read retries; each reads through
``read_bytes_with_retry``, which retries that window, while a genuinely absent or
damaged file still answers
``None`` / ``[]``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from windows_sim import read_sharing_violation

from kiro_crew import atomic_write as aw
from kiro_crew import platform_compat
from kiro_crew.apps import bridges_agents, bridges_crons
from kiro_crew.apps.app_storage import AppStorage


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(aw, "_REPLACE_BACKOFF_SECONDS", 0)


def test_digest_key_read_returns_the_recorded_key(windows, tmp_path: Path) -> None:
    path = tmp_path / "digest.json"
    path.write_text(json.dumps({"key": "a-real-key", "value": 1}), encoding="utf-8")
    storage = AppStorage("test-app", tmp_path / "store")
    with read_sharing_violation(match="digest.json", times=2) as sim:
        assert storage._read_digest_key(path) == "a-real-key"
    assert sim["n"] == 3


def test_digest_key_read_still_skips_a_damaged_file(windows, tmp_path: Path) -> None:
    path = tmp_path / "digest.json"
    path.write_bytes(b"\xff\xfe not json")
    storage = AppStorage("test-app", tmp_path / "store")
    assert storage._read_digest_key(path) is None


def test_agent_config_read_returns_the_on_disk_object(windows, tmp_path: Path) -> None:
    path = tmp_path / "app--agent.json"
    path.write_text(json.dumps({"name": "app--agent", "model": "m"}), encoding="utf-8")
    with read_sharing_violation(match="app--agent.json", times=2) as sim:
        assert bridges_agents._read_agent_config(path) == {"name": "app--agent", "model": "m"}
    assert sim["n"] == 3


def test_agent_config_read_still_answers_none_when_absent(windows, tmp_path: Path) -> None:
    assert bridges_agents._read_agent_config(tmp_path / "missing.json") is None


def test_cron_defs_read_returns_the_persisted_list(windows, tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "app-crons.json"
    path.write_text(json.dumps([{"name": "nightly", "schedule": "0 1 * * *"}]), encoding="utf-8")
    monkeypatch.setattr(bridges_crons, "_app_crons_path", lambda _name: path)
    with read_sharing_violation(match="app-crons.json", times=2) as sim:
        defs = bridges_crons.load_app_cron_defs("demo")
    assert [d.get("name") for d in defs] == ["nightly"]
    assert sim["n"] == 3


def test_cron_defs_read_still_answers_empty_for_a_damaged_file(
    windows, tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "app-crons.json"
    path.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(bridges_crons, "_app_crons_path", lambda _name: path)
    assert bridges_crons.load_app_cron_defs("demo") == []
