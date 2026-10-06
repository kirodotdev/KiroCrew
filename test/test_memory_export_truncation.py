"""``memory export`` names a collection it cut at its row limit.

The limits stay; what is pinned is that a short file never passes for a full one:
a store past the limit gets a stderr warning with the real count and exactly
limit-many rows in the file, and a store under it gets no warning at all.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from kiro_crew import cli_commands as cc
from kiro_crew.memory_stores import DEFAULT_MEMORY_STORE, resolve_store_path
from kiro_crew.vector_memory import VectorMemoryStore


def _seed_default_store(episodes: int) -> int:
    """Write *episodes* distinct episodes to the default store; return its event count."""
    db_path = resolve_store_path(DEFAULT_MEMORY_STORE)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = VectorMemoryStore(db_path=db_path)
    store.init()
    try:
        for i in range(episodes):
            text = f"Episode {i}: the crew shipped release {i}."
            assert store.write_episodic(text, source="test", importance=0.5) is True
        return len(store.get_events(limit=-1))
    finally:
        store.close()


def _export(tmp_path: Path) -> dict:
    out = tmp_path / "export.json"
    cc._memory_cmd(argparse.Namespace(mem_action="export", output=str(out), store=None))
    return json.loads(out.read_text(encoding="utf-8"))


def test_a_store_over_the_limit_warns_and_writes_limit_many_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_default_store(3)
    monkeypatch.setattr(cc, "_EXPORT_EPISODIC_LIMIT", 2)
    monkeypatch.setattr(cc, "_EXPORT_EVENTS_LIMIT", 10_000)
    payload = _export(tmp_path)
    assert len(payload["episodic"]) == 2
    err = capsys.readouterr().err
    assert "warning: exported 2 of 3 episodes; 1 omitted" in err
    assert "events" not in err


def test_cut_events_are_named_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    total = _seed_default_store(2)
    if total < 2:
        pytest.skip("episode writes recorded fewer than two events")
    monkeypatch.setattr(cc, "_EXPORT_EVENTS_LIMIT", total - 1)
    payload = _export(tmp_path)
    assert len(payload["events"]) == total - 1
    err = capsys.readouterr().err
    assert f"warning: exported {total - 1} of {total} events; 1 omitted" in err
    assert "episodes" not in err


def test_a_store_under_the_limit_exports_without_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    total = _seed_default_store(2)
    monkeypatch.setattr(cc, "_EXPORT_EPISODIC_LIMIT", 3)
    monkeypatch.setattr(cc, "_EXPORT_EVENTS_LIMIT", total + 1)
    payload = _export(tmp_path)
    assert len(payload["episodic"]) == 2
    assert "warning" not in capsys.readouterr().err


def test_a_store_exactly_at_the_limit_exports_without_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    total = _seed_default_store(2)
    monkeypatch.setattr(cc, "_EXPORT_EPISODIC_LIMIT", 2)
    monkeypatch.setattr(cc, "_EXPORT_EVENTS_LIMIT", total)
    payload = _export(tmp_path)
    assert len(payload["episodic"]) == 2
    assert "warning" not in capsys.readouterr().err
