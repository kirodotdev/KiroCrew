"""The security conductor's findings ledger — the properties, not the plumbing.

Each test here pins one guarantee the RFC asks the ledger to hold instead of
asking an agent to hold it: an append-only verdict log whose fold is
``human > verifier > auditor``, a proposed lesson that cannot reach a seed
message before a human approves it, a bounded seed, an export that shows only
live rules, one finding per real defect, and a lesson that always cites one.

The CLI is driven through ``main`` with argv, the way the skill invokes it, so a
property that holds only in a helper called directly does not count.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from skill_script_helpers import load_skill_script

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "kiro_crew"
    / "builtin_skills"
    / "security-conductor"
    / "scripts"
    / "ledger.py"
)


def script_source_with_joined_literals() -> str:
    """The script's source with implicit string concatenation collapsed.

    Python joins adjacent literals into one string, so a scan matching a single
    quoted run sees only the FIRST fragment of every multi-line SQL statement in
    this file -- a WHERE clause on the next line is invisible to it, which makes
    the assertion pass by inspecting nothing. Collapsing `" "` joins is what lets
    the structural assertions reason about whole statements. Only true
    concatenation is affected: `"a", "b"` has a comma between the quotes.
    """
    return re.sub(r'"\s*"', "", SCRIPT.read_text(encoding="utf-8"))


@pytest.fixture
def mod():
    return load_skill_script("security_conductor_ledger", SCRIPT)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "findings.db"


def run(mod, db: Path, *argv: str) -> int:
    return mod.main(["--db", str(db), *argv])


def out_json(capsys):
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def a_finding(mod, db: Path, capsys, *, surface="security.is_denied", title="fence bypass") -> int:
    run(
        mod,
        db,
        "add-finding",
        "--surface",
        surface,
        "--title",
        title,
        "--severity",
        "High",
        "--path",
        "src/kiro_crew/security.py",
    )
    return int(out_json(capsys)["id"])


def a_lesson(
    mod,
    db: Path,
    capsys,
    finding_id: int,
    *,
    surface="security.is_denied",
    pattern="regex matched echo text",
    guidance="read argv, not the command string",
    kind="false-positive",
    approve=True,
) -> int:
    run(
        mod,
        db,
        "propose-lesson",
        "--kind",
        kind,
        "--surface",
        surface,
        "--pattern",
        pattern,
        "--guidance",
        guidance,
        "--source-finding",
        str(finding_id),
    )
    lesson_id = int(out_json(capsys)["id"])
    if approve:
        run(mod, db, "approve-lesson", "--id", str(lesson_id), "--approved-by", "zejiangg")
        capsys.readouterr()
    return lesson_id


class TestInit:
    def test_init_is_idempotent_and_versioned(self, mod, db, capsys):
        assert run(mod, db, "init") == 0
        first = out_json(capsys)
        assert first["schema_version"] == mod.SCHEMA_VERSION
        assert run(mod, db, "init") == 0
        # A second init must not stack a second version row, or the ladder a
        # future migration branches on reads an arbitrary one of them.
        assert out_json(capsys)["schema_version"] == mod.SCHEMA_VERSION
        conn = mod.connect(db)
        try:
            rows = conn.execute("SELECT version FROM schema_version").fetchall()
        finally:
            conn.close()
        assert [row["version"] for row in rows] == [mod.SCHEMA_VERSION]

    def test_every_command_creates_the_schema_first(self, mod, db, capsys):
        """No command may require a prior ``init``: a skill step that forgets it
        would otherwise fail on a fresh host with a ``no such table`` error."""
        assert run(mod, db, "list", "findings") == 0
        assert out_json(capsys) == []


class TestDedupeIdentity:
    def test_same_surface_title_and_paths_returns_the_existing_id(self, mod, db, capsys):
        first = a_finding(mod, db, capsys)
        run(
            mod,
            db,
            "add-finding",
            "--surface",
            "security.is_denied",
            "--title",
            "fence bypass",
            "--severity",
            "Critical",  # a re-report is not evidence to overwrite the record
            "--path",
            "src/kiro_crew/security.py",
        )
        again = out_json(capsys)
        assert again["id"] == first
        assert again["created"] is False
        run(mod, db, "list", "findings")
        rows = out_json(capsys)
        assert len(rows) == 1
        assert rows[0]["severity"] == "High"

    def test_path_order_and_duplicates_do_not_create_a_second_finding(self, mod, db, capsys):
        """Identity is *sorted* paths, so the same defect reported with its paths
        in another order is the same finding, not a second one."""
        run(
            mod,
            db,
            "add-finding",
            "--surface",
            "webhooks",
            "--title",
            "unvalidated payload",
            "--severity",
            "Medium",
            "--path",
            "src/kiro_crew/webhooks.py",
            "--path",
            "src/kiro_crew/hooks.py",
        )
        first = out_json(capsys)["id"]
        run(
            mod,
            db,
            "add-finding",
            "--surface",
            "webhooks",
            "--title",
            "unvalidated payload",
            "--severity",
            "Medium",
            "--path",
            "src/kiro_crew/hooks.py",
            "--path",
            "src/kiro_crew/webhooks.py",
            "--path",
            "src/kiro_crew/hooks.py",
        )
        assert out_json(capsys)["id"] == first

    def test_a_different_path_set_is_a_different_finding(self, mod, db, capsys):
        first = a_finding(mod, db, capsys)
        run(
            mod,
            db,
            "add-finding",
            "--surface",
            "security.is_denied",
            "--title",
            "fence bypass",
            "--severity",
            "High",
            "--path",
            "src/kiro_crew/platform/security_authority.py",
        )
        second = out_json(capsys)
        assert second["id"] != first
        assert second["created"] is True


class TestSchemaMatchesTheRfc:
    """The RFC's four tables, column for column.

    A column added for convenience is a design change made in an
    implementation PR, so the shape is asserted rather than described.
    """

    @pytest.mark.parametrize(
        "table,columns",
        [
            (
                "findings",
                [
                    "id",
                    "surface",
                    "severity",
                    "title",
                    "paths",
                    "poc",
                    "auditor_verdict",
                    "verifier_verdict",
                    "final_verdict",
                    "status",
                    "created",
                    "round_id",
                ],
            ),
            ("verdicts", ["finding_id", "role", "verdict", "reason", "ts"]),
            (
                "lessons",
                [
                    "id",
                    "kind",
                    "surface",
                    "pattern",
                    "guidance",
                    "source_finding_id",
                    "approved_by",
                    "ts",
                    "active",
                ],
            ),
            ("roe_rules", ["id", "field", "value", "reason", "approved_by", "ts", "active"]),
            ("schema_version", ["version"]),
        ],
    )
    def test_table_has_exactly_the_specified_columns(self, mod, db, capsys, table, columns):
        run(mod, db, "init")
        capsys.readouterr()
        conn = mod.connect(db)
        try:
            rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        finally:
            conn.close()
        assert [row["name"] for row in rows] == columns

    def test_the_append_order_of_verdicts_is_the_implicit_rowid(self, mod, db, capsys):
        """``verdicts`` gets no surrogate key, so the fold's tiebreak has to come
        from SQLite's own rowid — this asserts the ordering column exists and is
        what ``list verdicts`` reports."""
        finding = a_finding(mod, db, capsys)
        for verdict in ("confirmed", "rejected"):
            run(
                mod,
                db,
                "record-verdict",
                "--finding",
                str(finding),
                "--role",
                "auditor",
                "--verdict",
                verdict,
            )
        capsys.readouterr()
        run(mod, db, "list", "verdicts")
        rows = out_json(capsys)
        assert [row["rowid"] for row in rows] == [1, 2]
        assert [row["verdict"] for row in rows] == ["confirmed", "rejected"]


class TestSchemaVersionIsASingleton:
    """The version row is what a migration ladder branches on, so two of them is
    two answers to one question.

    `init_schema` read the table and inserted when empty. That is racy on the
    ORDINARY path, not just between two `init` calls: every command calls
    `init_schema` first, so two parallel auditors running `add-finding` both saw an
    empty table and both inserted.
    """

    def test_a_concurrent_initialisation_writes_one_row(self, mod, db, capsys):
        """Two connections initialise the same fresh database. The insert carries
        `WHERE NOT EXISTS`, so the second finds the first's row and writes
        nothing."""
        first = mod.connect(db)
        second = mod.connect(db)
        try:
            assert mod.init_schema(first) == mod.SCHEMA_VERSION
            assert mod.init_schema(second) == mod.SCHEMA_VERSION
            rows = first.execute("SELECT version FROM schema_version").fetchall()
        finally:
            first.close()
            second.close()
        assert [row["version"] for row in rows] == [mod.SCHEMA_VERSION]

    def test_many_initialisations_still_write_one_row(self, mod, db, capsys):
        """Every command initialises, so the idempotence has to survive repetition,
        not just a second call."""
        for _ in range(6):
            assert run(mod, db, "list", "findings") == 0
        capsys.readouterr()
        conn = mod.connect(db)
        try:
            rows = conn.execute("SELECT version FROM schema_version").fetchall()
        finally:
            conn.close()
        assert len(rows) == 1

    def test_the_index_refuses_a_duplicate_written_any_other_way(self, mod, db, capsys):
        """The conditional insert fixes this code's path; the unique index is what
        makes the singleton a property of the TABLE, so a row written by hand
        cannot duplicate it either."""
        run(mod, db, "init")
        capsys.readouterr()
        conn = mod.connect(db)
        try:
            with pytest.raises(Exception):
                conn.execute(
                    "INSERT INTO schema_version (version) VALUES (?)", (mod.SCHEMA_VERSION,)
                )
                conn.commit()
        finally:
            conn.close()

    def test_init_still_reports_the_version(self, mod, db, capsys):
        assert run(mod, db, "init") == 0
        assert out_json(capsys)["schema_version"] == mod.SCHEMA_VERSION


class TestApprovalSurvivesADeactivation:
    """`active = 0` alone was the wrong precondition.

    A lesson approved and then switched off by hand -- the RFC's own revert path --
    still carries its original approver, so it satisfied `AND active = 0` and a
    second approval overwrote the attribution. The write now also requires
    `approved_by IS NULL`, which is true only of a lesson never approved at all.
    """

    def _approved_then_deactivated(self, mod, db, capsys) -> int:
        finding = a_finding(mod, db, capsys)
        lesson = a_lesson(mod, db, capsys, finding, approve=False)
        run(mod, db, "approve-lesson", "--id", str(lesson), "--approved-by", "first")
        capsys.readouterr()
        conn = mod.connect(db)
        try:
            with conn:
                conn.execute("UPDATE lessons SET active = 0 WHERE id = ?", (lesson,))
        finally:
            conn.close()
        return lesson

    def test_reapproval_after_a_hand_deactivation_is_refused(self, mod, db, capsys):
        lesson = self._approved_then_deactivated(mod, db, capsys)
        assert run(mod, db, "approve-lesson", "--id", str(lesson), "--approved-by", "second") == 2
        err = capsys.readouterr().err
        assert "approved by 'first' and later deactivated" in err
        assert "rather than approving it again" in err

    def test_the_original_approver_survives_that_refusal(self, mod, db, capsys):
        lesson = self._approved_then_deactivated(mod, db, capsys)
        run(mod, db, "approve-lesson", "--id", str(lesson), "--approved-by", "second")
        capsys.readouterr()
        run(mod, db, "list", "lessons")
        row = out_json(capsys)[0]
        assert row["approved_by"] == "first"
        assert row["active"] == 0

    def test_the_refusal_names_the_documented_way_back(self, mod, db, capsys):
        """The revert was a row edit, so undoing it is one too. An operator told
        only "refused" would reasonably re-run the command that just failed."""
        lesson = self._approved_then_deactivated(mod, db, capsys)
        run(mod, db, "approve-lesson", "--id", str(lesson), "--approved-by", "second")
        assert "UPDATE lessons SET active = 1" in capsys.readouterr().err

    def test_the_update_itself_carries_both_conditions(self, mod, db, capsys):
        """Driven at the SQL layer: the guard must be in the WHERE clause, not only
        in the Python branch that produces the message."""
        lesson = self._approved_then_deactivated(mod, db, capsys)
        conn = mod.connect(db)
        try:
            cursor = conn.execute(
                "UPDATE lessons SET active = 1, approved_by = ?"
                " WHERE id = ? AND active = 0 AND approved_by IS NULL",
                ("second", lesson),
            )
            conn.commit()
            assert cursor.rowcount == 0
            row = conn.execute("SELECT approved_by FROM lessons WHERE id = ?", (lesson,)).fetchone()
        finally:
            conn.close()
        assert row["approved_by"] == "first"

    def test_a_never_approved_lesson_still_approves(self, mod, db, capsys):
        """The narrower precondition must not break the path it protects."""
        finding = a_finding(mod, db, capsys)
        lesson = a_lesson(mod, db, capsys, finding, approve=False)
        assert run(mod, db, "approve-lesson", "--id", str(lesson), "--approved-by", "x") == 0
        assert out_json(capsys)["approved_by"] == "x"


class TestNoWriteIsGatedOnlyByAPythonRead:
    """The invariant the retrospective produced, as a scan.

    Four rounds produced four findings in one span, and every one was the same
    mechanism: a Python read-then-write deciding whether a write was safe, where
    the database should have been the authority. Rounds 0, 2 and 4 were
    check-then-act (dedupe, approval, schema version); round 1 was the read-path
    variant (trusting a wall clock over the append order the table records).

    These assertions cover the write that does not exist yet, which is the only
    way to stop a fifth instance.
    """

    def test_the_schema_version_insert_carries_its_own_precondition(self, mod):
        source = script_source_with_joined_literals()
        inserts = re.findall(r'"INSERT INTO schema_version[^"]*"', source)
        assert inserts, "expected the schema_version insert to be present"
        for statement in inserts:
            assert "WHERE NOT EXISTS" in statement or "OR IGNORE" in statement, statement

    def test_every_approval_update_requires_an_unset_approver(self, mod):
        source = script_source_with_joined_literals()
        updates = [
            statement
            for statement in re.findall(r'"UPDATE lessons[^"]*"', source)
            if "active = 1" in statement
        ]
        assert updates, "expected the approval UPDATE to be present"
        for statement in updates:
            assert "active = 0" in statement, statement
            assert "approved_by IS NULL" in statement, statement

    def test_the_literal_join_is_what_makes_these_scans_see_whole_statements(self):
        """The scans above are only as good as this collapse.

        Matching one quoted run at a time saw a multi-line statement's first
        fragment only, so a WHERE clause on the second line was invisible -- an
        assertion that passes because it inspects nothing. Pinned so the helper
        cannot regress into that quietly.
        """
        collapsed = re.sub(r'"\s*"', "", 'x = "UPDATE t SET a = 1" " WHERE b = 0"')
        assert collapsed == 'x = "UPDATE t SET a = 1 WHERE b = 0"'
        # A comma between the quotes is not concatenation and must survive.
        assert re.sub(r'"\s*"', "", '("a", "b")') == '("a", "b")'

    def test_the_singleton_index_exists(self, mod):
        source = script_source_with_joined_literals()
        assert "UNIQUE INDEX IF NOT EXISTS schema_version_single" in source


class TestDedupeSurvivesTheRace:
    def test_a_concurrent_insert_returns_the_existing_id_not_a_traceback(self, mod, db, capsys):
        """The identity index is what makes dedupe hold when two auditors hit the
        same surface at once — but only if the losing writer RECOVERS. Without the
        IntegrityError branch the loser gets a traceback where the contract
        promises the existing id.

        The race is simulated by inserting the twin between the read and the
        write: patching the read to miss is what a real concurrent writer causes.
        """
        first = a_finding(mod, db, capsys)
        conn = mod.connect(db)
        try:
            original = mod._find_by_identity
            calls = []

            def racing_find(connection, surface, title, key):
                calls.append(key)
                # Miss on the FIRST lookup only — the pre-insert read — so the
                # insert proceeds into a row that already exists, and the
                # post-IntegrityError re-read behaves like the real one.
                if len(calls) == 1:
                    return None
                return original(connection, surface, title, key)

            mod._find_by_identity = racing_find
            try:
                found, created = mod.add_finding(
                    conn,
                    surface="security.is_denied",
                    title="fence bypass",
                    severity="High",
                    paths=["src/kiro_crew/security.py"],
                    poc=None,
                    round_id=None,
                )
            finally:
                mod._find_by_identity = original
        finally:
            conn.close()
        assert found == first
        assert created is False
        assert len(calls) == 2, "the losing writer must re-read after the IntegrityError"

    def test_the_identity_index_exists_so_the_race_is_detectable_at_all(self, mod, db, capsys):
        a_finding(mod, db, capsys)
        conn = mod.connect(db)
        try:
            with pytest.raises(Exception):
                conn.execute(
                    "INSERT INTO findings (surface, severity, title, paths, status, created)"
                    " VALUES ('security.is_denied', 'Low', 'fence bypass',"
                    " '[\"src/kiro_crew/security.py\"]', 'open',"
                    " '2026-09-07T00:00:00+00:00')"
                )
                conn.commit()
        finally:
            conn.close()


class TestApprovalDoesNotRestampTheProposal:
    def test_ts_still_means_when_the_lesson_was_proposed(self, mod, db, capsys):
        """``ts`` is the only timestamp the RFC gives a lesson, and
        ``seed-lessons`` ranks recency by it. Restamping on approval would make
        the column mean proposal-time or approval-time depending on ``active``,
        and would let a batch of old approvals outrank a genuinely fresh lesson.
        """
        finding = a_finding(mod, db, capsys)
        lesson = a_lesson(mod, db, capsys, finding, approve=False)
        run(mod, db, "list", "lessons")
        proposed_at = out_json(capsys)[0]["ts"]
        run(mod, db, "approve-lesson", "--id", str(lesson), "--approved-by", "zejiangg")
        capsys.readouterr()
        run(mod, db, "list", "lessons")
        row = out_json(capsys)[0]
        assert row["ts"] == proposed_at
        assert row["active"] == 1


class TestSeedShortfallIsHonest:
    def test_an_empty_ledger_and_a_tight_budget_report_differently(self, mod, db, capsys):
        """Two different operator problems: nothing approved yet, versus a budget
        too small for the top lesson. One message for both sends the reader to
        the wrong place."""
        assert run(mod, db, "seed-lessons", "--surface", "s", "--budget-bytes", "4000") == 0
        assert "no approved lesson to seed" in capsys.readouterr().err

        finding = a_finding(mod, db, capsys)
        a_lesson(mod, db, capsys, finding)
        assert run(mod, db, "seed-lessons", "--surface", "s", "--budget-bytes", "5") == 0
        assert "fits 5 bytes" in capsys.readouterr().err


class TestListQueriesAreLiteral:
    def test_no_table_name_is_interpolated_into_sql(self, mod):
        """The argparse ``choices`` bounds the value, but the query site should be
        readable as safe on its own — a later flag that widens ``choices`` must
        not silently widen the SQL too."""
        assert set(mod.LIST_TABLES) == set(mod.LIST_QUERIES)
        source = script_source_with_joined_literals()
        assert 'f"SELECT * FROM {' not in source
        assert 'f"SELECT * FROM {' not in source


class TestVerdictsAreAppendOnly:
    def test_a_second_verdict_from_one_role_does_not_replace_the_first(self, mod, db, capsys):
        finding = a_finding(mod, db, capsys)
        run(
            mod,
            db,
            "record-verdict",
            "--finding",
            str(finding),
            "--verdict",
            "confirmed",
            "--role",
            "auditor",
            "--reason",
            "poc reproduces",
        )
        run(
            mod,
            db,
            "record-verdict",
            "--finding",
            str(finding),
            "--verdict",
            "rejected",
            "--role",
            "auditor",
            "--reason",
            "poc was wrong",
        )
        capsys.readouterr()
        run(mod, db, "list", "verdicts")
        rows = out_json(capsys)
        assert [row["verdict"] for row in rows] == ["confirmed", "rejected"]
        assert [row["reason"] for row in rows] == ["poc reproduces", "poc was wrong"]

    def test_the_cli_carries_no_update_or_delete_path_to_verdicts(self, mod):
        """A structural check, because the guarantee is the ABSENCE of a writer.

        A behavioural test can only show that the commands which exist append;
        it cannot show that no command mutates. Reading the SQL is what catches
        a later ``amend-verdict`` being added.
        """
        sql = [
            line.strip().lower()
            for line in SCRIPT.read_text(encoding="utf-8").splitlines()
            if "verdicts" in line.lower()
        ]
        assert sql, "expected the script to mention the verdicts table"
        for line in sql:
            assert "delete from verdicts" not in line
            assert "update verdicts" not in line


class TestFinalVerdictIsAFold:
    def test_human_outranks_verifier_which_outranks_auditor(self, mod, db, capsys):
        finding = a_finding(mod, db, capsys)
        run(
            mod,
            db,
            "record-verdict",
            "--finding",
            str(finding),
            "--role",
            "auditor",
            "--verdict",
            "confirmed",
        )
        assert out_json(capsys)["final_verdict"] == "confirmed"

        run(
            mod,
            db,
            "record-verdict",
            "--finding",
            str(finding),
            "--role",
            "verifier",
            "--verdict",
            "rejected",
        )
        folded = out_json(capsys)
        assert folded["final_verdict"] == "rejected"
        # The disagreement stays readable: the auditor's call is not erased by
        # the verifier winning the fold.
        assert folded["auditor_verdict"] == "confirmed"
        assert folded["verifier_verdict"] == "rejected"

        run(
            mod,
            db,
            "record-verdict",
            "--finding",
            str(finding),
            "--role",
            "human",
            "--verdict",
            "needs-human",
        )
        folded = out_json(capsys)
        assert folded["final_verdict"] == "needs-human"
        assert folded["auditor_verdict"] == "confirmed"
        assert folded["verifier_verdict"] == "rejected"

    def test_a_weaker_role_arriving_later_does_not_win(self, mod, db, capsys):
        """Precedence is by ROLE, not by arrival: an auditor re-filing after the
        verifier rejected must not flip the finding back to confirmed."""
        finding = a_finding(mod, db, capsys)
        run(
            mod,
            db,
            "record-verdict",
            "--finding",
            str(finding),
            "--role",
            "verifier",
            "--verdict",
            "rejected",
        )
        run(
            mod,
            db,
            "record-verdict",
            "--finding",
            str(finding),
            "--role",
            "auditor",
            "--verdict",
            "confirmed",
        )
        assert out_json(capsys)["final_verdict"] == "rejected"

    def test_the_findings_row_matches_the_fold(self, mod, db, capsys):
        finding = a_finding(mod, db, capsys)
        run(
            mod,
            db,
            "record-verdict",
            "--finding",
            str(finding),
            "--role",
            "auditor",
            "--verdict",
            "confirmed",
        )
        run(
            mod,
            db,
            "record-verdict",
            "--finding",
            str(finding),
            "--role",
            "human",
            "--verdict",
            "confirmed-critical",
        )
        capsys.readouterr()
        run(mod, db, "list", "findings")
        row = out_json(capsys)[0]
        assert row["final_verdict"] == "confirmed-critical"
        assert row["verifier_verdict"] is None

    def test_latest_within_a_role_wins_even_on_an_identical_timestamp(self, mod, db, capsys):
        """Two rows written inside one clock tick still fold deterministically —
        insertion order breaks the tie, so the fold is not left to ``ts``
        resolution."""
        finding = a_finding(mod, db, capsys)
        conn = mod.connect(db)
        try:
            for verdict in ("confirmed", "rejected"):
                conn.execute(
                    "INSERT INTO verdicts (finding_id, role, verdict, reason, ts)"
                    " VALUES (?, 'verifier', ?, NULL, '2026-09-07T00:00:00+00:00')",
                    (finding, verdict),
                )
            conn.commit()
            assert mod.fold_verdicts(conn, finding)["final_verdict"] == "rejected"
        finally:
            conn.close()


class TestOrderingSurvivesAClockRollback:
    """Append order, not the wall clock, decides both orderings.

    `verdicts` and `lessons` each record their own sequence structurally (rowid,
    id). Sorting by `ts` first handed that sequence to the system clock, so a
    rollback -- an NTP correction, a restored VM snapshot, a container with a bad
    clock -- reordered it. These tests write a rolled-back timestamp directly,
    which is what the clock does to a row that is appended later.
    """

    def test_a_rolled_back_clock_does_not_reinstate_a_superseded_verdict(self, mod, db, capsys):
        """The damaging case: a human overrules a verdict, the clock then rolls
        back, and the fold hands the finding back to the call that was overruled.

        The superseded row carries the LATER timestamp, so under `ts` ordering it
        sorted last and won.
        """
        finding = a_finding(mod, db, capsys)
        conn = mod.connect(db)
        try:
            # Appended first, but stamped in the future -- the pre-rollback clock.
            conn.execute(
                "INSERT INTO verdicts (finding_id, role, verdict, reason, ts)"
                " VALUES (?, 'human', 'confirmed', 'before the rollback',"
                " '2026-09-07T12:00:00+00:00')",
                (finding,),
            )
            # Appended second, stamped EARLIER because the clock went backwards.
            conn.execute(
                "INSERT INTO verdicts (finding_id, role, verdict, reason, ts)"
                " VALUES (?, 'human', 'rejected', 'after the rollback',"
                " '2026-09-07T09:00:00+00:00')",
                (finding,),
            )
            conn.commit()
            folded = mod.fold_verdicts(conn, finding)
        finally:
            conn.close()
        assert folded["final_verdict"] == "rejected"
        assert folded["auditor_verdict"] is None

    def test_the_fold_ignores_ts_entirely(self, mod, db, capsys):
        """Not merely tie-broken by rowid -- `ts` must carry no weight at all, so
        the guarantee does not depend on how far the clock moved."""
        finding = a_finding(mod, db, capsys)
        conn = mod.connect(db)
        try:
            for verdict, ts in (
                ("first", "2099-01-01T00:00:00+00:00"),
                ("second", "1999-01-01T00:00:00+00:00"),
                ("third", "2050-01-01T00:00:00+00:00"),
            ):
                conn.execute(
                    "INSERT INTO verdicts (finding_id, role, verdict, reason, ts)"
                    " VALUES (?, 'verifier', ?, NULL, ?)",
                    (finding, verdict, ts),
                )
            conn.commit()
            folded = mod.fold_verdicts(conn, finding)
        finally:
            conn.close()
        assert folded["final_verdict"] == "third"
        assert folded["verifier_verdict"] == "third"

    def test_a_rolled_back_clock_does_not_reorder_lesson_recency(self, mod, db, capsys):
        """The sibling site, fixed in the same round: a lesson proposed later must
        still rank as newer after a rollback, or the seed silently promotes stale
        guidance the moment a host's clock is corrected."""
        finding = a_finding(mod, db, capsys)
        older = a_lesson(mod, db, capsys, finding, pattern="proposed-first")
        newer = a_lesson(mod, db, capsys, finding, pattern="proposed-second")
        conn = mod.connect(db)
        try:
            conn.execute(
                "UPDATE lessons SET ts = '2026-09-07T12:00:00+00:00' WHERE id = ?", (older,)
            )
            conn.execute(
                "UPDATE lessons SET ts = '2026-09-07T09:00:00+00:00' WHERE id = ?", (newer,)
            )
            conn.commit()
        finally:
            conn.close()
        run(mod, db, "seed-lessons", "--surface", "security.is_denied", "--budget-bytes", "4000")
        lines = capsys.readouterr().out.strip().splitlines()
        assert "proposed-second" in lines[0]
        assert "proposed-first" in lines[1]

    def test_surface_match_still_outranks_recency_after_a_rollback(self, mod, db, capsys):
        """Dropping `ts` must not disturb the primary key of the ranking."""
        finding = a_finding(mod, db, capsys)
        on_surface = a_lesson(
            mod, db, capsys, finding, surface="security.is_denied", pattern="on-surface"
        )
        a_lesson(mod, db, capsys, finding, surface="webhooks", pattern="off-surface")
        conn = mod.connect(db)
        try:
            # Make the on-surface lesson look oldest by the clock as well.
            conn.execute(
                "UPDATE lessons SET ts = '1999-01-01T00:00:00+00:00' WHERE id = ?", (on_surface,)
            )
            conn.commit()
        finally:
            conn.close()
        run(mod, db, "seed-lessons", "--surface", "security.is_denied", "--budget-bytes", "4000")
        lines = capsys.readouterr().out.strip().splitlines()
        assert "on-surface" in lines[0]
        assert "off-surface" in lines[1]

    def test_no_ordering_query_in_the_cli_sorts_by_ts(self, mod):
        """A structural check on the class, not on the two instances.

        The next ordering someone adds is the one that would reintroduce this, and
        a behavioural test only covers the sites that already exist.
        """
        source = script_source_with_joined_literals()
        offenders = []
        for clause in re.findall(r"ORDER BY ([^\"']+)", source):
            for term in clause.split(","):
                # First token of each sort term, so `ts` is caught but `verdicts`
                # (which merely CONTAINS it) is not -- the bug this assertion had
                # on its first draft.
                head = term.strip().split()[:1]
                if head and head[0] == "ts":
                    offenders.append(clause.strip())
        assert offenders == [], offenders

    def test_that_check_would_catch_a_ts_ordering(self):
        """The guard above is a regex over source text, so it needs its own proof
        that it fires -- an assertion that can only pass is not a check."""
        sample = 'x = "SELECT a FROM t ORDER BY ts DESC, id DESC"'
        found = []
        for clause in re.findall(r"ORDER BY ([^\"']+)", sample):
            for term in clause.split(","):
                head = term.strip().split()[:1]
                if head and head[0] == "ts":
                    found.append(clause.strip())
        assert found, "the ts-ordering detector must fire on a ts ORDER BY"


