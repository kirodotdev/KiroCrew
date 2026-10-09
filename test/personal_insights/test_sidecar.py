from __future__ import annotations

import sqlite3
import stat

import pytest

from kiro_crew.personal_insights.insights_sidecar import (
    BINDING_BOUND,
    BINDING_RECOVERY_REEXPORT,
    BindingMetadata,
    BindingValidation,
    EvidenceDisposed,
    RedactionDriftError,
    SidecarError,
    SidecarStore,
)

SCHEMA = "kiro.personal-insights.sidecar/1.0"


def _store(tmp_path):
    return SidecarStore(tmp_path / "sidecar" / "bindings.db")


def _validation(**changes):
    fields = {
        "schema_version": SCHEMA,
        "source_content_digest": "a" * 64,
        "projection_generation_id": "generation-1",
        "descriptor_sha256": "d" * 64,
        "redaction_contract_version": "redaction:1.0",
    }
    fields.update(changes)
    return BindingValidation(**fields)


def _metadata():
    return BindingMetadata(
        session_key="session-1",
        snapshot_digest="b" * 64,
        group_id="group-1",
        retained_until="2026-11-08T00:00:00Z",
    )


def _neutral(character):
    return "ne-" + character * 64


def test_owner_only_store_modes(tmp_path):
    store = _store(tmp_path)
    assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600


def test_validation_rejects_malformed_fields():
    cases = (
        {"schema_version": "wrong/1.0"},
        {"source_content_digest": "bad"},
        {"projection_generation_id": "contains space"},
        {"descriptor_sha256": "bad"},
        {"redaction_contract_version": ""},
    )
    for changes in cases:
        with pytest.raises(ValueError):
            _validation(**changes)


def test_rekey_is_atomic_and_removes_raw_rows(tmp_path):
    store = _store(tmp_path)
    validation = _validation()
    store.stage_raw_text("source-1", {"raw-1": "first", "raw-2": "second"}, validation, _metadata())
    mapping = {"raw-1": _neutral("1"), "raw-2": _neutral("2")}
    store.rekey("source-1", mapping, validation)
    assert store.raw_text("source-1", "raw-1") is None
    assert store.raw_text("source-1", "raw-2") is None
    assert store.neutral_text("source-1", _neutral("1"), validation) == "first"
    assert store.neutral_text("source-1", _neutral("2"), validation) == "second"


def test_database_abort_rolls_back_both_rekey_halves(tmp_path):
    store = _store(tmp_path)
    validation = _validation()
    store.stage_raw_text("source-1", {"raw-1": "first"}, validation)
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "CREATE TRIGGER abort_raw_delete BEFORE DELETE ON bindings "
            "WHEN OLD.key_kind='raw' BEGIN SELECT RAISE(ABORT,'stop'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        store.rekey("source-1", {"raw-1": _neutral("1")}, validation)
    assert store.raw_text("source-1", "raw-1", validation) == "first"
    assert store.neutral_text("source-1", _neutral("1"), validation) is None


def test_raw_ids_are_source_scoped(tmp_path):
    store = _store(tmp_path)
    validation = _validation()
    store.stage_raw_text("source-a", {"raw": "text-a"}, validation)
    store.stage_raw_text("source-b", {"raw": "text-b"}, validation)
    store.rekey("source-a", {"raw": _neutral("a")}, validation)
    assert store.neutral_text("source-a", _neutral("a"), validation) == "text-a"
    assert store.raw_text("source-b", "raw", validation) == "text-b"


def test_rekey_rejects_wrong_source_and_stale_raw_binding(tmp_path):
    store = _store(tmp_path)
    validation = _validation()
    store.stage_raw_text("source-a", {"raw": "text"}, validation)
    with pytest.raises(SidecarError):
        store.rekey("source-b", {"raw": _neutral("a")}, validation)
    with pytest.raises(SidecarError):
        store.rekey(
            "source-a",
            {"raw": _neutral("a")},
            _validation(descriptor_sha256="e" * 64),
        )


def test_duplicate_or_invalid_neutral_ids_are_rejected(tmp_path):
    store = _store(tmp_path)
    validation = _validation()
    store.stage_raw_text("source", {"r1": "a", "r2": "b"}, validation)
    with pytest.raises(SidecarError):
        store.rekey("source", {"r1": _neutral("a"), "r2": _neutral("a")}, validation)
    with pytest.raises(SidecarError):
        store.rekey("source", {"r1": "ne-short"}, validation)


