"""A denylist read that meets a concurrent replace retries instead of reading ``{}``.

On Windows a read of ``denied_commands.json`` fails with ``PermissionError``
(WinError 32) while ``_write_denied_state`` replaces the file. The tolerant
reader folded that into an empty object, so the user-rule toggle and delete
pre-checks answered 404 "unknown user rule" for a rule that exists, and the
panel showed no rules. Both readers now go through
``loosening_provenance.read_regular_with_retry``.

The seam raises ``PermissionError`` once from the read of the keystone file with
the Windows flag set, so this runs on any OS. Nothing here writes the file, so
no Windows-only write branch is reached.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from kiro_crew import atomic_write, platform_compat
from kiro_crew.config import loosening_provenance
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
    real = loosening_provenance._read_regular
    refused: list[Path] = []

    def _read(target: Path) -> bytes:
        if Path(target).resolve() == keystone.resolve() and not refused:
            refused.append(keystone)
            raise PermissionError(13, "being used by another process", str(target))
        return real(target)

    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(atomic_write, "_REPLACE_BACKOFF_SECONDS", 0)
    monkeypatch.setattr(loosening_provenance, "_read_regular", _read)
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


def _refuse_always(monkeypatch: pytest.MonkeyPatch, keystone: Path) -> None:
    """Every read of *keystone* raises, as a holder with no read sharing causes."""
    real = loosening_provenance._read_regular

    def _read(target: Path) -> bytes:
        if Path(target).resolve() == keystone.resolve():
            raise PermissionError(13, "being used by another process", str(target))
        return real(target)

    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(atomic_write, "_REPLACE_BACKOFF_SECONDS", 0)
    monkeypatch.setattr(loosening_provenance, "_read_regular", _read)


def test_gate_keeps_last_rules_when_a_read_keeps_failing(keystone, monkeypatch):
    from kiro_crew import hooks

    assert hooks.load_denied_commands_state() == {"user_added": [_RULE]}
    _refuse_always(monkeypatch, keystone)

    assert hooks.load_denied_commands_state() == {"user_added": [_RULE]}
    assert security_mod._read_denied_data() == {"user_added": [_RULE]}


def test_gate_retries_a_single_sharing_violation(keystone, monkeypatch):
    from kiro_crew import hooks

    monkeypatch.setattr(hooks, "_DENIED_STATE_LAST_GOOD", {}, raising=False)
    refused = _contend_once(monkeypatch, keystone)

    assert hooks.load_denied_commands_state() == {"user_added": [_RULE]}
    assert refused == [keystone]


def test_a_read_error_never_revives_an_opt_out(keystone, monkeypatch):
    from kiro_crew import hooks

    keystone.write_text(
        json.dumps({"disable_all": True, "disabled_ids": ["x"], "user_added": [_RULE]}),
        encoding="utf-8",
    )
    assert hooks.load_denied_commands_state()["disable_all"] is True
    _refuse_always(monkeypatch, keystone)

    assert hooks.load_denied_commands_state() == {"user_added": [_RULE]}


def test_a_corrupt_file_clears_the_last_good_state(keystone, monkeypatch):
    from kiro_crew import hooks

    assert hooks.load_denied_commands_state() == {"user_added": [_RULE]}
    keystone.write_text("{not json", encoding="utf-8")
    assert hooks.load_denied_commands_state() == {}
    _refuse_always(monkeypatch, keystone)

    assert hooks.load_denied_commands_state() == {}


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs a FIFO")
def test_the_retrying_reader_refuses_a_fifo_without_blocking(tmp_path):
    fifo = tmp_path / "denied_commands.json"
    os.mkfifo(fifo)

    with pytest.raises(OSError):
        loosening_provenance.read_regular_with_retry(fifo)


def test_a_read_begun_before_a_save_cannot_replace_the_saved_state(keystone, monkeypatch):
    from kiro_crew import hooks

    newer = {"id": "fx-user-2", "pattern": "newer", "enabled": True}
    real = loosening_provenance._read_regular
    phase = ["save-mid-read"]

    def _read(target: Path) -> bytes:
        if Path(target).resolve() != keystone.resolve():
            return real(target)
        if phase[0] == "refuse":
            raise PermissionError(13, "being used by another process", str(target))
        old = real(target)
        hooks.record_denied_state_save(keystone, json.dumps({"user_added": [newer]}))
        phase[0] = "refuse"
        return old

    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(atomic_write, "_REPLACE_BACKOFF_SECONDS", 0)
    monkeypatch.setattr(loosening_provenance, "_read_regular", _read)

    assert hooks.load_denied_commands_state() == {"user_added": [_RULE]}
    assert hooks.load_denied_commands_state() == {"user_added": [newer]}