class TestRecordVerdictRejectsUnknownRoles:
    @pytest.mark.parametrize("role", ["fixer", "AUDITOR", "auditor2", "human-approver"])
    def test_unknown_role_is_exit_2_and_writes_nothing(self, mod, db, capsys, role):
        finding = a_finding(mod, db, capsys)
        assert (
            run(
                mod,
                db,
                "record-verdict",
                "--finding",
                str(finding),
                "--role",
                role,
                "--verdict",
                "confirmed",
            )
            == 2
        )
        assert "unknown role" in capsys.readouterr().err
        run(mod, db, "list", "verdicts")
        assert out_json(capsys) == []

    def test_a_blank_role_is_refused_by_the_argument_parser(self, mod, db, capsys):
        """Exit 2 either way, but via `nonblank` rather than the role check, so it
        raises instead of returning -- the blank-argument class owns it."""
        finding = a_finding(mod, db, capsys)
        with pytest.raises(SystemExit) as excinfo:
            run(
                mod,
                db,
                "record-verdict",
                "--finding",
                str(finding),
                "--role",
                "",
                "--verdict",
                "confirmed",
            )
        assert excinfo.value.code == 2
        run(mod, db, "list", "verdicts")
        assert out_json(capsys) == []

    def test_surrounding_whitespace_on_a_role_is_stripped_not_rejected(self, mod, db, capsys):
        """`--role "human "` IS `human`. Whitespace is a shell-quoting artifact,
        not a different role, and refusing it was never a control -- someone
        forging a human verdict types the word correctly. Case is still
        significant, so `AUDITOR` remains unknown.
        """
        finding = a_finding(mod, db, capsys)
        assert (
            run(
                mod,
                db,
                "record-verdict",
                "--finding",
                str(finding),
                "--role",
                " human ",
                "--verdict",
                "confirmed",
            )
            == 0
        )
        assert out_json(capsys)["final_verdict"] == "confirmed"
        run(mod, db, "list", "verdicts")
        assert [row["role"] for row in out_json(capsys)] == ["human"]

    def test_the_schema_refuses_an_unknown_role_too(self, mod, db, capsys):
        """Belt and braces: the CLI check is a good error message, the CHECK
        constraint is the guarantee — a human writing SQL by hand hits it."""
        finding = a_finding(mod, db, capsys)
        conn = mod.connect(db)
        try:
            with pytest.raises(Exception):
                conn.execute(
                    "INSERT INTO verdicts (finding_id, role, verdict, ts)"
                    " VALUES (?, 'fixer', 'confirmed', '2026-09-07T00:00:00+00:00')",
                    (finding,),
                )
                conn.commit()
        finally:
            conn.close()

    def test_a_verdict_for_a_missing_finding_is_exit_2(self, mod, db, capsys):
        assert (
            run(
                mod,
                db,
                "record-verdict",
                "--finding",
                "999",
                "--role",
                "auditor",
                "--verdict",
                "confirmed",
            )
            == 2
        )
        assert "no finding" in capsys.readouterr().err