def test_verified_neutral_wins_over_injected_raw(tmp_path):
    store = _store(tmp_path)
    validation = _validation()
    store.stage_raw_text("source", {"raw": "verified"}, validation)
    store.rekey("source", {"raw": _neutral("a")}, validation)
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "INSERT INTO bindings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "source",
                "raw",
                "raw",
                "INJECTED",
                SCHEMA,
                "f" * 64,
                "other-generation",
                "e" * 64,
                "redaction:2.0",
                "session-1",
                "b" * 64,
                None,
                None,
            ),
        )
    assert store.recover("source", {"raw": _neutral("a")}, validation) == BINDING_BOUND
    assert store.raw_text("source", "raw") is None
    assert store.neutral_text("source", _neutral("a"), validation) == "verified"


def test_recovery_rekeys_valid_raw_state(tmp_path):
    store = _store(tmp_path)
    validation = _validation()
    store.stage_raw_text("source", {"raw": "text"}, validation)
    assert store.recover("source", {"raw": _neutral("a")}, validation) == BINDING_BOUND
    assert store.neutral_text("source", _neutral("a"), validation) == "text"


def test_recovery_both_missing_requires_reexport(tmp_path):
    store = _store(tmp_path)
    assert (
        store.recover("source", {"raw": _neutral("a")}, _validation()) == BINDING_RECOVERY_REEXPORT
    )


def test_wrong_descriptor_requires_reexport(tmp_path):
    store = _store(tmp_path)
    validation = _validation()
    mapping = {"raw": _neutral("a")}
    store.stage_raw_text("source", {"raw": "text"}, validation)
    store.rekey("source", mapping, validation)
    wrong = _validation(descriptor_sha256="e" * 64)
    assert store.recover("source", mapping, wrong) == BINDING_RECOVERY_REEXPORT


def test_recovery_does_not_touch_another_source(tmp_path):
    store = _store(tmp_path)
    validation = _validation()
    store.stage_raw_text("source-a", {"raw-a": "a"}, validation)
    store.stage_raw_text("source-b", {"raw-b": "b"}, validation)
    store.recover("source-a", {"raw-a": _neutral("a")}, validation)
    assert store.raw_text("source-b", "raw-b", validation) == "b"


def test_locator_and_viewer_text_survive_for_retention_window(tmp_path):
    store = _store(tmp_path)
    validation = _validation()
    metadata = _metadata()
    store.stage_raw_text("source", {"raw": "text"}, validation, metadata)
    store.rekey("source", {"raw": _neutral("a")}, validation)
    locator = store.locator("source", _neutral("a"))
    assert locator is not None
    assert locator.session_key == metadata.session_key
    assert locator.group_id == metadata.group_id
    assert locator.retained_until == metadata.retained_until
    assert store.retrieve_for_view("source", _neutral("a"), validation) == "text"


def test_viewer_rejects_digest_and_redaction_drift(tmp_path):
    store = _store(tmp_path)
    validation = _validation()
    store.stage_raw_text("source", {"raw": "text"}, validation)
    store.rekey("source", {"raw": _neutral("a")}, validation)
    with pytest.raises(EvidenceDisposed):
        store.retrieve_for_view(
            "source",
            _neutral("a"),
            _validation(source_content_digest="f" * 64),
        )
    with pytest.raises(RedactionDriftError):
        store.retrieve_for_view(
            "source",
            _neutral("a"),
            _validation(redaction_contract_version="redaction:2.0"),
        )


def test_text_disposal_retains_locator_then_full_disposal_removes_it(tmp_path):
    store = _store(tmp_path)
    validation = _validation()
    store.stage_raw_text("source", {"raw": "text"}, validation, _metadata())
    store.rekey("source", {"raw": _neutral("a")}, validation)
    store.dispose_viewer_text("source")
    assert store.locator("source", _neutral("a")) is not None
    with pytest.raises(EvidenceDisposed):
        store.retrieve_for_view("source", _neutral("a"), validation)
    store.dispose("source")
    assert store.locator("source", _neutral("a")) is None
