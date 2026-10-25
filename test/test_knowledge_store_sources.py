"""Additional KnowledgeStore coverage, folder sources and the watcher loop.

The store's wider surface, the retirement of folders Kiro Crew registered
itself, imported folder sources that wait for confirmation, the watcher's run
loop and dedup cadence, and ``sync_all`` skipping errored sources.
The ``store`` fixtures come from ``knowledge_helpers.py``.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from knowledge_helpers import store_factory_fixture  # noqa: F401
from knowledge_helpers import store_fixture  # noqa: F401

from kiro_crew.knowledge.store import KnowledgeStore
from kiro_crew.knowledge.sync import SyncScheduler
from kiro_crew.testing.wait import await_until, finished

# ---------------------------------------------------------------------------
# 7. KnowledgeStore -- additional coverage
# ---------------------------------------------------------------------------


class TestKnowledgeStoreExtended:
    def test_update_item_fts_sync(self, store):
        item_id = store.add_item("Original", "old content about cats", "doc")
        assert len(store.search_items_fts("cats")) == 1
        store.update_item(item_id, title="Updated", content="new content about dogs")
        assert len(store.search_items_fts("dogs")) == 1
        item = store.get_item(item_id)
        assert item["title"] == "Updated"
        assert item["content"] == "new content about dogs"

    def test_update_item_no_fields(self, store):
        item_id = store.add_item("Doc", "content", "doc")
        before = dict(store.get_item(item_id))
        store.update_item(item_id)
        # A call with nothing to write must not even bump updated_at.
        assert dict(store.get_item(item_id)) == before

    def test_update_item_non_fts_field(self, store):
        item_id = store.add_item("Doc", "content", "doc")
        store.update_item(item_id, status="archived")
        assert store.get_item(item_id)["status"] == "archived"

    def test_get_item_missing(self, store):
        assert store.get_item("nonexistent") is None

    def test_add_source_and_get_by_uri(self, store):
        sid = store.add_source(
            "myfile", "local_file", "/tmp/test.md", properties={"content_hash": "abc123"}
        )
        found = store.get_source_by_uri("/tmp/test.md")
        assert found is not None
        assert found["id"] == sid
        assert store.get_source_by_uri("/tmp/nope") is None

    def test_add_source_persists_sync_status_column(self, store):
        """Insert seeds the COLUMN and stores no second copy in the blob.

        The dashboard reads the COLUMN to pick the row's control (the Confirm
        button renders only for 'pending_confirmation'), so a column stuck at
        the 'pending' default while properties carries 'pending_confirmation'
        makes a folder source unstartable. Callers still STATE the initial
        status in properties; it is lifted onto the column and dropped from the
        blob so the row cannot hold two answers.
        """
        sid = store.add_source(
            "vault",
            "local_folder",
            "/tmp/vault",
            properties={"sync_status": "pending_confirmation"},
        )
        row = store.db.execute(
            "SELECT sync_status, properties FROM sources WHERE id = ?", (sid,)
        ).fetchone()
        assert row["sync_status"] == "pending_confirmation"
        assert "sync_status" not in json.loads(row["properties"])

    def test_add_source_does_not_mutate_the_callers_properties(self, store):
        """Lifting the status out is not allowed to edit the caller's dict."""
        props = {"sync_status": "active", "namespace": "docs"}
        store.add_source("vault", "local_folder", "/tmp/vault-nomut", properties=props)
        assert props == {"sync_status": "active", "namespace": "docs"}

    def test_add_source_sync_status_defaults_to_pending(self, store):
        """A caller that states no sync_status keeps the column's default."""
        sid = store.add_source("f", "local_file", "/tmp/nostatus.md", properties={})
        row = store.db.execute("SELECT sync_status FROM sources WHERE id = ?", (sid,)).fetchone()
        assert row["sync_status"] == "pending"

    def test_add_source_rejects_non_initial_sync_status(self, store):
        """A transient or outcome state in properties never seeds the column.

        The create endpoint passes request-body properties through, so a
        caller-supplied 'syncing' would otherwise persist and make the sync
        endpoint report a conflict forever for a source whose sync never
        started. Only durable states pass; a claim about work that never ran
        falls back to 'pending', and the forged value survives in neither store.
        """
        for forged in ("syncing", "synced", "error", "missing", "garbage"):
            sid = store.add_source(
                "f", "local_file", f"/tmp/forged-{forged}.md", properties={"sync_status": forged}
            )
            row = store.db.execute(
                "SELECT sync_status, properties FROM sources WHERE id = ?", (sid,)
            ).fetchone()
            assert row["sync_status"] == "pending", forged
            assert "sync_status" not in json.loads(row["properties"]), forged

    def test_add_source_accepts_paused_as_an_initial_state(self, store):
        """A source may START paused: it is a durable decision, not a claim.

        A bundle import restores a source the user had paused, and the column is
        now the only place that state can live -- dropping it would silently
        resume scanning a folder the user stopped.
        """
        sid = store.add_source(
            "vault", "local_folder", "/tmp/vault-paused", properties={"sync_status": "paused"}
        )
        row = store.db.execute("SELECT sync_status FROM sources WHERE id = ?", (sid,)).fetchone()
        assert row["sync_status"] == "paused"

    def test_auto_added_source_persists_sync_status_column(self, store):
        """An auto-added source keeps the same single-store invariant.

        The aggregate source the agent's add-document tool creates seeds
        sync_status='active' in properties; the column must carry it or the
        dashboard renders the stale 'pending' control for a source that is
        already active.
        """
        sid = store.add_source(
            "agent-added",
            "agent",
            "agent://",
            properties={"sync_status": "active", "auto_added": True},
        )
        assert sid is not None
        row = store.db.execute(
            "SELECT sync_status, properties FROM sources WHERE id = ?", (sid,)
        ).fetchone()
        assert row["sync_status"] == "active"
        assert "sync_status" not in json.loads(row["properties"])
        assert json.loads(row["properties"])["auto_added"] is True

    def test_migration_repairs_divergent_sync_status_rows(self, store, tmp_path):
        """Reopening a store repairs rows whose column diverged from the JSON.

        Rows inserted while the INSERT paths wrote only the properties JSON
        have a column stuck at 'pending'; the migration copies the JSON state
        over so those sources become startable. Rows a handler already
        transitioned (non-'pending' column) are never touched.
        """
        divergent = str(uuid4())
        live = str(uuid4())
        agree = str(uuid4())
        listprops = str(uuid4())
        forged = str(uuid4())
        # flake-ok: pre-existing naive local stamp written into the test's own rows, moved verbatim; tracked for a de-flake pass
        # nosemgrep: kirocrew.test-naive-local-time -- pre-existing naive stamp for the test's own rows, moved verbatim; tracked for a de-flake pass
        now = datetime.now().isoformat()
        for sid, column, props_json, uri in (
            (divergent, "pending", json.dumps({"sync_status": "pending_confirmation"}), "/tmp/div"),
            (live, "paused", json.dumps({"sync_status": "active"}), "/tmp/live"),
            (agree, "pending", json.dumps({}), "/tmp/agree"),
            # Imported/legacy rows can hold non-object JSON; the repair must
            # skip them instead of crashing store initialization.
            (listprops, "pending", "[]", "/tmp/listprops"),
            # A lifecycle state in the JSON is never a valid initial state and
            # must not be copied over (a forged 'syncing' would lock the sync
            # endpoint into reporting a conflict).
            (forged, "pending", json.dumps({"sync_status": "syncing"}), "/tmp/forged"),
        ):
            store.db.execute(
                "INSERT INTO sources (id, name, source_type, uri, properties, sync_status, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (sid, "s", "local_folder", uri, props_json, column, now, now),
            )
        store.db.commit()
        store.close()

        reopened = KnowledgeStore(str(tmp_path / "test.db"))
        try:
            rows = {
                r["id"]: r["sync_status"]
                for r in reopened.db.execute("SELECT id, sync_status FROM sources").fetchall()
            }
            assert rows[divergent] == "pending_confirmation"
            assert rows[live] == "paused"
            assert rows[agree] == "pending"
            assert rows[listprops] == "pending"
            assert rows[forged] == "pending"
        finally:
            reopened.close()

    def test_migration_skips_a_row_whose_properties_moved_mid_repair(self, store, tmp_path):
        """A properties-only write landing mid-pass wins over the snapshot.

        The pass reads both copies, then writes. A pre-column
        ``SyncScheduler._record_failure`` moved the properties copy WITHOUT the
        column, so comparing only the column would let the repair stamp
        'pending_confirmation' onto a row whose blob now reads 'error' -- the
        dashboard would offer Confirm for a source the scheduler has given up on.
        Binding the blob as read refuses every write for that row, including the
        retire, so nothing is lost; the next open sees the settled state and
        converges it.
        """
        import sqlite3

        db_path = str(tmp_path / "test.db")
        sid = str(uuid4())
        # flake-ok: pre-existing naive local stamp written into the test's own rows, moved verbatim; tracked for a de-flake pass
        # nosemgrep: kirocrew.test-naive-local-time -- pre-existing naive stamp for the test's own rows, moved verbatim; tracked for a de-flake pass
        now = datetime.now().isoformat()
        store.db.execute(
            "INSERT INTO sources (id, name, source_type, uri, properties, sync_status, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                sid,
                "s",
                "local_folder",
                "/tmp/race",
                json.dumps({"sync_status": "pending_confirmation"}),
                "pending",
                now,
                now,
            ),
        )
        store.db.commit()
        store.close()

        real_loads = json.loads
        fired: list[bool] = []

        def failure_lands_mid_scan(raw):
            parsed = real_loads(raw)
            # Fire once, only for the row under test: the pass parses each
            # candidate row between its SELECT and its UPDATE.
            if (
                not fired
                and isinstance(parsed, dict)
                and parsed.get("sync_status") == "pending_confirmation"
            ):
                fired.append(True)
                conn = sqlite3.connect(db_path, timeout=30)
                try:
                    conn.execute(
                        "UPDATE sources SET properties = ? WHERE id = ?",
                        (json.dumps({"sync_status": "error", "consecutive_failures": 3}), sid),
                    )
                    conn.commit()
                finally:
                    conn.close()
            return parsed

        with patch("kiro_crew.knowledge.store.json.loads", failure_lands_mid_scan):
            reopened = KnowledgeStore(db_path)
        try:
            assert fired, "the mid-scan write never landed; the test proves nothing"
            row = reopened.db.execute(
                "SELECT sync_status, properties FROM sources WHERE id = ?", (sid,)
            ).fetchone()
            # Never the stale snapshot: the row the scheduler gave up on must not
            # come back offering Confirm.
            assert row["sync_status"] == "pending"
            # The refused retire left the copy intact, so nothing was dropped.
            assert json.loads(row["properties"])["sync_status"] == "error"
        finally:
            reopened.close()

        # The next open sees a row nobody is racing and retires the copy. The
        # blob's 'error' is a lifecycle value, so it is dropped rather than
        # promoted: it cannot be ordered against the column, and the scheduler's
        # own failure count re-marks the source on its next failed attempt.
        settled = KnowledgeStore(db_path)
        try:
            row = settled.db.execute(
                "SELECT sync_status, properties FROM sources WHERE id = ?", (sid,)
            ).fetchone()
            assert row["sync_status"] == "pending"
            assert "sync_status" not in json.loads(row["properties"])
            assert json.loads(row["properties"])["consecutive_failures"] == 3
        finally:
            settled.close()

    def test_migration_lifts_a_legacy_json_only_error_onto_the_column(self, store, tmp_path):
        """The copy is retired, and a LIFECYCLE value in it is never promoted.

        Only an INITIAL state is repaired, and only onto a column still at its
        un-written default. A blob 'error' is left where it is: it cannot be
        ordered against the column, so promoting it would mark a recovered source
        errored with no copy left to correct it.
        """
        divergent = str(uuid4())
        legacy_error = str(uuid4())
        healthy = str(uuid4())
        recovered = str(uuid4())
        listprops = str(uuid4())
        # flake-ok: pre-existing naive local stamp written into the test's own rows, moved verbatim; tracked for a de-flake pass
        # nosemgrep: kirocrew.test-naive-local-time -- pre-existing naive stamp for the test's own rows, moved verbatim; tracked for a de-flake pass
        now = datetime.now().isoformat()
        for sid, column, props_json, uri in (
            (
                divergent,
                "pending",
                json.dumps({"sync_status": "pending_confirmation"}),
                "/tmp/divergent",
            ),
            (
                legacy_error,
                "pending",
                json.dumps({"sync_status": "error", "consecutive_failures": 3}),
                "/tmp/legacy",
            ),
            (healthy, "synced", json.dumps({"mtime": 1}), "/tmp/healthy"),
            # A pre-column failure recorded in the blob, then a successful
            # re-ingest that wrote the COLUMN only. The column is the newer
            # answer and must survive untouched.
            (recovered, "synced", json.dumps({"sync_status": "error"}), "/tmp/recovered"),
            (listprops, "pending", "[]", "/tmp/listprops"),
        ):
            # local_folder: the reopen also runs the orphan cleanup, which
            # deletes item-less sources of every other type.
            store.db.execute(
                "INSERT INTO sources (id, name, source_type, uri, properties, sync_status, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (sid, "s", "local_folder", uri, props_json, column, now, now),
            )
        store.db.commit()
        store.close()

        reopened = KnowledgeStore(str(tmp_path / "test.db"))
        try:
            rows = {
                r["id"]: dict(r)
                for r in reopened.db.execute(
                    "SELECT id, sync_status, properties FROM sources"
                ).fetchall()
            }
            # An initial state IS repaired onto an un-written column.
            assert rows[divergent]["sync_status"] == "pending_confirmation"
            # A lifecycle value is NOT promoted, whatever the column reads.
            assert rows[legacy_error]["sync_status"] == "pending"
            assert rows[recovered]["sync_status"] == "synced"
            assert rows[healthy]["sync_status"] == "synced"
            assert rows[listprops]["sync_status"] == "pending"
            # The second store is RETIRED, not left for the next open to re-read.
            for sid, r in rows.items():
                parsed = json.loads(r["properties"] or "{}")
                if isinstance(parsed, dict):
                    assert "sync_status" not in parsed, sid
            # The rest of the blob survives the strip.
            assert json.loads(rows[legacy_error]["properties"])["consecutive_failures"] == 3
        finally:
            reopened.close()

    def test_migration_does_not_re_error_a_source_that_has_since_synced(self, store, tmp_path):
        """A recovered source survives the upgrade, whenever it recovered.

        Ingestion's success writers are column-only -- they never touch
        properties -- so a legacy blob-'error' row that syncs keeps its blob copy.
        Both orderings must leave the healthy column alone: a recovery that landed
        BEFORE the first open under this change (the copy is still present when
        the migration first runs) and one that lands after it.
        """
        before = str(uuid4())
        after = str(uuid4())
        # flake-ok: pre-existing naive local stamp written into the test's own rows, moved verbatim; tracked for a de-flake pass
        # nosemgrep: kirocrew.test-naive-local-time -- pre-existing naive stamp for the test's own rows, moved verbatim; tracked for a de-flake pass
        now = datetime.now().isoformat()
        for sid, column, uri in (
            (before, "synced", "/tmp/before"),
            (after, "pending", "/tmp/after"),
        ):
            store.db.execute(
                "INSERT INTO sources (id, name, source_type, uri, properties, sync_status, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    sid,
                    "s",
                    "local_folder",
                    uri,
                    json.dumps({"sync_status": "error", "consecutive_failures": 3}),
                    column,
                    now,
                    now,
                ),
            )
        store.db.commit()
        store.close()

        first = KnowledgeStore(str(tmp_path / "test.db"))
        try:
            rows = {
                r["id"]: r["sync_status"]
                for r in first.db.execute("SELECT id, sync_status FROM sources").fetchall()
            }
            # Recovered before the upgrade: never overwritten.
            assert rows[before] == "synced"
            assert rows[after] == "pending"
            # A successful re-sync, written the way ingestion writes it: column
            # only, properties untouched.
            first.db.execute("UPDATE sources SET sync_status = 'synced' WHERE id = ?", (after,))
            first.db.commit()
        finally:
            first.close()

        second = KnowledgeStore(str(tmp_path / "test.db"))
        try:
            rows = {
                r["id"]: r["sync_status"]
                for r in second.db.execute("SELECT id, sync_status FROM sources").fetchall()
            }
            assert rows[before] == "synced"
            assert rows[after] == "synced"
        finally:
            second.close()

    def test_update_source_drops_a_properties_borne_status(self, store):
        """A status written through properties is dropped, not stored.

        This is the seam that makes the column the only store: a legacy row's
        second copy disappears the first time anything writes its properties,
        and no caller can create a new one.
        """
        sid = store.add_source("f", "local_file", "/tmp/lift.md")
        store.update_source(sid, properties={"sync_status": "missing", "mtime": 7})
        row = store.db.execute(
            "SELECT sync_status, properties FROM sources WHERE id = ?", (sid,)
        ).fetchone()
        props = json.loads(row["properties"])
        assert "sync_status" not in props
        assert props["mtime"] == 7
        # Dropped, NOT applied: see the next test for why that matters.
        assert row["sync_status"] == "pending"

    def test_update_source_does_not_let_a_stale_blob_move_the_column(self, store):
        """An unrelated properties write must not resurrect a legacy status.

        A row written by the pre-column watcher carries 'missing' in its blob.
        The watcher re-reads that blob to persist mtime/content_hash after
        re-ingesting the file, so a seam that APPLIED the blob's status would
        stamp 'missing' back onto a source that had just been re-ingested.
        """
        sid = store.add_source("f", "local_file", "/tmp/stale.md")
        store.db.execute(
            "UPDATE sources SET properties = ?, sync_status = 'synced' WHERE id = ?",
            (json.dumps({"sync_status": "missing", "mtime": 1}), sid),
        )
        store.db.commit()

        legacy_blob = json.loads(
            store.db.execute("SELECT properties FROM sources WHERE id = ?", (sid,)).fetchone()[
                "properties"
            ]
        )
        legacy_blob["mtime"] = 2
        store.update_source(sid, properties=json.dumps(legacy_blob))

        row = store.db.execute(
            "SELECT sync_status, properties FROM sources WHERE id = ?", (sid,)
        ).fetchone()
        assert row["sync_status"] == "synced"
        assert json.loads(row["properties"]) == {"mtime": 2}

    def test_update_source_drops_a_status_out_of_a_serialized_blob(self, store):
        """Callers that hand over pre-serialized JSON get the same treatment."""
        sid = store.add_source("f", "local_file", "/tmp/lift-str.md")
        store.update_source(sid, properties=json.dumps({"sync_status": "error", "mtime": 3}))
        row = store.db.execute("SELECT properties FROM sources WHERE id = ?", (sid,)).fetchone()
        assert json.loads(row["properties"]) == {"mtime": 3}

    def test_update_source_writes_an_explicit_status(self, store):
        """The kwarg is the only channel a transition may use."""
        sid = store.add_source("f", "local_file", "/tmp/lift-both.md")
        store.update_source(sid, properties={"sync_status": "missing"}, sync_status="active")
        row = store.db.execute(
            "SELECT sync_status, properties FROM sources WHERE id = ?", (sid,)
        ).fetchone()
        assert row["sync_status"] == "active"
        assert "sync_status" not in json.loads(row["properties"])

    def test_update_source_leaves_a_non_object_blob_alone(self, store):
        """A blob that is not a JSON object is stored as given, not rewritten."""
        sid = store.add_source("f", "local_file", "/tmp/lift-list.md")
        store.update_source(sid, properties="[]")
        row = store.db.execute("SELECT properties FROM sources WHERE id = ?", (sid,)).fetchone()
        assert row["properties"] == "[]"

    def test_update_source(self, store):
        sid = store.add_source("f", "local_file", "/tmp/f.md")
        store.update_source(sid, last_synced="2026-01-01T00:00:00")
        row = store.db.execute("SELECT last_synced FROM sources WHERE id = ?", (sid,)).fetchone()
        assert row["last_synced"] == "2026-01-01T00:00:00"

    def test_update_source_no_fields(self, store):
        sid = store.add_source("f", "local_file", "/tmp/f2.md")
        query = "SELECT * FROM sources WHERE id = ?"
        before = dict(store.db.execute(query, (sid,)).fetchone())
        store.update_source(sid)
        assert dict(store.db.execute(query, (sid,)).fetchone()) == before

    def test_add_source_location(self, store):
        sid = store.add_source("f", "local_file", "/tmp/loc.md")
        item_id = store.add_item("Doc", "content", "doc", source_id=sid)
        store.add_source_location(item_id, sid, chunk_range="0-10", section_title="Intro")
        rows = store.db.execute(
            "SELECT * FROM source_locations WHERE item_id = ?", (item_id,)
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["section_title"] == "Intro"

    def test_get_neighbors_depth(self, store):
        e1 = store.add_entity("A", "svc")
        e2 = store.add_entity("B", "svc")
        e3 = store.add_entity("C", "svc")
        store.add_entity_relation(e1, e2, "calls")
        store.add_entity_relation(e2, e3, "calls")
        n1 = store.get_neighbors(e1, depth=1)
        assert {n["id"] for n in n1} == {e2}
        n2 = store.get_neighbors(e1, depth=2)
        assert {n["id"] for n in n2} == {e2, e3}

    def test_get_neighbors_bidirectional(self, store):
        e1 = store.add_entity("A", "svc")
        e2 = store.add_entity("B", "svc")
        store.add_entity_relation(e2, e1, "calls")
        # e1 has no outgoing but has incoming from e2
        neighbors = store.get_neighbors(e1, depth=1)
        assert {n["id"] for n in neighbors} == {e2}

    def test_find_entity_by_alias(self, store):
        store.add_entity("DynamoDB", "technology", aliases=["ddb", "dynamo"])
        found = store.find_entity("ddb")
        assert found is not None
        assert found["name"] == "DynamoDB"

    def test_find_entity_not_found(self, store):
        assert store.find_entity("nonexistent") is None

    def test_export_item_with_entities(self, store):
        sid = store.add_source("f", "local_file", "/tmp/exp.md")
        item_id = store.add_item("Doc", "content", "doc", source_id=sid)
        e1 = store.add_entity("Svc", "service")
        e2 = store.add_entity("DB", "technology")
        store.add_mention(item_id, e1)
        store.add_mention(item_id, e2)
        store.add_entity_relation(e1, e2, "uses", source_item_id=item_id)
        store.add_source_location(item_id, sid, section_title="Main")
        bundle = store.export_item(item_id)
        assert bundle["items"][0]["id"] == item_id
        assert len(bundle["entities"]) == 2
        assert len(bundle["relations"]) == 1
        assert len(bundle["source_locations"]) == 1
        assert len(bundle["mentions"]) == 2
        assert bundle["sources"][0]["id"] == sid

    def test_export_item_missing(self, store):
        assert store.export_item("nope") == {}

    def test_export_item_without_source(self, store):
        item_id = store.add_item("Doc", "content", "doc")
        bundle = store.export_item(item_id)
        assert bundle["items"][0]["id"] == item_id
        assert bundle["sources"] == []

    def test_export_item_roundtrips_into_a_fresh_instance(self, store_factory):
        s1 = store_factory("export_item_src.db")
        sid = s1.add_source("f", "local_file", "/tmp/exp2.md")
        item_id = s1.add_item("Doc", "content", "doc", source_id=sid)
        eid = s1.add_entity("Svc", "service")
        s1.add_mention(item_id, eid)
        s1.add_source_location(item_id, sid, section_title="Main")
        bundle = s1.export_item(item_id)

        s2 = store_factory("export_item_dst.db")
        result = s2.import_bundle(bundle)
        assert result["items_imported"] == 1
        assert s2.get_item(item_id)["title"] == "Doc"
        mentions = s2.db.execute("SELECT * FROM mentions WHERE item_id = ?", (item_id,)).fetchall()
        assert len(mentions) == 1
        assert mentions[0]["entity_id"] == eid

    def test_export_item_excludes_relations_whose_other_endpoint_is_not_exported(self, store):
        """A relation touching an entity outside this item's mentions must not
        ride along -- the receiving store never gets that entity's row, so
        re-importing the relation would violate entity_relations' FK on
        source_id/target_id."""
        item_id = store.add_item("Doc", "content", "doc")
        mentioned = store.add_entity("Svc", "service")
        outside = store.add_entity("Unrelated", "service")
        store.add_mention(item_id, mentioned)
        store.add_entity_relation(mentioned, outside, "calls")
        bundle = store.export_item(item_id)
        assert bundle["relations"] == []
        assert {e["id"] for e in bundle["entities"]} == {mentioned}

    def test_export_item_excludes_relations_owned_by_a_different_item(self, store):
        """A relation recorded under another item's observation (source_item_id
        set to that other item) must not ride along either -- re-importing it
        here references an item that was never exported alongside it."""
        item_id = store.add_item("Doc", "content", "doc")
        other_item_id = store.add_item("Other", "content", "doc")
        e1 = store.add_entity("A", "service")
        e2 = store.add_entity("B", "service")
        store.add_mention(item_id, e1)
        store.add_mention(item_id, e2)
        store.add_entity_relation(e1, e2, "calls", source_item_id=other_item_id)
        bundle = store.export_item(item_id)
        assert bundle["relations"] == []

    def test_export_item_with_a_cross_referencing_relation_roundtrips_cleanly(self, store_factory):
        """End-to-end reproduction of the FK bug: exporting an item whose
        mentioned entity has a relation to an unexported entity must still
        re-import cleanly (the offending relation is simply dropped, not
        carried along to break the import)."""
        s1 = store_factory("cross_ref_src.db")
        item_id = s1.add_item("Doc", "content", "doc")
        mentioned = s1.add_entity("Svc", "service")
        outside = s1.add_entity("Unrelated", "service")
        s1.add_mention(item_id, mentioned)
        s1.add_entity_relation(mentioned, outside, "calls")
        bundle = s1.export_item(item_id)

        s2 = store_factory("cross_ref_dst.db")
        result = s2.import_bundle(bundle)
        assert result["items_imported"] == 1
        assert result["relations_rebuilt"] == 0
        assert s2.get_item(item_id) is not None

    def test_delete_item_cleans_mentions(self, store):
        item_id = store.add_item("Doc", "content", "doc")
        eid = store.add_entity("Svc", "service")
        store.add_mention(item_id, eid, context="test")
        sid = store.add_source("f", "local_file", "/tmp/del.md")
        store.add_source_location(item_id, sid)
        store.delete_item(item_id)
        assert (
            store.db.execute("SELECT * FROM mentions WHERE item_id = ?", (item_id,)).fetchone()
            is None
        )
        assert (
            store.db.execute(
                "SELECT * FROM source_locations WHERE item_id = ?", (item_id,)
            ).fetchone()
            is None
        )

    def test_get_stats(self, store):
        store.add_item("A", "a", "doc")
        store.add_entity("E", "svc")
        stats = store.get_stats()
        assert stats["items"] == 1
        assert stats["entities"] == 1
        assert stats["relations"] == 0
        assert stats["sources"] == 0

    def test_graph_has_node(self, store):
        eid = store.add_entity("Svc", "service")
        assert store.graph.has_node(eid)
        assert not store.graph.has_node("fake")

    def test_graph_degree(self, store):
        e1 = store.add_entity("A", "svc")
        e2 = store.add_entity("B", "svc")
        store.add_entity_relation(e1, e2, "calls")
        assert store.graph.degree(e1) == 1
        assert store.graph.degree(e2) == 1

    def test_load_graph_on_reopen(self, tmp_path):
        db_path = str(tmp_path / "reload.db")
        s1 = KnowledgeStore(db_path)
        e1 = s1.add_entity("A", "svc")
        e2 = s1.add_entity("B", "svc")
        s1.add_entity_relation(e1, e2, "calls")
        s1.close()
        s2 = KnowledgeStore(db_path)
        assert s2.graph.has_node(e1)
        assert s2.graph.has_edge(e1, e2)
        s2.close()


# ---------------------------------------------------------------------------
# 7b. Retirement of folders Kiro Crew registered itself
# ---------------------------------------------------------------------------


class TestRetireAutoRegisteredFolders:
    """A folder row left behind by the removed auto-registration paths.

    Those paths registered a directory nobody named, and the sweep re-validated its
    containment before every scan. Both are gone, so such a row must not be walked at
    all until the user adopts it. The refusal lives in ``FolderWatcher.scan_source``
    because that is the single funnel every scan goes through -- the watcher's sweep
    and both dashboard endpoints -- and deliberately NOT in the store constructor,
    where taking the write lock would stall the event loop at startup.
    """

    @staticmethod
    def _add(store, props, status="active", uri="/tmp/legacy", stype="local_folder"):
        sid = store.add_source("s", stype, uri, properties={**props, "sync_status": status})
        return sid

    @staticmethod
    def _row(store, sid):
        r = store.db.execute(
            "SELECT sync_status, properties FROM sources WHERE id = ?", (sid,)
        ).fetchone()
        return r["sync_status"], json.loads(r["properties"] or "{}")

    @staticmethod
    def _watcher(store):
        from kiro_crew.knowledge.watcher import KnowledgeWatcher

        return KnowledgeWatcher(store=store, pipeline=object(), interval=1)

    # -- the funnel ---------------------------------------------------------

    @pytest.mark.asyncio
    async def test_a_scan_of_an_auto_registered_source_is_refused_and_retires_it(
        self, store, tmp_path
    ):
        """The gate is in scan_source, so every caller is covered by construction.

        A database written before the removal holds such a row, and ``import_bundle``
        restores one verbatim into a running gateway, so the row can appear at any
        time -- not only at startup.
        """
        folder = tmp_path / "legacy"
        folder.mkdir()
        (folder / "note.md").write_text("# legacy\n\nbody\n" * 40)
        sid = self._add(store, {"auto_added": True}, uri=str(folder))
        fw = self._watcher(store)._folder_watcher
        fw._do_scan = AsyncMock()  # type: ignore[method-assign]

        stats = await fw.scan_source(
            {
                "id": sid,
                "uri": str(folder),
                "source_type": "local_folder",
                "properties": json.dumps({"auto_added": True}),
            }
        )

        fw._do_scan.assert_not_awaited()
        assert stats["unconfirmed"] is True
        status, props = self._row(store, sid)
        assert status == "pending_confirmation"
        assert props["auto_registration_retired"] is True
        assert props["auto_added"] is True  # redaction + the gate still read it

    @pytest.mark.asyncio
    async def test_the_refusal_is_audited(self, store, tmp_path):
        """A refusal to walk a tree is a permission decision, so it is auditable.

        The paths this replaced logged both their auto-add and their scan-denied
        decisions, and this file's two TOCTOU refusals log theirs, so a silent refusal
        would be the one permission outcome with no record.
        """
        folder = tmp_path / "audited"
        folder.mkdir()
        sid = self._add(store, {"auto_added": True}, uri=str(folder))
        fw = self._watcher(store)._folder_watcher
        fw._do_scan = AsyncMock()  # type: ignore[method-assign]
        events: list[dict] = []

        class _Sel:
            @staticmethod
            def log_tool_invocation(**kw):
                events.append(kw)

        with patch("kiro_crew.knowledge.folder_watcher.sel", lambda: _Sel()):
            await fw.scan_source(
                {
                    "id": sid,
                    "uri": str(folder),
                    "source_type": "local_folder",
                    "properties": json.dumps({"auto_added": True}),
                }
            )

        denied = [e for e in events if e.get("tool_name") == "knowledge.source.scan_denied"]
        assert len(denied) == 1, events
        assert denied[0]["outcome"] == "denied"
        assert f"source_id={sid}" in denied[0]["resources"]
        assert "reason=auto_registered_unconfirmed" in denied[0]["resources"]
        # Records whether the row also moved, so a refusal that could not take the
        # write lock is distinguishable in the log from one that retired the row.
        assert "retired=True" in denied[0]["resources"]

    @pytest.mark.asyncio
    async def test_a_folder_the_user_added_scans_normally(self, store, tmp_path):
        folder = tmp_path / "mine"
        folder.mkdir()
        sid = self._add(store, {}, uri=str(folder))
        fw = self._watcher(store)._folder_watcher
        fw._do_scan = AsyncMock(return_value={"new": 0})  # type: ignore[method-assign]

        await fw.scan_source(
            {"id": sid, "uri": str(folder), "source_type": "local_folder", "properties": "{}"}
        )

        fw._do_scan.assert_awaited_once()
        assert self._row(store, sid)[0] == "active"

    @pytest.mark.asyncio
    async def test_an_adopted_source_scans_normally(self, store, tmp_path):
        """Once the marker is on the row, the gate lets it through."""
        folder = tmp_path / "adopted"
        folder.mkdir()
        props = {"auto_added": True, "auto_registration_retired": True}
        sid = self._add(store, props, uri=str(folder))
        fw = self._watcher(store)._folder_watcher
        fw._do_scan = AsyncMock(return_value={"new": 0})  # type: ignore[method-assign]

        await fw.scan_source(
            {
                "id": sid,
                "uri": str(folder),
                "source_type": "local_folder",
                "properties": json.dumps(props),
            }
        )

        fw._do_scan.assert_awaited_once()
        assert self._row(store, sid)[0] == "active"

    @pytest.mark.asyncio
    async def test_the_sweep_refuses_it_too(self, store, tmp_path):
        """The sweep reaches the same gate rather than carrying its own copy."""
        folder = tmp_path / "swept"
        folder.mkdir()
        sid = self._add(store, {"auto_added": True}, uri=str(folder))
        watcher = self._watcher(store)
        watcher._folder_watcher._do_scan = AsyncMock()  # type: ignore[method-assign]

        await watcher._scan()

        watcher._folder_watcher._do_scan.assert_not_awaited()
        assert self._row(store, sid)[0] == "pending_confirmation"

    # -- the marker contract ------------------------------------------------

    @pytest.mark.parametrize(
        "props",
        [
            {"auto_added": "false"},  # truthy string: must NOT look auto-added
            {"auto_added": 1},  # not a real boolean
            {"auto_added": True, "auto_registration_retired": "false"},  # fail-OPEN if truthy
            {"auto_added": True, "auto_registration_retired": 0},
        ],
    )
    def test_markers_are_compared_strictly(self, props):
        """``properties`` is user-editable JSON and arrives through import_bundle.

        ``"false"`` is truthy, and on the retired marker that direction is fail-open:
        the row would read as adopted and skip the refusal entirely.
        """
        from kiro_crew.knowledge.store import is_auto_registered

        expected = (
            props.get("auto_added") is True and props.get("auto_registration_retired") is not True
        )
        assert is_auto_registered(props) is expected

    def test_a_real_legacy_row_is_recognised(self):
        from kiro_crew.knowledge.store import is_auto_registered

        assert is_auto_registered({"auto_added": True}) is True
        assert is_auto_registered({"auto_added": True, "auto_registration_retired": True}) is False

    # -- the store primitive ------------------------------------------------

    def test_a_paused_row_keeps_its_pause_and_is_still_marked(self, store):
        """Pausing already stops the scan; overwriting it would lose user state."""
        sid = self._add(store, {"auto_added": True}, status="paused", uri="/tmp/paused")
        assert store.retire_auto_registered_folder(sid) is True
        status, props = self._row(store, sid)
        assert status == "paused"
        assert props["auto_registration_retired"] is True

    def test_the_aggregate_sources_are_out_of_scope(self, store):
        """They carry the marker and walk nothing, so they must not be gated."""
        agent = self._add(store, {"auto_added": True}, uri="agent://", stype="agent")
        artifact = self._add(store, {"auto_added": True}, uri="artifact://", stype="artifact")
        for sid in (agent, artifact):
            assert store.retire_auto_registered_folder(sid) is False
            status, props = self._row(store, sid)
            assert status == "active", sid
            assert "auto_registration_retired" not in props, sid

    def test_retiring_is_idempotent(self, store):
        sid = self._add(store, {"auto_added": True}, uri="/tmp/once")
        assert store.retire_auto_registered_folder(sid) is True
        # Adopted by the user in between; a second call must not undo that.
        store.db.execute("UPDATE sources SET sync_status = 'active' WHERE id = ?", (sid,))
        store.db.commit()
        assert store.retire_auto_registered_folder(sid) is False
        assert self._row(store, sid)[0] == "active"

    def test_an_unreadable_properties_blob_is_not_retired_and_does_not_raise(self, store):
        sid = self._add(store, {}, uri="/tmp/broken")
        store.db.execute("UPDATE sources SET properties = ? WHERE id = ?", ("{not json", sid))
        store.db.commit()
        assert store.retire_auto_registered_folder(sid) is False

    def test_an_ingestion_write_cannot_land_inside_the_retirement(self, store, tmp_path):
        """The retirement holds the write lock across its read and its write.

        Ingestion writes ``sync_status = 'synced'`` straight onto the row, so without
        the lock that write lands in between and the paused branch stamps the retired
        marker onto a row that has just become scannable again -- permanently, since
        the marker is what stops a later retirement.

        The racer opens through ``store.sqlite3`` -- the module the store itself bound
        -- because that is the library the real writer uses. A stdlib ``sqlite3``
        connection does not contend with a ``pysqlite3`` one on this database
        (measured: both sides took BEGIN IMMEDIATE at once), so a racer opened that way
        would land whether or not a lock is held, and the test would prove nothing.
        """
        from kiro_crew.knowledge import store as store_mod

        sqlite = store_mod.sqlite3

        db_path = str(tmp_path / "test.db")
        sid = self._add(store, {"auto_added": True}, status="paused", uri="/tmp/raced")
        real_loads = json.loads
        outcome: list[str] = []

        def ingestion_writes_mid_pass(raw):
            parsed = real_loads(raw)
            if not outcome and isinstance(parsed, dict) and parsed.get("auto_added"):
                conn = sqlite.connect(db_path, timeout=0.2, isolation_level=None)
                try:
                    conn.execute("UPDATE sources SET sync_status = 'synced' WHERE id = ?", (sid,))
                    outcome.append("landed")
                except sqlite.OperationalError as exc:
                    outcome.append(f"refused: {exc}")
                finally:
                    conn.close()
            return parsed

        with patch("kiro_crew.knowledge.store.json.loads", ingestion_writes_mid_pass):
            store.retire_auto_registered_folder(sid)

        assert outcome, "the mid-pass write never ran; the test proves nothing"
        assert outcome[0].startswith("refused"), outcome[0]
        status, props = self._row(store, sid)
        assert status == "paused"
        assert props["auto_registration_retired"] is True

    def test_a_lock_it_cannot_take_defers_instead_of_raising(self, store, tmp_path):
        """A busy database must not turn a refusal into an exception.

        The scan is refused either way -- the funnel's ``continue`` does not depend on
        this returning True -- so deferring costs nothing and raising would surface as
        a failed scan the user cannot act on.
        """
        from kiro_crew.knowledge import store as store_mod

        sqlite = store_mod.sqlite3

        db_path = str(tmp_path / "test.db")
        sid = self._add(store, {"auto_added": True}, uri="/tmp/busy")
        holder = sqlite.connect(db_path, timeout=30, isolation_level=None)
        try:
            holder.execute("BEGIN IMMEDIATE")
            store.db.execute("PRAGMA busy_timeout=50")
            assert store.retire_auto_registered_folder(sid) is False
            status, props = self._row(store, sid)
            assert status == "active"
            assert "auto_registration_retired" not in props
        finally:
            store.db.execute("PRAGMA busy_timeout=10000")
            holder.execute("ROLLBACK")
            holder.close()


class TestImportedFolderSourcesWaitForConfirmation:
    """``import_bundle`` may not hand the Library a directory that scans on arrival.

    The status allowlist admits ``active``, and a bundle is untrusted input that names
    its own ``uri``, so trusting the claimed status would let an imported row point at
    any readable directory and have the next sweep walk it and spend extraction calls
    on it. Importing a bundle is one decision; each directory inside it is not.
    """

    @staticmethod
    def _bundle(source):
        return {"version": 1, "sources": [source], "items": [], "entities": [], "relations": []}

    @staticmethod
    def _status(store, sid):
        return store.db.execute("SELECT sync_status FROM sources WHERE id = ?", (sid,)).fetchone()[
            0
        ]

    @pytest.mark.parametrize("claimed", ["active", "synced", "pending", "error", None])
    @pytest.mark.parametrize("stype", ["local_folder", "obsidian_vault"])
    def test_a_restored_walking_source_waits(self, store, tmp_path, claimed, stype):
        """Every status the sweep would scan is narrowed, including 'pending'."""
        sid = str(uuid4())
        src = {
            "id": sid,
            "name": "imported",
            "source_type": stype,
            "uri": str(tmp_path / "anywhere"),
            "properties": "{}",
        }
        if claimed is not None:
            src["sync_status"] = claimed
        store.import_bundle(self._bundle(src))
        assert self._status(store, sid) == "pending_confirmation"

    @pytest.mark.parametrize("stype", ["local_folder", "obsidian_vault"])
    def test_a_restored_paused_source_keeps_its_pause(self, store, tmp_path, stype):
        """Pausing already stops the scan, and it is the user's own decision.

        Overwriting it with a confirmation prompt would lose that state to protect
        against nothing -- the same reasoning the retirement path uses for a paused
        row.
        """
        sid = str(uuid4())
        store.import_bundle(
            self._bundle(
                {
                    "id": sid,
                    "name": "paused",
                    "source_type": stype,
                    "uri": str(tmp_path / "paused"),
                    "sync_status": "paused",
                    "properties": "{}",
                }
            )
        )
        assert self._status(store, sid) == "paused"

    def test_the_markers_do_not_change_that(self, store, tmp_path):
        """Not even a bundle claiming the source was already adopted.

        This is the shape the review raised: both markers true and an active status.
        The status is refused on the source TYPE, so the markers never get a say.
        """
        sid = str(uuid4())
        store.import_bundle(
            self._bundle(
                {
                    "id": sid,
                    "name": "adopted?",
                    "source_type": "local_folder",
                    "uri": str(tmp_path / "claimed"),
                    "sync_status": "active",
                    "properties": json.dumps(
                        {"auto_added": True, "auto_registration_retired": True}
                    ),
                }
            )
        )
        assert self._status(store, sid) == "pending_confirmation"

    @pytest.mark.parametrize("stype", ["local_file", "agent", "artifact", "wiki"])
    def test_a_source_that_walks_nothing_keeps_its_restored_status(self, store, stype):
        """The narrowing is scoped to walking sources, not to imports in general.

        An aggregate or single-file source has no tree to descend, so restoring it as
        active costs nothing and forcing confirmation on it would strand content the
        bundle legitimately carries.
        """
        sid = str(uuid4())
        store.import_bundle(
            self._bundle(
                {
                    "id": sid,
                    "name": "agg",
                    "source_type": stype,
                    "uri": f"{stype}://x",
                    "sync_status": "active",
                    "properties": "{}",
                }
            )
        )
        assert self._status(store, sid) == "active"


class TestWatcherRunLoop:
    """``KnowledgeWatcher.start`` / ``stop``: the background loop's own contract."""

    @staticmethod
    def _watcher(store, interval=0.01):
        from kiro_crew.knowledge.watcher import KnowledgeWatcher

        return KnowledgeWatcher(store=store, pipeline=object(), interval=interval)

    @pytest.mark.asyncio
    async def test_a_failing_sweep_does_not_kill_the_loop(self, store):
        """One bad sweep must cost one interval, not the whole watcher.

        The loop is the only thing that ever scans, so an exception escaping it leaves
        every source unscanned until the gateway restarts -- with nothing reporting
        that it stopped.
        """
        w = self._watcher(store)
        calls = []

        async def _scan():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("first sweep exploded")
            await w.stop()

        w._scan = _scan  # type: ignore[method-assign]
        await finished(w.start(), "the watcher loop stopping itself from the second sweep")
        assert len(calls) >= 2, "the loop did not survive the failing sweep"

    @pytest.mark.asyncio
    async def test_stop_ends_the_loop_without_waiting_out_the_interval(self, store):
        """``stop`` sets the event the interval wait is racing, so it returns at once."""
        w = self._watcher(store, interval=3600)
        w._scan = AsyncMock()  # type: ignore[method-assign]
        task = asyncio.create_task(w.start())
        # flake-ok: pre-existing one-tick yield before the stop, which the bounded wait below covers; moved verbatim, tracked for a de-flake pass
        await asyncio.sleep(0)  # let the first sweep run and enter the wait
        await w.stop()
        await await_until(task, what="the stopped watcher loop returning")
        assert task.result() is None
        # One sweep before the stop, and no second one after it.
        assert w._scan.await_count == 1


class TestScheduledDedupCadence:
    """``KnowledgeWatcher._maybe_dedup_sweep``: when it runs, and what it may delete.

    A scheduled pass differs in kind from a human-invoked one -- it deletes
    unattended -- so the first one in a process is a dry run whose findings are only
    logged. These pin the cadence gate and that preview-then-apply rule; a regression
    in either deletes documents on the first sweep after a restart.
    """

    @staticmethod
    def _watcher(store):
        from kiro_crew.knowledge.watcher import KnowledgeWatcher

        return KnowledgeWatcher(store=store, pipeline=object(), interval=1)

    @staticmethod
    def _cfg(every: int):
        cfg = MagicMock()
        cfg.knowledge.dedup_every_n_sweeps = every
        return cfg

    @pytest.mark.asyncio
    async def test_zero_disables_the_scheduled_pass(self, store):
        w = self._watcher(store)
        with (
            patch("kiro_crew.knowledge.watcher.KiroCrewConfig") as cfg,
            patch("kiro_crew.knowledge.watcher.dedup_sweep") as sweep,
        ):
            cfg.load.return_value = self._cfg(0)
            await w._maybe_dedup_sweep()
        sweep.assert_not_called()
        # A skipped pass must not consume the first-pass preview either.
        assert w._dedup_applied_once is False

    @pytest.mark.asyncio
    async def test_it_only_runs_on_the_cadence(self, store):
        w = self._watcher(store)
        w._sweep_count = 5  # 5 % 4 != 0
        with (
            patch("kiro_crew.knowledge.watcher.KiroCrewConfig") as cfg,
            patch("kiro_crew.knowledge.watcher.dedup_sweep", return_value=[]) as sweep,
        ):
            cfg.load.return_value = self._cfg(4)
            await w._maybe_dedup_sweep()
            assert sweep.call_count == 0
            assert w._dedup_applied_once is False
            # The next multiple of the cadence does run, so the gate is the cadence.
            w._sweep_count = 8
            await w._maybe_dedup_sweep()
        assert sweep.call_count == 1
        assert w._dedup_applied_once is True

    @pytest.mark.asyncio
    async def test_the_first_pass_previews_and_the_next_one_applies(self, store):
        """apply=False first, then apply=True -- and only exact matches either way.

        ``certain_only`` is not a tuning knob here: an unattended fuzzy collapse
        deletes the loser's copy, so a wrong match (same filename, cosine over the
        threshold, different facts) would cost a document its only text with nobody
        watching.
        """
        w = self._watcher(store)
        w._sweep_count = 4
        calls = []

        def _sweep(store_arg, apply, certain_only, max_fuzzy_bucket=None):
            calls.append(
                {"apply": apply, "certain_only": certain_only, "max_fuzzy_bucket": max_fuzzy_bucket}
            )
            return [{"loser": "a", "winner": "b", "reason": "exact"}]

        with (
            patch("kiro_crew.knowledge.watcher.KiroCrewConfig") as cfg,
            patch("kiro_crew.knowledge.watcher.dedup_sweep", _sweep),
        ):
            cfg.load.return_value = self._cfg(4)
            await w._maybe_dedup_sweep()
            assert w._dedup_applied_once is True
            await w._maybe_dedup_sweep()

        assert [c["apply"] for c in calls] == [False, True]
        assert all(c["certain_only"] for c in calls)
        # The SCHEDULED sweep passes the single-signature bucket cap so a
        # pathological bucket cannot stall the gateway.
        from kiro_crew.knowledge.dedup import SCHEDULED_FUZZY_BUCKET_CAP

        assert all(c["max_fuzzy_bucket"] == SCHEDULED_FUZZY_BUCKET_CAP for c in calls)

    @pytest.mark.asyncio
    async def test_a_failed_pass_does_not_arm_the_applying_one(self, store):
        """A raised sweep must not consume the preview: the next pass would delete
        without anything ever having been logged."""
        w = self._watcher(store)
        w._sweep_count = 4
        with (
            patch("kiro_crew.knowledge.watcher.KiroCrewConfig") as cfg,
            patch("kiro_crew.knowledge.watcher.dedup_sweep", side_effect=RuntimeError("locked")),
        ):
            cfg.load.return_value = self._cfg(4)
            await w._maybe_dedup_sweep()  # contained, must not raise
        assert w._dedup_applied_once is False

    @pytest.mark.asyncio
    async def test_an_unreadable_cadence_setting_skips_the_pass(self, store):
        w = self._watcher(store)
        with (
            patch("kiro_crew.knowledge.watcher.KiroCrewConfig") as cfg,
            patch("kiro_crew.knowledge.watcher.dedup_sweep") as sweep,
        ):
            cfg.load.side_effect = RuntimeError("no config")
            await w._maybe_dedup_sweep()
        sweep.assert_not_called()
        assert w._dedup_applied_once is False


# ---------------------------------------------------------------------------
# SyncScheduler.sync_all -- errored sources must be quiesced
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestSyncAllSkipsErroredSources:
    """sync_all must skip an errored source, whichever writer marked it.

    KnowledgeIngestion and SyncScheduler both mark failure in the sync_status
    COLUMN, which is the only store sync_all reads. A row that carries its
    errored state only in its properties JSON cannot be ordered against the
    column and so is never promoted onto it; such a row is polled until an
    attempt of its own fails, and that failure writes the column.
    """

    def _scheduler(self, store):
        scheduler = SyncScheduler(store, pipeline=None, connectors={})
        attempted: list[str] = []

        async def _spy(source_id: str) -> dict:
            attempted.append(source_id)
            return {"synced": False, "items_created": 0, "error": None}

        scheduler.sync_source = _spy  # type: ignore[method-assign]
        return scheduler, attempted

    async def test_column_only_error_is_skipped(self, store):
        # A healthy source that should still be attempted.
        ok_id = store.add_source("Healthy", "local_file", "/tmp/ok")
        # A source errored the way ingestion.py does it: COLUMN only, no
        # sync_status entry in the properties JSON.
        err_id = store.add_source("Dead", "local_file", "/tmp/dead")
        store.db.execute("UPDATE sources SET sync_status = 'error' WHERE id = ?", (err_id,))
        store.db.commit()
        # Guard: the failing writer really left the JSON untouched.
        row = store.db.execute("SELECT properties FROM sources WHERE id = ?", (err_id,)).fetchone()
        assert json.loads(row["properties"] or "{}").get("sync_status") is None

        scheduler, attempted = self._scheduler(store)
        await scheduler.sync_all()

        assert err_id not in attempted, "column-only errored source must be skipped"
        assert ok_id in attempted, "healthy source must still be synced"

    async def test_legacy_json_only_error_is_quiesced_by_its_first_failure(self, store, tmp_path):
        """A pre-column errored row is polled until an attempt of its own fails.

        Its state lives in the properties blob only, which cannot be ordered
        against the column, so the store does not promote it -- promoting would
        mark a source errored that had in fact recovered. It is therefore polled
        like any healthy source, and the first attempt that FAILS is what quiesces
        it: ``_record_failure`` reads ``consecutive_failures`` from the blob, which
        such a row already carries at or above MAX_FAILURES, so that one failure
        writes the column and the source is skipped from then on.
        """
        err_id = str(uuid4())
        ok_id = str(uuid4())
        # flake-ok: pre-existing naive local stamp written into the test's own rows, moved verbatim; tracked for a de-flake pass
        # nosemgrep: kirocrew.test-naive-local-time -- pre-existing naive stamp for the test's own rows, moved verbatim; tracked for a de-flake pass
        now = datetime.now().isoformat()
        for sid, uri, props_json in (
            (
                err_id,
                "/tmp/legacy",
                json.dumps({"sync_status": "error", "consecutive_failures": 3}),
            ),
            (ok_id, "/tmp/legacy-ok", json.dumps({})),
        ):
            # local_folder: the reopen also runs the orphan cleanup, which
            # deletes item-less sources of every other type.
            store.db.execute(
                "INSERT INTO sources (id, name, source_type, uri, properties, sync_status, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (sid, "LegacyDead", "local_folder", uri, props_json, "pending", now, now),
            )
        store.db.commit()
        store.close()

        reopened = KnowledgeStore(str(tmp_path / "test.db"))
        try:
            scheduler, attempted = self._scheduler(reopened)
            await scheduler.sync_all()
            assert attempted == [err_id, ok_id] or set(attempted) == {
                err_id,
                ok_id,
            }, "both legacy rows are polled, the blob-errored one included"

            # An attempt that FAILS is what writes the column.
            scheduler._record_failure(err_id)
            assert (
                reopened.db.execute(
                    "SELECT sync_status FROM sources WHERE id = ?", (err_id,)
                ).fetchone()["sync_status"]
                == "error"
            )

            attempted.clear()
            await scheduler.sync_all()
            assert err_id not in attempted, "an errored column must never be retried"
            assert ok_id in attempted, "healthy source must still be synced"
        finally:
            reopened._close_all_for_tests()
