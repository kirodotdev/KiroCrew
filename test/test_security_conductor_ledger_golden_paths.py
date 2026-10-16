"""The findings ledger's golden paths: the legitimate operations a fix is judged against.

The table arrives additively on an existing ledger, holds one row per operation per
platform, widens only through a human approval, validates what it stores, and imports
the shipped corpus. Shared fixtures and builders live in
``security_conductor_ledger_helpers.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from security_conductor_ledger_helpers import (  # noqa: F401 -- fixtures are used by name
    db_fixture,
    mod_fixture,
    out_json,
    run,
    script_source_with_joined_literals,
)


def retire_by_hand(mod, db: Path, path_id: int) -> None:
    """The RFC's retirement: a human row edit, not a CLI verb.

    There is deliberately no ``deactivate-golden-path`` -- a CLI that retired rows
    would let the agent whose fix broke an operation shrink the corpus the fix is
    judged against -- so a test that needs a retired row does what the human does.
    """
    conn = mod.connect(db)
    try:
        with conn:
            conn.execute("UPDATE golden_paths SET active = 0 WHERE id = ?", (path_id,))
    finally:
        conn.close()


class TestGoldenPathsMigrateAdditively:
    """A ledger written before this table existed must open, keep its rows, and
    gain the table -- an audit in flight is exactly when a schema bump lands."""

    def a_v1_database(self, path: Path) -> None:
        """A v1 ledger, built the way one really exists on disk.

        Written with raw SQL rather than by calling an older copy of the script,
        because what has to migrate is the FILE, and reconstructing it here is the
        only way to have one that predates the current DDL.
        """
        import sqlite3

        conn = sqlite3.connect(str(path))
        conn.executescript("""
            CREATE TABLE schema_version (version INTEGER NOT NULL);
            CREATE UNIQUE INDEX schema_version_single ON schema_version (version);
            INSERT INTO schema_version (version) VALUES (1);
            CREATE TABLE findings (
                id INTEGER PRIMARY KEY AUTOINCREMENT, surface TEXT NOT NULL,
                severity TEXT NOT NULL, title TEXT NOT NULL, paths TEXT NOT NULL,
                poc TEXT, auditor_verdict TEXT, verifier_verdict TEXT,
                final_verdict TEXT, status TEXT NOT NULL, created TEXT NOT NULL,
                round_id TEXT
            );
            INSERT INTO findings (surface, severity, title, paths, status, created)
                VALUES ('security', 'High', 'legacy finding', '[]', 'open',
                        '2000-01-01T00:00:00+00:00');
            """)
        conn.commit()
        conn.close()

    def test_an_existing_v1_ledger_opens_and_gains_the_table(self, mod, db, capsys):
        self.a_v1_database(db)
        assert run(mod, db, "init") == 0
        assert out_json(capsys)["schema_version"] == mod.SCHEMA_VERSION
        run(mod, db, "list", "golden-paths")
        assert out_json(capsys) == []

    def test_the_migration_keeps_the_rows_that_were_there(self, mod, db, capsys):
        """Additive means additive: the bump must not be a rebuild."""
        self.a_v1_database(db)
        run(mod, db, "list", "findings")
        rows = out_json(capsys)
        assert [row["title"] for row in rows] == ["legacy finding"]

    def test_the_version_is_not_walked_backwards(self, mod, db, capsys):
        """A ledger written by a newer checkout, opened by this one, is left alone.

        The guard is ``<`` rather than ``!=`` for this case: overwriting a higher
        version would record a downgrade whose tables this code cannot recreate.
        """
        run(mod, db, "init")
        capsys.readouterr()
        conn = mod.connect(db)
        try:
            with conn:
                conn.execute("UPDATE schema_version SET version = 99")
        finally:
            conn.close()
        run(mod, db, "init")
        assert out_json(capsys)["schema_version"] == 99

    def test_the_version_row_stays_a_singleton_across_the_bump(self, mod, db, capsys):
        """The bump is an UPDATE, not an INSERT: a second row would be a second
        answer to the question a migration ladder branches on."""
        self.a_v1_database(db)
        run(mod, db, "init")
        capsys.readouterr()
        conn = mod.connect(db)
        try:
            count = conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
        finally:
            conn.close()
        assert count == 1


class TestGoldenPathIdentity:
    """One row per legitimate operation, per platform."""

    def test_the_same_command_is_recorded_once(self, mod, db, capsys):
        for _ in range(2):
            run(
                mod,
                db,
                "propose-golden-path",
                "--kind",
                "shell",
                "--surface",
                "gh-read",
                "--command",
                "gh pr view 1 --json state",
                "--reason",
                "reading a PR is how a loop decides what to do next",
            )
        second = out_json(capsys)
        assert second["created"] is False
        assert second["id"] == 1

    def test_a_dedupe_hit_reports_the_stored_state_not_the_requested_one(self, mod, db, capsys):
        """Re-proposing a known identity reports the row as stored, never as asked."""
        argv = [
            "--kind",
            "shell",
            "--surface",
            "gh-read",
            "--command",
            "gh pr view 1 --json state",
            "--reason",
            "reading a PR is how a loop decides what to do next",
        ]
        run(mod, db, "propose-golden-path", *argv)
        path_id = out_json(capsys)["id"]
        run(mod, db, "approve-golden-path", "--id", str(path_id), "--approved-by", "reviewer")
        capsys.readouterr()
        # An approved identity re-proposed reports the STORED state: active.
        run(mod, db, "propose-golden-path", *argv)
        assert out_json(capsys) == {"id": path_id, "created": False, "active": 1}
        # And retired, the same re-proposal reports it inert rather than reviving it.
        retire_by_hand(mod, db, path_id)
        capsys.readouterr()
        run(mod, db, "propose-golden-path", *argv)
        assert out_json(capsys) == {"id": path_id, "created": False, "active": 0}

    def test_the_platform_is_part_of_the_identity(self, mod, db, capsys):
        """The same text is a different claim on each host, so collapsing them
        would make importing a Windows row skip because a POSIX one is present."""
        for platform in ("posix", "windows"):
            run(
                mod,
                db,
                "propose-golden-path",
                "--kind",
                "shell",
                "--surface",
                "tests",
                "--command",
                "python -m pytest -n0 -q",
                "--platform",
                platform,
                "--reason",
                "the sanctioned test invocation",
            )
            assert out_json(capsys)["created"] is True

    def test_the_kind_is_part_of_the_identity(self, mod, db, capsys):
        """A kind is a checking STRATEGY, so the same text classified and the same
        text run are two different assertions."""
        for kind in ("shell", "flow"):
            run(
                mod,
                db,
                "propose-golden-path",
                "--kind",
                kind,
                "--surface",
                "git-read",
                "--command",
                "git rev-parse HEAD",
                "--reason",
                "the head SHA is what a lease is keyed to",
            )
            assert out_json(capsys)["created"] is True

    def test_interior_spacing_is_preserved(self, mod, db, capsys):
        """A shell row's interior spacing is part of the shape the fence sees, so
        normalising it would make the corpus assert a command nobody runs."""
        spaced = "gh pr view 1 --json state ; gh run list"
        run(
            mod,
            db,
            "propose-golden-path",
            "--kind",
            "shell",
            "--surface",
            "gh-read",
            "--command",
            f"  {spaced}  ",
            "--reason",
            "two reads joined by a separator were false-positive refused",
        )
        capsys.readouterr()
        run(mod, db, "list", "golden-paths")
        assert out_json(capsys)[0]["command_or_flow"] == spaced


class TestGoldenPathApprovalIsTheOnlyWideningWrite:
    def test_a_proposed_path_is_inert(self, mod, db, capsys):
        run(
            mod,
            db,
            "propose-golden-path",
            "--kind",
            "shell",
            "--surface",
            "gh-read",
            "--command",
            "gh pr list --json number",
            "--reason",
            "enumerating PRs is the collision check",
        )
        assert out_json(capsys)["active"] == 0
        capsys.readouterr()
        run(mod, db, "list", "golden-paths")
        assert [row["active"] for row in out_json(capsys)] == [0]

    def test_approval_activates_and_attributes(self, mod, db, capsys):
        run(
            mod,
            db,
            "propose-golden-path",
            "--kind",
            "shell",
            "--surface",
            "gh-read",
            "--command",
            "gh pr list --json number",
            "--reason",
            "enumerating PRs is the collision check",
        )
        path_id = out_json(capsys)["id"]
        run(mod, db, "approve-golden-path", "--id", str(path_id), "--approved-by", "reviewer")
        assert out_json(capsys) == {"active": 1, "approved_by": "reviewer", "id": path_id}

    def test_approval_is_write_once(self, mod, db, capsys):
        run(
            mod,
            db,
            "propose-golden-path",
            "--kind",
            "shell",
            "--surface",
            "gh-read",
            "--command",
            "gh pr list --json number",
            "--reason",
            "enumerating PRs is the collision check",
        )
        path_id = out_json(capsys)["id"]
        run(mod, db, "approve-golden-path", "--id", str(path_id), "--approved-by", "first")
        capsys.readouterr()
        assert (
            run(mod, db, "approve-golden-path", "--id", str(path_id), "--approved-by", "second")
            == 2
        )
        assert "already approved by 'first'" in capsys.readouterr().err

    def test_a_retired_path_cannot_be_re_approved_by_someone_else(self, mod, db, capsys):
        """Retiring is not un-approving: the row still names who admitted it, and a
        second approval would replace that name. Retirement is the RFC's hand row
        edit; there is no CLI verb for it."""
        run(
            mod,
            db,
            "propose-golden-path",
            "--kind",
            "shell",
            "--surface",
            "gh-read",
            "--command",
            "gh pr list --json number",
            "--reason",
            "enumerating PRs is the collision check",
        )
        path_id = out_json(capsys)["id"]
        run(mod, db, "approve-golden-path", "--id", str(path_id), "--approved-by", "first")
        retire_by_hand(mod, db, path_id)
        capsys.readouterr()
        assert (
            run(mod, db, "approve-golden-path", "--id", str(path_id), "--approved-by", "second")
            == 2
        )
        assert "approved by 'first' and later retired" in capsys.readouterr().err

    def test_there_is_no_cli_verb_that_retires_a_golden_path(self, mod, db):
        """The RFC gates deactivation exactly like activation. A verb here would let
        the agent whose fix broke an operation shrink the corpus its fix is judged
        against, so retirement stays the human's row edit."""
        with pytest.raises(SystemExit) as excinfo:
            run(mod, db, "deactivate-golden-path", "--id", "1")
        assert excinfo.value.code == 2
        source = script_source_with_joined_literals()
        assert 'command("deactivate-golden-path"' not in source
        assert "def deactivate_golden_path" not in source
        # The one UPDATE that flips a golden path's active flag is the approval, and
        # it flips it ON. Nothing in this CLI flips it off.
        # Only lines that are SQL handed to execute(): the module docstring quotes
        # the human's own UPDATE as prose, and the approval error message prints the
        # re-enable spelling. Neither is a write this CLI performs.
        updates = [
            line.strip()
            for line in source.splitlines()
            if "UPDATE golden_paths SET active" in line and line.strip().startswith('"')
        ]
        assert updates, "the approval UPDATE must still exist"
        assert all("active = 1" in line for line in updates), updates

    def test_an_unknown_id_is_refused(self, mod, db, capsys):
        assert run(mod, db, "approve-golden-path", "--id", "404", "--approved-by", "x") == 2

    def test_there_is_no_one_step_verb_that_records_an_active_golden_path(self, mod, db):
        """The RFC names propose, approve and import. A verb that wrote an active,
        attributed row in one step would be the approval without the write-once
        record around it, so the widening write has exactly one spelling."""
        with pytest.raises(SystemExit) as excinfo:
            run(mod, db, "add-golden-path", "--kind", "shell", "--surface", "s")
        assert excinfo.value.code == 2
        assert 'command("add-golden-path"' not in script_source_with_joined_literals()


