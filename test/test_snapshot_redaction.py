"""The copy that leaves the host is redacted; the copy on local disk is not.

The boundary is the copy that crosses off-host, not the archive. A local bundle sits on
the machine that already holds these secrets, so redacting it would destroy the only copy
that restores complete and buy nothing. What crosses the boundary is a separate, redacted
copy, produced by ``snapshot.prepare_redacted_copy`` -- the seam an off-host sender
consumes. The destination itself (bucket, hardening, consent, transport) belongs to the
AWS Control app; what stays here is rewriting the bytes that leave.
"""

from __future__ import annotations

import json
import tarfile
from pathlib import Path

import pytest

from kiro_crew import snapshot as snap
from kiro_crew import snapshot_redact


def _real_db(path: Path, secret: str | None = None) -> bytes:
    conn = snap.sqlite3.connect(str(path))
    conn.execute("CREATE TABLE semantic_memory (key, value_json)")
    conn.execute("INSERT INTO semantic_memory VALUES (?, ?)", ("note", secret or "nothing secret"))
    conn.commit()
    conn.close()
    return path.read_bytes()


TOKEN = "8412345678:AAH9xSECRETtokenvalue_here12345"


@pytest.fixture(autouse=True)
def opted_in(monkeypatch):
    """Outbound redaction is OFF by default, so these tests must ask for it.

    Redaction is an opt-in the operator sets, so a test of the redaction PASS has to turn
    it on the way an operator would. `TestTheOptOut` patches this same function in its own
    bodies, which runs after this fixture and therefore still wins.
    """
    monkeypatch.setattr(snapshot_redact, "outbound_redaction_enabled", lambda: True)


@pytest.fixture
def bundle(tmp_path):
    """A written archive carrying a bot token in config and a key inside memory."""
    payload = tmp_path / "kirocrew-snapshot-20260101T000000Z"
    payload.mkdir(parents=True)
    (payload / "config.json").write_text(
        json.dumps({"telegram": {"bot_token": TOKEN}}), encoding="utf-8"
    )
    (payload / "memory.db").write_bytes(
        _real_db(tmp_path / "src.db", f"my key is ghp_{'A' * 36} ok")
    )
    (payload / "memory_index.db").write_bytes(_real_db(tmp_path / "idx.db"))
    (payload / "MANIFEST.json").write_text(
        json.dumps({"version": 3, "components": {"memory": "unresolved"}}),
        encoding="utf-8",
    )
    out = tmp_path / "kirocrew-snapshot-20260101T000000Z.tar.gz"
    with tarfile.open(out, "w:gz") as tf:
        tf.add(str(payload), arcname=payload.name)
    return out


@pytest.fixture
def workdir(tmp_path):
    """A caller-owned directory the redacted copy is written inside."""
    d = tmp_path / "work"
    d.mkdir()
    return d


def _prepare(bundle: Path, workdir: Path):
    """Drive redaction through the surviving seam and return the redacted copy's path."""
    return snap.prepare_redacted_copy(bundle, workdir, ["memory"])


def _read_member(archive: Path, suffix: str) -> bytes:
    with tarfile.open(archive) as tf:
        for m in tf.getmembers():
            if m.name.endswith(suffix):
                f = tf.extractfile(m)
                assert f is not None
                return f.read()
    raise AssertionError(f"{suffix} not found in {archive}")


def _member_names(archive: Path) -> list[str]:
    with tarfile.open(archive) as tf:
        return tf.getnames()


def _bundle_with(tmp_path: Path, extra: dict[str, bytes]) -> Path:
    """A minimal valid bundle carrying *extra* entries alongside a real manifest."""
    payload = tmp_path / "kirocrew-snapshot-20260101T000000Z"
    payload.mkdir(parents=True)
    (payload / "MANIFEST.json").write_text(
        json.dumps({"version": 3, "components": {"memory": "unresolved"}}), encoding="utf-8"
    )
    (payload / "memory.db").write_bytes(_real_db(tmp_path / "m.db"))
    for name, data in extra.items():
        target = payload / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    out = tmp_path / "kirocrew-snapshot-20260101T000000Z.tar.gz"
    with tarfile.open(out, "w:gz") as tf:
        tf.add(str(payload), arcname=payload.name)
    return out


