"""The one-shot Windows adoption of a stale ``agent.sandbox_allow_unsandboxed_exec: false``.

Old full saves wrote ``false`` on every host. On Windows an undeclared key resolves
to ``true``, but a stored ``false`` counts as declared and refuses every KAS spawn.
The ``superseded_defaults`` row drops that ``false`` once, on Windows only, and the
adoption ledger keeps a ``false`` the owner sets afterwards.
"""

from __future__ import annotations

import argparse
import json
import logging
from types import SimpleNamespace

import pytest

from kiro_crew.config import loader as L
from kiro_crew.config import superseded_defaults as SD
from kiro_crew.config import windows_exec_adoption
from kiro_crew.config.loader import KiroCrewConfig

KEY = "agent.sandbox_allow_unsandboxed_exec"
FIELD = "sandbox_allow_unsandboxed_exec"


@pytest.fixture(autouse=True)
def _home(tmp_path, _floor_monkeypatch):
    """Point every config path, and the adoption ledger, at *tmp_path*."""
    _floor_monkeypatch.setattr(L, "config_path", lambda: tmp_path / "config.json")
    _floor_monkeypatch.setattr(L, "config_dir", lambda: tmp_path)
    _floor_monkeypatch.setattr(L, "config_local_path", lambda: tmp_path / "config.local.json")
    L._REPORTED_SUPERSEDED_KEYS.clear()
    yield
    L._REPORTED_SUPERSEDED_KEYS.clear()


def _write(tmp_path, agent: dict) -> None:
    (tmp_path / "config.json").write_text(json.dumps({"agent": agent}), encoding="utf-8")
    L._invalidate_config_cache()


def _stored(tmp_path) -> dict:
    return json.loads((tmp_path / "config.json").read_text(encoding="utf-8")).get("agent", {})