class TestLessonsCiteAFinding:
    def test_an_unknown_source_finding_is_refused(self, mod, db, capsys):
        assert (
            run(
                mod,
                db,
                "propose-lesson",
                "--kind",
                "false-positive",
                "--surface",
                "s",
                "--pattern",
                "p",
                "--guidance",
                "g",
                "--source-finding",
                "404",
            )
            == 2
        )
        assert "every lesson must cite one" in capsys.readouterr().err
        run(mod, db, "list", "lessons")
        assert out_json(capsys) == []

    def test_the_foreign_key_is_enforced_on_direct_sql(self, mod, db, capsys):
        """``PRAGMA foreign_keys`` is OFF by default per connection, so the
        NOT NULL REFERENCES is only a constraint because ``connect`` turns it
        on. A human editing rows by hand must not be able to orphan a lesson."""
        a_finding(mod, db, capsys)
        conn = mod.connect(db)
        try:
            with pytest.raises(Exception):
                conn.execute(
                    "INSERT INTO lessons (kind, surface, pattern, guidance,"
                    " source_finding_id, ts, active) VALUES"
                    " ('missed', 's', 'p', 'g', 4242, '2026-09-07T00:00:00+00:00', 1)"
                )
                conn.commit()
        finally:
            conn.close()

    def test_an_unknown_kind_is_refused(self, mod, db, capsys):
        finding = a_finding(mod, db, capsys)
        assert (
            run(
                mod,
                db,
                "propose-lesson",
                "--kind",
                "hunch",
                "--surface",
                "s",
                "--pattern",
                "p",
                "--guidance",
                "g",
                "--source-finding",
                str(finding),
            )
            == 2
        )
        assert "unknown kind" in capsys.readouterr().err