class TestTheOutboundCopyIsRedacted:
    def test_the_token_does_not_leave_the_host(self, bundle, workdir, capsys):
        redacted = _prepare(bundle, workdir)
        out = capsys.readouterr().out
        assert redacted is not None, out
        assert TOKEN not in _read_member(redacted, "config.json").decode("utf-8")

    def test_a_key_pasted_into_memory_does_not_leave_either(self, bundle, workdir, capsys):
        redacted = _prepare(bundle, workdir)
        assert redacted is not None, capsys.readouterr().out
        assert b"ghp_" + b"A" * 36 not in _read_member(redacted, "memory.db")

    def test_the_redacted_database_is_still_a_database(self, bundle, workdir, tmp_path, capsys):
        """The whole reason redaction goes through SQL instead of over the bytes."""
        redacted = _prepare(bundle, workdir)
        assert redacted is not None, capsys.readouterr().out
        restored = tmp_path / "roundtrip.db"
        restored.write_bytes(_read_member(redacted, "memory.db"))
        conn = snap.sqlite3.connect(str(restored))
        assert conn.execute("PRAGMA integrity_check;").fetchone()[0] == "ok"
        assert conn.execute("SELECT count(*) FROM semantic_memory").fetchone()[0] == 1
        conn.close()

    def test_the_local_bundle_is_left_unredacted(self, bundle, workdir, capsys):
        redacted = _prepare(bundle, workdir)
        assert redacted is not None, capsys.readouterr().out
        assert TOKEN in _read_member(bundle, "config.json").decode(
            "utf-8"
        ), "the local archive was modified -- it must stay restorable"

    def test_the_redacted_copy_lives_in_the_caller_workdir(self, bundle, workdir, capsys):
        """The seam writes the copy inside the directory the caller controls and removes."""
        redacted = _prepare(bundle, workdir)
        assert redacted is not None, capsys.readouterr().out
        assert workdir in redacted.parents, redacted
        assert redacted != bundle

    def test_it_reports_what_it_changed(self, bundle, workdir, capsys):
        _prepare(bundle, workdir)
        out = capsys.readouterr().out
        assert "Redacted the outbound copy" in out, out
        assert "config.json" in out, out
        assert "still restores complete" in out, out


class TestRedactionFailureRefusesRatherThanLeaking:
    def test_a_pass_that_cannot_run_prepares_nothing(self, bundle, workdir, monkeypatch):
        """An IO failure inside the pass is a refusal, not a traceback or a silent pass.

        A traceback would leave the operator unable to tell a crash from a leak, and
        "could not redact" must never fall through to "send it unredacted".
        """

        def boom(*_a, **_k):
            raise OSError("no space left on device")

        monkeypatch.setattr(snapshot_redact, "redact_bundle_for_egress", boom)
        with pytest.raises(snap.RedactionFailed) as e:
            _prepare(bundle, workdir)
        assert "no space left on device" in str(e.value)

    def test_an_unreadable_bundle_refuses_with_a_reason(self, tmp_path, workdir):
        """Refused before redaction is even reached, by the re-read guard.

        Asserted as a refusal rather than a message, because which guard catches it is an
        ordering detail; that nothing usable is produced is the property.
        """
        broken = tmp_path / "kirocrew-snapshot-20260101T000000Z.tar.gz"
        broken.write_bytes(b"not a tarball")
        with pytest.raises(snap.RedactionFailed):
            _prepare(broken, workdir)


class TestTheOptOut:
    def test_disabling_redaction_prepares_no_copy_so_the_original_is_sent(
        self, bundle, workdir, monkeypatch, capsys
    ):
        # The opt-out is not a config field: it lives behind the keystone fence on the
        # backup directory, because a switch the agent can write is a switch the agent can
        # use to publish plaintext credentials through a sanctioned path.
        monkeypatch.setattr(snapshot_redact, "outbound_redaction_enabled", lambda: False)
        redacted = _prepare(bundle, workdir)
        out = capsys.readouterr().out
        assert redacted is None, out
        assert "Redaction is DISABLED" in out, out
        # None means the caller sends the ORIGINAL bundle -- which is unredacted.
        assert TOKEN in _read_member(bundle, "config.json").decode(
            "utf-8"
        ), "the opt-out must leave the complete bundle to be sent"

    def test_an_unreadable_app_config_does_not_change_the_switch(
        self, bundle, workdir, monkeypatch, capsys
    ):
        """The switch is its own fenced file, not a field in the app config.

        So a config that cannot be read says nothing about redaction either way: with the
        operator opted in, the pass still runs.
        """
        from kiro_crew.config import loader as cfg_loader

        def boom(*a, **k):
            raise OSError("config unreadable")

        monkeypatch.setattr(cfg_loader.KiroCrewConfig, "load", staticmethod(boom))
        redacted = _prepare(bundle, workdir)
        assert redacted is not None, capsys.readouterr().out
        assert TOKEN not in _read_member(redacted, "config.json").decode("utf-8")


