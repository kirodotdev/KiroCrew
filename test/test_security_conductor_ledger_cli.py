"""The findings ledger's read surface: the ROE export, the listings and ``--db``.

An export shows only live rules and every rule in it is attributable, a listing
returns the table it names and refuses any other, and ``--db`` has one documented
position and a default that follows the data home. Shared fixtures and builders
live in ``security_conductor_ledger_helpers.py``.
"""

from __future__ import annotations

import json

import pytest
from security_conductor_ledger_helpers import (  # noqa: F401 -- fixtures are used by name
    db_fixture,
    mod_fixture,
    out_json,
    run,
)


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