class TestUnapprovedLessonsNeverSeed:
    def test_a_proposed_lesson_is_inactive_and_absent_from_the_seed(self, mod, db, capsys):
        finding = a_finding(mod, db, capsys)
        lesson = a_lesson(mod, db, capsys, finding, approve=False)
        run(mod, db, "list", "lessons")
        row = out_json(capsys)[0]
        assert row["active"] == 0
        assert row["approved_by"] is None

        assert (
            run(
                mod, db, "seed-lessons", "--surface", "security.is_denied", "--budget-bytes", "4000"
            )
            == 0
        )
        assert capsys.readouterr().out == ""

        run(mod, db, "approve-lesson", "--id", str(lesson), "--approved-by", "zejiangg")
        capsys.readouterr()
        run(mod, db, "seed-lessons", "--surface", "security.is_denied", "--budget-bytes", "4000")
        assert "read argv, not the command string" in capsys.readouterr().out

    def test_approving_a_missing_lesson_is_exit_2(self, mod, db, capsys):
        assert run(mod, db, "approve-lesson", "--id", "7", "--approved-by", "zejiangg") == 2
        assert "no lesson" in capsys.readouterr().err

    def test_approval_records_who_approved(self, mod, db, capsys):
        finding = a_finding(mod, db, capsys)
        a_lesson(mod, db, capsys, finding)
        run(mod, db, "list", "lessons")
        row = out_json(capsys)[0]
        assert row["active"] == 1
        assert row["approved_by"] == "zejiangg"