class TestNothingUnprovableLeavesTheHost:
    """Anything that cannot be shown redacted is removed, not shipped hoping it is fine."""

    def test_secret_only_files_are_left_out(self, tmp_path, workdir, capsys):
        out = _bundle_with(tmp_path, {"telemetry_salt": b"saltvalue", "sel_hmac.key": b"k"})
        redacted = _prepare(out, workdir)
        assert redacted is not None, capsys.readouterr().out
        names = _member_names(redacted)
        assert not any(n.endswith("telemetry_salt") for n in names), names
        assert not any(n.endswith("sel_hmac.key") for n in names), names

    def test_an_ordinary_source_file_is_redacted_not_deleted(self, tmp_path, workdir, capsys):
        """A workspace holds arbitrary files; an unlisted suffix is not a reason to drop one.

        `tool.py` is text. Deciding by suffix classified it as opaque and removed it, so an
        off-host restore reported success while permanently lacking the operator's file.
        """
        out = _bundle_with(
            tmp_path,
            {
                "workspace/project/tool.py": f'TOKEN = "{TOKEN}"\nprint("hi")\n'.encode(),
                "workspace/data/rows.csv": b"a,b\n1,2\n",
                "workspace/page.html": b"<p>ok</p>",
            },
        )
        redacted = _prepare(out, workdir)
        assert redacted is not None, capsys.readouterr().out
        names = _member_names(redacted)
        for keep in ("tool.py", "rows.csv", "page.html"):
            assert any(n.endswith(keep) for n in names), f"{keep} was dropped: {names}"
        # And it was actually redacted, not merely kept.
        assert TOKEN not in _read_member(redacted, "tool.py").decode("utf-8")

    def test_a_file_that_cannot_be_read_refuses_rather_than_passing(
        self, tmp_path, workdir, monkeypatch
    ):
        """Unreadable is not the same as clean, and it must not escape as a traceback.

        Patched rather than produced with permissions, because a mode-000 file is still
        readable by its owner on Windows -- the branch would go untested exactly where the
        platform differs.
        """
        out = _bundle_with(tmp_path, {"workspace/notes.md": b"hello"})
        real_read = Path.read_text

        def fail_on_notes(self, *a, **k):
            if self.name == "notes.md":
                raise OSError(5, "Input/output error")
            return real_read(self, *a, **k)

        monkeypatch.setattr(Path, "read_text", fail_on_notes)
        with pytest.raises(snap.RedactionFailed) as e:
            _prepare(out, workdir)
        assert "Traceback" not in str(e.value)

    def test_the_unreadable_signal_is_an_io_error(self):
        """So the upload path's IO handler reports it instead of letting it escape."""
        assert issubclass(snapshot_redact._FileUnreadable, OSError)

    def test_a_genuinely_opaque_file_refuses_and_is_kept(self, tmp_path, workdir, capsys):
        """Not text -> cannot be shown clean. Refuse; do not quietly drop the file.

        The bytes here are undecodable on purpose: a NUL-filled file IS valid UTF-8, so
        "looks binary" is not the test -- decoding is.
        """
        png = b"\x89PNG\r\n\x1a\n\xff\xd8\xff\xe0binary"
        out = _bundle_with(tmp_path, {"workspace/shot.png": png})
        with pytest.raises(snap.RedactionFailed) as e:
            _prepare(out, workdir)
        text = str(e.value)
        assert "not text" in text, text
        assert "shot.png" in text, text
        assert "NOT removed" in text, text
        # The local bundle is untouched, so the operator still holds the complete copy.
        assert any(n.endswith("shot.png") for n in _member_names(out))

    def test_a_corrupt_product_database_refuses_rather_than_shipping_without_it(
        self, tmp_path, workdir
    ):
        """Dropping the payload IS shipping a broken backup, so this refuses instead.

        This asserted that an unprovable product database was removed and the remainder
        sent. That produces an off-host copy which reports success and restores nothing,
        discovered only once the machine it came from is gone -- the exact failure the
        feature exists to prevent. The database the backup carries is not a file that may
        be silently omitted, so the copy is refused and names it, and the complete local
        archive is left untouched.
        """
        out = _bundle_with(
            tmp_path, {"workspace/knowledge/knowledge.db": b"this is not a database"}
        )
        with pytest.raises(snap.RedactionFailed) as e:
            _prepare(out, workdir)
        assert "knowledge.db" in str(e.value)

    def test_a_corrupt_database_the_product_does_not_own_refuses(self, tmp_path, workdir):
        """Deleting an operator's file to protect them from it is not a trade to make."""
        out = _bundle_with(tmp_path, {"workspace/projects/notes.db": b"not a database"})
        with pytest.raises(snap.RedactionFailed) as e:
            _prepare(out, workdir)
        assert "notes.db" in str(e.value)

    def test_the_redacted_manifest_records_the_redaction(self, bundle, workdir, capsys):
        """Restore's warning depends on this stamp, so the redaction pass must write it."""
        redacted = _prepare(bundle, workdir)
        assert redacted is not None, capsys.readouterr().out
        mf = json.loads(_read_member(redacted, "MANIFEST.json").decode("utf-8"))
        assert mf.get("redaction", {}).get("redacted") is True, mf
        assert mf["redaction"]["replacements"], mf

    def test_a_redaction_failure_prepares_nothing(self, tmp_path, workdir):
        """Two roots is a state the pass refuses -- and a refusal must not produce a copy."""
        stage = tmp_path / "two"
        for name in ("kirocrew-snapshot-20260101T000000Z", "kirocrew-snapshot-other"):
            (stage / name).mkdir(parents=True)
        out = tmp_path / "kirocrew-snapshot-20260101T000000Z.tar.gz"
        with tarfile.open(out, "w:gz") as tf:
            for child in sorted(stage.iterdir()):
                tf.add(str(child), arcname=child.name)

        with pytest.raises(snap.RedactionFailed):
            _prepare(out, workdir)