def test_windows_drops_the_stale_false_once_and_logs_it(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr("sys.platform", "win32")
    _write(tmp_path, {FIELD: False, "approval_mode": "auto"})

    with caplog.at_level(logging.WARNING):
        cfg = KiroCrewConfig.load()

    # The run that performed the migration already holds the Windows default.
    assert cfg.agent.sandbox_allow_unsandboxed_exec is True
    assert FIELD not in _stored(tmp_path)
    assert _stored(tmp_path)["approval_mode"] == "auto"
    assert SD.adopted_superseded() == {KEY: False}
    adopted_lines = [r.getMessage() for r in caplog.records if KEY in r.getMessage()]
    assert [line for line in adopted_lines if "removed stored value False" in line]
    assert len(adopted_lines) == 2, adopted_lines  # the adoption line and its note


def test_a_false_set_after_the_adoption_is_kept(tmp_path, monkeypatch):
    monkeypatch.setattr("sys.platform", "win32")
    _write(tmp_path, {FIELD: False})
    assert KiroCrewConfig.load().agent.sandbox_allow_unsandboxed_exec is True

    # The owner locks the host down by hand after the one-shot ran.
    _write(tmp_path, {FIELD: False})
    assert KiroCrewConfig.load().agent.sandbox_allow_unsandboxed_exec is False
    L._invalidate_config_cache()
    assert KiroCrewConfig.load().agent.sandbox_allow_unsandboxed_exec is False
    assert _stored(tmp_path)[FIELD] is False


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_a_stored_false_off_windows_is_untouched(tmp_path, monkeypatch, platform):
    monkeypatch.setattr("sys.platform", platform)
    _write(tmp_path, {FIELD: False})

    assert KiroCrewConfig.load().agent.sandbox_allow_unsandboxed_exec is False
    assert _stored(tmp_path)[FIELD] is False
    assert SD.adopted_superseded() == {}
    # Not reported either: false is still the default there.
    assert SD.superseded_default_drift({"agent": {FIELD: False}}, acked={}) == []


def test_a_stored_true_on_windows_is_untouched(tmp_path, monkeypatch):
    monkeypatch.setattr("sys.platform", "win32")
    _write(tmp_path, {FIELD: True})

    assert KiroCrewConfig.load().agent.sandbox_allow_unsandboxed_exec is True
    assert _stored(tmp_path)[FIELD] is True
    assert SD.adopted_superseded() == {}


def test_an_acked_false_on_windows_is_kept(tmp_path, monkeypatch):
    """``config set``, Settings saves and ``kirocrew setup`` ack the false they write."""
    monkeypatch.setattr("sys.platform", "win32")
    _write(tmp_path, {FIELD: False})
    assert SD.ack_explicit_write(KEY, False) is True

    assert KiroCrewConfig.load().agent.sandbox_allow_unsandboxed_exec is False
    assert _stored(tmp_path)[FIELD] is False
    assert SD.adopted_superseded() == {}


def test_only_this_key_is_dropped(tmp_path, monkeypatch):
    monkeypatch.setattr("sys.platform", "win32")
    _write(tmp_path, {FIELD: False, "sandbox_allow_no_isolation": False})

    KiroCrewConfig.load()

    assert _stored(tmp_path) == {"sandbox_allow_no_isolation": False}


def test_a_windows_pod_keeps_its_seed_floor_false(tmp_path, monkeypatch):
    """A pod's seed floor writes this false on purpose into a fresh home."""
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setenv("KIROCREW_POD", "1")
    _write(tmp_path, {FIELD: False})

    assert KiroCrewConfig.load().agent.sandbox_allow_unsandboxed_exec is False
    assert _stored(tmp_path)[FIELD] is False
    assert SD.adopted_superseded() == {}


@pytest.mark.parametrize("stored", [None, True])
def test_a_windows_install_with_nothing_stale_keeps_a_later_false(tmp_path, monkeypatch, stored):
    """With no stale false to drop, the first load ends the one-shot."""
    monkeypatch.setattr("sys.platform", "win32")
    _write(tmp_path, {} if stored is None else {FIELD: stored})
    KiroCrewConfig.load()
    assert SD.acked_superseded() == {KEY: False}

    # The operator then locks the host down by hand.
    _write(tmp_path, {FIELD: False})
    assert KiroCrewConfig.load().agent.sandbox_allow_unsandboxed_exec is False
    assert _stored(tmp_path)[FIELD] is False
    assert SD.adopted_superseded() == {}


def test_a_deferred_close_is_retried_on_the_next_unchanged_load(tmp_path, monkeypatch):
    """A close the ack lock deferred must not be skipped by the document cache."""
    monkeypatch.setattr("sys.platform", "win32")
    real_update = SD._update_map
    lock = {"busy": True}

    def _update_unless_busy(*args, **kwargs):
        if lock["busy"]:
            raise BlockingIOError("ack lock held by another writer")
        return real_update(*args, **kwargs)

    monkeypatch.setattr(SD, "_update_map", _update_unless_busy)
    _write(tmp_path, {})

    # Two deferred loads: the first may write the document back, so the second is
    # the one that reads bytes the next load will see unchanged.
    KiroCrewConfig.load()
    KiroCrewConfig.load()
    assert SD.acked_superseded() == {}
    lock["busy"] = False
    # Same bytes on disk, no cache invalidation: the retry must still run.
    KiroCrewConfig.load()
    assert SD.acked_superseded() == {KEY: False}

    _write(tmp_path, {FIELD: False})
    assert KiroCrewConfig.load().agent.sandbox_allow_unsandboxed_exec is False
    assert _stored(tmp_path)[FIELD] is False


def test_an_unreadable_ledger_does_not_turn_the_config_cache_off(tmp_path, monkeypatch):
    """Only a busy lock defers the close; an unreadable ledger fails the same way next load."""
    monkeypatch.setattr("sys.platform", "win32")
    ledger = tmp_path / SD.ACK_FILE_NAME
    ledger.write_text("{not json", encoding="utf-8")
    _write(tmp_path, {})
    calls = []
    real_close = windows_exec_adoption.close_when_nothing_stale

    def _counting_close(base_data):
        calls.append(real_close(base_data))
        return calls[-1]

    monkeypatch.setattr(windows_exec_adoption, "close_when_nothing_stale", _counting_close)

    # The first load may write the document back, so the second reads the bytes
    # that stay unchanged.
    KiroCrewConfig.load()
    KiroCrewConfig.load()
    assert calls and all(calls)
    settled = len(calls)

    # The next unchanged load is served from the cache, so it never re-runs the close.
    KiroCrewConfig.load()
    assert len(calls) == settled
    assert ledger.read_text(encoding="utf-8") == "{not json"


def test_a_false_written_after_config_defaults_adopt_is_kept(tmp_path, monkeypatch):
    """``config defaults --adopt`` records the adoption, so the one-shot stays spent."""
    from kiro_crew import cli_config

    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setattr(cli_config, "config_path", lambda: tmp_path / "config.json")
    monkeypatch.setattr(cli_config, "config_local_path", lambda: tmp_path / "config.local.json")
    monkeypatch.setattr(cli_config, "sel", lambda: SimpleNamespace(log_api_access=lambda **_: None))
    _write(tmp_path, {})
    KiroCrewConfig.load()
    # A hand-written false after the close is kept, and then adopted by command.
    _write(tmp_path, {FIELD: False})
    assert KiroCrewConfig.load().agent.sandbox_allow_unsandboxed_exec is False
    cli_config._config_cmd(
        argparse.Namespace(config_action="defaults", keys=[KEY], adopt=True, keep=False)
    )
    assert FIELD not in _stored(tmp_path)
    assert SD.adopted_superseded() == {KEY: False}

    # The operator locks the host down by hand again.
    _write(tmp_path, {FIELD: False})
    assert KiroCrewConfig.load().agent.sandbox_allow_unsandboxed_exec is False
    assert _stored(tmp_path)[FIELD] is False


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_off_windows_nothing_is_recorded_for_an_absent_key(tmp_path, monkeypatch, platform):
    monkeypatch.setattr("sys.platform", platform)
    _write(tmp_path, {})
    KiroCrewConfig.load()
    assert SD.acked_superseded() == {}


def test_the_drop_writes_the_setup_grant_audit_event(tmp_path, monkeypatch):
    from kiro_crew import sel as sel_mod

    events: list[dict] = []

    class _Sel:
        def log_tool_invocation(self, **kwargs):
            events.append(kwargs)

    monkeypatch.setattr(sel_mod, "sel", lambda: _Sel())
    monkeypatch.setattr("sys.platform", "win32")
    _write(tmp_path, {FIELD: False})

    assert KiroCrewConfig.load().agent.sandbox_allow_unsandboxed_exec is True
    assert len(events) == 1
    event = events[0]
    assert event["tool_name"] == FIELD
    assert event["tool_kind"] == "config"
    assert event["outcome"] == "allowed"
    assert event["critical"] is True


def test_a_failed_audit_keeps_the_false(tmp_path, monkeypatch, caplog):
    from kiro_crew import sel as sel_mod

    class _Sel:
        def log_tool_invocation(self, **kwargs):
            raise OSError("audit log unwritable")

    monkeypatch.setattr(sel_mod, "sel", lambda: _Sel())
    monkeypatch.setattr("sys.platform", "win32")
    _write(tmp_path, {FIELD: False})

    with caplog.at_level(logging.WARNING):
        cfg = KiroCrewConfig.load()

    assert cfg.agent.sandbox_allow_unsandboxed_exec is False
    assert _stored(tmp_path)[FIELD] is False
    assert SD.adopted_superseded() == {}
    assert any("audit event" in r.getMessage() for r in caplog.records)


def test_the_adoption_notice_names_the_isolation_cost(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr("sys.platform", "win32")
    _write(tmp_path, {FIELD: False})

    with caplog.at_level(logging.WARNING):
        KiroCrewConfig.load()

    lines = [r.getMessage() for r in caplog.records if KEY in r.getMessage()]
    assert any("without OS isolation" in line for line in lines)
    summary = SD.adoption_summary(KEY, False)
    assert "without OS isolation" in summary
    assert f"kirocrew config set {KEY} false" in summary
