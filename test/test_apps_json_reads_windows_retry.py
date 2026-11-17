"""apps/ JSON readers ride out a Windows sharing violation.

On Windows a read raises ``PermissionError`` (``WinError 32``) while a concurrent
tmp-file-plus-rename writer holds the file open. Each reader below would fold that
into "no data" (a key missing from ``list_keys``, a stored value ``get`` reads as
absent, an agent config read as absent, an app enabled with no crons) unless the
read retries; each reads through ``read_bytes_with_retry``, which retries that
window, while a genuinely absent or damaged file still answers ``None`` / ``[]``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from windows_sim import read_sharing_violation

from kiro_crew import atomic_write as aw
from kiro_crew import platform_compat
from kiro_crew.apps import bridges_agents, bridges_crons
from kiro_crew.apps import official_catalog as oc
from kiro_crew.apps import official_category_order as co
from kiro_crew.apps import official_editorial as oe
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


def test_storage_get_returns_the_stored_value(windows, tmp_path: Path) -> None:
    storage = AppStorage("test-app", tmp_path)
    storage.set("settings", {"theme": "dark"})
    with read_sharing_violation(match="settings.json", times=2) as sim:
        assert storage.get("settings") == {"theme": "dark"}
    assert sim["n"] == 3


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


# Official catalog caches
#
# The store refreshes these caches through ``atomic_write`` with no lock, so two
# store requests race every reader. A read folded into "no cache" makes the
# module fetch again for a document it already has.

_CACHE_MODULES = [oc, co, oe]


@pytest.mark.parametrize("module", _CACHE_MODULES, ids=lambda m: m.__name__.rsplit(".", 1)[-1])
def test_official_cache_read_returns_the_cached_document(
    windows, tmp_path: Path, monkeypatch, module
) -> None:
    path = tmp_path / "cache.json"
    doc = {"apps": [{"name": "demo"}], "marker": module.__name__}
    path.write_text(json.dumps(doc), encoding="utf-8")
    monkeypatch.setattr(module, "_cache_path", lambda: path)
    with read_sharing_violation(match="cache.json", times=2) as sim:
        assert module._read_cache() == doc
    assert sim["n"] == 3


@pytest.mark.parametrize("module", _CACHE_MODULES, ids=lambda m: m.__name__.rsplit(".", 1)[-1])
def test_official_cache_read_still_answers_none_for_absent_or_damaged(
    windows, tmp_path: Path, monkeypatch, module
) -> None:
    path = tmp_path / "cache.json"
    monkeypatch.setattr(module, "_cache_path", lambda: path)
    assert module._read_cache() is None
    path.write_text("{not json", encoding="utf-8")
    assert module._read_cache() is None


def test_cached_entries_returns_the_cached_apps(windows, tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "official-catalog.json"
    path.write_text(json.dumps({"apps": [{"name": "demo"}]}), encoding="utf-8")
    monkeypatch.setattr(oc, "_cache_path", lambda: path)
    with read_sharing_violation(match="official-catalog.json", times=2) as sim:
        entries, mtime = oc.cached_entries()
    assert entries == [{"name": "demo"}]
    assert mtime is not None
    assert sim["n"] == 3


def test_cached_entries_still_answers_empty_for_absent_or_damaged(
    windows, tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "official-catalog.json"
    monkeypatch.setattr(oc, "_cache_path", lambda: path)
    assert oc.cached_entries() == ([], None)
    path.write_text("{not json", encoding="utf-8")
    assert oc.cached_entries() == ([], None)