class TestRestoreAnnouncesARedactedBundle:
    def test_it_says_the_credentials_are_inert(self, tmp_path, capsys):
        payload = tmp_path / "snap"
        payload.mkdir()
        (payload / "MANIFEST.json").write_text(
            json.dumps(
                {
                    "version": 3,
                    "components": {"memory": "unresolved"},
                    "redaction": {
                        "redacted": True,
                        "replacements": {"config.json": 1},
                        "dropped": ["telemetry_salt"],
                        "indexes_needing_rebuild": ["memory_index.db"],
                    },
                }
            ),
            encoding="utf-8",
        )
        snap._report_redacted_bundle(payload)
        out = capsys.readouterr().out
        assert "REDACTED before it left its host" in out, out
        assert "config.json: 1" in out, out
        assert "Re-enter those credentials" in out, out
        assert "rebuilding" in out, out

    def test_it_stays_quiet_for_an_ordinary_bundle(self, tmp_path, capsys):
        payload = tmp_path / "snap"
        payload.mkdir()
        (payload / "MANIFEST.json").write_text(
            json.dumps({"version": 3, "components": {"memory": "unresolved"}}),
            encoding="utf-8",
        )
        snap._report_redacted_bundle(payload)
        assert capsys.readouterr().out == ""


class TestNoRowidIsBelowTheScan:
    """Every row is scanned, whatever its rowid -- including negative ones.

    The pager used to open with `last = -1` and always select `handle > ?`, so any row whose
    rowid was <= -1 was never yielded and its credential shipped in the "redacted" copy
    while the report still claimed a successful replacement. SQLite lets you set an explicit
    negative INTEGER PRIMARY KEY, so nothing exotic is needed to land there.

    The INT64 minimum is included deliberately: it is the row that any FIXED sentinel floor
    would still miss, so it is what distinguishes the actual fix (no floor on the first
    page) from merely lowering the old one.
    """

    TOKEN = "AKIAIOSFODNN7EXAMPLE"

    def _staged(self, tmp_path):
        stage = tmp_path / "stage"
        stage.mkdir()
        db = stage / "memory.db"
        conn = snap.sqlite3.connect(str(db))
        conn.executescript("CREATE TABLE t(id INTEGER PRIMARY KEY, body TEXT);")
        for rid, label in (
            (5, "positive"),
            (-1, "minus-one"),
            (-5, "negative"),
            (-(2**63), "int64min"),
        ):
            conn.execute("INSERT INTO t(id, body) VALUES(?, ?)", (rid, f"{label} {self.TOKEN}"))
        conn.commit()
        conn.close()
        return stage, db

    def test_a_credential_at_a_negative_rowid_is_still_redacted(self, tmp_path):
        stage, db = self._staged(tmp_path)
        snapshot_redact.redact_bundle_for_egress(stage)

        conn = snap.sqlite3.connect(str(db))
        rows = conn.execute("SELECT id, body FROM t").fetchall()
        conn.close()

        assert len(rows) == 4, "the pass must not drop rows"
        kept = [rid for rid, body in rows if self.TOKEN in (body or "")]
        assert not kept, f"rowid(s) {kept} shipped the credential unredacted"

    def test_the_report_counts_every_row_it_changed(self, tmp_path):
        """A count that omits the skipped rows is how the bypass stayed invisible."""
        stage, _ = self._staged(tmp_path)
        report = snapshot_redact.redact_bundle_for_egress(stage)
        assert report.replacements.get("memory.db") == 4, report.replacements


class TestAWriteOnlyTriggerThatReplacesARowIsRefused:
    """A body with no `DELETE` can still destroy a row, and those forms are refused.

    `INSERT OR REPLACE` and `REPLACE INTO` delete the conflicting row and insert in its
    place. The refusal looked only for `DELETE FROM`, so a trigger whose body only ever
    "writes" could clobber a row in a table this pass never looked at -- and the fixpoint
    can observe a settled database, never bring back a row that is gone.

    Both directions are asserted, because a guard that fires on everything is as wrong as
    one that fires on nothing: the two REPLACE spellings refuse, and a plain value-copying
    trigger still goes through the fixpoint and gets cleaned. Refusing every non-FTS UPDATE
    trigger was measured twice and rejected -- it discards the fixpoint and refuses the
    product's own external-content index -- so the narrowness here is the point.
    """

    TOKEN = "AKIAIOSFODNN7EXAMPLE"

    def _staged(self, tmp_path, schema: str, *, with_other: bool):
        stage = tmp_path / "stage"
        stage.mkdir()
        db = stage / "memory.db"
        conn = snap.sqlite3.connect(str(db))
        conn.executescript(schema)
        if with_other:
            conn.execute("INSERT INTO other(id, keep) VALUES(1, 'operator data')")
        conn.execute("INSERT INTO t(id, body) VALUES(1, ?)", (f"secret {self.TOKEN}",))
        conn.commit()
        conn.close()
        return stage, db

    def test_insert_or_replace_in_an_update_trigger_refuses(self, tmp_path):
        stage, _ = self._staged(
            tmp_path,
            """
            CREATE TABLE t(id INTEGER PRIMARY KEY, body TEXT);
            CREATE TABLE other(id INTEGER PRIMARY KEY, keep TEXT);
            CREATE TRIGGER clobber AFTER UPDATE ON t BEGIN
              INSERT OR REPLACE INTO other(id, keep) VALUES (1, 'stomped');
            END;
            """,
            with_other=True,
        )
        with pytest.raises(snapshot_redact.PayloadDatabaseUnprovable):
            snapshot_redact.redact_bundle_for_egress(stage)

    def test_replace_into_in_an_update_trigger_refuses(self, tmp_path):
        stage, _ = self._staged(
            tmp_path,
            """
            CREATE TABLE t(id INTEGER PRIMARY KEY, body TEXT);
            CREATE TABLE other(id INTEGER PRIMARY KEY, keep TEXT);
            CREATE TRIGGER clobber AFTER UPDATE ON t BEGIN
              REPLACE INTO other(id, keep) VALUES (1, 'stomped');
            END;
            """,
            with_other=True,
        )
        with pytest.raises(snapshot_redact.PayloadDatabaseUnprovable):
            snapshot_redact.redact_bundle_for_egress(stage)

    def test_a_plain_value_writing_trigger_is_still_cleaned_not_refused(self, tmp_path):
        """The discrimination half. Refusing this would discard the fixpoint."""
        stage, db = self._staged(
            tmp_path,
            """
            CREATE TABLE t(id INTEGER PRIMARY KEY, body TEXT, mirror TEXT);
            CREATE TRIGGER copyval AFTER UPDATE ON t BEGIN
              UPDATE t SET mirror = NEW.body WHERE id = NEW.id;
            END;
            """,
            with_other=False,
        )
        snapshot_redact.redact_bundle_for_egress(stage)
        conn = snap.sqlite3.connect(str(db))
        body = conn.execute("SELECT body FROM t WHERE id=1").fetchone()[0]
        conn.close()
        assert self.TOKEN not in body, "the fixpoint no longer cleans a value-writing trigger"


