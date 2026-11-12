"""``kirocrew memory restore`` refuses an empty ``--store`` name.

``--store "$STORE"`` with ``STORE`` unset reaches the verb as ``''``. Restore is a
write, so the empty name is refused rather than read as the default store, the way
``export`` and ``import`` refuse it.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew import memory_backup as mb
from kiro_crew.memory_stores import DEFAULT_MEMORY_STORE, resolve_store_path
from kiro_crew.vector_memory import VectorMemoryStore


def _point_home_at(monkeypatch: pytest.MonkeyPatch, data_home: Path) -> None:
    """Make *data_home* the data home, declaring only the default store."""
    import kiro_crew.config.paths as paths
    from kiro_crew import memory_stores
    from kiro_crew.config.loader import _invalidate_config_cache

    resolved_home = data_home.resolve()
    config = {
        "memory_stores": {DEFAULT_MEMORY_STORE: {}},
        "default_memory_store": DEFAULT_MEMORY_STORE,
    }
    (resolved_home / "config.json").write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setenv("KIROCREW_HOME", str(resolved_home))
    monkeypatch.setattr(paths, "_resolved_home", None, raising=False)
    monkeypatch.setattr(paths, "_config_dir_memo", None, raising=False)
    monkeypatch.setattr(memory_stores, "_DECLARED_MEMO", None, raising=False)
    _invalidate_config_cache()


@pytest.mark.parametrize("cancel_pending", [False, True])
def test_an_empty_store_name_is_refused_and_stages_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cancel_pending: bool,
) -> None:
    from kiro_crew.cli_commands import _memory_cmd

    _point_home_at(monkeypatch, tmp_path)
    store = VectorMemoryStore()
    store.init()
    try:
        store.set_semantic("project.p0", "v0", 1.0, "user_explicit")
    finally:
        store.close()
    path = resolve_store_path(DEFAULT_MEMORY_STORE)
    backup = mb.backup_store(path)
    assert backup is not None
    if cancel_pending:
        mb.restore_from_backup(backup)
    pending = mb.backup_dir_for(path) / mb._V1_PENDING
    assert pending.exists() is cancel_pending

    args = SimpleNamespace(
        mem_action="restore", store="", from_backup=None, cancel_pending=cancel_pending
    )
    code: object = 0
    try:
        _memory_cmd(args)
    except SystemExit as exc:
        code = exc.code

    captured = capsys.readouterr()
    assert pending.exists() is cancel_pending, captured.out
    assert code == 1, captured.out
    assert "--store names no store" in captured.err
