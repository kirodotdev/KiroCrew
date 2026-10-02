"""The one-shot rewrite of a ``skills.lazy_load: false`` that 0.6.x materialized.

0.6.x and earlier wrote every default into ``config.json``, and their default was
``false`` -- the full skills listing. Since 0.7.0 ``false`` selects the short skill
entry and the default is ``true``, and a stored value beats the default, so an
upgraded install silently runs the narrowest mode.

The rewrite is sound only where the ``false`` provably predates 0.7.0: the stamp
names 0.6.x or older AND the ``connections_ui`` marker, which every 0.7+ load
writes, is absent. Everything else stays exactly as stored. The truth table:
migrated once and a second load is a no-op; a 0.7+ stamp, an existing marker, an
explicit true, and an unreadable or missing stamp are each left alone.
"""

from __future__ import annotations

import json
import logging

import pytest

from kiro_crew.config import loader as L
from kiro_crew.config import migration as M
from kiro_crew.config import superseded_defaults as SD
from kiro_crew.config.loader import KiroCrewConfig

KEY = "skills.lazy_load"


@pytest.fixture(autouse=True)
def _forget_process_warnings():
    """The warned-keys set is process-global; each test starts from empty."""
    L._REPORTED_SUPERSEDED_KEYS.clear()
    yield
    L._REPORTED_SUPERSEDED_KEYS.clear()


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Aim config.json, its overlay, the marker and the ledger at *tmp_path*."""
    monkeypatch.setattr(L, "config_path", lambda: tmp_path / "config.json")
    monkeypatch.setattr(L, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(L, "config_local_path", lambda: tmp_path / "config.local.json")
    return tmp_path


def _legacy_config(stamp: object = "0.6.0", lazy_load: object = False) -> dict:
    """A 0.6.x-shaped document: meta stamp plus a fully materialized skills section."""
    return {
        "meta": {"lastTouchedVersion": stamp, "lastTouchedAt": "2026-08-01T00:00:00+00:00"},
        "skills": {"max_triggered": 0, "lazy_load": lazy_load},
        "auto_update": True,
    }


def _write(home, data: dict) -> None:
    (home / "config.json").write_text(json.dumps(data), encoding="utf-8")
    L._invalidate_config_cache()


def _on_disk(home) -> dict:
    return json.loads((home / "config.json").read_text(encoding="utf-8"))


def _marker(home):
    return home / M.CONNECTIONS_UI_MIGRATION_MARKER


def _rewrite_notices(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if "removed skills.lazy_load" in r.getMessage()]


def test_a_0_6_config_holding_false_is_migrated_once(home, caplog):
    _write(home, _legacy_config())

    with caplog.at_level(logging.INFO, logger="kiro_crew.config.loader"):
        cfg = KiroCrewConfig.load()

    assert cfg.skills.lazy_load is True, "this very load must already run the new default"
    stored = _on_disk(home)
    assert "lazy_load" not in stored["skills"], "the stale false must leave config.json"
    assert stored["skills"]["max_triggered"] == 0, "the delta must not touch other keys"
    assert stored["auto_update"] is True
    assert SD.adopted_superseded() == {KEY: False}, "the rewrite is recorded in the ledger"
    assert _marker(home).exists(), "the 0.7.0 boundary marker lands in the same pass"
    notices = _rewrite_notices(caplog)
    assert len(notices) == 1 and "0.6.0" in notices[0]
    assert "The current default (the ranked skill index) applies." in notices[0]
    assert "kirocrew config set skills.lazy_load false" in notices[0]

    migrated = (home / "config.json").read_bytes()
    ledger = SD.ack_file_path().read_bytes()
    caplog.clear()
    L._invalidate_config_cache()
    with caplog.at_level(logging.INFO, logger="kiro_crew.config.loader"):
        again = KiroCrewConfig.load()

    assert again.skills.lazy_load is True
    assert (home / "config.json").read_bytes() == migrated, "a second load is a no-op"
    assert SD.ack_file_path().read_bytes() == ledger
    assert _rewrite_notices(caplog) == []


@pytest.mark.parametrize(
    "stamp", ["0.5.0", "0.6.0", "0.6.2", "0.6.0-insider.3", "0.6.0.10", "0.6.0rc1"]
)
def test_every_pre_0_7_stamp_is_provable(home, stamp):
    _write(home, _legacy_config(stamp=stamp))
    assert KiroCrewConfig.load().skills.lazy_load is True
    assert "lazy_load" not in _on_disk(home)["skills"]


@pytest.mark.parametrize(
    "stamp", ["0.7.0", "0.7.0-insider.1", "0.7.2", "0.8.0-insider.3", "0.9.0", "10.6.0"]
)
def test_a_0_7_or_later_stamp_keeps_false_and_is_only_reported(home, monkeypatch, caplog, stamp):
    """Left as stored, and a superseded-default report row for the key still names it.

    The registry row is simulated, so this pins how the two compose: this cohort is
    reported, and the migrated cohort is not
    (``test_the_report_skips_the_key_the_same_load_is_removing``).
    """
    row = SD.SupersededDefault(
        dotted_key=KEY, old_default=False, new_default=True, changed_in="#12131"
    )
    monkeypatch.setattr(SD, "SUPERSEDED_DEFAULTS", (*SD.SUPERSEDED_DEFAULTS, row))
    _write(home, _legacy_config(stamp=stamp))

    with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
        cfg = KiroCrewConfig.load()

    assert cfg.skills.lazy_load is False
    assert _on_disk(home)["skills"]["lazy_load"] is False
    assert KEY not in SD.adopted_superseded()
    assert any(
        "superseded default" in r.getMessage() and KEY in r.getMessage() for r in caplog.records
    )
    assert _rewrite_notices(caplog) == []


def test_the_report_skips_the_key_the_same_load_is_removing(home, monkeypatch, caplog):
    row = SD.SupersededDefault(
        dotted_key=KEY, old_default=False, new_default=True, changed_in="#12131"
    )
    monkeypatch.setattr(SD, "SUPERSEDED_DEFAULTS", (*SD.SUPERSEDED_DEFAULTS, row))
    _write(home, _legacy_config())

    with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
        KiroCrewConfig.load()

    assert not any("superseded default" in r.getMessage() for r in caplog.records)
    assert len(_rewrite_notices(caplog)) == 1


def test_an_existing_connections_ui_marker_blocks_the_migration(home):
    """The marker proves a 0.7+ build already loaded this home, whatever the stamp says."""
    _marker(home).write_text("{}", encoding="utf-8")
    _write(home, _legacy_config())

    cfg = KiroCrewConfig.load()

    assert cfg.skills.lazy_load is False
    assert _on_disk(home)["skills"]["lazy_load"] is False
    assert KEY not in SD.adopted_superseded()


def test_an_explicit_true_is_untouched(home):
    _write(home, _legacy_config(lazy_load=True))

    cfg = KiroCrewConfig.load()

    assert cfg.skills.lazy_load is True
    assert _on_disk(home)["skills"]["lazy_load"] is True
    assert KEY not in SD.adopted_superseded()
    assert _marker(home).exists()


@pytest.mark.parametrize("lazy_load", [0, "false", None])
def test_only_the_exact_boolean_false_is_rewritten(home, lazy_load):
    _write(home, _legacy_config(lazy_load=lazy_load))
    KiroCrewConfig.load()
    assert _on_disk(home)["skills"]["lazy_load"] == lazy_load
    assert KEY not in SD.adopted_superseded()


@pytest.mark.parametrize(
    "meta",
    [
        "absent",
        "not-an-object",
        {},
        {"lastTouchedVersion": None},
        {"lastTouchedVersion": 6},
        {"lastTouchedVersion": ""},
        {"lastTouchedVersion": "banana"},
        {"lastTouchedVersion": "0.6"},
        {"lastTouchedVersion": "v0.6.0"},
        {"lastTouchedVersion": "0.6.0\n"},
    ],
)
def test_an_unreadable_or_missing_stamp_does_not_migrate(home, meta):
    data = _legacy_config()
    if meta == "absent":
        del data["meta"]
    else:
        data["meta"] = meta
    _write(home, data)

    cfg = KiroCrewConfig.load()

    assert cfg.skills.lazy_load is False
    assert _on_disk(home)["skills"]["lazy_load"] is False
    assert KEY not in SD.adopted_superseded()
    assert _marker(home).exists(), "an unknown writer closes the window for good"


def test_an_unreadable_ledger_does_not_migrate(home):
    """Unknown is not "never adopted": an unparsable ledger declines."""
    SD.ack_file_path().write_text("{not json", encoding="utf-8")
    _write(home, _legacy_config())

    assert KiroCrewConfig.load().skills.lazy_load is False
    assert _on_disk(home)["skills"]["lazy_load"] is False
    # Declined up front, not by a ledger write that fails and aborts the pass: the
    # other migrations, and the boundary marker, still land.
    assert _marker(home).exists()


def test_a_ledger_entry_keeps_it_one_shot_without_the_marker(home):
    """A restored false is the operator's even if the marker was later deleted."""
    SD.ack_file_path().write_text(
        json.dumps({"acked": {}, "adopted": {KEY: False}}), encoding="utf-8"
    )
    _write(home, _legacy_config())

    assert KiroCrewConfig.load().skills.lazy_load is False
    assert _on_disk(home)["skills"]["lazy_load"] is False


