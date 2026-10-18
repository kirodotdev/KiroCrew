"""The findings ledger's table rebuilds, and the rows each one must keep.

A lesson may cite a policy block instead of a finding, which relaxes a NOT NULL
column, and the golden-path table accepts the ``test`` kind, which changes a CHECK
constraint. SQLite alters neither in place, so each is a table recreate on a ledger
an older checkout wrote, and these pin that the recreate loses nothing. Shared
fixtures and builders live in ``security_conductor_ledger_helpers.py``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from security_conductor_ledger_helpers import (  # noqa: F401 -- fixtures are used by name
    a_finding,
    a_lesson,
    db_fixture,
    mod_fixture,
    out_json,
    run,
    script_source_with_joined_literals,
)


class TestALessonMaySourceAPolicyBlock:
    """A fence that wrongly refused a legitimate operation must still teach.

    A ``policy_block`` is deliberately an EVENT and not a finding, so before this
    the retrospective could record the golden path that stops a future fix
    re-breaking the operation but had no id with which to store the guidance that
    stops the next auditor walking into the same refusal. The round that found this
    lost exactly that half: ``deny-classifier-fence`` filed no finding, so its
    lesson existed only as a line in a report.
    """

    def propose(self, mod, db, *extra: str) -> int:
        return run(
            mod,
            db,
            "propose-lesson",
            "--kind",
            "false-positive",
            "--surface",
            "deny-classifier-fence",
            "--pattern",
            "argv floor refused a read-only scan of committed source",
            "--guidance",
            "read the file with the file tool and hand the block up for a ruling",
            *extra,
        )

    def test_a_policy_block_sourced_lesson_reaches_the_seed_once_approved(
        self, mod, db, capsys
    ) -> None:
        """The whole point: no finding anywhere in this ledger, and a seed line."""
        assert self.propose(mod, db, "--source-policy-block", "m2-round-1/policy_block-1") == 0
        lesson_id = int(out_json(capsys)["id"])
        assert run(mod, db, "approve-lesson", "--id", str(lesson_id), "--approved-by", "zj") == 0
        capsys.readouterr()

        assert (
            run(
                mod,
                db,
                "seed-lessons",
                "--surface",
                "deny-classifier-fence",
                "--budget-bytes",
                "4000",
            )
            == 0
        )
        out = capsys.readouterr().out
        assert "policy block m2-round-1/policy_block-1" in out
        # The finding spelling must not leak into a line that has no finding: a seed
        # reader treating "finding #None" as an id would go looking for row None.
        assert "finding #" not in out

    def test_the_proposed_lesson_is_still_inert_until_approved(self, mod, db, capsys) -> None:
        """The second source widens WHAT may be cited, never the approval gate."""
        assert self.propose(mod, db, "--source-policy-block", "m2-round-1/policy_block-1") == 0
        assert out_json(capsys)["active"] == 0
        run(mod, db, "seed-lessons", "--surface", "deny-classifier-fence", "--budget-bytes", "4000")
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "no approved lesson to seed" in captured.err

    def test_neither_source_is_refused(self, mod, db, capsys) -> None:
        """The red-before case. A lesson attributable to nothing is the row that
        would break the seed's claim that guidance traces back to what earned it."""
        assert self.propose(mod, db) == 2
        assert "exactly one of --source-finding or --source-policy-block" in capsys.readouterr().err
        run(mod, db, "list", "lessons")
        assert out_json(capsys) == []

    def test_both_sources_are_refused(self, mod, db, capsys) -> None:
        """Two origins is as much a defect as none: a reader trusting the first
        field it checks would attribute the guidance to whichever that was."""
        finding = a_finding(mod, db, capsys)
        assert (
            self.propose(
                mod,
                db,
                "--source-finding",
                str(finding),
                "--source-policy-block",
                "m2-round-1/policy_block-1",
            )
            == 2
        )
        assert "exactly one of --source-finding or --source-policy-block" in capsys.readouterr().err
        run(mod, db, "list", "lessons")
        assert out_json(capsys) == []

    def test_a_blank_policy_block_reference_is_refused(self, mod, db, capsys) -> None:
        """``nonblank`` on the flag, for the same reason ``--approved-by`` carries it:
        a present-but-empty reference stores no attribution while satisfying the
        parser.

        The stderr assertion is what makes this test about blankness. Exit 2 alone is
        also what argparse returns for an option it does not recognise, so on a tree
        where the flag does not exist at all this would pass while proving nothing.
        """
        with pytest.raises(SystemExit) as excinfo:
            self.propose(mod, db, "--source-policy-block", "   ")
        assert excinfo.value.code == 2
        err = capsys.readouterr().err
        assert "must not be blank" in err
        assert "unrecognized" not in err

    def a_hand_written_lesson(self, conn, source_finding, source_block) -> None:
        """One lesson inserted the way a human editing rows really does it."""
        with conn:
            conn.execute(
                "INSERT INTO lessons (kind, surface, pattern, guidance,"
                " source_finding_id, source_policy_block, ts, active)"
                " VALUES ('missed', 's', 'p', 'g', ?, ?,"
                " '2026-09-12T00:00:00+00:00', 0)",
                (source_finding, source_block),
            )

    @pytest.mark.parametrize(
        "source_finding, source_block, case",
        [
            pytest.param(None, None, "no source at all", id="neither"),
            pytest.param(1, "pb-1", "two different origins", id="both"),
            pytest.param(None, "", "an empty reference", id="empty-string"),
            pytest.param(None, "   ", "a spaces-only reference", id="spaces"),
            pytest.param(None, "\t\n", "a tabs-and-newlines reference", id="ascii-whitespace"),
        ],
    )
    def test_the_check_refuses_an_unattributable_row_written_by_hand(
        self, mod, db, capsys, source_finding, source_block, case
    ) -> None:
        """The guarantee is the TABLE's, not the parser's.

        Anything that can run this script can open the same file with ``sqlite3``,
        so a constraint enforced only in :func:`_dispatch` would be advice rather
        than the storage guarantee the module docstring claims.

        The blank cases are the ones a NULL-only CHECK misses: ``''`` and ``'   '``
        are both PRESENT, so a row carrying one is attributable to nothing while
        satisfying IS NOT NULL -- and it arrives by exactly the direct-INSERT path
        that makes a parser-only rule insufficient in the first place.
        """
        run(mod, db, "init")
        capsys.readouterr()
        conn = mod.connect(db)
        try:
            with pytest.raises(Exception):
                self.a_hand_written_lesson(conn, source_finding, source_block)
        finally:
            conn.close()

    def test_a_reference_that_only_looks_blank_is_kept(self, mod, db, capsys) -> None:
        """The trim must not swallow a real reference.

        A guard that refused anything containing whitespace would reject the
        ``{round_id}/policy_block-N`` spellings a round actually writes, so the
        boundary is asserted from both sides: blank is refused above, and a
        reference with interior or surrounding whitespace is stored.
        """
        run(mod, db, "init")
        capsys.readouterr()
        conn = mod.connect(db)
        try:
            self.a_hand_written_lesson(conn, None, "  m2-round-1/policy_block 1  ")
        finally:
            conn.close()
        run(mod, db, "list", "lessons")
        rows = out_json(capsys)
        assert [row["source_policy_block"] for row in rows] == ["  m2-round-1/policy_block 1  "]

    def test_a_finding_sourced_lesson_still_reads_the_same(self, mod, db, capsys) -> None:
        """The line format for the original source kind is unchanged, because every
        seed already built from this ledger is that spelling."""
        finding = a_finding(mod, db, capsys)
        a_lesson(mod, db, capsys, finding)
        run(mod, db, "seed-lessons", "--surface", "security.is_denied", "--budget-bytes", "4000")
        out = capsys.readouterr().out
        assert f"(finding #{finding})" in out
        assert "policy block" not in out