class TestFtsDetectionIsCaseInsensitive:
    """SQLite stores DDL verbatim, and its own docs write `USING FTS5(...)`.

    The detector folded only the all-lowercase spelling, so a table created the documented
    way was not recognised as full-text: its `WITHOUT ROWID` shadow tables were scanned as
    ordinary ones and the pager's `SELECT MAX(<handle>)` raised `no such column: rowid`,
    refusing the ENTIRE backup of a sound database. A legitimate backup lost to a spelling.

    Both halves are pinned: the uppercase table now redacts, AND the contentless refusal
    still fires for an uppercase declaration -- a case fix that quietly stopped recognising
    `content=''` would trade a false refusal for a real leak.
    """

    TOKEN = "AKIAIOSFODNN7EXAMPLE"

    def _staged(self, tmp_path, ddl: str):
        stage = tmp_path / "stage"
        stage.mkdir()
        db = stage / "memory.db"
        conn = snap.sqlite3.connect(str(db))
        conn.executescript(ddl)
        conn.execute("INSERT INTO docs(id, body) VALUES(1, ?)", (f"secret {self.TOKEN}",))
        conn.commit()
        conn.close()
        return stage, db

    def test_an_uppercase_fts5_table_does_not_refuse_a_sound_database(self, tmp_path):
        stage, db = self._staged(
            tmp_path,
            """
            CREATE TABLE docs(id INTEGER PRIMARY KEY, body TEXT);
            CREATE VIRTUAL TABLE docs_fts USING FTS5(body, content='docs', content_rowid='id');
            """,
        )
        snapshot_redact.redact_bundle_for_egress(stage)
        conn = snap.sqlite3.connect(str(db))
        body = conn.execute("SELECT body FROM docs WHERE id=1").fetchone()[0]
        conn.close()
        assert self.TOKEN not in body

    def test_a_contentless_index_is_still_refused_when_declared_uppercase(self, tmp_path):
        """The half a naive case fix would lose."""
        stage, _ = self._staged(
            tmp_path,
            """
            CREATE TABLE docs(id INTEGER PRIMARY KEY, body TEXT);
            CREATE VIRTUAL TABLE docs_fts USING FTS5(body, CONTENT='');
            """,
        )
        with pytest.raises(snapshot_redact.PayloadDatabaseUnprovable):
            snapshot_redact.redact_bundle_for_egress(stage)