def test_a_deferred_write_defers_everything(home):
    """A contended lock writes nothing: no in-memory flip, no marker, retried next load."""
    _write(home, _legacy_config())
    seen: list[frozenset] = []

    def contended(path, pending, **kwargs):
        seen.append(pending)
        return False

    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(L, "_persist_config_migration", contended)
        cfg = KiroCrewConfig.load()

    assert any(M.MIGRATE_SKILLS_LAZY_LOAD in p for p in seen)
    assert cfg.skills.lazy_load is False, "memory never runs ahead of the stored document"
    assert _on_disk(home)["skills"]["lazy_load"] is False
    assert not _marker(home).exists(), "the boundary marker must defer with the rewrite"

    assert KiroCrewConfig.load().skills.lazy_load is True
    assert "lazy_load" not in _on_disk(home)["skills"]


def test_an_overlay_value_wins_in_memory(home, caplog):
    """The base's stale false is cleared, but config.local.json is the live choice."""
    _write(home, _legacy_config())
    (home / "config.local.json").write_text(
        json.dumps({"skills": {"lazy_load": False}}), encoding="utf-8"
    )
    L._invalidate_config_cache()

    with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
        cfg = KiroCrewConfig.load()

    assert "lazy_load" not in _on_disk(home)["skills"]
    assert cfg.skills.lazy_load is False
    notices = _rewrite_notices(caplog)
    assert len(notices) == 1 and "config.local.json still sets the key" in notices[0]
    assert "The current default" not in notices[0], "the notice must not claim a default applies"