class TestLessonsRebuildKeepsWhatWasThere:
    """The rebuild is the one migration step the ``IF NOT EXISTS`` ladder cannot take.

    SQLite has no ``ALTER COLUMN``, so relaxing ``source_finding_id`` from NOT NULL
    is a table recreate -- and a recreate is where rows get lost. These tests are
    about the FILE: a ledger written by the previous checkout, mid-audit, opened by
    this one.
    """

    def a_v2_database(self, path: Path) -> None:
        """A v2 ledger carrying two lessons, written the way one really exists.

        Raw SQL rather than an older copy of the script, for the same reason the v1
        fixture does it: what has to migrate is the file, and only spelling out the
        previous DDL produces one that predates the current shape.
        """
        import sqlite3

        conn = sqlite3.connect(str(path))
        conn.executescript("""
            CREATE TABLE schema_version (version INTEGER NOT NULL);
            CREATE UNIQUE INDEX schema_version_single ON schema_version (version);
            INSERT INTO schema_version (version) VALUES (2);
            CREATE TABLE findings (
                id INTEGER PRIMARY KEY AUTOINCREMENT, surface TEXT NOT NULL,
                severity TEXT NOT NULL, title TEXT NOT NULL, paths TEXT NOT NULL,
                poc TEXT, auditor_verdict TEXT, verifier_verdict TEXT,
                final_verdict TEXT, status TEXT NOT NULL, created TEXT NOT NULL,
                round_id TEXT
            );
            INSERT INTO findings (surface, severity, title, paths, status, created)
                VALUES ('token-session', 'Medium', 'legacy finding', '[]', 'open',
                        '2000-01-01T00:00:00+00:00');
            CREATE TABLE lessons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL CHECK (
                    kind IN ('true-positive', 'false-positive', 'missed', 'out-of-scope')
                ),
                surface TEXT NOT NULL,
                pattern TEXT NOT NULL,
                guidance TEXT NOT NULL,
                source_finding_id INTEGER NOT NULL REFERENCES findings(id),
                approved_by TEXT,
                ts TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX lessons_active ON lessons (active, surface);
            INSERT INTO lessons (id, kind, surface, pattern, guidance, source_finding_id,
                                 approved_by, ts, active)
                VALUES (7, 'true-positive', 'token-session', 'old pattern', 'old guidance',
                        1, 'a-human', '2000-01-02T00:00:00+00:00', 1);
            INSERT INTO lessons (id, kind, surface, pattern, guidance, source_finding_id,
                                 approved_by, ts, active)
                VALUES (9, 'missed', 'ingest-validation', 'unapproved pattern',
                        'unapproved guidance', 1, NULL,
                        '2000-01-03T00:00:00+00:00', 0);
            """)
        conn.commit()
        conn.close()

    def test_the_ledger_opens_and_reports_the_new_version(self, mod, db, capsys) -> None:
        self.a_v2_database(db)
        assert run(mod, db, "init") == 0
        assert out_json(capsys)["schema_version"] == mod.SCHEMA_VERSION

    def test_every_existing_lesson_survives_with_its_id_and_approver(self, mod, db, capsys) -> None:
        """Ids are carried across explicitly: :func:`seed_lessons` ranks recency by
        id, so renumbering the rows would silently reorder every later seed. And
        ``approved_by`` is the audit trail the human gate exists to leave."""
        self.a_v2_database(db)
        run(mod, db, "list", "lessons")
        rows = {int(row["id"]): row for row in out_json(capsys)}

        assert sorted(rows) == [7, 9]
        assert rows[7]["pattern"] == "old pattern"
        assert rows[7]["approved_by"] == "a-human"
        assert rows[7]["active"] == 1
        assert rows[7]["source_finding_id"] == 1
        # NULL rather than a back-filled placeholder: these lessons were proposed
        # when a finding id was the only source there was, which is a fact about
        # them and not missing data.
        assert rows[7]["source_policy_block"] is None
        # The unapproved one stays unapproved -- a rebuild that activated rows would
        # walk guidance into a seed no human let in.
        assert rows[9]["active"] == 0
        assert rows[9]["approved_by"] is None

    def test_a_migrated_ledger_accepts_a_policy_block_lesson(self, mod, db, capsys) -> None:
        """The rebuild is only worth anything if the relaxed column really relaxed:
        the old table's NOT NULL would refuse this insert."""
        self.a_v2_database(db)
        assert (
            run(
                mod,
                db,
                "propose-lesson",
                "--kind",
                "false-positive",
                "--surface",
                "deny-classifier-fence",
                "--pattern",
                "p",
                "--guidance",
                "g",
                "--source-policy-block",
                "m2-round-1/policy_block-1",
            )
            == 0
        )
        assert out_json(capsys)["active"] == 0

    def test_the_rebuild_is_idempotent(self, mod, db, capsys) -> None:
        """Every command calls ``init_schema``, so the second open must find nothing
        to do -- a rebuild that ran every time would rewrite the table on each
        invocation and the scratch table would collide with itself."""
        self.a_v2_database(db)
        run(mod, db, "init")
        capsys.readouterr()
        assert run(mod, db, "init") == 0
        assert out_json(capsys)["schema_version"] == mod.SCHEMA_VERSION
        run(mod, db, "list", "lessons")
        assert [int(row["id"]) for row in out_json(capsys)] == [7, 9]

    def test_the_scratch_table_is_not_left_behind(self, mod, db, capsys) -> None:
        """A leftover ``lessons_rebuild`` would be a second lessons table that
        nothing reads and every later rebuild would have to reason about.

        Existence is read with ``PRAGMA table_info``, which returns no rows for a
        table that is not there -- the same probe :func:`_lessons_needs_rebuild`
        uses, so the test and the code agree on what "present" means.
        """
        self.a_v2_database(db)
        run(mod, db, "init")
        capsys.readouterr()
        conn = mod.connect(db)
        try:
            scratch = conn.execute(f"PRAGMA table_info({mod.LESSONS_REBUILD_TABLE})").fetchall()
            lessons = conn.execute("PRAGMA table_info(lessons)").fetchall()
        finally:
            conn.close()
        assert scratch == []
        assert lessons != []

    def test_the_active_index_survives_the_rebuild(self, mod, db, capsys) -> None:
        """Dropping the table dropped its indexes; the rebuild recreates the one
        :data:`DDL` declares rather than leaving the seed's query unindexed."""
        self.a_v2_database(db)
        run(mod, db, "init")
        capsys.readouterr()
        conn = mod.connect(db)
        try:
            names = {row["name"] for row in conn.execute("PRAGMA index_list(lessons)")}
        finally:
            conn.close()
        assert "lessons_active" in names

    def delete_lesson_by_hand(self, mod, db: Path, lesson_id: int) -> None:
        """Remove one lesson the way the RFC's revert path does: a human row edit.

        There is no delete verb in the CLI, which is why this is spelled out here --
        and it is what makes the id counter and ``MAX(id)`` disagree.
        """
        conn = mod.connect(db)
        try:
            with conn:
                conn.execute("DELETE FROM lessons WHERE id = ?", (lesson_id,))
        finally:
            conn.close()

    def propose_policy_block_lesson(self, mod, db: Path) -> int:
        """One policy-block-sourced proposal, returning the id it was given."""
        return run(
            mod,
            db,
            "propose-lesson",
            "--kind",
            "false-positive",
            "--surface",
            "deny-classifier-fence",
            "--pattern",
            "p",
            "--guidance",
            "g",
            "--source-policy-block",
            "m2-round-1/policy_block-1",
        )

    def test_the_rebuild_does_not_reissue_a_retired_id(self, mod, db, capsys) -> None:
        """AUTOINCREMENT's promise is that an id is never REUSED, and the rebuild
        must not quietly withdraw it.

        The copy carries explicit ids, so the rebuilt table's counter lands on
        ``MAX(id)`` -- the largest id still PRESENT. Delete the top lesson by hand
        first and the two diverge: without the counter being carried across, the next
        proposal is handed an id that has already named a different lesson, and a
        report citing it means two things.
        """
        self.a_v2_database(db)
        self.delete_lesson_by_hand(mod, db, 9)

        run(mod, db, "init")
        capsys.readouterr()
        assert self.propose_policy_block_lesson(mod, db) == 0
        assert int(out_json(capsys)["id"]) > 9

    def test_an_emptied_table_does_not_restart_the_counter_at_one(self, mod, db, capsys) -> None:
        """The worst version of the same defect.

        A table whose rows were ALL deleted copies nothing, so the rebuilt table gets
        no sequence row at all and the counter restarts at 1 -- handing out ids that
        every earlier lesson in this ledger already used.
        """
        self.a_v2_database(db)
        self.delete_lesson_by_hand(mod, db, 7)
        self.delete_lesson_by_hand(mod, db, 9)

        run(mod, db, "init")
        capsys.readouterr()
        run(mod, db, "list", "lessons")
        assert out_json(capsys) == []

        assert self.propose_policy_block_lesson(mod, db) == 0
        assert int(out_json(capsys)["id"]) > 9

    def test_the_counter_only_ever_moves_forward(self, mod, db, capsys) -> None:
        """The restore is guarded so it cannot walk the counter BACKWARDS.

        A ledger written by a newer checkout could carry a mark above anything this
        rebuild saw; lowering it would be the reissue defect with the roles swapped.
        """
        self.a_v2_database(db)
        conn = mod.connect(db)
        try:
            with conn:
                conn.execute("UPDATE sqlite_sequence SET seq = 500 WHERE name = 'lessons'")
        finally:
            conn.close()

        run(mod, db, "init")
        capsys.readouterr()
        conn = mod.connect(db)
        try:
            seq = mod._lessons_id_high_water(conn)
        finally:
            conn.close()
        assert seq == 500

    def test_the_foreign_key_still_bites_after_the_rebuild(self, mod, db, capsys) -> None:
        """Nullable is not unchecked: a PRESENT finding id must still resolve, or the
        rebuild traded one guarantee for the other."""
        self.a_v2_database(db)
        run(mod, db, "init")
        capsys.readouterr()
        conn = mod.connect(db)
        try:
            with pytest.raises(Exception):
                with conn:
                    conn.execute(
                        "INSERT INTO lessons (kind, surface, pattern, guidance,"
                        " source_finding_id, source_policy_block, ts, active)"
                        " VALUES ('missed', 's', 'p', 'g', 4242, NULL,"
                        " '2026-09-12T00:00:00+00:00', 0)"
                    )
        finally:
            conn.close()

    def test_a_failed_rebuild_leaves_nothing_behind(self, mod, db, capsys, monkeypatch) -> None:
        """The rebuild is atomic, and that is a property of the explicit BEGIN.

        Python's sqlite3 driver opens a transaction implicitly before DML only, never
        before DDL. Every statement in the rebuild except the copy is DDL, so without
        ``BEGIN IMMEDIATE`` the scratch table's ``DROP`` and ``CREATE`` are
        autocommitted -- and a rebuild that then fails leaves that table behind, live,
        for the next upgrader to find and destroy mid-flight.

        The failure is injected at the last step so the interesting statements have all
        run: if the whole pass is one transaction, the old table comes back untouched
        and no scratch table survives.
        """

        def boom(*_args, **_kwargs):
            raise RuntimeError("injected failure at the end of the rebuild")

        self.a_v2_database(db)
        monkeypatch.setattr(mod, "_restore_lessons_id_high_water", boom)

        conn = mod.connect(db)
        try:
            with pytest.raises(RuntimeError):
                mod.init_schema(conn)
        finally:
            conn.close()

        conn = mod.connect(db)
        try:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(lessons)")}
            scratch = conn.execute(f"PRAGMA table_info({mod.LESSONS_REBUILD_TABLE})").fetchall()
            ids = [int(row["id"]) for row in conn.execute("SELECT id FROM lessons ORDER BY id")]
        finally:
            conn.close()

        assert scratch == []
        # Rolled all the way back: the table is the OLD shape, not a half-migrated one.
        assert "source_policy_block" not in columns
        assert ids == [7, 9]

    def test_a_second_upgrader_finds_nothing_to_do(self, mod, db, capsys) -> None:
        """The loser of the race re-probes rather than rebuilding a second time.

        Two connections, each running the pass to completion in turn -- which is what
        the serialization reduces concurrent upgraders to. The second must see the
        finished shape and leave it alone; a version-based gate would be equally happy
        here, but a shape-based one is what makes the LOSER of a real race safe, since
        the winner's version bump and its tables land together.
        """
        self.a_v2_database(db)
        first, second = mod.connect(db), mod.connect(db)
        try:
            assert mod.init_schema(first) == mod.SCHEMA_VERSION
            assert mod._lessons_needs_rebuild(second) is False
            assert mod.init_schema(second) == mod.SCHEMA_VERSION
        finally:
            first.close()
            second.close()

        run(mod, db, "list", "lessons")
        assert [int(row["id"]) for row in out_json(capsys)] == [7, 9]

    def test_the_pass_takes_the_write_lock_immediately(self, mod) -> None:
        """Asserted structurally, because the interleaving it prevents cannot be driven
        from one process without hooks into SQLite's locking.

        Both halves matter. ``BEGIN`` at all is what puts the rebuild's DDL in a
        transaction, and ``IMMEDIATE`` is what takes the lock BEFORE the probe decides
        whether to rebuild -- a deferred transaction would acquire it at the first
        write, by which time both upgraders have already decided to.
        """
        source = script_source_with_joined_literals()
        # The STATEMENT, not the phrase: two docstrings explain the transaction, and one
        # of them belongs to a function defined earlier in the file, so a bare substring
        # search finds prose and the position assertions below become meaningless.
        statement = 'conn.execute("BEGIN IMMEDIATE")'
        assert statement in source
        begin = source.index(statement)
        # Inside init_schema, and before the DDL loop it is supposed to cover.
        assert source.index("def init_schema") < begin
        assert begin < source.index("for statement in DDL")

    def test_the_columns_are_spelled_once(self, mod) -> None:
        """:data:`DDL` and the rebuild both build the table from
        :data:`LESSONS_COLUMNS`. Two copies of a column list drift the moment one of
        them gains a column, and the drift is invisible: fresh ledgers would get one
        shape and migrated ones the other."""
        source = script_source_with_joined_literals()
        creates = re.findall(r"CREATE TABLE[^(]*\(\{LESSONS_COLUMNS\}\)", source)
        assert len(creates) == 2, creates
        assert "source_policy_block TEXT" in mod.LESSONS_COLUMNS


