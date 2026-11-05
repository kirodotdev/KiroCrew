"""The kiro-cli terminal conversation export in ``run_sessions_backup``.

``data.sqlite3`` is both the conversation store and the identity auth store, so the
archive carries a table-scoped export of it, never the file. These tests pin that no
byte of the auth half reaches the archive and that the archived row count matches
the source. The source DB is synthetic and stays inside ``tmp_path``.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import subprocess
import sys
import tarfile
import textwrap
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

    The token-bearing table and token-named column give the leak test something to
    look for. Left in WAL mode and checkpointed, matching a quiescent store.
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

    ``mkdtemp``'s mode 0700 keeps out other users, not the same-UID agent, so `stat`
    then `open` on a name leaves a swap window that uploads a host file off-host.
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

        `run_sessions_backup` refuses the whole sessions kind without
        `_CAN_PIN_TRAVERSAL`, so on such a host this export never runs. A weaker read
        to pass on Windows would ship the unpinned path the refusal prevents.
        """
        if not backup._CAN_PIN_TRAVERSAL:
            pytest.skip("sessions export needs descriptor-pinned open; kind is unavailable here")

    def test_the_scratch_export_is_cut_under_the_agent_masked_root(self, tmp_path, monkeypatch):
        """The scratch export is cut under the agent-masked root, not the system temp dir.

        Pinning cannot cover a shared temp root: a same-UID agent that swaps the temp
        directory first makes every pinned check pass on its own file.
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

        Either the `is_link_or_junction` guard or the post-`mkdir` resolve re-check alone
        catches this leaf link, so this test pins only that one of them is present.
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

        The allowlist bounds tables, not text: a pasted key lands in `value`, so the
        text is scrubbed on the way into the export.
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

        The bytes feed `_tree_fingerprint`, so a per-run placeholder would defeat the
        unchanged-run skip. Measured because `security.redaction` holds a per-process
        random key for another function.
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

        The reason also suppresses the retention sweep: an earlier archive may hold
        what this run could not carry.
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

        One field can be arbitrarily wide and exhaust memory, so the width is measured
        in SQL before the first ``fetchmany``. The reason suppresses the retention sweep.
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

        An off-by-one (``>=`` for ``>``) fires only on a host sitting on the boundary,
        so the oversized test alone cannot catch it.
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

        The ceiling should make this unreachable; it is caught anyway so this
        sub-member cannot lose the whole transcript archive.
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
        """A live store with a non-checkpointed WAL still exports every committed row.

        The store is opened read-only, so a checkpoint would raise; a non-empty WAL is
        the normal state of a live terminal.
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
        """An export whose credential-read audit cannot be recorded is dropped.

        The store holds live bearer tokens, so opening it owes an SEL trail.
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

        The agent fence is home-anchored, so a redirected root sits outside it where an
        agent could plant rows for an unattended upload. The cost: a relocated-store
        install backs up no terminal conversations.
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

        On Windows a junction to ``\\\\host\\share`` makes the stat itself an outbound
        SMB handshake. A POSIX symlink stands in for the junction.

        ``is_file`` raises under the linked ancestor, which pins the order: a leaf-first
        check also returns ``None`` here.
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

        A store wiped, reinstalled or on an unmounted volume was present for last week's
        archive, so the reason must be set and the sweep suppressed.
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

        A redirected Roaming folder is an ordinary enterprise setup; reporting it as a
        relocation would suppress retention forever. A legacy store there is reported
        as `store_absent`, which the last case pins.

        Paths come from `tmp_path`: `pathlib` follows the real OS, not `sys.platform`,
        and no literal is absolute under both.
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

        Same arbitration as `identity_stores.selected_store`. In WAL mode a commit lands
        in the `-wal` sidecar, so the main file's mtime alone makes a live store look older.
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


def _build_store_with_tables(path: Path, *, v1_rows: int | None, v2_rows: int | None) -> None:
    """Write a synthetic kiro-cli store naming which chat tables exist.

    ``None`` leaves the table out; an int creates it with that many rows. The
    un-migrated table has the real shape (key and JSON blob). The auth table is always
    planted, so every test also checks the auth half is held back.
    """
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        if v1_rows is not None:
            conn.execute("CREATE TABLE conversations (key TEXT PRIMARY KEY, value TEXT)")
            conn.executemany(
                "INSERT INTO conversations (key, value) VALUES (?, ?)",
                [(f"/work/proj-{i}", json.dumps({"turn": i})) for i in range(v1_rows)],
            )
        if v2_rows is not None:
            conn.execute(
                "CREATE TABLE conversations_v2 (conversation_id TEXT PRIMARY KEY, value TEXT)"
            )
            conn.executemany(
                "INSERT INTO conversations_v2 (conversation_id, value) VALUES (?, ?)",
                [(f"conv-{i}", json.dumps({"turn": i})) for i in range(v2_rows)],
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


class TestBothChatTablesAreInTheAllowlist:
    """The allowlist names the terminal's un-migrated AND migrated chat tables.

    With only the migrated one, an un-migrated store would export nothing and report
    ``no_conversation_table``, so these tests pin the export and the reason together.
    """

    @pytest.fixture
    def needs_pinning(self):
        """Requested only by the cases that let the export reach its tar write.

        Not autouse: the no-table and allowlist cases run before the pinning gate and
        must still run on a host without pinning. Without ``_CAN_PIN_TRAVERSAL``,
        ``run_sessions_backup`` refuses the sessions kind, so the other cases never run.
        """
        if not backup._CAN_PIN_TRAVERSAL:
            pytest.skip("sessions export needs descriptor-pinned open; kind is unavailable here")

    def test_an_unmigrated_store_has_its_conversations_exported(
        self, tmp_path, monkeypatch, needs_pinning
    ):
        """A store with only the un-migrated table exports its rows.

        MUTATION: drop ``"conversations"`` from ``backup._CONVERSATION_TABLES`` and this
        reddens -- the rows come back 0 and the reason becomes
        ``no_conversation_table``, which is the whole defect.
        """
        db = tmp_path / "data.sqlite3"
        _build_store_with_tables(db, v1_rows=5, v2_rows=None)
        monkeypatch.setattr(backup, "_kiro_cli_conversation_db", lambda: (db, ""))

        archive = tmp_path / "out.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            result = backup._export_cli_conversations(tar)

        assert result.skipped == ""
        assert result.rows == 5
        assert result.members == 2

        names, db_bytes, manifest = _export_to_tar(tmp_path)
        assert manifest is not None
        assert manifest["tables"] == {"conversations": 5}
        assert manifest["total_rows"] == 5
        assert db_bytes is not None
        assert _TOKEN_VALUE.encode() not in db_bytes
        assert _TOKEN_TABLE.encode() not in db_bytes
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
            carried = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
        finally:
            conn.close()
        assert tables == {"conversations"}
        assert carried == 5
        assert not any(_TOKEN_TABLE in n or _TOKEN_COLUMN in n for n in names)

    def test_a_migrated_store_carries_both_tables_and_counts_them_apart(
        self, tmp_path, monkeypatch, needs_pinning
    ):
        """Both tables ride, and the manifest counts them separately.

        A migrated store keeps the older table empty; the manifest shows it so a restore
        can tell an empty table from an omitted one.
        """
        db = tmp_path / "data.sqlite3"
        _build_store_with_tables(db, v1_rows=0, v2_rows=4)
        monkeypatch.setattr(backup, "_kiro_cli_conversation_db", lambda: (db, ""))

        _, db_bytes, manifest = _export_to_tar(tmp_path)
        assert manifest is not None
        assert manifest["tables"] == {"conversations": 0, "conversations_v2": 4}
        assert manifest["total_rows"] == 4
        assert db_bytes is not None
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
        assert tables == {"conversations", "conversations_v2"}
        assert _TOKEN_TABLE.encode() not in db_bytes

    def test_rows_in_both_tables_are_both_carried(self, tmp_path, monkeypatch, needs_pinning):
        """A partly-migrated store loses neither half.

        MUTATION: drop either table name from ``backup._CONVERSATION_TABLES`` and this
        reddens on the total.
        """
        db = tmp_path / "data.sqlite3"
        _build_store_with_tables(db, v1_rows=3, v2_rows=6)
        monkeypatch.setattr(backup, "_kiro_cli_conversation_db", lambda: (db, ""))

        archive = tmp_path / "out.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            result = backup._export_cli_conversations(tar)

        assert result.rows == 9
        _, _, manifest = _export_to_tar(tmp_path)
        assert manifest is not None
        assert manifest["tables"] == {"conversations": 3, "conversations_v2": 6}
        assert manifest["total_rows"] == 9

    def test_no_chat_table_at_all_still_reports_no_conversation_table(self, tmp_path, monkeypatch):
        """A store holding neither chat table reports ``no_conversation_table``."""
        db = tmp_path / "data.sqlite3"
        _build_store_with_tables(db, v1_rows=None, v2_rows=None)
        monkeypatch.setattr(backup, "_kiro_cli_conversation_db", lambda: (db, ""))

        archive = tmp_path / "out.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            result = backup._export_cli_conversations(tar)

        assert result == backup._ConversationExport(0, 0, "no_conversation_table")
        with tarfile.open(archive) as tar:
            assert tar.getnames() == []

    def test_the_allowlist_is_exactly_the_two_chat_tables(self):
        """The declared boundary, pinned as a value.

        The module header states the boundary and says this tuple is the single place
        it is expressed, so a change here without a change there is the drift this
        catches. MUTATION: add any third name and this reddens.
        """
        assert backup._CONVERSATION_TABLES == ("conversations", "conversations_v2")
        assert _TOKEN_TABLE not in backup._CONVERSATION_TABLES


# A WAL reset from a child process, stdlib only: POSIX locks are per process, so only
# another process (kiro-cli, in production) can see whether this one still holds the
# export's read lock.
_WAL_RESET_PROBE = textwrap.dedent("""
    import sqlite3, sys
    db = sqlite3.connect(sys.argv[1], timeout=0, isolation_level=None)
    busy, _log, _done = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    print("BUSY" if busy else "RESET")
    """)


def _another_process_resets_the_wal(db: Path) -> str:
    result = subprocess.run(
        [sys.executable, "-c", _WAL_RESET_PROBE, str(db)],
        # The child opens the database by its path; its working directory is the
        # test's own folder, so nothing it creates lands outside tmp_path.
        cwd=db.parent,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    return (result.stdout or result.stderr).strip()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX fcntl lock semantics")
class TestTheExportKeepsItsReadLock:
    """The export's read transaction survives the gateway's other readers of the file.

    The gateway also reads kiro-cli's ``data.sqlite3`` through ``kiro_cli`` (the spawn
    and prerequisite checks). One SQLite library defers a connection's close while
    another connection to the same file holds locks; a second library does not, and
    closing any descriptor drops every POSIX lock this process holds on the file.
    """

    def test_a_kiro_cli_read_during_the_export_keeps_the_exports_lock(self, tmp_path, monkeypatch):
        if not backup._CAN_PIN_TRAVERSAL:
            pytest.skip("sessions export needs descriptor-pinned open; kind is unavailable here")
        from kiro_crew import kiro_cli

        db = tmp_path / "data.sqlite3"
        _build_source_db(db, conversation_rows=3)
        # Committed rows left in the WAL, on a connection kept open so its close does not
        # fold them: resetting the WAL then needs every reader to be gone.
        keeper = sqlite3.connect(str(db))
        keeper.execute("PRAGMA wal_autocheckpoint=0")
        keeper.executemany(
            "INSERT INTO conversations_v2 (conversation_id, value) VALUES (?, ?)",
            [(f"conv-wal-{i}", json.dumps({"turn": i})) for i in range(50)],
        )
        keeper.commit()
        monkeypatch.setattr(backup, "_kiro_cli_conversation_db", lambda: (db, ""))
        real_copy = backup._copy_table
        probes: list[str] = []

        def copy_then_read_state(source, target, table):
            copied = real_copy(source, target, table)
            if not probes:
                probes.append(_another_process_resets_the_wal(db))
                state = kiro_cli._open_state_db_readonly(db)
                assert state is not None
                state.execute("SELECT count(*) FROM conversations_v2").fetchone()
                state.close()
                probes.append(_another_process_resets_the_wal(db))
            return copied

        monkeypatch.setattr(backup, "_copy_table", copy_then_read_state)
        try:
            with tarfile.open(tmp_path / "out.tar.gz", "w:gz") as tar:
                backup._export_cli_conversations(tar)
        finally:
            keeper.close()
        assert probes == ["BUSY", "BUSY"], f"kiro-cli could reset the WAL mid-export: {probes}"