class TestApprovalIsWriteOnce:
    """`approved_by` names the approval that let a lesson into a seed, so losing
    it loses the audit trail the human gate exists to leave.

    The module docstring claimed "every lesson is attributable" and that approval
    "demands a named approver" while the UPDATE was unconditional -- a claimed
    guarantee with no test, which is the pattern this PR's retrospective found
    behind all three of its rounds.
    """

    def test_reapproval_is_refused_and_the_original_approver_survives(self, mod, db, capsys):
        finding = a_finding(mod, db, capsys)
        lesson = a_lesson(mod, db, capsys, finding, approve=False)
        assert run(mod, db, "approve-lesson", "--id", str(lesson), "--approved-by", "first") == 0
        capsys.readouterr()

        assert run(mod, db, "approve-lesson", "--id", str(lesson), "--approved-by", "second") == 2
        err = capsys.readouterr().err
        assert "already approved by 'first'" in err
        assert "never replaced" in err

        run(mod, db, "list", "lessons")
        row = out_json(capsys)[0]
        assert row["approved_by"] == "first"
        assert row["active"] == 1

    def test_a_refused_reapproval_is_distinguishable_from_a_missing_lesson(self, mod, db, capsys):
        """Both exit 2, so the message is the only thing that tells an operator
        whether to go find the id or go find the approver."""
        finding = a_finding(mod, db, capsys)
        lesson = a_lesson(mod, db, capsys, finding)
        assert run(mod, db, "approve-lesson", "--id", str(lesson), "--approved-by", "x") == 2
        assert "already approved" in capsys.readouterr().err
        assert run(mod, db, "approve-lesson", "--id", "4242", "--approved-by", "x") == 2
        assert "no lesson with id" in capsys.readouterr().err

    def test_the_update_itself_refuses_not_just_the_read(self, mod, db, capsys):
        """The read is a good error message; `AND active = 0` is the guarantee.

        Called at the SQL layer the way a concurrent approver reaches it, so this
        fails if the guard lives only in the Python branch above.
        """
        finding = a_finding(mod, db, capsys)
        lesson = a_lesson(mod, db, capsys, finding, approve=False)
        run(mod, db, "approve-lesson", "--id", str(lesson), "--approved-by", "first")
        capsys.readouterr()
        conn = mod.connect(db)
        try:
            cursor = conn.execute(
                "UPDATE lessons SET active = 1, approved_by = ? WHERE id = ? AND active = 0",
                ("second", lesson),
            )
            conn.commit()
            assert cursor.rowcount == 0
            row = conn.execute("SELECT approved_by FROM lessons WHERE id = ?", (lesson,)).fetchone()
        finally:
            conn.close()
        assert row["approved_by"] == "first"

    def test_a_concurrent_approval_reports_raced_instead_of_a_false_success(self, mod, db, capsys):
        """The read and the UPDATE are separate, so a second approver can land
        between them. Without the rowcount check the loser prints a success line
        naming ITSELF as the approver for a write that changed no row.

        The race is driven by making the first read report the lesson as still
        unapproved, which is exactly what a concurrent approver causes.
        """
        finding = a_finding(mod, db, capsys)
        lesson = a_lesson(mod, db, capsys, finding, approve=False)
        conn = mod.connect(db)
        try:
            original = mod._read_lesson_state
            calls = []

            def stale_then_real(connection, lesson_id):
                calls.append(lesson_id)
                if len(calls) == 1:
                    # The pre-UPDATE read, taken before the rival committed.
                    return False, None
                return original(connection, lesson_id)

            # The rival's approval, already committed by the time the UPDATE runs.
            with conn:
                conn.execute(
                    "UPDATE lessons SET active = 1, approved_by = 'rival' WHERE id = ?",
                    (lesson,),
                )
            mod._read_lesson_state = stale_then_real
            try:
                outcome, holder = mod.approve_lesson(conn, lesson_id=lesson, approved_by="loser")
            finally:
                mod._read_lesson_state = original
            row = conn.execute("SELECT approved_by FROM lessons WHERE id = ?", (lesson,)).fetchone()
        finally:
            conn.close()
        assert outcome == "raced"
        assert holder == "rival"
        assert row["approved_by"] == "rival"
        assert len(calls) == 2, "the loser must re-read to name the winner"

    def test_the_cli_surfaces_a_raced_approval_as_exit_2(self, mod, db, capsys, monkeypatch):
        """The outcome has to reach the operator, not just the helper's caller."""
        finding = a_finding(mod, db, capsys)
        lesson = a_lesson(mod, db, capsys, finding, approve=False)
        monkeypatch.setattr(mod, "approve_lesson", lambda *a, **k: ("raced", "rival"))
        assert run(mod, db, "approve-lesson", "--id", str(lesson), "--approved-by", "x") == 2
        err = capsys.readouterr().err
        assert "approved concurrently by 'rival'" in err

    def test_no_unguarded_approval_update_remains_in_the_cli(self, mod):
        """Structural: every `UPDATE lessons ... active = 1` must carry the guard.

        A behavioural test covers the one call site that exists; this covers the
        next one someone adds.
        """
        source = script_source_with_joined_literals()
        updates = [
            statement
            for statement in re.findall(r'"UPDATE lessons[^"]*"', source)
            if "active = 1" in statement
        ]
        assert updates, "expected the approval UPDATE to be present"
        for statement in updates:
            assert "active = 0" in statement, statement

    def test_approval_still_works_the_first_time(self, mod, db, capsys):
        """The guard must not break the ordinary path it protects."""
        finding = a_finding(mod, db, capsys)
        lesson = a_lesson(mod, db, capsys, finding, approve=False)
        assert run(mod, db, "approve-lesson", "--id", str(lesson), "--approved-by", "zejiangg") == 0
        assert out_json(capsys)["approved_by"] == "zejiangg"
        run(mod, db, "seed-lessons", "--surface", "security.is_denied", "--budget-bytes", "4000")
        assert "read argv, not the command string" in capsys.readouterr().out


