"""The member-memory fixture provisions each test an isolated DB from a template.

``write_member_manifest`` file-copies a per-worker cached member database instead
of running ``create_member_database``'s FULL-sync WAL commit on every test.
These pin the invariants that make the copy sound: each test gets its own file,
the copy keeps the written identity and opens through admission, the template is
built once and reused, and a created member DB is self-contained (no WAL sidecars)
so copying ``memory.db`` alone reproduces the whole store.
"""

from __future__ import annotations

from unittest import mock

import member_memory_helpers as mmh

from kiro_crew import memory_stores
from kiro_crew.vector_memory import (
    create_member_database,
    open_member_database,
    read_member_database_identity,
)


class TestCreatedMemberDatabaseIsSelfContained:
    def test_no_wal_sidecars_remain_after_create(self, tmp_path):
        """A closed member DB is one ``memory.db`` — the copy carries the whole store."""
        directory = tmp_path / "member-alice"
        directory.mkdir()
        create_member_database(
            directory / memory_stores.MEMORY_DB_FILE, member_id="alice", store_id="member-alice"
        )
        assert sorted(p.name for p in directory.iterdir()) == [memory_stores.MEMORY_DB_FILE]


class TestWriteMemberManifestCopiesATemplate:
    def test_each_call_gets_its_own_isolated_file(self, tmp_path):
        """Two provisionings of the same identity are distinct files, not a shared one."""
        one = tmp_path / "one" / "member-alice"
        two = tmp_path / "two" / "member-alice"
        mmh.write_member_manifest(one, "alice")
        mmh.write_member_manifest(two, "alice")
        db_one = one / memory_stores.MEMORY_DB_FILE
        db_two = two / memory_stores.MEMORY_DB_FILE
        assert db_one.exists() and db_two.exists()
        assert db_one.resolve() != db_two.resolve()

        # A write to one store's file must not change the other's bytes.
        before = db_two.read_bytes()
        db_one.write_bytes(db_one.read_bytes() + b"\x00")
        assert db_two.read_bytes() == before

    def test_copied_database_keeps_identity_and_opens(self, tmp_path):
        """The copy preserves the written (member_id, store_id) and admits on open."""
        directory = tmp_path / "member-bob"
        mmh.write_member_manifest(directory, "bob")
        database = directory / memory_stores.MEMORY_DB_FILE
        assert read_member_database_identity(database) == ("bob", "member-bob")
        store = open_member_database(database, member_id="bob", store_id="member-bob")
        try:
            assert store is not None
        finally:
            store.close()

    def test_identity_follows_directory_name_not_a_fixed_template(self, tmp_path):
        """Distinct store names each get their own template identity, not one shared DB."""
        alpha = tmp_path / "member-alpha"
        beta = tmp_path / "member-beta"
        mmh.write_member_manifest(alpha, "alpha")
        mmh.write_member_manifest(beta, "beta")
        assert read_member_database_identity(alpha / memory_stores.MEMORY_DB_FILE) == (
            "alpha",
            "member-alpha",
        )
        assert read_member_database_identity(beta / memory_stores.MEMORY_DB_FILE) == (
            "beta",
            "member-beta",
        )

    def test_template_is_built_once_and_reused(self, tmp_path):
        """A second provisioning of the same identity copies; it does not re-create."""
        first = tmp_path / "a" / "member-alice"
        mmh.write_member_manifest(first, "alice")  # ensures the template exists
        with mock.patch.object(
            mmh, "create_member_database", wraps=mmh.create_member_database
        ) as spy:
            mmh.write_member_manifest(tmp_path / "b" / "member-alice", "alice")
            mmh.write_member_manifest(tmp_path / "c" / "member-alice", "alice")
        spy.assert_not_called()
        template_bytes = (first / memory_stores.MEMORY_DB_FILE).read_bytes()
        for copy in ("b", "c"):
            database = tmp_path / copy / "member-alice" / memory_stores.MEMORY_DB_FILE
            assert database.read_bytes() == template_bytes
            assert read_member_database_identity(database) == ("alice", "member-alice")

    def test_existing_database_is_never_overwritten(self, tmp_path):
        """Matching create_member_database: a present memory.db keeps its bytes."""
        directory = tmp_path / "member-alice"
        mmh.write_member_manifest(directory, "alice")
        database = directory / memory_stores.MEMORY_DB_FILE
        sentinel = database.read_bytes() + b"\x01"
        database.write_bytes(sentinel)
        mmh.write_member_manifest(directory, "alice")
        assert database.read_bytes() == sentinel

    def test_v1_store_writes_no_database(self, tmp_path):
        """A memory_version=1 store declares no member DB, as before."""
        directory = tmp_path / "member-legacy"
        mmh.write_member_manifest(directory, "legacy", memory_version=1)
        assert not (directory / memory_stores.MEMORY_DB_FILE).exists()
