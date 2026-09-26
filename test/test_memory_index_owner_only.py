"""The FTS index (``memory_index.db``) and its sidecars are repaired to owner-only."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from kiro_crew import memory as memory_module
from kiro_crew import platform_compat
from kiro_crew.memory import MemoryStore

posix_only = pytest.mark.skipif(not platform_compat.IS_POSIX, reason="POSIX mode bits")


def _existing_index(tmp_path: Path) -> tuple[Path, Path]:
    MemoryStore(workspace=tmp_path)._get_db().close()
    index = tmp_path / memory_module.INDEX_DB_FILE
    wal = Path(f"{index}-wal")
    wal.touch()
    return index, wal


@posix_only
def test_existing_index_and_sidecar_are_made_owner_only(tmp_path: Path) -> None:
    index, wal = _existing_index(tmp_path)
    for p in (index, wal):
        p.chmod(0o644)
    MemoryStore(workspace=tmp_path)._get_db().close()
    for p in (index, wal):
        assert stat.S_IMODE(p.stat().st_mode) == 0o600, p


def test_windows_arm_routes_existing_files_through_restrict_to_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    index, wal = _existing_index(tmp_path)
    seen: list[str] = []
    monkeypatch.setattr(memory_module, "IS_POSIX", False)
    monkeypatch.setattr(memory_module, "restrict_to_owner", lambda p: seen.append(str(p)))
    MemoryStore(workspace=tmp_path)._get_db().close()
    assert str(index) in seen and str(wal) in seen, seen
    assert f"{index}-shm" not in seen  # a missing sidecar is skipped, not an error


@posix_only
def test_restriction_runs_once_per_store(tmp_path: Path) -> None:
    index, _ = _existing_index(tmp_path)
    store = MemoryStore(workspace=tmp_path)
    store._get_db().close()
    index.chmod(0o644)
    store._get_db().close()
    assert stat.S_IMODE(index.stat().st_mode) == 0o644  # latched: not re-run per open


def test_a_failed_restrict_warns_opens_and_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _existing_index(tmp_path)

    def _deny(p: object) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr(memory_module, "IS_POSIX", False)
    monkeypatch.setattr(memory_module, "restrict_to_owner", _deny)
    store = MemoryStore(workspace=tmp_path)
    store._get_db().close()
    assert "Cannot restrict" in caplog.text
    assert store._index_owner_only is False  # a failed pass is retried on the next open


@posix_only
def test_a_planted_sidecar_link_is_not_followed(tmp_path: Path) -> None:
    target = tmp_path / "victim"
    target.write_text("x")
    target.chmod(0o644)
    store = MemoryStore(workspace=tmp_path)
    Path(f"{store._index_db}-wal").symlink_to(target)
    store._get_db().close()
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    assert store._index_owner_only is True  # a refused link is not a failure