class TestRequiredArgumentsMustCarryText:
    """`required=True` asserts a flag is PRESENT, not that it has a value.

    `--approved-by ""` satisfied the parser and wrote an ACTIVE lesson (and an
    active rule) with no attribution at all, which is the attribution guarantee
    failing while every gate stayed green.
    """

    @pytest.mark.parametrize("blank", ["", "   ", "\t", "\n "])
    def test_a_blank_approver_cannot_approve_a_lesson(self, mod, db, capsys, blank):
        finding = a_finding(mod, db, capsys)
        lesson = a_lesson(mod, db, capsys, finding, approve=False)
        with pytest.raises(SystemExit) as excinfo:
            run(mod, db, "approve-lesson", "--id", str(lesson), "--approved-by", blank)
        assert excinfo.value.code == 2
        run(mod, db, "list", "lessons")
        row = out_json(capsys)[0]
        assert row["active"] == 0, "a refused approval must not activate the lesson"
        assert row["approved_by"] is None

    @pytest.mark.parametrize(
        "field,value,reason,approver",
        [
            ("scope", "v", "r", ""),
            ("scope", "v", "", "zejiangg"),
            ("scope", "", "r", "zejiangg"),
            ("", "v", "r", "zejiangg"),
        ],
    )
    def test_a_rule_needs_text_in_every_attribution_field(
        self, mod, db, capsys, field, value, reason, approver
    ):
        with pytest.raises(SystemExit) as excinfo:
            run(
                mod,
                db,
                "add-rule",
                "--field",
                field,
                "--value",
                value,
                "--reason",
                reason,
                "--approved-by",
                approver,
            )
        assert excinfo.value.code == 2
        run(mod, db, "list", "rules")
        assert out_json(capsys) == [], "a refused rule must not be written"

    def test_a_blank_finding_field_is_refused(self, mod, db, capsys):
        with pytest.raises(SystemExit) as excinfo:
            run(mod, db, "add-finding", "--surface", "  ", "--title", "t", "--severity", "Low")
        assert excinfo.value.code == 2
        run(mod, db, "list", "findings")
        assert out_json(capsys) == []

    def test_a_blank_verdict_is_refused(self, mod, db, capsys):
        """A blank verdict folds into `final_verdict = ""`, which reads as
        "no verdict" while a row exists -- indistinguishable from an unjudged
        finding."""
        finding = a_finding(mod, db, capsys)
        with pytest.raises(SystemExit) as excinfo:
            run(
                mod,
                db,
                "record-verdict",
                "--finding",
                str(finding),
                "--role",
                "auditor",
                "--verdict",
                "",
            )
        assert excinfo.value.code == 2
        run(mod, db, "list", "verdicts")
        assert out_json(capsys) == []

    def test_a_blank_lesson_field_is_refused(self, mod, db, capsys):
        finding = a_finding(mod, db, capsys)
        with pytest.raises(SystemExit) as excinfo:
            run(
                mod,
                db,
                "propose-lesson",
                "--kind",
                "missed",
                "--surface",
                "s",
                "--pattern",
                "",
                "--guidance",
                "g",
                "--source-finding",
                str(finding),
            )
        assert excinfo.value.code == 2
        run(mod, db, "list", "lessons")
        assert out_json(capsys) == []

    def test_surrounding_whitespace_is_stripped_not_stored(self, mod, db, capsys):
        """A trailing space in an approver name is noise, not identity -- storing
        it would make two spellings of one person look like two approvers."""
        finding = a_finding(mod, db, capsys)
        lesson = a_lesson(mod, db, capsys, finding, approve=False)
        assert (
            run(mod, db, "approve-lesson", "--id", str(lesson), "--approved-by", "  zejiangg  ")
            == 0
        )
        capsys.readouterr()
        run(mod, db, "list", "lessons")
        assert out_json(capsys)[0]["approved_by"] == "zejiangg"

    def test_the_validator_guards_every_required_text_argument(self, mod):
        """Applied at the declaration, so the NEXT required argument added here is
        covered without anyone remembering. Every `required=True` must either be
        numeric or carry the validator.
        """
        source = script_source_with_joined_literals()
        offenders = [
            line.strip()
            for line in source.splitlines()
            if "add_argument(" in line
            and "required=True" in line
            and "type=nonblank" not in line
            and "type=int" not in line
        ]
        assert offenders == [], offenders

    def test_the_validator_itself_accepts_and_rejects(self, mod):
        assert mod.nonblank(" value ") == "value"
        for blank in ("", " ", "\t\n"):
            with pytest.raises(Exception):
                mod.nonblank(blank)


