"""A denylist read that meets a concurrent replace retries instead of reading ``{}``.

On Windows a read of ``denied_commands.json`` fails with ``PermissionError``
(WinError 32) while ``_write_denied_state`` replaces the file. The tolerant
reader folded that into an empty object, so the user-rule toggle and delete
pre-checks answered 404 "unknown user rule" for a rule that exists, and the
panel showed no rules. Both readers now go through
``atomic_write.read_bytes_with_retry``.

The seam raises ``PermissionError`` once from the read of the keystone file with
the Windows flag set, so this runs on any OS. Nothing here writes the file, so
no Windows-only write branch is reached.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew import atomic_write, platform_compat
from kiro_crew.dashboard.handlers import security as security_mod

_RULE = {"id": "fx-user-1", "pattern": "danger", "enabled": True}


@pytest.fixture
def keystone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    path = tmp_path / "denied_commands.json"
    path.write_text(json.dumps({"user_added": [_RULE]}), encoding="utf-8")
    return path


def _contend_once(monkeypatch: pytest.MonkeyPatch, keystone: Path) -> list[Path]:
    """The first read of *keystone* raises a sharing violation; later reads pass."""
    real = atomic_write._read_bytes
    refused: list[Path] = []

    def _read(target: Path, max_bytes: int | None) -> bytes:
        if Path(target).resolve() == keystone.resolve() and not refused:
            refused.append(keystone)
            raise PermissionError(13, "being used by another process", str(target))
        return real(target, max_bytes)

    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(atomic_write, "_REPLACE_BACKOFF_SECONDS", 0)
    monkeypatch.setattr(atomic_write, "_read_bytes", _read)
    return refused


def test_user_rule_ids_survive_a_sharing_violation(keystone, monkeypatch):
    refused = _contend_once(monkeypatch, keystone)

    assert "fx-user-1" in security_mod._user_rule_ids()
    assert refused == [keystone], "the seam must have fired"


def test_snapshot_reader_returns_the_file_not_empty(keystone, monkeypatch):
    refused = _contend_once(monkeypatch, keystone)

    assert security_mod._read_denied_data() == {"user_added": [_RULE]}
    assert refused == [keystone]


def test_strict_reader_does_not_call_contention_corrupt(keystone, monkeypatch):
    refused = _contend_once(monkeypatch, keystone)

    assert security_mod._read_denied_strict() == {"user_added": [_RULE]}
    assert refused == [keystone]


def test_a_corrupt_file_still_reads_as_empty_on_the_snapshot_path(keystone):
    keystone.write_text("{not json", encoding="utf-8")

    assert security_mod._read_denied_data() == {}
    with pytest.raises(security_mod.ConfigCorruptError):
        security_mod._read_denied_strict()