class TestAGeneratedColumnCannotCarryACredentialOffHost:
    """`PRAGMA table_info` omits GENERATED columns, and they are read-only.

    So the row pass can neither see nor rewrite one. Two existing guards cover most of the
    exposure -- the schema scan refuses a credential written as a literal in the generation
    expression, and a STORED column is recomputed when its source is updated -- but neither
    covers a credential ASSEMBLED across columns: no single value matches, the DDL holds
    nothing, and the generated column materialises the whole key.

    Reproduced before the fix with `a='AKIA'`, `b='IOSFODNN7EXAMPLE'` and
    `joined AS (a || b) STORED`: nothing refused, and the outbound copy carried the key.

    Refused rather than redacted, because a derived column cannot be rewritten in place. The
    discrimination is asserted too: a generated column with nothing sensitive must NOT refuse,
    and an FTS table -- whose own columns present as hidden flag 1 rather than 2 or 3 -- must
    keep working, or this hardening becomes an outage for every full-text backup.
    """

    HEAD = "AKIA"
    TAIL = "IOSFODNN7EXAMPLE"

    def _staged(self, tmp_path, ddl: str, rows: list[tuple]):
        stage = tmp_path / "stage"
        stage.mkdir()
        conn = snap.sqlite3.connect(str(stage / "memory.db"))
        conn.executescript(ddl)
        for row in rows:
            conn.execute(f"INSERT INTO t VALUES({', '.join('?' * len(row))})", row)
        conn.commit()
        conn.close()
        return stage

    _GEN = (
        "CREATE TABLE t(id INTEGER PRIMARY KEY, a TEXT, b TEXT, "
        "joined TEXT GENERATED ALWAYS AS (a || b) STORED);"
    )

    def test_a_credential_assembled_across_columns_refuses_the_upload(self, tmp_path):
        stage = self._staged(tmp_path, self._GEN, [(1, self.HEAD, self.TAIL)])
        with pytest.raises(snapshot_redact.PayloadDatabaseUnprovable) as e:
            snapshot_redact.redact_bundle_for_egress(stage)
        assert "GENERATED column" in str(e.value)

    def test_a_generated_column_with_nothing_sensitive_is_not_refused(self, tmp_path):
        """The discrimination half: refusing every generated column would be an outage."""
        stage = self._staged(tmp_path, self._GEN, [(1, "hello", "world")])
        snapshot_redact.redact_bundle_for_egress(stage)

    def test_a_bytes_valued_generated_column_is_scanned_too(self, tmp_path):
        """`isinstance(value, str)` alone would walk straight past this one.

        The form matters. `a || b` over blobs yields TEXT, so that shape is caught by a
        str-only filter anyway; `CAST(... AS BLOB)` genuinely stores bytes -- verified by
        `typeof()` reporting `blob` and the driver handing back a `bytes` -- which is what
        makes the bytes branch reachable rather than merely defensive.
        """
        stage = tmp_path / "stage"
        stage.mkdir()
        conn = snap.sqlite3.connect(str(stage / "memory.db"))
        conn.executescript(
            "CREATE TABLE t(id INTEGER PRIMARY KEY, a TEXT, b TEXT, "
            "joined BLOB GENERATED ALWAYS AS (CAST(a || b AS BLOB)) STORED);"
        )
        conn.execute("INSERT INTO t(id, a, b) VALUES(1, ?, ?)", (self.HEAD, self.TAIL))
        conn.commit()
        kind = conn.execute("SELECT typeof(joined) FROM t").fetchone()[0]
        conn.close()
        assert kind == "blob", f"probe no longer stores bytes ({kind}) -- retarget this test"

        with pytest.raises(snapshot_redact.PayloadDatabaseUnprovable):
            snapshot_redact.redact_bundle_for_egress(stage)

    def test_an_fts_table_is_untouched_by_the_generated_column_check(self, tmp_path):
        """FTS columns present as hidden flag 1, not 2 or 3, so they must not be swept in."""
        stage = tmp_path / "stage"
        stage.mkdir()
        conn = snap.sqlite3.connect(str(stage / "memory.db"))
        conn.executescript("""
            CREATE TABLE docs(id INTEGER PRIMARY KEY, body TEXT);
            CREATE VIRTUAL TABLE docs_fts USING fts5(body, content='docs', content_rowid='id');
            """)
        conn.execute("INSERT INTO docs(id, body) VALUES(1, ?)", (f"secret {self.HEAD}{self.TAIL}",))
        conn.commit()
        conn.close()
        snapshot_redact.redact_bundle_for_egress(stage)
        conn = snap.sqlite3.connect(str(stage / "memory.db"))
        body = conn.execute("SELECT body FROM docs WHERE id=1").fetchone()[0]
        conn.close()
        assert self.HEAD + self.TAIL not in body


class TestAStaleFtsIndexIsRebuiltEvenWithNoRowHits:
    """The FTS rebuild used to be gated on `hits`, which cannot see this case.

    An external-content FTS index keeps its own tokenized copy and does NOT auto-sync, so a
    base table can move on and leave the index holding text no live row contains. The row scan
    then reports `hits == 0` -- correctly, the rows are clean -- and a rebuild gated on that
    never runs. VACUUM does not save it either: VACUUM repacks live content and never
    re-tokenizes, and the stale doclist IS live content of the shadow table.

    Reproduced before the fix: base row updated to drop the credential without syncing the
    index, no refusal, and the egress copy still answered a MATCH for the key afterwards.

    Asserted through a QUERY, and on the lowercased form in the bytes, because FTS5's default
    tokenizer lowercases terms -- checking only the credential's own casing reports a clean
    file for a database that still answers a search for it.
    """

    HEAD = "AKIA"
    TAIL = "IOSFODNN7EXAMPLE"

    def _staged(self, tmp_path, *, go_stale: bool):
        stage = tmp_path / "stage"
        stage.mkdir()
        conn = snap.sqlite3.connect(str(stage / "memory.db"))
        conn.executescript("""
            CREATE TABLE docs(id INTEGER PRIMARY KEY, body TEXT);
            CREATE VIRTUAL TABLE docs_fts USING fts5(body, content='docs', content_rowid='id');
            """)
        conn.execute(
            "INSERT INTO docs(id, body) VALUES(1, ?)", (f"key {self.HEAD}{self.TAIL} here",)
        )
        conn.execute("INSERT INTO docs_fts(docs_fts) VALUES('rebuild')")
        conn.commit()
        if go_stale:
            # The base table moves on without the index -- the ordinary external-content
            # situation, with no trigger here to sync it.
            conn.execute("UPDATE docs SET body = 'key redacted here' WHERE id = 1")
            conn.commit()
        conn.close()
        return stage

    def _searchable(self, stage) -> int:
        conn = snap.sqlite3.connect(str(stage / "memory.db"))
        try:
            return conn.execute(
                "SELECT count(*) FROM docs_fts WHERE docs_fts MATCH ?",
                (self.HEAD + self.TAIL,),
            ).fetchone()[0]
        finally:
            conn.close()

    def test_a_stale_index_is_cleaned_even_though_no_row_matched(self, tmp_path):
        stage = self._staged(tmp_path, go_stale=True)
        assert self._searchable(stage) == 1, "probe did not create a stale index"

        snapshot_redact.redact_bundle_for_egress(stage)

        assert self._searchable(stage) == 0, "stale index still answers a search for the key"
        raw = (stage / "memory.db").read_bytes()
        assert (self.HEAD + self.TAIL).lower().encode() not in raw

    def test_the_ordinary_case_still_works(self, tmp_path):
        stage = self._staged(tmp_path, go_stale=False)
        snapshot_redact.redact_bundle_for_egress(stage)
        assert self._searchable(stage) == 0


