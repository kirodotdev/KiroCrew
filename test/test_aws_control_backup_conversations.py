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

import contextlib
import json
import os
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


class TestTheScratchExportIsPinned:
    """The scratch export is read through a descriptor, not re-derived from its name.

    The earlier code read it by path and justified that with "it is under our own
    private ``TemporaryDirectory``". That is wrong about the attacker that matters:
    ``mkdtemp`` gives mode 0700, which excludes other USERS and not the same-UID agent
    this product assumes, and `sandbox.py` describes exactly that agent planting links
    in the world-writable root. So `stat` then `open` on a name left a swap window whose
    payout is a host file uploaded off-host, unrecallable.
    """

    @pytest.fixture(autouse=True)
    def _needs_pinning(self):
        if not backup._CAN_PIN_TRAVERSAL:
            pytest.skip("descriptor pinning unavailable on this platform")

    def _dir_fd(self, path: Path) -> int:
        return os.open(str(path), os.O_RDONLY | backup._O_DIRECTORY | backup._O_NOFOLLOW)

    def test_a_genuine_scratch_file_is_read_and_sized_from_its_descriptor(self, tmp_path):
        """The happy path still works, and the size comes from the pinned descriptor."""
        (tmp_path / "conversations.sqlite3").write_bytes(b"payload-12")
        fd = self._dir_fd(tmp_path)
        try:
            handle, size = backup._open_pinned_scratch(fd, "conversations.sqlite3")
        finally:
            os.close(fd)
        with handle:
            assert size == 10
            assert handle.read() == b"payload-12"

    def test_a_symlinked_scratch_file_is_refused(self, tmp_path):
        """The swap the finding names: the name now resolves elsewhere.

        MUTATION: drop `_O_NOFOLLOW` from the open and this reddens.
        """
        secret = tmp_path / "credentials"
        secret.write_bytes(b"aws_secret_access_key = AKIA-not-real\n")
        (tmp_path / "conversations.sqlite3").symlink_to(secret)
        fd = self._dir_fd(tmp_path)
        try:
            with pytest.raises(OSError):
                backup._open_pinned_scratch(fd, "conversations.sqlite3")
        finally:
            os.close(fd)

    def test_a_hard_linked_scratch_file_is_refused(self, tmp_path):
        """A hard link defeats `O_NOFOLLOW` and `S_ISREG` by construction.

        The name resolves without a symlink and the target IS a regular file, so only
        the link count separates our own export from somebody else's file under our name.

        MUTATION: drop the `st_nlink` check and this reddens.
        """
        secret = tmp_path / "credentials"
        secret.write_bytes(b"aws_secret_access_key = AKIA-not-real\n")
        os.link(secret, tmp_path / "conversations.sqlite3")
        fd = self._dir_fd(tmp_path)
        try:
            with pytest.raises(backup._ScratchExportUnsafe) as caught:
                backup._open_pinned_scratch(fd, "conversations.sqlite3")
        finally:
            os.close(fd)
        assert "links" in str(caught.value)

    def test_a_fifo_in_place_of_the_scratch_file_is_refused(self, tmp_path):
        """Not a regular file: a FIFO would block the read or stream something else.

        MUTATION: drop the `S_ISREG` check and this reddens (the open succeeds because
        `O_NONBLOCK` is set, so only the stat-mode check stands between it and the tar).
        """
        os.mkfifo(tmp_path / "conversations.sqlite3")
        fd = self._dir_fd(tmp_path)
        try:
            with pytest.raises((backup._ScratchExportUnsafe, OSError)) as caught:
                backup._open_pinned_scratch(fd, "conversations.sqlite3")
        finally:
            os.close(fd)
        assert "regular file" in str(caught.value) or isinstance(caught.value, OSError)

    def test_a_symlinked_scratch_DIRECTORY_is_refused(self, tmp_path):
        """The directory is pinned too, so replacing it whole does not redirect us."""
        real = tmp_path / "real"
        real.mkdir()
        (real / "conversations.sqlite3").write_bytes(b"x")
        link = tmp_path / "kc-conv-link"
        link.symlink_to(real, target_is_directory=True)
        with pytest.raises(OSError):
            os.close(self._dir_fd(link))