class TestTheBehaviourKindIsDeclaredAndStorable:
    """``test`` is a golden-path kind, and the table has to agree with the CLI.

    Declaring it in ``GOLDEN_PATH_KINDS`` alone was not enough: the ``golden_paths``
    table carries a CHECK constraint spelling the kinds as literals, SQLite has no
    ALTER for a CHECK, and the ``CREATE TABLE IF NOT EXISTS`` ladder leaves an
    existing table in its old shape -- so the first ``propose-golden-path --kind
    test`` against a live ledger died with an IntegrityError traceback instead of a
    sentence. The rebuild is what these tests are about, and the property that
    matters most is that it is LOSSLESS: an approved golden path is cited by id, and
    renumbering would point a recorded human approval at a different operation.
    """

    #: The table exactly as it shipped before the kind existed, so the migration runs
    #: against the real old shape rather than a guess at it.
    LEGACY_TABLE = """CREATE TABLE golden_paths (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL CHECK (kind IN ('shell', 'flow', 'cron')),
        surface TEXT NOT NULL,
        command_or_flow TEXT NOT NULL,
        platform TEXT NOT NULL CHECK (platform IN ('any', 'posix', 'windows'))
            DEFAULT 'any',
        reason TEXT NOT NULL,
        source_finding_id INTEGER REFERENCES findings(id),
        approved_by TEXT,
        ts TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1
    )"""

    def propose(self, mod, db: Path, kind: str, command: str) -> int:
        return run(
            mod,
            db,
            "propose-golden-path",
            "--kind",
            kind,
            "--surface",
            "S10 cron + scripts",
            "--command",
            command,
            "--platform",
            "any",
            "--reason",
            "a behaviour this fix must keep alive",
        )

    def a_legacy_ledger(self, mod, db: Path) -> None:
        """A ledger whose ``golden_paths`` predates the kind, with an approved row in it.

        The id counter is left ABOVE the surviving maximum, which is what the RFC's
        retirement-by-hand leaves behind and what a rebuild must not walk back.
        """
        conn = mod.connect(db)
        try:
            mod.init_schema(conn)
            with conn:
                conn.execute("DROP TABLE golden_paths")
                conn.execute(self.LEGACY_TABLE)
                conn.execute(
                    "INSERT INTO golden_paths (id, kind, surface, command_or_flow, platform,"
                    " reason, approved_by, ts, active)"
                    " VALUES (4, 'shell', 'gh-read', 'gh pr view 1 --json state', 'any',"
                    " 'reading a PR state', 'operator', '2026-01-01T00:00:00+00:00', 1)"
                )
                conn.execute(
                    "INSERT INTO sqlite_sequence (name, seq) SELECT 'golden_paths', 9"
                    " WHERE NOT EXISTS"
                    " (SELECT 1 FROM sqlite_sequence WHERE name = 'golden_paths')"
                )
                conn.execute("UPDATE sqlite_sequence SET seq = 9 WHERE name = 'golden_paths'")
        finally:
            conn.close()

    def test_a_behaviour_row_can_be_proposed(self, mod, db, capsys) -> None:
        assert self.propose(mod, db, "test", "test/test_governance_policy.py") == 0
        body = out_json(capsys)
        # Proposed rows are inert: activation is the human's verb and nothing else's.
        assert body == {"id": body["id"], "created": True, "active": 0}

    def test_an_undeclared_kind_is_still_refused_with_a_sentence(self, mod, db, capsys) -> None:
        assert self.propose(mod, db, "unit", "test/test_governance_policy.py") == 2
        assert "unknown kind" in capsys.readouterr().err

    def test_every_stored_check_is_derived_from_its_own_constant(self, mod, db) -> None:
        """The CLI's screen and the table's CHECK must not be two lists.

        They were for golden-path kinds, and that is how a kind the parser accepted
        became a kind the storage layer refused. Lessons kinds and verdict roles carried
        the same shape, so all three are asserted here rather than the one that broke.
        """
        conn = mod.connect(db)
        try:
            mod.init_schema(conn)
            stored = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table'"  # wokeignore:rule=master
                " AND name = 'golden_paths'"
            ).fetchone()["sql"]
        finally:
            conn.close()
        for kind in mod.GOLDEN_PATH_KINDS:
            assert f"'{kind}'" in stored
        for table, constants in (("lessons", mod.LESSON_KINDS), ("verdicts", mod.ROLES)):
            conn = mod.connect(db)
            try:
                mod.init_schema(conn)
                sql = conn.execute(
                    "SELECT sql FROM sqlite_master"  # wokeignore:rule=master
                    " WHERE type = 'table' AND name = ?",
                    (table,),
                ).fetchone()["sql"]
            finally:
                conn.close()
            for value in constants:
                assert f"'{value}'" in sql, (table, value)

    def test_a_legacy_table_is_rebuilt_on_open_and_only_once(self, mod, db) -> None:
        self.a_legacy_ledger(mod, db)
        conn = mod.connect(db)
        try:
            assert mod._golden_paths_needs_rebuild(conn) is True
            mod.init_schema(conn)
            assert mod._golden_paths_needs_rebuild(conn) is False
        finally:
            conn.close()

    def test_the_rebuild_keeps_every_row_its_id_the_counter_and_the_indexes(self, mod, db) -> None:
        self.a_legacy_ledger(mod, db)
        conn = mod.connect(db)
        try:
            mod.init_schema(conn)
            rows = [
                dict(row)
                for row in conn.execute(
                    "SELECT id, kind, command_or_flow, approved_by, active FROM golden_paths"
                )
            ]
            counter = conn.execute(
                "SELECT seq FROM sqlite_sequence WHERE name = 'golden_paths'"
            ).fetchone()["seq"]
            indexes = sorted(
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"  # wokeignore:rule=master
                    " AND tbl_name = 'golden_paths' AND name NOT LIKE 'sqlite_%'"
                )
            )
        finally:
            conn.close()
        assert rows == [
            {
                "id": 4,
                "kind": "shell",
                "command_or_flow": "gh pr view 1 --json state",
                "approved_by": "operator",
                "active": 1,
            }
        ]
        assert int(counter) == 9, "the id counter was walked back, so an id can be reissued"
        assert indexes == ["golden_paths_active", "golden_paths_identity"]

    def test_the_new_kind_is_storable_after_the_rebuild(self, mod, db, capsys) -> None:
        self.a_legacy_ledger(mod, db)
        assert self.propose(mod, db, "test", "test/test_governance_policy.py") == 0
        assert out_json(capsys)["active"] == 0

    def test_storage_still_refuses_an_undeclared_kind_after_the_rebuild(self, mod, db) -> None:
        """The rebuild must widen the constraint, never drop it.

        Anything that can reach this database can INSERT directly, so the CHECK is the
        guarantee and the CLI's screen is only the courteous message.
        """
        self.a_legacy_ledger(mod, db)
        conn = mod.connect(db)
        try:
            mod.init_schema(conn)
            with pytest.raises(Exception) as caught:
                with conn:
                    conn.execute(
                        "INSERT INTO golden_paths (kind, surface, command_or_flow, platform,"
                        " reason, ts) VALUES ('unit', 's', 'c', 'any', 'r', '2026-01-01')"
                    )
        finally:
            conn.close()
        assert "CHECK constraint failed" in str(caught.value)

    def test_a_database_already_in_shape_is_left_alone(self, mod, db) -> None:
        """The probe is a SHAPE test, not a version test, so an ordinary open rebuilds
        nothing -- and an interrupted upgrade finds the same work still to do."""
        run(mod, db, "init")
        conn = mod.connect(db)
        try:
            assert mod._golden_paths_needs_rebuild(conn) is False
        finally:
            conn.close()

    def test_a_behaviour_row_validates_in_a_corpus_file(self, mod) -> None:
        """The committed corpus and the import are judged by the one validator."""
        rows = mod.load_golden_path_corpus(
            json.dumps(
                {
                    "golden_paths": [
                        {
                            "kind": "test",
                            "surface": "S7 secrets + governance ceiling",
                            "command_or_flow": "test/test_governance_policy.py::test_x",
                            "platform": "any",
                            "reason": "single-tier governance must resolve as before",
                        }
                    ]
                }
            )
        )
        assert rows[0]["kind"] == "test"