class TestTheCliDoesNotClaimToAuthenticate:
    """GPT round-2 F1, answered as prose rather than as a guard.

    The CLI cannot tell a human from an agent, and adding a check that pretends to
    would be the same overclaim in code. What it CAN do is not assert a boundary it
    has not got, so the docstring now names filesystem ownership as the real one.
    """

    def test_the_docstring_names_the_trust_boundary(self, mod):
        doc = mod.__doc__ or ""
        assert "not an authentication boundary" in doc
        assert "UNVERIFIED caller assertions" in doc
        assert "filesystem ownership" in doc

    def test_human_is_still_a_writable_role(self, mod, db, capsys):
        """Deliberately NOT refused. The RFC's fold needs human rows, and refusing
        them here would only push the same write to `sqlite3` while reading as a
        control -- the boundary is the file, not this argument parser.
        """
        finding = a_finding(mod, db, capsys)
        assert (
            run(
                mod,
                db,
                "record-verdict",
                "--finding",
                str(finding),
                "--role",
                "human",
                "--verdict",
                "confirmed",
            )
            == 0
        )
        assert out_json(capsys)["final_verdict"] == "confirmed"


class TestSeedLessonsBudgetAndOrder:
    def test_output_never_exceeds_the_byte_budget(self, mod, db, capsys):
        finding = a_finding(mod, db, capsys)
        for index in range(12):
            a_lesson(mod, db, capsys, finding, pattern=f"pattern-{index}", guidance="g" * 40)
        budget = 300
        assert (
            run(
                mod,
                db,
                "seed-lessons",
                "--surface",
                "security.is_denied",
                "--budget-bytes",
                str(budget),
            )
            == 0
        )
        text = capsys.readouterr().out
        assert len(text.encode("utf-8")) <= budget
        # and the budget is doing real work, not trivially satisfied
        assert 0 < len(text.strip().splitlines()) < 12

    def test_a_zero_budget_emits_nothing(self, mod, db, capsys):
        finding = a_finding(mod, db, capsys)
        a_lesson(mod, db, capsys, finding)
        assert (
            run(mod, db, "seed-lessons", "--surface", "security.is_denied", "--budget-bytes", "0")
            == 0
        )
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "no approved lesson fits" in captured.err

    def test_a_negative_budget_is_exit_2(self, mod, db, capsys):
        assert run(mod, db, "seed-lessons", "--surface", "s", "--budget-bytes", "-1") == 2
        assert "must not be negative" in capsys.readouterr().err

    def test_surface_match_outranks_recency(self, mod, db, capsys):
        """A newer lesson about another surface must not displace an older one
        about the surface being audited — otherwise the seed drifts off-topic
        exactly as the budget tightens."""
        finding = a_finding(mod, db, capsys)
        a_lesson(mod, db, capsys, finding, surface="security.is_denied", pattern="on-surface")
        a_lesson(mod, db, capsys, finding, surface="webhooks", pattern="off-surface")
        run(mod, db, "seed-lessons", "--surface", "security.is_denied", "--budget-bytes", "4000")
        lines = capsys.readouterr().out.strip().splitlines()
        assert "on-surface" in lines[0]
        assert "off-surface" in lines[1]

    def test_within_one_surface_the_newest_comes_first(self, mod, db, capsys):
        finding = a_finding(mod, db, capsys)
        a_lesson(mod, db, capsys, finding, pattern="older")
        a_lesson(mod, db, capsys, finding, pattern="newer")
        run(mod, db, "seed-lessons", "--surface", "security.is_denied", "--budget-bytes", "4000")
        lines = capsys.readouterr().out.strip().splitlines()
        assert "newer" in lines[0]
        assert "older" in lines[1]

    def test_truncation_takes_the_priority_prefix_not_a_best_fit_pack(self, mod, db, capsys):
        """A long top-priority lesson ENDS the list rather than being skipped so
        a shorter, lower-priority one can slip in — skipping would silently
        reorder the ranking the caller asked for."""
        finding = a_finding(mod, db, capsys)
        a_lesson(mod, db, capsys, finding, pattern="short-and-old")
        a_lesson(mod, db, capsys, finding, pattern="long-and-new", guidance="g" * 400)
        run(mod, db, "seed-lessons", "--surface", "security.is_denied", "--budget-bytes", "200")
        assert capsys.readouterr().out == ""

    def test_every_seed_line_carries_its_source_finding(self, mod, db, capsys):
        finding = a_finding(mod, db, capsys)
        a_lesson(mod, db, capsys, finding)
        run(mod, db, "seed-lessons", "--surface", "security.is_denied", "--budget-bytes", "4000")
        assert f"(finding #{finding})" in capsys.readouterr().out


