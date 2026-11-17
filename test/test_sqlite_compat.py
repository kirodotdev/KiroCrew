"""Tests for the SQLite driver _sqlite_compat resolves: its FTS5 probe and its build."""

from __future__ import annotations

import configparser
import shlex
from pathlib import Path

import pytest

from kiro_crew import _sqlite_compat

SETUP_CFG = Path(__file__).resolve().parents[1] / "setup.cfg"


def test_fts5_available_true_on_this_platform():
    # CI and dev machines (macOS, manylinux x86_64) ship FTS5. If this fails,
    # the host Python's sqlite3 lacks FTS5 and the probe is correctly reporting it.
    assert _sqlite_compat.fts5_available() is True


def test_the_bundled_sqlite_cannot_deadlock_a_wal_close_against_an_open():
    """SQLite 3.51.0 and 3.51.1 can deadlock a thread closing a WAL database's last
    connection against a thread opening the same file; pysqlite3 closes with the GIL
    held, so the whole process stops. setup.cfg excludes the wheel that bundles 3.51.1."""
    if _sqlite_compat.sqlite3 is not _sqlite_compat.pysqlite3:
        pytest.skip("the standard library links the host's SQLite, which setup.cfg does not pin")
    version = _sqlite_compat.sqlite3.sqlite_version_info
    assert not (3, 51, 0) <= version < (3, 51, 2), _sqlite_compat.sqlite3.sqlite_version


def test_the_pysqlite3_spec_is_the_one_setup_cfg_declares():
    """The printed install commands name this constant, so a ``setup.cfg`` exclusion it
    lacks is a hint that installs the excluded wheel."""
    parser = configparser.ConfigParser()
    parser.read(SETUP_CFG, encoding="utf-8")
    declared = [
        line.split(";")[0].strip()
        for line in parser["options"]["install_requires"].splitlines()
        if line.strip().startswith("pysqlite3-binary")
    ]

    assert declared == [_sqlite_compat.PYSQLITE3_REQUIREMENT]


def test_every_fts5_hint_command_installs_the_declared_spec():
    """A bare ``pip install pysqlite3-binary`` can resolve to a wheel ``setup.cfg``
    excludes, such as the one whose SQLite can deadlock."""
    commands = _sqlite_compat.FTS5_UNAVAILABLE_HINT.split("pip install ")[1:]

    assert len(commands) == 2
    for command in commands:
        assert command.startswith(shlex.quote(_sqlite_compat.PYSQLITE3_REQUIREMENT))


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