class TestGoldenPathValidation:
    @pytest.mark.parametrize("kind", ["deploy", "SHELL", ""])
    def test_an_unknown_kind_is_refused(self, mod, db, capsys, kind):
        argv = [
            "propose-golden-path",
            "--kind",
            kind,
            "--surface",
            "s",
            "--command",
            "c",
            "--reason",
            "r",
        ]
        if kind == "":
            with pytest.raises(SystemExit) as excinfo:
                run(mod, db, *argv)
            assert excinfo.value.code == 2
        else:
            assert run(mod, db, *argv) == 2

    def test_an_unknown_platform_is_refused(self, mod, db, capsys):
        assert (
            run(
                mod,
                db,
                "propose-golden-path",
                "--kind",
                "shell",
                "--surface",
                "s",
                "--command",
                "c",
                "--platform",
                "darwin",
                "--reason",
                "r",
            )
            == 2
        )
        assert "unknown platform 'darwin'" in capsys.readouterr().err

    def test_a_cited_finding_must_exist(self, mod, db, capsys):
        assert (
            run(
                mod,
                db,
                "propose-golden-path",
                "--kind",
                "shell",
                "--surface",
                "s",
                "--command",
                "c",
                "--reason",
                "r",
                "--source-finding",
                "404",
            )
            == 2
        )
        assert "no finding with id 404" in capsys.readouterr().err

    def test_a_blank_reason_is_refused(self, mod, db):
        """A golden path with no reason is a check nobody can judge when it fires."""
        with pytest.raises(SystemExit) as excinfo:
            run(
                mod,
                db,
                "propose-golden-path",
                "--kind",
                "shell",
                "--surface",
                "s",
                "--command",
                "c",
                "--reason",
                "   ",
            )
        assert excinfo.value.code == 2