class TestATriggerOverwritingAnotherTableIsRefused:
    """The redaction's own UPDATE can fire a trigger that overwrites UNRELATED rows.

    Not a credential-survival problem -- a data-loss one, which is why the earlier trigger
    measurements do not answer it. `UPDATE unrelated SET note='CLOBBERED'` runs when this pass
    rewrites a value, and the operator's row reads 'CLOBBERED' in the copy that is uploaded
    and later RESTORED. The fixpoint can observe a settled database; it cannot restore a value
    that is gone.

    Refused by TARGET, and the scope is the whole point. Two broader rules were measured and
    fail: refusing every non-FTS UPDATE-writing trigger discards the fixpoint and the
    product's own FTS index, and refusing any body containing an `UPDATE` breaks a mirror
    column, which is ordinary and provably cleaned. The three tests below are the shapes that
    must stay allowed; without them this class would read as a licence to widen the rule.
    """

    KEY = "AKIA" + "IOSFODNN7EXAMPLE"

    def _run(self, tmp_path, ddl: str, insert_body: str = "items"):
        stage = tmp_path / "stage"
        stage.mkdir()
        conn = snap.sqlite3.connect(str(stage / "memory.db"))
        conn.executescript(ddl)
        conn.execute(f"INSERT INTO {insert_body}(body) VALUES(?)", (f"key {self.KEY} here",))
        conn.commit()
        conn.close()
        return stage

    def test_an_update_to_another_table_refuses(self, tmp_path):
        stage = self._run(
            tmp_path,
            """
            CREATE TABLE items(id INTEGER PRIMARY KEY, body TEXT);
            CREATE TABLE unrelated(id INTEGER PRIMARY KEY, note TEXT);
            INSERT INTO unrelated(id, note) VALUES(1, 'the operator''s own note');
            CREATE TRIGGER clobber AFTER UPDATE ON items BEGIN
              UPDATE unrelated SET note = 'CLOBBERED';
            END;
            """,
        )
        with pytest.raises(snapshot_redact.PayloadDatabaseUnprovable):
            snapshot_redact.redact_bundle_for_egress(stage)
        # Refused BEFORE the damage, not after: the note must still be the operator's.
        conn = snap.sqlite3.connect(str(stage / "memory.db"))
        note = conn.execute("SELECT note FROM unrelated WHERE id=1").fetchone()[0]
        conn.close()
        assert note == "the operator's own note"

    def test_an_unbound_update_on_its_own_table_refuses(self, tmp_path):
        """The residual the previous rule left open, now closed.

        `UPDATE items SET note='CLOBBERED'` names the trigger's own table, so a rule keyed only
        on the target let it through -- and with no row bound it rewrites EVERY row of the table
        it fires on. Distinguished from the mirror column below by whether the statement is
        bound to the triggering row at all, not by which table it names.
        """
        stage = self._run(
            tmp_path,
            """
            CREATE TABLE items(id INTEGER PRIMARY KEY, body TEXT, note TEXT);
            CREATE TRIGGER clobber_self AFTER UPDATE ON items BEGIN
              UPDATE items SET note = 'CLOBBERED';
            END;
            """,
        )
        with pytest.raises(snapshot_redact.PayloadDatabaseUnprovable):
            snapshot_redact.redact_bundle_for_egress(stage)

    def test_a_mirror_column_on_its_own_table_is_still_allowed(self, tmp_path):
        """Ordinary shape, and the fixpoint cleans it -- refusing it would be an outage."""
        stage = self._run(
            tmp_path,
            """
            CREATE TABLE items(id INTEGER PRIMARY KEY, body TEXT, mirror TEXT);
            CREATE TRIGGER copyval AFTER UPDATE ON items BEGIN
              UPDATE items SET mirror = NEW.body WHERE id = NEW.id;
            END;
            """,
        )
        snapshot_redact.redact_bundle_for_egress(stage)
        conn = snap.sqlite3.connect(str(stage / "memory.db"))
        body = conn.execute("SELECT body FROM items WHERE id=1").fetchone()[0]
        conn.close()
        assert self.KEY not in body

    def test_an_insert_of_a_copy_is_still_allowed(self, tmp_path):
        """An inserted copy is what the fixpoint exists for; only overwrites are refused."""
        stage = self._run(
            tmp_path,
            """
            CREATE TABLE items(id INTEGER PRIMARY KEY, body TEXT);
            CREATE TABLE audit(note TEXT);
            CREATE TRIGGER keep_old AFTER UPDATE ON items BEGIN
              INSERT INTO audit(note) VALUES (OLD.body);
            END;
            """,
        )
        snapshot_redact.redact_bundle_for_egress(stage)
        conn = snap.sqlite3.connect(str(stage / "memory.db"))
        rows = conn.execute("SELECT note FROM audit").fetchall()
        conn.close()
        assert all(self.KEY not in (r[0] or "") for r in rows)


