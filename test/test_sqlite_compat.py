"""Tests for the SQLite FTS5 capability probe (_sqlite_compat)."""

from __future__ import annotations

import pytest

from kiro_crew import _sqlite_compat


def test_fts5_available_true_on_this_platform():
    # CI and dev machines (macOS, manylinux x86_64) ship FTS5. If this fails,
    # the host Python's sqlite3 lacks FTS5 and the probe is correctly reporting it.
    assert _sqlite_compat.fts5_available() is True


def test_require_fts5_noop_when_available():
    # Should not raise on a platform with FTS5.
    _sqlite_compat.require_fts5()


def test_fts5_available_is_cached():
    # lru_cache(maxsize=1): repeated calls return the same object identity-free
    # boolean and do not re-probe (no exception, stable result).
    first = _sqlite_compat.fts5_available()
    second = _sqlite_compat.fts5_available()
    assert first == second


def test_require_fts5_raises_with_hint_when_unavailable(monkeypatch):
    monkeypatch.setattr(_sqlite_compat, "fts5_available", lambda: False)
    with pytest.raises(RuntimeError) as exc:
        _sqlite_compat.require_fts5()
    assert "FTS5" in str(exc.value)


def test_memory_get_db_raises_clear_error_when_fts5_missing(tmp_path, monkeypatch):
    """_get_db must fail loudly (not loop) when FTS5 is genuinely missing."""
    from kiro_crew import memory as memory_mod

    store = memory_mod.MemoryStore(workspace=tmp_path)

    # Simulate a sqlite3 build without FTS5: CREATE VIRTUAL TABLE fails AND the
    # probe reports unavailable.
    monkeypatch.setattr(memory_mod, "fts5_available", lambda: False)

    def _boom():
        raise memory_mod.sqlite3.OperationalError("no such module: fts5")

    monkeypatch.setattr(store, "_try_create_db", _boom)

    with pytest.raises(RuntimeError) as exc:
        store._get_db()
    assert "FTS5" in str(exc.value)