class TestGoldenPathCorpusImport:
    def a_corpus(self, tmp_path: Path, rows) -> Path:
        path = tmp_path / "corpus.json"
        path.write_text(json.dumps({"golden_paths": rows}), encoding="utf-8")
        return path

    def a_row(self, **overrides):
        row = {
            "kind": "shell",
            "surface": "gh-read",
            "command_or_flow": "gh pr view 1 --json state",
            "platform": "any",
            "reason": "reading a PR is how a loop decides what to do next",
        }
        row.update(overrides)
        return row

    def validated(self, mod, rows):
        """Rows in the shape :func:`import_golden_paths` consumes.

        The CLI always validates before importing, so a test that hands it raw
        entries would be exercising a call the product never makes.
        """
        return [mod.validate_golden_path_row(row, index) for index, row in enumerate(rows)]

    def test_the_import_is_idempotent(self, mod, db, capsys, tmp_path):
        corpus = self.a_corpus(
            tmp_path, [self.a_row(), self.a_row(command_or_flow="git status --porcelain")]
        )
        run(mod, db, "import-golden-paths", str(corpus), "--approved-by", "reviewer")
        assert out_json(capsys) == {"imported": 2, "skipped": 0, "total": 2}
        run(mod, db, "import-golden-paths", str(corpus), "--approved-by", "reviewer")
        assert out_json(capsys) == {"imported": 0, "skipped": 2, "total": 2}

    def test_a_re_import_does_not_revive_a_retired_row(self, mod, db, capsys, tmp_path):
        """Re-importing the file a row came from is not a decision to bring it back:
        the retirement was a reviewer's call about this target."""
        corpus = self.a_corpus(tmp_path, [self.a_row()])
        run(mod, db, "import-golden-paths", str(corpus), "--approved-by", "reviewer")
        capsys.readouterr()
        run(mod, db, "list", "golden-paths")
        path_id = out_json(capsys)[0]["id"]
        retire_by_hand(mod, db, path_id)
        run(mod, db, "import-golden-paths", str(corpus), "--approved-by", "reviewer")
        capsys.readouterr()
        run(mod, db, "list", "golden-paths")
        assert out_json(capsys)[0]["active"] == 0

    def test_a_bare_json_list_is_refused(self, mod, db, capsys, tmp_path):
        """One accepted shape -- the object the committed export is written in. A
        second shape is surface nothing writes, and the gate reads through this
        same loader, so the two must not disagree about what a corpus is."""
        path = tmp_path / "bare.json"
        path.write_text(json.dumps([self.a_row()]), encoding="utf-8")
        assert run(mod, db, "import-golden-paths", str(path), "--approved-by", "reviewer") == 2
        assert "'golden_paths' list" in capsys.readouterr().err
        run(mod, db, "list", "golden-paths")
        assert out_json(capsys) == []

    def test_a_malformed_file_writes_nothing(self, mod, db, capsys, tmp_path):
        """All-or-nothing, and this is the failure that matters: a half-imported
        corpus is a set of checks the reviewer did not choose, and the rows that
        never landed are invisible."""
        corpus = self.a_corpus(
            tmp_path, [self.a_row(), self.a_row(command_or_flow="x", kind="wat")]
        )
        assert run(mod, db, "import-golden-paths", str(corpus), "--approved-by", "reviewer") == 2
        assert "entry 1: unknown kind 'wat'" in capsys.readouterr().err
        run(mod, db, "list", "golden-paths")
        assert out_json(capsys) == []

    @pytest.mark.parametrize(
        "row, fragment",
        [
            pytest.param({"reason": ""}, "blank or missing reason", id="blank-reason"),
            pytest.param({"surface": "  "}, "blank or missing surface", id="blank-surface"),
            pytest.param({"platform": "darwin"}, "unknown platform", id="bad-platform"),
            pytest.param({"source_finding_id": "1"}, "must be an integer", id="string-finding"),
            # ``bool`` subclasses ``int``, so an isinstance check accepts ``true``
            # and SQLite stores it as 1 -- the golden path would silently cite
            # finding 1, with nothing reported.
            pytest.param({"source_finding_id": True}, "must be an integer", id="bool-finding"),
            pytest.param({"source_finding_id": False}, "must be an integer", id="false-finding"),
            pytest.param({"source_finding_id": 1.0}, "must be an integer", id="float-finding"),
            # A text field that is not a JSON string is refused, never stringified:
            # ``str(["gh", "pr"])`` would store "['gh', 'pr']" as a golden path and the
            # gate would classify a command nobody runs instead of rejecting the row.
            pytest.param(
                {"command_or_flow": ["gh", "pr", "view"]},
                "command_or_flow must be a string, got list",
                id="argv-list-command",
            ),
            pytest.param({"reason": 42}, "reason must be a string, got int", id="int-reason"),
            pytest.param({"surface": {"k": 1}}, "surface must be a string", id="object-surface"),
            pytest.param({"kind": ["shell"]}, "kind must be a string", id="list-kind"),
            pytest.param({"platform": 1}, "platform must be a string", id="int-platform"),
            # A present null is not an omission: ``"platform": null`` must be refused,
            # not widened to ``any`` -- a host-specific row would otherwise gate every
            # host -- and a null reason is not a blank one.
            pytest.param(
                {"platform": None}, "platform must be a string, got NoneType", id="null-platform"
            ),
            pytest.param({"platform": ""}, "unknown platform ''", id="empty-platform"),
            pytest.param(
                {"reason": None}, "reason must be a string, got NoneType", id="null-reason"
            ),
        ],
    )
    def test_every_field_is_checked_before_any_write(
        self, mod, db, capsys, tmp_path, row, fragment
    ):
        corpus = self.a_corpus(tmp_path, [self.a_row(**row)])
        assert run(mod, db, "import-golden-paths", str(corpus), "--approved-by", "reviewer") == 2
        assert fragment in capsys.readouterr().err

    def test_the_whole_import_is_one_transaction(self, mod, db, capsys, tmp_path):
        """An interrupted import must leave NOTHING, not a subset.

        A subset is a fence nobody chose, and a silent one: the rows that landed are
        a corpus the reviewer did not approve as a whole, and the rows that did not
        are invisible. Simulated by failing the second insert, which is what an
        interruption between two per-row commits looked like.
        """
        rows = self.validated(mod, [self.a_row(), self.a_row(command_or_flow="git status -s")])
        real = mod._insert_golden_path
        calls = {"n": 0}

        def flaky(conn, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("interrupted")
            return real(conn, **kwargs)

        mod._insert_golden_path = flaky
        try:
            conn = mod.connect(db)
            try:
                mod.init_schema(conn)
                with pytest.raises(RuntimeError):
                    mod.import_golden_paths(conn, rows, approved_by="reviewer")
            finally:
                conn.close()
        finally:
            mod._insert_golden_path = real
        capsys.readouterr()
        run(mod, db, "list", "golden-paths")
        assert out_json(capsys) == [], "a partial corpus was committed"

    def test_a_row_another_writer_inserted_is_skipped_not_fatal(self, mod, db, capsys, tmp_path):
        """The identity index firing mid-import is a race, not a corpus error.

        SQLite rolls back the STATEMENT rather than the transaction on a constraint
        violation, so the rest of the corpus still lands atomically and the loser
        counts the row as skipped.
        """
        rows = self.validated(mod, [self.a_row(), self.a_row(command_or_flow="git status -s")])
        real = mod._find_golden_path

        def blind(conn, kind, key, platform):
            # Report every row as absent, so the pre-insert lookup misses the row
            # this test has already inserted and the index is what catches it.
            return None

        conn = mod.connect(db)
        try:
            mod.init_schema(conn)
            mod.propose_golden_path(
                conn,
                kind=rows[0]["kind"],
                surface=rows[0]["surface"],
                command_or_flow=rows[0]["command_or_flow"],
                platform=rows[0]["platform"],
                reason=rows[0]["reason"],
                source_finding_id=None,
            )
            mod._find_golden_path = blind
            try:
                result = mod.import_golden_paths(conn, rows, approved_by="reviewer")
            finally:
                mod._find_golden_path = real
        finally:
            conn.close()
        assert result == {"imported": 1, "skipped": 1, "total": 2}
        capsys.readouterr()
        run(mod, db, "list", "golden-paths")
        assert len(out_json(capsys)) == 2

    def test_an_absent_file_is_refused(self, mod, db, capsys, tmp_path):
        assert (
            run(
                mod,
                db,
                "import-golden-paths",
                str(tmp_path / "nowhere.json"),
                "--approved-by",
                "reviewer",
            )
            == 2
        )
        assert "cannot read corpus" in capsys.readouterr().err

    def test_a_cited_finding_must_exist_before_the_corpus_lands(self, mod, db, capsys, tmp_path):
        corpus = self.a_corpus(tmp_path, [self.a_row(source_finding_id=404)])
        assert run(mod, db, "import-golden-paths", str(corpus), "--approved-by", "reviewer") == 2
        assert "no finding with id 404" in capsys.readouterr().err
        run(mod, db, "list", "golden-paths")
        assert out_json(capsys) == []