class TestConversationExport:
    @pytest.fixture(autouse=True)
    def _needs_pinning(self):
        """The export requires descriptor-pinned open, so it reports rather than degrade.

        Not a convenience skip. `run_sessions_backup` refuses the whole sessions kind
        without `_CAN_PIN_TRAVERSAL` (`kind_unavailable_reason`), so on such a host this
        export is unreachable in production and every assertion below is about behaviour
        that platform never performs. The alternative -- inventing a weaker read so these
        tests pass on Windows -- would ship exactly the unpinned path the refusal exists
        to prevent, and `st_nlink` from `fstat` is not a dependable link count there.
        """
        if not backup._CAN_PIN_TRAVERSAL:
            pytest.skip("sessions export needs descriptor-pinned open; kind is unavailable here")

    def test_the_scratch_export_is_cut_under_the_agent_masked_root(self, tmp_path, monkeypatch):
        """Not the system temp directory. This removes the attack instead of detecting it.

        Descriptor pinning alone cannot cover a shared temp root: a same-UID agent that
        replaces the temp DIRECTORY before this process opens it hands over a directory of
        its own, and every pinned check then passes on a file the attacker chose. The
        masked root closes that, because the agent cannot reach it at all.

        MUTATION: drop `dir=` from the `TemporaryDirectory` call and this reddens.
        """
        seen: list[str] = []
        real = backup.tempfile.TemporaryDirectory

        def recording(*args, **kwargs):
            seen.append(str(kwargs.get("dir") or ""))
            return real(*args, **kwargs)

        monkeypatch.setattr(backup.tempfile, "TemporaryDirectory", recording)
        db = tmp_path / "data.sqlite3"
        _build_source_db(db, conversation_rows=3)
        monkeypatch.setattr(backup, "_kiro_cli_conversation_db", lambda: (db, ""))
        with tarfile.open(tmp_path / "out.tar.gz", "w:gz") as tar:
            backup._export_cli_conversations(tar)

        expected = str(backup.app_data_dir(backup.APP_NAME) / "conversations")
        assert any(d == expected for d in seen), seen
        assert expected.endswith(os.path.join("aws-control", "data", "conversations"))

    def test_a_link_planted_at_the_scratch_root_is_refused(self, tmp_path, monkeypatch):
        """A link AT the root would put every scratch file outside the fence at once.

        No per-file check can see that, which is why the root itself is guarded.

        MUTATION: remove BOTH the `is_link_or_junction` guard and the post-`mkdir` resolve
        re-check and this reddens. Measured: either one ALONE still catches a link planted
        at the leaf, so neither is individually pinned by this test. Both are kept anyway,
        because they do not cover the same inputs -- the resolve also catches a component
        swapped higher up, which a link test on the leaf cannot see, and the link guard
        fires before `mkdir` is asked to accept a pre-existing link.
        """
        elsewhere = tmp_path / "attacker"
        elsewhere.mkdir()
        base = backup.app_data_dir(backup.APP_NAME)
        base.mkdir(parents=True, exist_ok=True)
        (base / "conversations").symlink_to(elsewhere, target_is_directory=True)

        db = tmp_path / "data.sqlite3"
        _build_source_db(db, conversation_rows=3)
        monkeypatch.setattr(backup, "_kiro_cli_conversation_db", lambda: (db, ""))
        with tarfile.open(tmp_path / "out.tar.gz", "w:gz") as tar:
            result = backup._export_cli_conversations(tar)

        assert result.skipped == "scratch_root_unusable"
        assert result.members == 0

    def test_a_platform_without_pinning_reports_instead_of_degrading(self, tmp_path, monkeypatch):
        """No weaker read is invented for a host that cannot pin descriptors.

        Unreachable in production, since `run_sessions_backup` refuses the kind there. The
        branch exists so a DIRECT caller cannot quietly obtain the unpinned read.

        MUTATION: fall back to a path-based open and this reddens.
        """
        db = tmp_path / "data.sqlite3"
        _build_source_db(db, conversation_rows=3)
        monkeypatch.setattr(backup, "_kiro_cli_conversation_db", lambda: (db, ""))
        monkeypatch.setattr(backup, "_CAN_PIN_TRAVERSAL", False)
        with tarfile.open(tmp_path / "out.tar.gz", "w:gz") as tar:
            result = backup._export_cli_conversations(tar)

        assert result.skipped == "scratch_pinning_unavailable"
        assert result.members == 0

    @pytest.fixture
    def source_db(self, tmp_path, monkeypatch):
        db = tmp_path / "data.sqlite3"
        _build_source_db(db, conversation_rows=7)
        monkeypatch.setattr(backup, "_kiro_cli_conversation_db", lambda: (db, ""))
        return db

    def test_a_credential_in_a_conversation_is_redacted_before_it_ships(
        self, tmp_path, monkeypatch
    ):
        """A key a model was shown does not leave the host in the archive.

        The allowlist bounds which TABLES ride; it says nothing about what a
        conversation's own text holds. An operator pasting a key into a terminal
        session puts it in `value`, and this archive goes off-host unrecallably, so the
        text is scrubbed on the way into the export.

        MUTATION: insert the raw rows and this reddens -- the key appears in the DB
        bytes.
        """
        db = tmp_path / "data.sqlite3"
        _build_source_db(db, conversation_rows=0)
        conn = sqlite3.connect(str(db))
        try:
            conn.execute(
                "INSERT INTO conversations_v2 (conversation_id, value) VALUES (?, ?)",
                ("c1", "here is my key AKIAIOSFODNN7EXAMPLE, use it"),
            )
            conn.commit()
        finally:
            conn.close()
        monkeypatch.setattr(backup, "_kiro_cli_conversation_db", lambda: (db, ""))

        _, db_bytes, _ = _export_to_tar(tmp_path)

        assert db_bytes is not None
        assert b"AKIAIOSFODNN7EXAMPLE" not in db_bytes
        assert b"REDACTED" in db_bytes

    def test_the_redaction_is_byte_stable_across_exports(self, tmp_path, monkeypatch):
        """Two exports of one unchanged store produce identical rows.

        The exported database is an archive member, so its bytes feed
        `_tree_fingerprint` and therefore the unchanged-run skip. A redactor using a
        per-run placeholder -- a random token, a counter, a timestamp -- would change
        the bytes every night, so the skip would never fire again on any Layer B host
        and each run would re-upload the whole terminal history. Measured here rather
        than assumed, because `security.redaction` does hold a per-process random key
        for a different function.

        MUTATION: make the redaction depend on anything per-run and this reddens.
        """
        db = tmp_path / "data.sqlite3"
        _build_source_db(db, conversation_rows=0)
        conn = sqlite3.connect(str(db))
        try:
            conn.execute(
                "INSERT INTO conversations_v2 (conversation_id, value) VALUES (?, ?)",
                ("c1", "key AKIAIOSFODNN7EXAMPLE and token ghp_" + "a" * 36),
            )
            conn.commit()
        finally:
            conn.close()
        monkeypatch.setattr(backup, "_kiro_cli_conversation_db", lambda: (db, ""))

        def _rows():
            _, db_bytes, _ = _export_to_tar(tmp_path)
            assert db_bytes is not None
            path = tmp_path / "read_back.sqlite3"
            path.write_bytes(db_bytes)
            with contextlib.closing(sqlite3.connect(str(path))) as c:
                return c.execute("SELECT * FROM conversations_v2 ORDER BY 1").fetchall()

        # Compared as ROWS, not as archive bytes: a `tar.gz` embeds per-entry mtimes,
        # so the container differs between two builds of one tree by design and
        # `_tree_fingerprint` exists for exactly that reason. What must be stable is
        # what the fingerprint hashes -- the member's content.
        assert _rows() == _rows()

    def test_a_redactor_failure_ships_nothing_and_says_why(self, tmp_path, monkeypatch):
        """Unable to scrub means do not ship, not drop the row and not ship it raw.

        Dropping would produce an archive a restore reads as complete while a
        conversation is missing; shipping raw would send the credential the pass exists
        to remove. So the export carries nothing and reports a reason, which also
        suppresses the retention sweep -- an earlier archive may hold what this run
        could not carry.

        MUTATION: let the exception escape, or fall back to the raw value, and this
        reddens.
        """
        db = tmp_path / "data.sqlite3"
        _build_source_db(db, conversation_rows=3)
        monkeypatch.setattr(backup, "_kiro_cli_conversation_db", lambda: (db, ""))

        def _boom(_text):
            raise RuntimeError("redactor exploded")

        monkeypatch.setattr(backup, "_redact_egress", _boom)

        archive = tmp_path / "out.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            result = backup._export_cli_conversations(tar)

        assert result == backup._ConversationExport(0, 0, "conversations_unredactable")
        with tarfile.open(archive) as tar:
            assert tar.getnames() == []

    def test_a_value_wider_than_the_ceiling_ships_nothing_and_says_why(self, tmp_path, monkeypatch):
        """A single oversized field is refused, and the refusal precedes any fetch.

        The row count alone bounds nothing, because one field can be arbitrarily wide:
        a batch of rows holding megabyte values exhausts memory, and the allocation
        failure would otherwise escape this function's guards and lose the whole
        sessions backup over a sub-member. So the width is measured in SQL before the
        first ``fetchmany`` and the export carries nothing, with a reason that
        suppresses the retention sweep.

        MUTATION: drop the :func:`backup._refuse_an_oversized_cell` call from the copy
        and this reddens -- the wide row is copied and the export reports success.
        """
        monkeypatch.setattr(backup, "_CONVERSATION_MAX_CELL_BYTES", 64)
        db = tmp_path / "data.sqlite3"
        _build_source_db(db, conversation_rows=3)
        with contextlib.closing(sqlite3.connect(str(db))) as conn:
            conn.execute(
                "INSERT INTO conversations_v2 (conversation_id, value) VALUES (?, ?)",
                ("conv-wide", "x" * 65),
            )
            conn.commit()
        monkeypatch.setattr(backup, "_kiro_cli_conversation_db", lambda: (db, ""))

        archive = tmp_path / "out.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            result = backup._export_cli_conversations(tar)

        assert result == backup._ConversationExport(0, 0, "conversations_oversized")
        with tarfile.open(archive) as tar:
            assert tar.getnames() == []

    def test_a_value_exactly_at_the_ceiling_is_carried(self, tmp_path, monkeypatch):
        """The ceiling is a maximum, not a threshold the widest allowed value trips.

        Pinned as its own test because the comparison direction decides whether an
        ordinary host is refused: the widest value measured on a live store is 2.5 MiB
        against a 16 MiB ceiling, so an off-by-one here is invisible in production and
        fires only on the host that happens to sit on the boundary.

        MUTATION: compare with ``>=`` instead of ``>`` and this reddens while the
        oversized test stays green.
        """
        monkeypatch.setattr(backup, "_CONVERSATION_MAX_CELL_BYTES", 64)
        db = tmp_path / "data.sqlite3"
        _build_source_db(db, conversation_rows=1)
        with contextlib.closing(sqlite3.connect(str(db))) as conn:
            conn.execute(
                "INSERT INTO conversations_v2 (conversation_id, value) VALUES (?, ?)",
                ("conv-edge", "x" * 64),
            )
            conn.commit()
        monkeypatch.setattr(backup, "_kiro_cli_conversation_db", lambda: (db, ""))

        archive = tmp_path / "out.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            result = backup._export_cli_conversations(tar)

        assert result.skipped == ""
        assert result.rows == 2

    def test_an_allocation_failure_costs_the_member_not_the_whole_backup(
        self, tmp_path, monkeypatch
    ):
        """A ``MemoryError`` in the copy is reported, never propagated.

        The ceiling above is meant to make this unreachable, and it is caught anyway
        because the alternative is losing a correct transcript archive over this
        sub-member -- which is what the guards around every other exit of this function
        exist to prevent.

        MUTATION: remove the ``except MemoryError`` clause and this reddens with the
        error escaping instead of a reason coming back.
        """
        db = tmp_path / "data.sqlite3"
        _build_source_db(db, conversation_rows=2)
        monkeypatch.setattr(backup, "_kiro_cli_conversation_db", lambda: (db, ""))

        def _exhausted(*_args, **_kwargs):
            raise MemoryError("cannot allocate the batch")

        monkeypatch.setattr(backup, "_copy_table", _exhausted)

        archive = tmp_path / "out.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            result = backup._export_cli_conversations(tar)

        assert result == backup._ConversationExport(0, 0, "conversations_memory_exhausted")
        with tarfile.open(archive) as tar:
            assert tar.getnames() == []

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
        assert returned.rows == 7
        assert returned.members == 2

    def test_missing_store_is_a_no_op_not_an_error(self, tmp_path, monkeypatch):
        """No store on this host -> return 0, add nothing, raise nothing.

        MUTATION: raise instead of returning 0 on ``db is None`` -> reddens.
        """
        monkeypatch.setattr(backup, "_kiro_cli_conversation_db", lambda: (None, ""))
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
            monkeypatch.setattr(backup, "_kiro_cli_conversation_db", lambda: (db, ""))

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
        monkeypatch.setattr(backup, "_kiro_cli_conversation_db", lambda: (db, ""))
        monkeypatch.setattr(backup.hooks, "emit_internal_read_audit", lambda rid, outcome: False)

        archive = tmp_path / "out.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            returned = backup._export_cli_conversations(tar)
        assert returned.rows == 0
        assert returned.members == 0
        with tarfile.open(archive) as tar:
            assert tar.getnames() == []

    def test_a_successful_export_emits_the_credential_read_audit(self, tmp_path, monkeypatch):
        """A successful export records a 'success' audit under the registered id.

        MUTATION: remove the `emit_internal_read_audit(... "success")` call -> no
        success audit is recorded and this reddens.
        """
        db = tmp_path / "data.sqlite3"
        _build_source_db(db, conversation_rows=7)
        monkeypatch.setattr(backup, "_kiro_cli_conversation_db", lambda: (db, ""))
        calls: list[tuple[str, str]] = []

        def record(rid, outcome):
            calls.append((rid, outcome))
            return True

        monkeypatch.setattr(backup.hooks, "emit_internal_read_audit", record)
        archive = tmp_path / "out.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            returned = backup._export_cli_conversations(tar)
        assert returned.rows == 7
        assert (backup._CONVERSATION_READ_ID, "success") in calls

    def test_the_conversation_read_id_is_registered_in_hooks(self):
        """The audit id the export uses must be registered, or every read
        fail-closes. Pins the reader to the hooks registry so they cannot drift.
        """
        assert backup._CONVERSATION_READ_ID in backup.hooks._AUDIT_ONLY_READ_IDS

    def test_an_env_relocated_store_is_not_consulted(self, tmp_path, monkeypatch):
        """``XDG_DATA_HOME`` / ``LOCALAPPDATA`` are deliberately IGNORED.

        The fence that makes this store unreadable and unwritable by agent file
        tools is home-anchored (``security.paths`` splices
        ``identity_stores.fenced_home_dirs``), so a root redirected by one of those
        variables sits OUTSIDE it, where an agent can author rows. This function
        feeds an archive uploaded off-host unattended, so honouring the variable
        would let an agent plant rows in a ``conversations_v2`` table at a writable
        location and have a scheduled backup ship them; an uploaded object cannot be
        un-sent. ``kiro_prerequisite`` records the same decision for the same two
        variables.

        The cost is real and is the direction chosen: a relocated-store install
        backs up no terminal conversations, visible as an absent ``conversations/``
        root.

        MUTATION: pass ``os.environ`` instead of ``{}`` and this reddens -- the
        relocated store is found and returned.
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
            resolved = backup._kiro_cli_conversation_db()[0]
        assert resolved is None

    def test_a_linked_ancestor_is_refused_before_the_store_is_stat_ed(self, tmp_path, monkeypatch):
        """An ancestor redirection is refused, and no stat runs on that candidate.

        On Windows an ancestor junction whose target is ``\\\\host\\share`` turns the
        first ``is_file()`` on a local-looking path into an outbound SMB handshake
        that authenticates as this process -- inside the stat, before any check can
        reject anything. So the ancestor walk has to run BEFORE the stat, and a
        leaf-only check cannot see an ancestor at all. A POSIX directory symlink
        stands in for the junction here; the predicate treats both as redirections.

        ``is_file`` is replaced with a raiser for paths under the linked ancestor,
        so REACHING the stat fails the test. That is what pins the ORDER rather than
        merely the refusal: a leaf-first implementation still returns ``None`` for
        this layout, and would pass an outcome-only assertion.

        MUTATION: restore ``if db.is_file() and not is_link_or_junction(db)`` and
        this reddens with the sentinel, not with an assertion on the return value.
        """
        if sys.platform not in ("linux", "linux2"):
            pytest.skip("a POSIX directory symlink stands in for the Windows junction")
        # Derived from the resolver itself, not hardcoded: only FIXED home-anchored
        # paths are candidates, because the environment is not consulted.
        home = tmp_path / "home"
        candidate = backup.state_db_candidates(sys.platform, home, {})[0]
        # Plant the real store elsewhere and redirect an ANCESTOR of that fixed
        # candidate path at it, which is the shape the guard has to catch.
        real = tmp_path / "real"
        (real / candidate.parent.name).mkdir(parents=True)
        _build_source_db(real / candidate.parent.name / candidate.name, conversation_rows=2)
        linked_ancestor = candidate.parent.parent
        linked_ancestor.parent.mkdir(parents=True, exist_ok=True)
        linked_ancestor.symlink_to(real, target_is_directory=True)

        class _StatRan(Exception):
            pass

        def _refuse_stat(self):
            # Only a candidate under the redirected ancestor is a violation. Any
            # other candidate is stat-ed legitimately and must answer normally, or
            # this test would trip on an unrelated path and prove nothing.
            if str(linked_ancestor) in str(self):
                raise _StatRan(f"is_file() ran on {self}, which has a linked ancestor")
            return False

        with monkeypatch.context() as m:
            m.setattr(Path, "home", classmethod(lambda cls: home))
            m.setattr(Path, "is_file", _refuse_stat)
            resolved, declined = backup._kiro_cli_conversation_db()
        assert resolved is None
        # And the refusal must be REPORTABLE, not indistinguishable from "this host
        # has no store". The caller suppresses the retention sweep on a non-empty
        # reason, and this layout -- a link anywhere above the store, up to and
        # including the home directory -- is an ordinary one that would otherwise
        # prune the last archive that held the conversations.
        #
        # MUTATION: return a bare `None` for a rejected candidate and this reddens.
        assert declined == "store_rejected_link"

    def test_a_host_with_no_store_reports_absence(self, tmp_path, monkeypatch):
        """An absent store is reported, because an EARLIER archive may hold rows.

        The tempting exemption is that a host with no store has no conversations for an
        older archive to hold. That reasons about the store's state NOW, and the
        retention question is about backup history: a store wiped to clear corruption,
        removed by a reinstall, or on an unmounted volume was present when last week's
        archive was written, so that archive holds rows this run cannot carry. Nothing
        pins a store's presence across runs, so the reason must be set and the sweep
        suppressed.

        MUTATION: return a bare `declined` from the no-candidate path and this reddens.
        """
        home = tmp_path / "empty_home"
        home.mkdir()
        with monkeypatch.context() as m:
            m.setattr(Path, "home", classmethod(lambda cls: home))
            resolved, declined = backup._kiro_cli_conversation_db()
        assert resolved is None
        assert declined == "store_absent"

    def test_appdata_is_not_a_relocation_signal(self, tmp_path, monkeypatch):
        """`APPDATA` names a root kiro-cli does not write, so a mismatch proves nothing.

        `state_db_candidates` anchors the Windows Roaming candidate to `home` and does
        not follow `APPDATA`, because "the current generation writes the `LOCALAPPDATA`
        location, and the roaming default is retained only as a legacy fallback". A
        store the CLI does not write to cannot be relocated away from this export, so
        comparing that variable against home reports a relocation on any host with a
        redirected Roaming folder -- an ordinary enterprise configuration -- and that
        false positive suppresses retention forever while a readable Local store sits
        beside it inside the fence.

        Nothing is given up. The variables that really do re-root a candidate still
        report, and a legacy install that keeps its store at a redirected Roaming root
        has no store at either fixed candidate, which the lookup reports as
        `store_absent`; the last case here pins that, and
        `test_an_absent_store_also_stops_retention` in test_aws_control_backup.py pins
        that the reason suppresses the sweep.

        Paths come from `tmp_path`, never written as literals. `pathlib` takes its
        flavour from the REAL operating system rather than from `sys.platform`, and no
        literal is absolute under both: a POSIX-looking one has no drive, and a
        drive-lettered one is not absolute to POSIX.

        MUTATION: restore the direct home comparison over `IDENTITY_STORE_ROOTS` and the
        first assertion reddens.
        """
        home = tmp_path / "home"
        home.mkdir()
        far = tmp_path / "far" / "Roaming"
        assert far.is_absolute()
        with monkeypatch.context() as m:
            m.setattr(Path, "home", classmethod(lambda cls: home))
            m.setattr(sys, "platform", "win32")
            m.delenv("LOCALAPPDATA", raising=False)
            m.delenv("XDG_DATA_HOME", raising=False)

            # The candidate set is identical either way, which is exactly why this
            # variable carries no information about where the store lives.
            fixed = set(backup.state_db_candidates("win32", home, {}))
            named = set(backup.state_db_candidates("win32", home, {"APPDATA": str(far.parent)}))
            assert named == fixed

            m.setenv("APPDATA", str(far))
            assert backup._store_relocated_outside_the_fence() is False

            # `LOCALAPPDATA` DOES re-root its candidate, so it still reports.
            m.setenv("LOCALAPPDATA", str(tmp_path / "elsewhere"))
            assert backup._store_relocated_outside_the_fence() is True
            m.delenv("LOCALAPPDATA")

            # Premise this deletion rests on: a legacy install whose store really is at
            # the redirected Roaming root has nothing at either fixed candidate, so the
            # skip is still reported and retention is still suppressed -- by
            # `store_absent` rather than by a relocation reason.
            legacy = far / "kiro-cli"
            legacy.mkdir(parents=True)
            (legacy / "data.sqlite3").write_bytes(b"legacy store")
            assert backup._store_relocated_outside_the_fence() is False
            assert backup._kiro_cli_conversation_db() == (None, "store_absent")

    def test_a_stale_leftover_does_not_mask_the_live_store(self, tmp_path, monkeypatch):
        """With both Windows stores present the most recently written one is exported.

        The candidate table lists Local (the current layout) before Roaming (legacy), so
        taking the first cleared candidate would export a leftover in the abandoned root
        and report success -- the archive would hold an old account's conversations and
        nothing would say so. `identity_stores.selected_store` arbitrates this same state
        by write time, and this walk reuses its reading.

        The WAL case is the one a plain `st_mtime` gets wrong: the store runs in WAL
        mode, so a commit lands in the `-wal` sidecar and the main file's mtime does not
        advance until a checkpoint. A live store being written therefore looks OLDER by
        its main file alone, which is exactly backwards.

        MUTATION: return the first cleared candidate instead of the newest, and the
        first assertion reddens. Drop the `-wal` read and the third reddens.
        """
        home = tmp_path / "home"
        local = home / "AppData" / "Local" / "kiro-cli" / "data.sqlite3"
        roaming = home / "AppData" / "Roaming" / "kiro-cli" / "data.sqlite3"
        for db in (local, roaming):
            db.parent.mkdir(parents=True)
            db.write_bytes(b"store")

        with monkeypatch.context() as m:
            m.setattr(Path, "home", classmethod(lambda cls: home))
            m.setattr(sys, "platform", "win32")
            m.delenv("LOCALAPPDATA", raising=False)
            m.delenv("APPDATA", raising=False)

            os.utime(local, (1_000, 1_000))
            os.utime(roaming, (2_000, 2_000))
            assert backup._kiro_cli_conversation_db() == (roaming, "")

            # A tie keeps table order, so the current layout wins rather than the
            # legacy root. Otherwise the answer would depend on dict ordering.
            os.utime(roaming, (1_000, 1_000))
            assert backup._kiro_cli_conversation_db() == (local, "")

            # Roaming's MAIN file stays older; only its WAL sidecar is newer. A reader
            # that consults the main file alone picks Local and exports the leftover.
            (roaming.parent / "data.sqlite3-wal").write_bytes(b"wal")
            os.utime(roaming.parent / "data.sqlite3-wal", (3_000, 3_000))
            assert backup._kiro_cli_conversation_db() == (roaming, "")

    def test_present_but_empty_table_is_carried_with_its_schema(self, tmp_path, monkeypatch):
        """An empty conversation table still exports (schema preserved), count 0.

        Distinguishes "no rows yet" (carry the schema, count 0) from "no table at
        all" (carry nothing). MUTATION: skip present-but-empty tables -> the
        member disappears and this reddens.
        """
        db = tmp_path / "data.sqlite3"
        _build_source_db(db, conversation_rows=0)
        monkeypatch.setattr(backup, "_kiro_cli_conversation_db", lambda: (db, ""))

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