class TestAWideEncodedCredentialCannotRideOutUnscanned:
    """UTF-16LE with no BOM decodes as valid UTF-8, because NUL is a legal codepoint.

    So the read succeeds, the text reads `A\\x00K\\x00I\\x00A...`, and no credential pattern can
    match across the NULs. `_scrub` returns 0 hits and the file is reported handled while the
    credential rides out intact. The NUL check that would have caught it sat inside `if hits:`,
    where it only ever guarded against CORRUPTING a NUL-bearing file that did match.

    Treating every NUL-bearing file as opaque was the prescribed remedy and is wrong: a workspace
    routinely holds binary blobs, and `test_a_nul_bearing_file_with_no_credential_still_rides`
    asserts an ordinary tar rides. Scanning the wide-encoding interpretations closes the bypass
    without refusing files that carry nothing -- both halves are asserted below.
    """

    KEY = "AKIA" + "IOSFODNN7EXAMPLE"

    def _stage(self, tmp_path):
        stage = tmp_path / "stage"
        (stage / "workspace").mkdir(parents=True)
        return stage

    def test_a_utf16le_credential_refuses_the_upload(self, tmp_path):
        stage = self._stage(tmp_path)
        f = stage / "workspace" / "notes.md"
        f.write_bytes(f"my key is {self.KEY} keep safe".encode("utf-16-le"))
        # The premise: nothing in this file matches as plain ASCII.
        assert self.KEY.encode() not in f.read_bytes()

        with pytest.raises(snapshot_redact.OpaqueFilesPresent):
            snapshot_redact.redact_bundle_for_egress(stage)

    def test_a_utf16be_credential_refuses_too(self, tmp_path):
        stage = self._stage(tmp_path)
        f = stage / "workspace" / "notes.md"
        f.write_bytes(f"my key is {self.KEY} keep safe".encode("utf-16-be"))
        with pytest.raises(snapshot_redact.OpaqueFilesPresent):
            snapshot_redact.redact_bundle_for_egress(stage)

    def test_a_utf32le_credential_refuses_too(self, tmp_path):
        """UTF-16 coverage alone does not close this.

        Read as UTF-16LE, a UTF-32LE credential is STILL NUL-separated -- `A\\x00` then `\\x00\\x00`
        -- so it survived the UTF-16-only scan exactly as it survived the original one.
        """
        stage = self._stage(tmp_path)
        f = stage / "workspace" / "notes.md"
        f.write_bytes(f"my key is {self.KEY} keep safe".encode("utf-32-le"))
        with pytest.raises(snapshot_redact.OpaqueFilesPresent):
            snapshot_redact.redact_bundle_for_egress(stage)

    def test_a_wide_encoded_blob_column_refuses(self, tmp_path):
        """The column path had the same hole for the same reason.

        It decodes latin-1, which is lossless byte-to-codepoint and therefore PRESERVES the NUL
        spacing, so its zero-hit result was not evidence of a clean value.
        """
        stage = tmp_path / "stage"
        stage.mkdir()
        conn = snap.sqlite3.connect(str(stage / "memory.db"))
        conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v BLOB)")
        conn.execute("INSERT INTO t(id, v) VALUES(1, ?)", (self.KEY.encode("utf-16-le"),))
        conn.commit()
        conn.close()
        with pytest.raises(snapshot_redact.PayloadDatabaseUnprovable):
            snapshot_redact.redact_bundle_for_egress(stage)

    def test_a_plain_ascii_credential_in_a_column_is_still_redacted(self, tmp_path):
        """The discrimination half for the column path: refuse only what cannot be rewritten."""
        stage = tmp_path / "stage"
        stage.mkdir()
        conn = snap.sqlite3.connect(str(stage / "memory.db"))
        conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
        conn.execute("INSERT INTO t(id, v) VALUES(1, ?)", (f"key {self.KEY} here",))
        conn.commit()
        conn.close()

        snapshot_redact.redact_bundle_for_egress(stage)

        conn = snap.sqlite3.connect(str(stage / "memory.db"))
        got = conn.execute("SELECT v FROM t WHERE id=1").fetchone()[0]
        conn.close()
        assert self.KEY not in got

    def test_a_nul_bearing_file_with_no_credential_is_untouched(self, tmp_path):
        """The discrimination half -- refusing this would cost a legitimate upload."""
        stage = self._stage(tmp_path)
        f = stage / "workspace" / "blob.bin"
        raw = b"\x00\x01binary\x00payload\x00nothing secret\x00"
        f.write_bytes(raw)
        snapshot_redact.redact_bundle_for_egress(stage)
        assert f.read_bytes() == raw
