"""The kiro-cli terminal conversation export in ``run_sessions_backup``.

``data.sqlite3`` is BOTH the terminal's conversation store and its identity auth
store, so the sessions archive carries a TABLE-SCOPED export of it -- a fresh
database holding only the conversation allowlist -- never the file itself. These
tests pin the two acceptance properties that decide whether the fix is shippable:

* No byte of the auth half reaches the archive -- pinned by a fixture whose
  source DB carries a token-bearing table and a token column, asserting neither
  name nor value appears anywhere in the exported member.
* The archived conversation count matches the source row count -- pinned so a
  silently-empty or partial export fails loudly instead of shipping.

Both are mutation-verified in each test's docstring: break the guard, confirm the
named test reddens; the permissive path still passes.

Every fixture stays inside ``tmp_path``; the source DB is synthetic, so no real
kiro-cli store is touched and no ``data_home`` / network path is reached.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tarfile
from pathlib import Path

import pytest

from kiro_crew.apps.builtins.aws_control.backend import backup

# A column name and value that would be a live bearer token if it leaked, plus a
# whole auth table. The export must carry NEITHER.
_TOKEN_TABLE = "auth_kv"
_TOKEN_COLUMN = "bearer_token"
_TOKEN_VALUE = "SECRET-BEARER-TOKEN-must-not-leak-abc123"


def _build_source_db(path: Path, *, conversation_rows: int) -> None:
    """Write a synthetic kiro-cli store: a conversation table AND an auth table.

    Shapes ``conversations_v2`` the way the export reads it (an id + a JSON blob),
    and plants a separate token-bearing table plus a token-named column so the
    leak test has something concrete to look for. Left in WAL mode and
    checkpointed so the export's own checkpoint has nothing to fold, matching a
    quiescent store.
    """
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE conversations_v2 (conversation_id TEXT PRIMARY KEY, value TEXT)")
        conn.executemany(
            "INSERT INTO conversations_v2 (conversation_id, value) VALUES (?, ?)",
            [(f"conv-{i}", json.dumps({"turn": i})) for i in range(conversation_rows)],
        )
        conn.execute(f'CREATE TABLE "{_TOKEN_TABLE}" (k TEXT, {_TOKEN_COLUMN} TEXT)')
        conn.execute(
            f'INSERT INTO "{_TOKEN_TABLE}" (k, {_TOKEN_COLUMN}) VALUES (?, ?)',
            ("idc:default", _TOKEN_VALUE),
        )
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()


def _export_to_tar(tmp_path: Path) -> tuple[list[str], bytes | None, dict | None]:
    """Run the export into a tarball; return member names, the DB bytes, and the
    parsed manifest."""
    archive = tmp_path / "out.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        backup._export_cli_conversations(tar)
    db_bytes: bytes | None = None
    manifest: dict | None = None
    with tarfile.open(archive) as tar:
        names = sorted(tar.getnames())
        for name in names:
            member = tar.extractfile(name)
            if member is None:
                continue
            data = member.read()
            if name == backup._CONVERSATIONS_DB_ARCNAME:
                db_bytes = data
            elif name == backup._CONVERSATIONS_MANIFEST_ARCNAME:
                manifest = json.loads(data)
    return names, db_bytes, manifest


class TestConversationExport:
    @pytest.fixture
    def source_db(self, tmp_path, monkeypatch):
        db = tmp_path / "data.sqlite3"
        _build_source_db(db, conversation_rows=7)
        monkeypatch.setattr(backup, "_kiro_cli_conversation_db", lambda: db)
        return db

    def test_no_byte_of_the_auth_half_reaches_the_archive(self, tmp_path, source_db):
        """The exported DB carries the conversation table and NOTHING else.

        MUTATION: add ``_TOKEN_TABLE`` to ``backup._CONVERSATION_TABLES`` so the
        auth table is copied -> this test reddens on the token name/value being
        present. The permissive path (allowlist = conversations only) passes.
        """
        names, db_bytes, _ = _export_to_tar(tmp_path)
        assert db_bytes is not None, "the export must have written the conversations DB"

        # 1. The token bytes appear NOWHERE in the exported database.
        assert _TOKEN_VALUE.encode() not in db_bytes
        assert _TOKEN_TABLE.encode() not in db_bytes
        assert _TOKEN_COLUMN.encode() not in db_bytes

        # 2. The exported DB's own schema lists ONLY the conversation table.
        scratch = tmp_path / "readback.sqlite3"
        scratch.write_bytes(db_bytes)
        conn = sqlite3.connect(str(scratch))
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_schema WHERE type='table'"
                ).fetchall()
            }
        finally:
            conn.close()
        assert tables == {"conversations_v2"}

        # 3. No token bytes leaked into any archive member name.
        assert not any(_TOKEN_TABLE in n or _TOKEN_COLUMN in n for n in names)

    def test_archived_conversation_count_matches_source(self, tmp_path, source_db):
        """The manifest count and the exported rows both equal the source's 7.

        MUTATION: make ``_copy_table`` stop early (``break`` after the first
        ``fetchmany``) -> the exported row count drops below 7 and this reddens.
        The full copy passes.
        """
        source_count = (
            sqlite3.connect(str(source_db))
            .execute("SELECT COUNT(*) FROM conversations_v2")
            .fetchone()[0]
        )
        assert source_count == 7

        _, db_bytes, manifest = _export_to_tar(tmp_path)
        assert manifest is not None
        assert manifest["total_rows"] == source_count
        assert manifest["tables"]["conversations_v2"] == source_count

        scratch = tmp_path / "readback.sqlite3"
        scratch.write_bytes(db_bytes)
        exported_count = (
            sqlite3.connect(str(scratch))
            .execute("SELECT COUNT(*) FROM conversations_v2")
            .fetchone()[0]
        )
        assert exported_count == source_count

    def test_return_value_is_the_source_row_count(self, tmp_path, source_db):
        """The helper returns the row count it carried, so the sessions archive's
        ``count`` includes the conversations.

        MUTATION: return 0 unconditionally -> reddens here. Real count passes.
        """
        archive = tmp_path / "out.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            returned = backup._export_cli_conversations(tar)
        assert returned == 7

    def test_missing_store_is_a_no_op_not_an_error(self, tmp_path, monkeypatch):
        """No store on this host -> return 0, add nothing, raise nothing.

        MUTATION: raise instead of returning 0 on ``db is None`` -> reddens.
        """
        monkeypatch.setattr(backup, "_kiro_cli_conversation_db", lambda: None)
        names, db_bytes, manifest = _export_to_tar(tmp_path)
        assert names == []
        assert db_bytes is None and manifest is None

    def test_committed_rows_in_a_live_wal_are_exported(self, tmp_path, monkeypatch):
        """A live store with a non-empty, non-checkpointed WAL still exports every
        committed row.

        The store is opened READ-ONLY, so a checkpoint (a write) is impossible;
        the read must instead see committed WAL frames through the read-only
        snapshot. A non-empty WAL is the NORMAL steady state of a live kiro-cli
        terminal, so an export that returned 0 here would silently omit
        conversations on the common path.

        MUTATION: reintroduce a ``wal_checkpoint(TRUNCATE)`` on the read-only
        connection (or a refuse-on-surviving-WAL branch) -> the checkpoint raises
        on mode=ro and the export returns 0, so this test reddens (`assert 5 ==
        0`).
        """
        db = tmp_path / "data.sqlite3"
        # Build a store and leave a WRITER connection open with committed but
        # NOT-checkpointed rows, so a real non-empty -wal sits beside the file.
        writer = sqlite3.connect(str(db))
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute(
            "CREATE TABLE conversations_v2 (conversation_id TEXT PRIMARY KEY, value TEXT)"
        )
        writer.executemany(
            "INSERT INTO conversations_v2 (conversation_id, value) VALUES (?, ?)",
            [(f"conv-{i}", "x") for i in range(5)],
        )
        writer.commit()
        try:
            wal = tmp_path / "data.sqlite3-wal"
            assert wal.exists() and wal.stat().st_size > 0, "the WAL must be non-empty and live"
            monkeypatch.setattr(backup, "_kiro_cli_conversation_db", lambda: db)

            _, db_bytes, manifest = _export_to_tar(tmp_path)
            assert manifest is not None and manifest["total_rows"] == 5
            scratch = tmp_path / "readback.sqlite3"
            scratch.write_bytes(db_bytes)
            exported = (
                sqlite3.connect(str(scratch))
                .execute("SELECT COUNT(*) FROM conversations_v2")
                .fetchone()[0]
            )
            assert exported == 5
        finally:
            writer.close()

    def test_a_failed_credential_audit_drops_the_export(self, tmp_path, monkeypatch):
        """If the sanctioned credential-read audit cannot be recorded, the export
        is dropped rather than shipped unaudited (fail-closed).

        The store holds live bearer tokens, so opening it owes an SEL trail; a
        success whose audit fails must not ship. MUTATION: drop the
        `if not hooks.emit_internal_read_audit(... "success"): return 0` guard ->
        the member ships despite the failed audit and this reddens.
        """
        db = tmp_path / "data.sqlite3"
        _build_source_db(db, conversation_rows=7)
        monkeypatch.setattr(backup, "_kiro_cli_conversation_db", lambda: db)
        monkeypatch.setattr(backup.hooks, "emit_internal_read_audit", lambda rid, outcome: False)

        archive = tmp_path / "out.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            returned = backup._export_cli_conversations(tar)
        assert returned == 0
        with tarfile.open(archive) as tar:
            assert tar.getnames() == []

    def test_a_successful_export_emits_the_credential_read_audit(self, tmp_path, monkeypatch):
        """A successful export records a 'success' audit under the registered id.

        MUTATION: remove the `emit_internal_read_audit(... "success")` call -> no
        success audit is recorded and this reddens.
        """
        db = tmp_path / "data.sqlite3"
        _build_source_db(db, conversation_rows=7)
        monkeypatch.setattr(backup, "_kiro_cli_conversation_db", lambda: db)
        calls: list[tuple[str, str]] = []

        def record(rid, outcome):
            calls.append((rid, outcome))
            return True

        monkeypatch.setattr(backup.hooks, "emit_internal_read_audit", record)
        archive = tmp_path / "out.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            returned = backup._export_cli_conversations(tar)
        assert returned == 7
        assert (backup._CONVERSATION_READ_ID, "success") in calls

    def test_the_conversation_read_id_is_registered_in_hooks(self):
        """The audit id the export uses must be registered, or every read
        fail-closes. Pins the reader to the hooks registry so they cannot drift.
        """
        assert backup._CONVERSATION_READ_ID in backup.hooks._AUDIT_ONLY_READ_IDS

    def test_relocated_store_via_env_var_is_found(self, tmp_path, monkeypatch):
        """A store relocated by XDG_DATA_HOME (POSIX) is resolved, not skipped.

        ``_kiro_cli_conversation_db`` must honour the same relocation vars the
        rest of kiro-cli state discovery does, or a supported layout silently
        yields no conversations.

        MUTATION: resolve fixed ``Path.home()`` paths ignoring the env var -> the
        relocated store is not found, the export is empty, and this reddens.
        """
        if sys.platform not in ("linux", "linux2"):
            pytest.skip("XDG_DATA_HOME relocation is the POSIX layout")
        xdg = tmp_path / "xdg"
        store = xdg / "kiro-cli"
        store.mkdir(parents=True)
        db = store / "data.sqlite3"
        _build_source_db(db, conversation_rows=4)
        with monkeypatch.context() as m:
            m.setenv("XDG_DATA_HOME", str(xdg))
            m.setattr(Path, "home", classmethod(lambda cls: tmp_path / "nowhere"))
            resolved = backup._kiro_cli_conversation_db()
        assert resolved == db

    def test_present_but_empty_table_is_carried_with_its_schema(self, tmp_path, monkeypatch):
        """An empty conversation table still exports (schema preserved), count 0.

        Distinguishes "no rows yet" (carry the schema, count 0) from "no table at
        all" (carry nothing). MUTATION: skip present-but-empty tables -> the
        member disappears and this reddens.
        """
        db = tmp_path / "data.sqlite3"
        _build_source_db(db, conversation_rows=0)
        monkeypatch.setattr(backup, "_kiro_cli_conversation_db", lambda: db)

        names, db_bytes, manifest = _export_to_tar(tmp_path)
        assert backup._CONVERSATIONS_DB_ARCNAME in names
        assert manifest is not None and manifest["total_rows"] == 0
        assert manifest["tables"]["conversations_v2"] == 0
        scratch = tmp_path / "readback.sqlite3"
        scratch.write_bytes(db_bytes)
        tables = {
            row[0]
            for row in sqlite3.connect(str(scratch))
            .execute("SELECT name FROM sqlite_schema WHERE type='table'")
            .fetchall()
        }
        assert tables == {"conversations_v2"}
