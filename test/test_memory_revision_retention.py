"""V1 bounds accepted editor history without losing records, proposals or CAS."""

from __future__ import annotations

import json
from contextlib import closing

import pytest

from kiro_crew import (
    memory_edit,
)
from kiro_crew import memory_record_metadata as metadata
from kiro_crew import (
    memory_stores,
    vector_memory,
)
from kiro_crew._sqlite_compat import sqlite3
from kiro_crew.config import loader
from kiro_crew.vector_memory import SemanticRejectCode, VectorMemoryStore


@pytest.fixture(params=["v1", "named-v1", "v2"])
def store(request, tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    name = "default" if request.param == "v1" else "member-alice"
    private = request.param == "v2"
    directory = tmp_path if name == "default" else tmp_path / "memory_stores" / name
    directory.mkdir(parents=True, exist_ok=True)
    declaration = {"memory_version": 2, "owner_member": "alice"} if private else {}
    config = {"memory_stores": {"default": {}, name: declaration}, "agents": {}}
    if private:
        config["agents"] = {"alice": {"memory_store": name}}
        (directory / "member-memory.json").write_text(json.dumps(declaration), encoding="utf-8")
    (tmp_path / "config.json").write_text(json.dumps(config), encoding="utf-8")
    loader._invalidate_config_cache()
    monkeypatch.setattr(memory_stores, "_DECLARED_MEMO", None)
    tier = VectorMemoryStore(db_path=directory / "memory.db", embedding_dim=2)
    try:
        tier.init()
        assert tier.algorithm_version == ("v2" if private else "v1")
        if request.param == "named-v1":
            assert tier._lineage == "crew"
        yield tier
    finally:
        tier.close()
        loader._invalidate_config_cache()


def _write(tier, number, key="user.email"):
    assert tier.set_semantic(key, f"address{number}@example.net", 1, "user_explicit") is None


def _accepted(tier, key="user.email"):
    return [
        dict(row)
        for row in tier.db.execute(
            "SELECT * FROM memory_revisions WHERE record_id=? AND status='accepted' ORDER BY id",
            ("key:" + key,),
        )
    ]


def _proposals(tier):
    return [
        tuple(row)
        for row in tier.db.execute(
            "SELECT * FROM memory_revisions WHERE status!='accepted' ORDER BY id"
        )
    ]


def _add_proposals(tier):
    before = tier.get_semantic("user.email")
    assert before is not None
    for status in ("conflict", "pending", "rejected", "future_review"):
        proposal = metadata.propose_conflict(
            tier.db,
            kind="fact",
            record_id="user.email",
            before=before,
            after={**before, "value_json": json.dumps(status + "@example.net")},
            source="consolidation:dm",
        )
        tier.db.execute("UPDATE memory_revisions SET status=? WHERE id=?", (status, proposal))
    tier.db.commit()


def _state(tier):
    return {
        table: [tuple(row) for row in tier.db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
        for table in ("memory_record_meta", "memory_revisions", "memory_events", "sqlite_sequence")
    } | {"facts": tier.get_all_semantic()}


class _LegacySyntaxConnection:
    """Current SQLite connection with the two avoided SQL forms refused."""

    def __init__(self, db):
        object.__setattr__(self, "db", db)
        object.__setattr__(self, "statements", [])

    def __getattr__(self, name):
        return getattr(self.db, name)

    def __setattr__(self, name, value):
        setattr(self.db, name, value)

    def __enter__(self):
        self.db.__enter__()
        return self

    def __exit__(self, *args):
        return self.db.__exit__(*args)

    def execute(self, statement, parameters=()):
        self.statements.append(statement)
        if "ROW_NUMBER" in statement or " OVER " in statement or "ON CONFLICT" in statement:
            raise sqlite3.OperationalError('near "(": syntax error')
        return self.db.execute(statement, parameters)


def test_writes_bound_each_v1_record_but_preserve_proposals_and_v2_history(store):
    for number in range(1, 26):
        _write(store, number)
        _write(store, number, "project.email")
        if number == 5:
            _add_proposals(store)
            # History order must not depend on clock spelling or clock rollback.
            store.db.execute("UPDATE memory_revisions SET created_at='invalid timestamp'")
            store.db.commit()
            proposals = _proposals(store)
    expected = list(range(6 if store.algorithm_version == "v1" else 1, 26))
    for key in ("user.email", "project.email"):
        assert [row["revision"] for row in _accepted(store, key)] == expected
        current = metadata.get_record_metadata(store.db, "key:" + key)
        assert current["revision"] == 25
        assert current["email_addresses"] == ["address25@example.net"]
        assert json.loads(store.get_semantic(key)["value_json"]) == "address25@example.net"
    assert _proposals(store) == proposals

    before = _state(store)
    refused = store.set_semantic(
        "user.email", "stale@example.net", 1, "user_explicit", expected_revision=1
    )
    assert refused == (
        SemanticRejectCode.CONFLICT,
        "Memory changed since it was read; reload before correcting",
    )
    assert _state(store) == before
    last_id = _accepted(store)[-1]["id"]
    store.close()
    store.init()
    assert _state(store) == before
    _write(store, 26)
    assert _accepted(store)[-1]["revision"] == 26
    assert _accepted(store)[-1]["id"] > last_id
    assert _proposals(store) == proposals


def _existing_journal(tier):
    """Build the pre-cap journal through the former unbounded metadata API."""
    _write(tier, 1)
    for number in range(2, 26):
        with tier.db:
            before = dict(
                tier.db.execute("SELECT * FROM semantic_memory WHERE key='user.email'").fetchone()
            )
            tier.db.execute(
                f"UPDATE {tier._sem_rel} SET value_json=? WHERE key=?",
                (json.dumps(f"address{number}@example.net"), "user.email"),
            )
            metadata.sync_record(
                tier.db,
                kind="fact",
                record_id="user.email",
                before=before,
                after=dict(
                    tier.db.execute(
                        "SELECT * FROM semantic_memory WHERE key='user.email'"
                    ).fetchone()
                ),
            )
    assert len(_accepted(tier)) == 25
    _add_proposals(tier)


@pytest.mark.parametrize("store", ["v1"], indirect=True)
def test_populated_pre_metadata_v1_open_avoids_newer_sql(store, monkeypatch):
    with store.db:
        store.db.execute(
            "INSERT INTO semantic_memory "
            "(key, value_json, confidence, source, created_at, updated_at, is_deleted) "
            "VALUES (?, ?, ?, ?, ?, ?, 0)",
            (
                "legacy.email",
                json.dumps("legacy@example.net"),
                1,
                "user_explicit",
                "2026-09-10T00:00:00+00:00",
                "2026-09-10T00:00:00+00:00",
            ),
        )
        store.db.execute("DROP TABLE memory_revisions")
        store.db.execute("DROP TABLE memory_record_meta")
    real_connect = sqlite3.connect
    opened = []

    def compatible_connect(*args, **kwargs):
        compatible = _LegacySyntaxConnection(real_connect(*args, **kwargs))
        opened.append(compatible)
        return compatible

    store.close()
    monkeypatch.setattr(vector_memory.sqlite3, "connect", compatible_connect)
    store.init()

    assert json.loads(store.get_semantic("legacy.email")["value_json"]) == "legacy@example.net"
    current = metadata.get_record_metadata(store.db, "key:legacy.email")
    assert current["revision"] == 1
    assert current["source_ref"] == "user_explicit"
    assert len(opened) == 1
    assert any(
        statement.startswith("UPDATE memory_record_meta") for statement in opened[0].statements
    )
    assert any(
        statement.startswith("INSERT INTO memory_record_meta") for statement in opened[0].statements
    )
    assert any(
        statement.startswith("DELETE FROM memory_revisions") for statement in opened[0].statements
    )


@pytest.mark.parametrize("older_writer_changed", [False, True])
def test_existing_journal_catchup_keeps_latest_evidence_before_reconcile(
    store, older_writer_changed
):
    _existing_journal(store)
    proposals = _proposals(store)
    before = metadata.get_record_metadata(store.db, "key:user.email")
    if older_writer_changed:
        store.db.execute(
            f"UPDATE {store._sem_rel} SET value_json=? WHERE key=?",
            (json.dumps("external@example.net"), "user.email"),
        )
        store.db.commit()
    store.close()
    store.init()
    current = metadata.get_record_metadata(store.db, "key:user.email")
    assert current["revision"] == 25 + older_writer_changed
    if older_writer_changed:
        latest = _accepted(store)[-1]
        assert latest["source"] == "legacy_untracked"
        assert (
            json.loads(json.loads(latest["before_json"])["value_json"]) == "address25@example.net"
        )
        assert json.loads(json.loads(latest["after_json"])["value_json"]) == "external@example.net"
    else:
        assert current == before
    assert len(_accepted(store)) == (20 if store.algorithm_version == "v1" else current["revision"])
    assert _proposals(store) == proposals
    stable = _state(store)
    store.close()
    store.init()
    assert _state(store) == stable


def test_owner_edit_rolls_back_pruning_then_retries_with_original_cas(store):
    for number in range(1, 21):
        _write(store, number)
    _add_proposals(store)
    row = memory_edit.list_records(store, {})["entries"][0]
    preview = memory_edit.preview_edit(
        store,
        "chosen",
        b"secret",
        {
            "selection": {"items": [{key: row[key] for key in ("kind", "id", "revision")}]},
            "operation": {"type": "set", "value": "edited@example.net"},
        },
    )
    before = _state(store)
    # Receipt persistence happens after the content and accepted-history writes.
    store.db.execute(
        "CREATE TRIGGER fail_receipt BEFORE INSERT ON memory_meta "
        "WHEN NEW.key LIKE 'bulk_edit_receipt:%' BEGIN "
        "SELECT RAISE(ABORT, 'receipt unavailable'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="receipt unavailable"):
        memory_edit.apply_edit(store, "chosen", b"secret", preview["preview_id"])
    assert _state(store) == before
    assert not store.db.in_transaction
    store.db.execute("DROP TRIGGER fail_receipt")
    result = memory_edit.apply_edit(store, "chosen", b"secret", preview["preview_id"])
    assert result["changed_count"] == 1
    assert json.loads(store.get_semantic("user.email")["value_json"]) == "edited@example.net"
    assert metadata.get_record_metadata(store.db, "key:user.email")["revision"] == 21
    assert len(_accepted(store)) == (20 if store.algorithm_version == "v1" else 21)
    after = _state(store)
    assert memory_edit.apply_edit(store, "chosen", b"secret", preview["preview_id"]) == result
    assert _state(store) == after


def test_retention_failure_rolls_back_the_normal_writer_and_v2_never_deletes_history(store):
    for number in range(1, 21):
        _write(store, number)
    before = _state(store)
    store.db.execute(
        "CREATE TRIGGER fail_retention AFTER DELETE ON memory_revisions BEGIN "
        "SELECT RAISE(ABORT, 'history unavailable'); END"
    )
    result = store.set_semantic("user.email", "new@example.net", 1, "user_explicit")
    if store.algorithm_version == "v1":
        assert result == (SemanticRejectCode.CONFLICT, "history unavailable")
        assert _state(store) == before
        assert not store.db.in_transaction
    else:
        assert result is None
        assert len(_accepted(store)) == 21
    store.db.execute("DROP TRIGGER fail_retention")


def test_failed_init_pruning_rolls_back_reconciliation_while_v2_still_opens(store):
    _existing_journal(store)
    store.db.execute(
        f"UPDATE {store._sem_rel} SET value_json=? WHERE key=?",
        (json.dumps("external@example.net"), "user.email"),
    )
    store.db.commit()
    before = _state(store)
    store.db.execute(
        "CREATE TRIGGER fail_retention AFTER DELETE ON memory_revisions BEGIN "
        "SELECT RAISE(ABORT, 'history unavailable'); END"
    )
    private = store.algorithm_version == "v2"
    store.close()
    if private:
        store.init()
        assert metadata.get_record_metadata(store.db, "key:user.email")["revision"] == 26
        assert len(_accepted(store)) == 26
        store.db.execute("DROP TRIGGER fail_retention")
    else:
        with pytest.raises(sqlite3.IntegrityError, match="history unavailable"):
            store.init()
        assert store._db is None
        with closing(sqlite3.connect(store._db_path)) as db:
            db.row_factory = sqlite3.Row
            persisted = {
                table: [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                for table in before
                if table != "facts"
            }
            persisted["facts"] = [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM semantic_memory WHERE is_deleted=0 ORDER BY key"
                )
            ]
            assert persisted == before
            db.execute("DROP TRIGGER fail_retention")
            db.commit()
        store.init()
        assert metadata.get_record_metadata(store.db, "key:user.email")["revision"] == 26
        assert len(_accepted(store)) == 20