def test_a_degraded_first_load_keeps_the_proof_until_a_clean_load(home):
    """The gateway's meta refresh must not re-stamp away a rewrite still pending."""
    _write(home, _legacy_config())
    (home / "config.local.json").write_text("{not json", encoding="utf-8")
    L._invalidate_config_cache()

    assert KiroCrewConfig.load().skills.lazy_load is False
    assert not _marker(home).exists()
    assert L.refresh_config_meta_stamp() is False
    assert _on_disk(home)["meta"]["lastTouchedVersion"] == "0.6.0"

    (home / "config.local.json").unlink()
    L.reset_degraded_observations()  # what the operator's restart does
    L._invalidate_config_cache()
    assert KiroCrewConfig.load().skills.lazy_load is True
    assert "lazy_load" not in _on_disk(home)["skills"]


def test_the_meta_refresh_still_runs_outside_the_cohort(home):
    _write(home, _legacy_config(lazy_load=True))
    assert L.refresh_config_meta_stamp() is True
    assert _on_disk(home)["meta"]["lastTouchedVersion"] == L.__version__


def test_the_transform_re_checks_the_value_inside_the_lock():
    """A hand edit to true since the load's read keeps the 0.6 stamp: leave it."""
    recorded: list[dict] = []
    doc = _legacy_config(lazy_load=True)

    changed = M.apply_document_migrations(
        doc,
        frozenset({M.MIGRATE_SKILLS_LAZY_LOAD}),
        overlay_kiro_agent=None,
        default_kiro_agent="kirocrew",
        record_adoptions=recorded.append,
    )

    assert changed is False
    assert doc["skills"]["lazy_load"] is True
    assert recorded == []


def test_one_ledger_record_carries_every_removal_of_the_pass():
    """Two records would let a failed second one strand the first as adopted."""
    recorded: list[dict] = []
    doc = _legacy_config()
    doc["agent"] = {"subagent_timeout_secs": 1800}

    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(SD, "_read_ack_document_status", lambda: (None, True))
        M.apply_document_migrations(
            doc,
            frozenset({M.MIGRATE_SKILLS_LAZY_LOAD, M.MIGRATE_SUPERSEDED_DEFAULTS}),
            overlay_kiro_agent=None,
            default_kiro_agent="kirocrew",
            adopt_keys=frozenset({"agent.subagent_timeout_secs"}),
            record_adoptions=recorded.append,
        )

    assert recorded == [{"agent.subagent_timeout_secs": 1800, KEY: False}]
    assert "lazy_load" not in doc["skills"]
    assert "subagent_timeout_secs" not in doc["agent"]


def test_the_transform_re_checks_the_stamp_inside_the_lock():
    """A write by this build since the load's read re-stamped the file: leave it."""
    recorded: list[dict] = []
    doc = _legacy_config(stamp="0.9.0")

    changed = M.apply_document_migrations(
        doc,
        frozenset({M.MIGRATE_SKILLS_LAZY_LOAD}),
        overlay_kiro_agent=None,
        default_kiro_agent="kirocrew",
        record_adoptions=recorded.append,
    )

    assert changed is False
    assert doc["skills"]["lazy_load"] is False
    assert recorded == []


def test_a_failed_ledger_record_aborts_the_removal():
    """Marker first: no ledger entry, no removal."""
    doc = _legacy_config()

    def refuse(_values):
        raise BlockingIOError("sidecar held")

    with pytest.raises(BlockingIOError):
        M.apply_document_migrations(
            doc,
            frozenset({M.MIGRATE_SKILLS_LAZY_LOAD}),
            overlay_kiro_agent=None,
            default_kiro_agent="kirocrew",
            record_adoptions=refuse,
        )
    assert doc["skills"]["lazy_load"] is False


def test_doctor_replays_the_rewrite_with_a_restore_command():
    line = SD.adoption_summary(KEY, False)
    assert line.endswith("kirocrew config set skills.lazy_load false")
    assert "still listed as drift" not in line, "this key has no registry row to list it"
    assert "no registered default explains" in SD.adoption_summary(KEY, True)