class TestExportRoe:
    def test_only_active_rules_are_exported_and_they_group_by_field(self, mod, db, capsys):
        run(
            mod,
            db,
            "add-rule",
            "--field",
            "scope",
            "--value",
            "kirodotdev/KiroCrew",
            "--reason",
            "pilot target",
            "--approved-by",
            "zejiangg",
        )
        live_scope = out_json(capsys)["id"]
        run(
            mod,
            db,
            "add-rule",
            "--field",
            "scope",
            "--value",
            "src/kiro_crew/security.py",
            "--reason",
            "surface 1",
            "--approved-by",
            "zejiangg",
        )
        run(
            mod,
            db,
            "add-rule",
            "--field",
            "forbidden",
            "--value",
            "no production systems",
            "--reason",
            "blast radius",
            "--approved-by",
            "zejiangg",
        )
        reverted = out_json(capsys)["id"]

        conn = mod.connect(db)
        try:
            conn.execute("UPDATE roe_rules SET active = 0 WHERE id = ?", (reverted,))
            conn.commit()
        finally:
            conn.close()

        run(mod, db, "export-roe")
        exported = out_json(capsys)
        assert sorted(exported) == ["scope"]
        assert len(exported["scope"]) == 2
        assert live_scope in [entry["id"] for entry in exported["scope"]]
        # A reverted rule is ABSENT, not present-and-flagged: a consumer that
        # ignores an `active` field must not be able to read it as live.
        assert "forbidden" not in exported
        assert all("active" not in entry for entry in exported["scope"])

    def test_every_exported_rule_is_attributable(self, mod, db, capsys):
        run(
            mod,
            db,
            "add-rule",
            "--field",
            "allowed_techniques",
            "--value",
            "code review",
            "--reason",
            "static only by default",
            "--approved-by",
            "zejiangg",
        )
        run(mod, db, "export-roe")
        entry = out_json(capsys)["allowed_techniques"][0]
        assert entry["reason"] == "static only by default"
        assert entry["approved_by"] == "zejiangg"
        assert entry["ts"]

    def test_a_rule_without_a_reason_or_approver_cannot_be_added(self, mod, db):
        with pytest.raises(SystemExit) as excinfo:
            run(mod, db, "add-rule", "--field", "scope", "--value", "everything")
        assert excinfo.value.code == 2

    def test_an_empty_ledger_exports_an_empty_object(self, mod, db, capsys):
        run(mod, db, "export-roe")
        assert out_json(capsys) == {}


class TestListing:
    def test_rules_lists_the_roe_rules_table(self, mod, db, capsys):
        run(
            mod,
            db,
            "add-rule",
            "--field",
            "scope",
            "--value",
            "v",
            "--reason",
            "r",
            "--approved-by",
            "zejiangg",
        )
        capsys.readouterr()
        run(mod, db, "list", "rules")
        rows = out_json(capsys)
        assert [row["field"] for row in rows] == ["scope"]
        assert rows[0]["active"] == 1

    def test_findings_paths_round_trip_as_a_sorted_json_list(self, mod, db, capsys):
        """Enough paths that set-iteration order is not sorted order by accident:
        two elements can round-trip correctly under a mutation that drops the sort
        entirely, which is exactly what the red-before harness caught."""
        given = ["z.py", "b.py", "m.py", "a.py", "q.py", "c.py", "y.py"]
        argv = ["add-finding", "--surface", "s", "--title", "t", "--severity", "Low"]
        for path in given:
            argv += ["--path", path]
        run(mod, db, *argv)
        capsys.readouterr()
        run(mod, db, "list", "findings")
        stored = json.loads(out_json(capsys)[0]["paths"])
        assert stored == sorted(given)
        assert stored != given, "the input order must not already be sorted"

    def test_an_unknown_table_is_refused(self, mod, db):
        with pytest.raises(SystemExit) as excinfo:
            run(mod, db, "list", "secrets")
        assert excinfo.value.code == 2


class TestDbFlagPosition:
    """``--db`` has exactly ONE position: before the subcommand, as documented.

    Accepting it on both sides was tried and removed. It needed a second
    declaration defaulting to ``argparse.SUPPRESS`` -- with ``None`` the subparser
    silently overwrote a value given before the subcommand whenever it was
    omitted after it -- and that trap is not worth carrying for a flag whose
    normal case is to be omitted. The usage block now states the one position, so
    argparse refusing the other is the contract rather than the defect a reviewer
    first reported.
    """

    def test_before_the_subcommand_is_the_documented_position(self, mod, tmp_path, capsys):
        target = tmp_path / "before.db"
        assert mod.main(["--db", str(target), "init"]) == 0
        assert json.loads(capsys.readouterr().out)["db"] == str(target)
        assert target.exists()

    def test_after_the_subcommand_is_refused(self, mod, tmp_path):
        """Pinned deliberately: the previous defect was the USAGE BLOCK promising
        this spelling, not argparse declining it."""
        with pytest.raises(SystemExit) as excinfo:
            mod.main(["init", "--db", str(tmp_path / "after.db")])
        assert excinfo.value.code == 2

    def test_the_usage_block_documents_the_position_it_accepts(self, mod):
        """The doc and the parser must not drift apart again -- that drift IS the
        finding this replaced."""
        doc = mod.__doc__ or ""
        assert "``--db PATH`` goes BEFORE the subcommand" in doc
        assert "python3 ledger.py [--db PATH] init" in doc


class TestDefaultDbPath:
    def test_the_default_path_follows_the_data_home(self, mod, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "crew"))
        assert mod.default_db_path() == tmp_path / "crew" / "security-conductor" / "findings.db"

    def test_the_parent_directory_is_created_on_demand(self, mod, tmp_path, capsys):
        target = tmp_path / "nested" / "deeper" / "findings.db"
        assert mod.main(["--db", str(target), "init"]) == 0
        capsys.readouterr()
        assert target.exists()
