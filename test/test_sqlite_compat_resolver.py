"""Tests for the one sqlite3 resolver every import site binds (_sqlite_compat).

An empty ``pysqlite3/`` directory left behind by a bundle prune imports fine as
a PEP 420 namespace package, so ``except ImportError`` never fires and the first
query raises ``AttributeError: module 'pysqlite3' has no attribute 'connect'``.
These tests pin the ``connect`` probe that turns that husk back into the stdlib
fallback, and pin that no import site carries its own copy of the idiom.
"""

from __future__ import annotations

import os
import re
import sqlite3 as stdlib_sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path
from types import ModuleType

from kiro_crew import _sqlite_compat

# Every module that binds a module-level ``sqlite3`` name from the resolver.
IMPORT_SITES = (
    "kiro_crew._sqlite_compat",
    "kiro_crew.vector_memory",
    "kiro_crew.snapshot",
    "kiro_crew.snapshot_redact",
    "kiro_crew.portability",
    "kiro_crew.knowledge.store",
    "kiro_crew.knowledge.retrieval",
    "kiro_crew.metrics.inventory_gauges",
    "kiro_crew.dashboard.handlers.kiro_usage_api",
    "kiro_crew.apps.builtins.aws_control.backend.backup",
)

REPO_SRC = Path(__file__).resolve().parents[1] / "src"
SRC_ROOT = REPO_SRC / "kiro_crew"


def test_husk_pysqlite3_resolves_to_stdlib(monkeypatch):
    """A module object with no ``connect`` is the pruned-bundle husk: not a driver."""
    husk = ModuleType("pysqlite3")
    assert not hasattr(husk, "connect")
    monkeypatch.setattr(_sqlite_compat, "pysqlite3", husk)

    resolved = _sqlite_compat.resolve_sqlite3()

    assert resolved is stdlib_sqlite3
    resolved.connect(":memory:").close()


def test_absent_pysqlite3_resolves_to_stdlib(monkeypatch):
    """No driver at all -- what the import raising ImportError leaves behind."""
    monkeypatch.setattr(_sqlite_compat, "pysqlite3", None)

    resolved = _sqlite_compat.resolve_sqlite3()

    assert resolved is stdlib_sqlite3
    resolved.connect(":memory:").close()


def test_real_pysqlite3_is_preferred(monkeypatch):
    """A driver that has ``connect`` still wins over the stdlib."""
    driver = ModuleType("pysqlite3")
    driver.connect = stdlib_sqlite3.connect  # type: ignore[attr-defined]
    monkeypatch.setattr(_sqlite_compat, "pysqlite3", driver)

    assert _sqlite_compat.resolve_sqlite3() is driver


def test_every_import_site_survives_a_husk(tmp_path):
    """With a husk installed, every site binds stdlib sqlite3 and can open a db.

    A fresh interpreter, because the husk must be in place before the first
    ``import kiro_crew`` and a reload would otherwise leak into other tests.
    Drop the ``connect`` probe from the resolver and this fails on the first
    site with the AttributeError the gateway crash reported.
    """
    script = textwrap.dedent("""
        import importlib
        import sqlite3
        import sys
        from types import ModuleType

        sys.modules['pysqlite3'] = ModuleType('pysqlite3')
        assert not hasattr(sys.modules['pysqlite3'], 'connect')

        for name in {sites!r}:
            module = importlib.import_module(name)
            assert module.sqlite3 is sqlite3, name
            module.sqlite3.connect(':memory:').close()
        """).format(sites=IMPORT_SITES)
    env = dict(os.environ, KIROCREW_HOME=str(tmp_path), KIRO_HOME=str(tmp_path / "kiro"))
    env.update(TMPDIR=str(tmp_path), TMP=str(tmp_path), TEMP=str(tmp_path))
    # Test the tree this file lives in, not whichever checkout an editable
    # install happens to point at.
    env["PYTHONPATH"] = os.pathsep.join([str(REPO_SRC), env.get("PYTHONPATH", "")]).rstrip(
        os.pathsep
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_idiom_lives_in_exactly_one_file():
    """No second copy of ``import pysqlite3``: that is how six sites went unguarded."""
    pattern = re.compile(r"^\s*import pysqlite3", re.MULTILINE)
    carriers = sorted(
        path.relative_to(SRC_ROOT).as_posix()
        for path in SRC_ROOT.rglob("*.py")
        if pattern.search(path.read_text(encoding="utf-8"))
    )
    assert carriers == ["_sqlite_compat.py"]


# Modules that, in the gateway process, open or handle the errors of a database that
# a resolver-bound connection also opens: memory.db (the V1 store check, and the
# lesson handlers' member-store check), knowledge.db (the inventory gauge) and
# kiro-cli's data.sqlite3 (kiro_cli, kiro_prerequisite, the backup's conversation
# export, the usage API). A stdlib connection there is a second SQLite library with
# its own lock table, whose close drops the other connections' POSIX locks on the
# file; a stdlib except clause there does not catch the resolver's error classes.
SHARED_DATABASE_OPENERS = (
    "memory_stores.py",
    "metrics/inventory_gauges.py",
    "dashboard/handlers/cron.py",
    "kiro_cli.py",
    "kiro_prerequisite.py",
    "apps/builtins/aws_control/backend/backup.py",
    "dashboard/handlers/kiro_usage_api.py",
)


def test_a_store_database_is_never_opened_with_the_stdlib_driver():
    stdlib_import = re.compile(r"^\s*(import sqlite3\b|from sqlite3 import)", re.MULTILINE)
    offenders = [
        name
        for name in SHARED_DATABASE_OPENERS
        if stdlib_import.search((SRC_ROOT / name).read_text(encoding="utf-8"))
    ]
    assert offenders == []
