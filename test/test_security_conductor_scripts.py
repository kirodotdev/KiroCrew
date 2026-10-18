"""The security conductor's three evaluator scripts -- the properties, not the plumbing.

Each test pins one guarantee the RFC asks a SCRIPT to hold rather than asking an
agent to hold it: a scope verdict whose precedence is fixed, an ``UNKNOWN`` that
is never permission, one finding per real defect, and a verifier that refuses a
proof of concept the rules of engagement forbid instead of running it.

Every script is driven through ``main`` with argv, the way the skill invokes it,
because the exit code IS the interface -- a property that holds only in a helper
called directly would not be the one the conductor reads.

This module holds the ``scope_check`` and ``finding_entry`` sections; the
``verify_finding`` section is in ``test_security_conductor_scripts_verify_finding.py``,
and the fixtures and builders both share are in ``security_conductor_scripts_helpers.py``.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from security_conductor_scripts_helpers import db_fixture  # noqa: F401
from security_conductor_scripts_helpers import finding_entry_fixture  # noqa: F401
from security_conductor_scripts_helpers import ledger_fixture  # noqa: F401
from security_conductor_scripts_helpers import missing_json_fixture  # noqa: F401
from security_conductor_scripts_helpers import scope_check_fixture  # noqa: F401
from security_conductor_scripts_helpers import (
    FINDING_ARGS,
    add_rule,
    file_finding,
    finding_json,
    out_json,
    seed_roe,
)


def run_scope(scope_check, db: Path, roe_json: Path, *argv: str) -> int:
    return scope_check.main(["--db", str(db), "--roe-json", str(roe_json), *argv])


# --------------------------------------------------------------------------- #
# scope_check
# --------------------------------------------------------------------------- #


def test_a_technique_both_allowed_and_forbidden_is_out_of_scope(
    ledger, scope_check, db, missing_json, capsys
):
    """Forbidden beats allowed. Precedence is the whole point of the script.

    A rules-of-engagement set can contain both an ``allowed_techniques`` row and
    a ``forbidden`` row that speak about the same technique -- that is what a
    narrowing edit LOOKS like, since reverting is flipping ``active`` rather than
    deleting the older row. If the allowed row could win, every narrowing would
    be silently undone by the row it was meant to narrow.
    """
    seed_roe(ledger, db)
    add_rule(ledger, db, "allowed_techniques", "network-egress")

    code = run_scope(scope_check, db, missing_json, "--technique", "network-egress")

    payload = out_json(capsys)
    assert code == 10
    assert payload["verdict"] == "OUT_OF_SCOPE"
    assert [rule["field"] for rule in payload["matched_rules"]] == ["forbidden"]
    assert "forbidden" in payload["reasons"][0]


def test_a_forbidden_path_beats_the_scope_row_that_covers_it(
    ledger, scope_check, db, missing_json, capsys
):
    """Same precedence, on the path axis rather than the technique axis."""
    seed_roe(ledger, db)
    add_rule(ledger, db, "forbidden", "path:src/kiro_crew/secrets/")

    code = run_scope(scope_check, db, missing_json, "--path", "src/kiro_crew/secrets/keys.py")

    payload = out_json(capsys)
    assert code == 10
    assert payload["verdict"] == "OUT_OF_SCOPE"
    assert payload["matched_rules"][0]["value"] == "path:src/kiro_crew/secrets/"


def test_an_empty_ledger_with_no_export_is_unknown_not_permission(
    scope_check, db, missing_json, capsys
):
    """No rules means no answer, and no answer is a nonzero exit."""
    code = run_scope(scope_check, db, missing_json, "--path", "src/kiro_crew/security.py")

    payload = out_json(capsys)
    assert code == 20
    assert payload["verdict"] == "UNKNOWN"
    assert payload["matched_rules"] == []


def test_a_rule_field_the_script_cannot_interpret_is_unknown(
    ledger, scope_check, db, missing_json, capsys
):
    """An unreadable rule withholds permission even when the query is covered.

    The query below is covered by ``path:src/``, so without this the verdict
    would be ``IN_SCOPE`` -- an answer computed while ignoring a rule somebody
    approved and nobody parsed.
    """
    seed_roe(ledger, db)
    add_rule(ledger, db, "aggression_budget", "3")

    code = run_scope(scope_check, db, missing_json, "--path", "src/kiro_crew/security.py")

    payload = out_json(capsys)
    assert code == 20
    assert payload["verdict"] == "UNKNOWN"
    assert any("aggression_budget" in reason for reason in payload["reasons"])


def test_a_bare_scope_value_is_unknown_because_repo_and_path_differ(
    ledger, scope_check, db, missing_json, capsys
):
    seed_roe(ledger, db)
    add_rule(ledger, db, "scope", "everything-really")

    code = run_scope(scope_check, db, missing_json, "--path", "src/kiro_crew/security.py")

    assert code == 20
    assert any("neither repo: nor path:" in reason for reason in out_json(capsys)["reasons"])


def test_severity_scale_and_report_schema_rows_are_skipped_not_unknown(
    ledger, scope_check, db, missing_json, capsys
):
    """The two non-scope fields must not poison every verdict.

    They ship in the real rules of engagement, so treating them as
    uninterpretable would make ``UNKNOWN`` the only reachable verdict.
    """
    seed_roe(ledger, db)
    add_rule(ledger, db, "report_schema", "poc")

    code = run_scope(scope_check, db, missing_json, "--path", "src/kiro_crew/security.py")

    assert code == 0
    assert out_json(capsys)["verdict"] == "IN_SCOPE"


def test_nothing_asked_is_unknown(scope_check, db, missing_json, capsys):
    code = run_scope(scope_check, db, missing_json)

    assert code == 20
    assert out_json(capsys)["verdict"] == "UNKNOWN"


def test_a_repo_and_path_and_technique_all_covered_is_in_scope(
    ledger, scope_check, db, missing_json, capsys
):
    seed_roe(ledger, db)

    code = run_scope(
        scope_check,
        db,
        missing_json,
        "--repo",
        "kirodotdev/KiroCrew",
        "--path",
        "src/kiro_crew/security.py",
        "--technique",
        "static-code-review",
    )

    payload = out_json(capsys)
    assert code == 0
    assert payload["verdict"] == "IN_SCOPE"
    assert {rule["field"] for rule in payload["matched_rules"]} == {"scope", "allowed_techniques"}


def test_an_uncovered_repo_is_out_of_scope(ledger, scope_check, db, missing_json, capsys):
    seed_roe(ledger, db)

    code = run_scope(scope_check, db, missing_json, "--repo", "someone-else/their-repo")

    assert code == 10
    assert out_json(capsys)["verdict"] == "OUT_OF_SCOPE"


def test_a_human_approval_technique_is_needs_approval_not_in_scope(
    ledger, scope_check, db, missing_json, capsys
):
    """The gate is held by a nonzero exit; only a human lifts it."""
    seed_roe(ledger, db)

    code = run_scope(
        scope_check, db, missing_json, "--technique", "active-testing-beyond-static-review"
    )

    payload = out_json(capsys)
    assert code == 30
    assert payload["verdict"] == "NEEDS_APPROVAL"
    assert payload["matched_rules"][0]["field"] == "human_approval"


def test_a_traversal_cannot_borrow_an_in_scope_prefix(
    ledger, scope_check, db, missing_json, capsys
):
    """``src/../../etc/passwd`` starts with ``src/`` as text and is not under it."""
    seed_roe(ledger, db)

    code = run_scope(scope_check, db, missing_json, "--path", "src/../../etc/passwd")

    assert code == 10
    assert out_json(capsys)["verdict"] == "OUT_OF_SCOPE"


def test_a_root_scope_prefix_still_does_not_cover_a_path_above_the_root(
    ledger, scope_check, db, missing_json, capsys
):
    """``path:.`` means everything under the repository root, and ``..`` is not.

    This is the case the traversal guard exists for: the escape survives
    normalisation as a leading ``..``, and a relative path is exactly what a
    bare-dot prefix otherwise admits, so without the guard the widest legitimate
    scope rule would also be the one that leaks the whole host.
    """
    add_rule(ledger, db, "scope", "path:.")

    code = run_scope(scope_check, db, missing_json, "--path", "../etc/passwd")

    assert code == 10
    assert out_json(capsys)["verdict"] == "OUT_OF_SCOPE"


def test_a_path_prefix_matches_whole_segments_only(ledger, scope_check, db, missing_json, capsys):
    """``path:src`` must not cover ``srcfoo/``."""
    add_rule(ledger, db, "scope", "path:src")

    code = run_scope(scope_check, db, missing_json, "--path", "srcfoo/thing.py")

    assert code == 10
    assert out_json(capsys)["verdict"] == "OUT_OF_SCOPE"


def test_a_partial_technique_word_does_not_authorise_the_whole_technique(
    ledger, scope_check, db, missing_json, capsys
):
    """A rule that PERMITS is matched whole; only a rule that REFUSES contains.

    Containment on ``allowed_techniques`` let a fragment stand in for the whole:
    ``unit`` sat inside the allowed ``local-unit-poc`` and came back IN_SCOPE for
    a technique no rule authorises. Widening must only ever widen what is
    refused.
    """
    seed_roe(ledger, db)

    code = run_scope(scope_check, db, missing_json, "--technique", "unit")

    payload = out_json(capsys)
    assert code == 10
    assert payload["verdict"] == "OUT_OF_SCOPE"
    assert payload["matched_rules"] == []


def test_a_partial_technique_word_still_meets_a_forbidden_phrase(
    ledger, scope_check, db, missing_json, capsys
):
    """The other half of the asymmetry: containment stays on ``forbidden``.

    ``no-network-egress-from-a-poc`` has to refuse ``--technique network-egress``,
    which is the whole reason the containment scan exists.
    """
    seed_roe(ledger, db)

    code = run_scope(scope_check, db, missing_json, "--technique", "network-egress")

    assert code == 10
    assert out_json(capsys)["matched_rules"][0]["field"] == "forbidden"


@pytest.mark.parametrize(
    "outside",
    [
        "C:\\outside\\file",
        "C:/outside/file",
        "//host/share/file",
        "\\\\host\\share\\file",
        # Drive-RELATIVE: no separator after the colon. Relative to that
        # drive's own current directory, which is still not this repository --
        # and the form a separator-requiring pattern let through.
        "C:Windows\\System32\\drivers\\etc\\hosts",
        "C:outside",
    ],
)
def test_a_root_scope_prefix_does_not_cover_an_absolute_path_on_any_host(
    ledger, scope_check, db, missing_json, capsys, outside
):
    """``path:.`` means "under the repository root" -- and a drive path is not.

    A drive-qualified Windows path has no leading slash, so a leading-slash test
    read it as relative and the widest legitimate scope rule covered the whole
    host. Absoluteness is judged for BOTH conventions, not for whichever host
    happens to run the script -- the rules of engagement are one document on
    Linux and Windows alike.
    """
    add_rule(ledger, db, "scope", "path:.")

    code = run_scope(scope_check, db, missing_json, "--path", outside)

    assert code == 10
    assert out_json(capsys)["verdict"] == "OUT_OF_SCOPE"


def test_a_permitting_rule_naming_a_path_is_unknown_not_permission(
    ledger, scope_check, db, missing_json, capsys
):
    """A malformed rule in the field that GRANTS must fail closed.

    The matcher compares a technique with its prefix stripped, so
    ``allowed_techniques=path:network-egress`` authorised the technique
    ``network-egress`` -- a rule nobody wrote as a technique grant, handing out
    latitude. Only ``scope`` was checked for a sensible prefix, so this was the
    one field where a malformed rule failed OPEN.
    """
    add_rule(ledger, db, "scope", "path:src/")
    add_rule(ledger, db, "allowed_techniques", "path:network-egress")

    code = run_scope(scope_check, db, missing_json, "--technique", "network-egress")

    payload = out_json(capsys)
    # OUT_OF_SCOPE, not UNKNOWN: the documented precedence puts "an item no rule
    # covers" ahead of "a rule that could not be interpreted", and the malformed
    # grant covers nothing. Both codes are nonzero, so what matters is
    # that the rule did not authorise the technique -- which is what the empty
    # matched_rules asserts.
    assert code == 10
    assert payload["verdict"] == "OUT_OF_SCOPE"
    assert payload["matched_rules"] == []


def test_a_permitting_rule_naming_a_path_is_recorded_as_uninterpretable(
    ledger, scope_check, db, missing_json, capsys
):
    """The other half: the malformed grant is REPORTED, not silently skipped.

    Dropping it from the grant list closes the fail-open, but a rule somebody
    approved and nobody could read must also withhold permission from the rest of
    the query rather than being quietly ignored -- so with everything asked about
    covered, the verdict is still UNKNOWN and names the rule.
    """
    add_rule(ledger, db, "scope", "path:src/")
    add_rule(ledger, db, "allowed_techniques", "path:network-egress")

    code = run_scope(scope_check, db, missing_json, "--path", "src/kiro_crew/security.py")

    payload = out_json(capsys)
    assert code == 20
    assert payload["verdict"] == "UNKNOWN"
    assert any("grants a technique" in reason for reason in payload["reasons"])


def test_a_human_approval_rule_naming_a_repo_is_unknown(
    ledger, scope_check, db, missing_json, capsys
):
    """Both granting fields, not just the one the finding cited."""
    add_rule(ledger, db, "scope", "path:src/")
    add_rule(ledger, db, "human_approval", "repo:active-testing")

    code = run_scope(scope_check, db, missing_json, "--technique", "active-testing")

    payload = out_json(capsys)
    assert code == 10
    assert payload["matched_rules"] == []


def test_a_forbidden_rule_may_still_name_a_path_or_repo(
    ledger, scope_check, db, missing_json, capsys
):
    """`forbidden` keeps the path and repo forms -- refusing is not granting.

    The asymmetry is the invariant: widening what is REFUSED is safe in a way
    that widening what is PERMITTED is not, so narrowing the granting fields must
    not narrow this one.
    """
    add_rule(ledger, db, "scope", "path:src/")
    add_rule(ledger, db, "forbidden", "path:src/kiro_crew/secrets/")

    code = run_scope(scope_check, db, missing_json, "--path", "src/kiro_crew/secrets/k.py")

    payload = out_json(capsys)
    assert code == 10
    assert payload["verdict"] == "OUT_OF_SCOPE"
    assert payload["matched_rules"][0]["field"] == "forbidden"


def test_a_forbidden_path_rule_does_not_poison_an_unrelated_verdict(
    ledger, scope_check, db, missing_json, capsys
):
    """A forbidden `path:` rule that is NOT hit must stay interpretable.

    This is the half that catches over-narrowing. The sibling test cannot: the
    forbidden check runs BEFORE the interpretability check, so a forbidden rule
    that MATCHES returns OUT_OF_SCOPE either way. Only an unrelated query
    separates "interpretable" from "uninterpretable" here -- and the shipped rules
    of engagement carry seven forbidden entries, so getting this wrong would
    answer UNKNOWN for every question ever asked.
    """
    add_rule(ledger, db, "scope", "path:src/")
    add_rule(ledger, db, "forbidden", "path:src/kiro_crew/secrets/")

    code = run_scope(scope_check, db, missing_json, "--path", "src/kiro_crew/security.py")

    payload = out_json(capsys)
    assert code == 0
    assert payload["verdict"] == "IN_SCOPE"


def test_a_forbidden_rule_with_a_mistyped_kind_withholds_permission(
    ledger, scope_check, db, missing_json, capsys
):
    """A typo in a REFUSING rule must not fail open.

    `forbidden=pth:src/private` is the operator meaning `path:`. Read as a bare
    technique phrase it matched no path, so the ceiling it was written to be
    silently vanished and `path:.` answered IN_SCOPE for `src/private/key.py`.
    An unknown kind is uninterpretable in every scope field, `forbidden`
    included: the permit/refuse asymmetry lets a forbidden rule name a path, it
    does not let one nobody can read count as enforced.
    """
    add_rule(ledger, db, "scope", "path:.")
    add_rule(ledger, db, "forbidden", "pth:src/private")

    code = run_scope(scope_check, db, missing_json, "--path", "src/private/key.py")

    payload = out_json(capsys)
    assert code == 20
    assert payload["verdict"] == "UNKNOWN"
    assert any("'pth'" in reason for reason in payload["reasons"])
    assert payload["matched_rules"][-1]["field"] == "forbidden"


@pytest.mark.parametrize(
    "value",
    [
        "no-dos-or-broad-fuzzing",
        "no bypassing a policy block: refusal is the boundary",
        "C:/Windows",
        "C:\\Windows",
        "D:",
    ],
)
def test_a_bare_forbidden_value_with_a_colon_is_still_a_technique_phrase(
    ledger, scope_check, db, missing_json, capsys, value
):
    """Only a word-shaped prefix is a kind. A colon inside prose, or a drive
    letter, is part of the value, and refusing those would answer UNKNOWN to
    every question the shipped rules of engagement are asked."""
    add_rule(ledger, db, "scope", "path:src/")
    add_rule(ledger, db, "forbidden", value)

    code = run_scope(scope_check, db, missing_json, "--path", "src/kiro_crew/security.py")

    assert code == 0
    assert out_json(capsys)["verdict"] == "IN_SCOPE"


def test_a_human_approval_gate_holds_even_when_the_technique_is_also_allowed(
    ledger, scope_check, db, missing_json, capsys
):
    """A gate any allow row can neutralise is not a gate.

    "In scope, but a human says go" is the ordinary configuration for a gated
    technique: the allow row puts it in scope, the approval row gates it. Checking
    the allow row first made the approval ceiling vanish exactly when both were
    present, which is the case the gate exists for.
    """
    add_rule(ledger, db, "allowed_techniques", "active-testing")
    add_rule(ledger, db, "human_approval", "active-testing")

    code = run_scope(scope_check, db, missing_json, "--technique", "active-testing")

    payload = out_json(capsys)
    assert code == 30
    assert payload["verdict"] == "NEEDS_APPROVAL"


@pytest.mark.parametrize("asked", ["SRC/private/key.py", "src/Private/KEY.py"])
def test_a_forbidden_path_refuses_regardless_of_case(
    ledger, scope_check, db, missing_json, capsys, asked
):
    """On a case-insensitive filesystem these are one file, so a forbidden prefix
    compared byte-for-byte missed the file it names. Only the REFUSING side folds
    case; the granting side stays exact, per the permit/refuse asymmetry."""
    add_rule(ledger, db, "scope", "path:.")
    add_rule(ledger, db, "forbidden", "path:src/private")

    code = run_scope(scope_check, db, missing_json, "--path", asked)

    payload = out_json(capsys)
    assert code == 10
    assert payload["verdict"] == "OUT_OF_SCOPE"


def test_a_granting_path_rule_does_not_fold_case(ledger, scope_check, db, missing_json, capsys):
    """Folding case on a GRANT would widen what is permitted."""
    add_rule(ledger, db, "scope", "path:src/")

    code = run_scope(scope_check, db, missing_json, "--path", "SRC/kiro_crew/security.py")

    assert code == 10
    assert out_json(capsys)["verdict"] == "OUT_OF_SCOPE"


def test_a_one_character_mistyped_kind_withholds_permission(
    ledger, scope_check, db, missing_json, capsys
):
    """`p:` for `path:` is a one-key typo and must not read as a technique phrase.
    A drive letter is the only single-letter colon prefix that is NOT a kind, and
    it is told apart by what follows the colon: a separator or nothing."""
    add_rule(ledger, db, "scope", "path:.")
    add_rule(ledger, db, "forbidden", "p:src/private")

    code = run_scope(scope_check, db, missing_json, "--path", "src/private/key.py")

    payload = out_json(capsys)
    assert code == 20
    assert payload["verdict"] == "UNKNOWN"
    assert any("'p'" in reason for reason in payload["reasons"])


def test_a_technique_prefixed_grant_is_still_interpretable(
    ledger, scope_check, db, missing_json, capsys
):
    """Narrowing the granting fields must not refuse their legitimate forms."""
    add_rule(ledger, db, "allowed_techniques", "technique:static-code-review")

    code = run_scope(scope_check, db, missing_json, "--technique", "static-code-review")

    assert code == 0
    assert out_json(capsys)["verdict"] == "IN_SCOPE"


def test_a_root_scope_prefix_still_covers_a_path_inside_the_repository(
    ledger, scope_check, db, missing_json, capsys
):
    """The absoluteness check must not refuse what ``path:.`` is FOR."""
    add_rule(ledger, db, "scope", "path:.")

    code = run_scope(scope_check, db, missing_json, "--path", "src/kiro_crew/security.py")

    assert code == 0
    assert out_json(capsys)["verdict"] == "IN_SCOPE"


def test_revoking_every_rule_is_unknown_and_does_not_reach_the_export(
    ledger, scope_check, db, tmp_path, capsys
):
    """Deactivating every rule is the RFC's REVERT path, not an empty ledger.

    A bad rule is undone by flipping ``active``, so the rows stay. Falling back on
    "zero ACTIVE rows" therefore consulted the export and handed back exactly the
    scope the revert removed -- with the export still listing it, because an
    export is a snapshot nobody re-runs on a revert.
    """
    seed_roe(ledger, db)
    export = tmp_path / "rules-of-engagement.json"
    export.write_text(
        json.dumps({"rules": {"scope": [{"value": "path:src/", "reason": "the revoked scope"}]}}),
        encoding="utf-8",
    )
    conn = ledger.connect(db)
    try:
        with conn:
            conn.execute("UPDATE roe_rules SET active = 0")
    finally:
        conn.close()

    code = scope_check.main(
        ["--db", str(db), "--roe-json", str(export), "--path", "src/kiro_crew/security.py"]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out.strip().splitlines()[-1])
    assert code == 20
    assert payload["verdict"] == "UNKNOWN"
    assert "none is active" in payload["reasons"][0]
    assert "falling back" not in captured.err


def test_a_truly_empty_ledger_still_reaches_the_export(scope_check, db, tmp_path, capsys):
    """The other side of the same condition: zero ROWS is the fallback trigger.

    Narrowing the trigger must not disable the fallback the RFC asks for.
    """
    conn_module = None  # the ledger is created by the export-less path below
    assert conn_module is None
    export = tmp_path / "rules-of-engagement.json"
    export.write_text(
        json.dumps({"rules": {"scope": [{"value": "path:src/", "reason": "seed"}]}}),
        encoding="utf-8",
    )

    code = scope_check.main(
        ["--db", str(db), "--roe-json", str(export), "--path", "src/kiro_crew/security.py"]
    )

    captured = capsys.readouterr()
    assert code == 0
    assert "falling back to the export" in captured.err


def test_a_ledger_whose_rules_were_all_revoked_reports_the_count(
    ledger, scope_check, db, missing_json, capsys
):
    """The reason names how many rules were revoked, so an operator can tell a
    revocation from a ledger nobody has written yet."""
    add_rule(ledger, db, "scope", "path:src/")
    add_rule(ledger, db, "allowed_techniques", "static-code-review")
    conn = ledger.connect(db)
    try:
        with conn:
            conn.execute("UPDATE roe_rules SET active = 0")
    finally:
        conn.close()

    code = run_scope(scope_check, db, missing_json, "--technique", "static-code-review")

    assert code == 20
    assert "2 rule(s)" in out_json(capsys)["reasons"][0]


def test_the_export_is_read_only_when_the_ledger_holds_no_rules(scope_check, db, tmp_path, capsys):
    """The fallback path, and the stderr notice that it was taken.

    The export is a file somebody can edit without leaving an attributable row,
    so an answer that came from it says so, and carries ``id: null`` where a row
    would have carried its id.
    """
    export = tmp_path / "rules-of-engagement.json"
    export.write_text(
        json.dumps(
            {
                "schema": "roe/v1",
                "rules": {
                    "scope": [{"value": "path:src/", "reason": "the export's own copy"}],
                    "allowed_techniques": [{"value": "static-code-review", "reason": "reading"}],
                },
            }
        ),
        encoding="utf-8",
    )

    code = scope_check.main(
        [
            "--db",
            str(db),
            "--roe-json",
            str(export),
            "--path",
            "src/kiro_crew/security.py",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out.strip().splitlines()[-1])
    assert code == 0
    assert payload["verdict"] == "IN_SCOPE"
    assert payload["matched_rules"] == [{"id": None, "field": "scope", "value": "path:src/"}]
    assert "falling back to the export" in captured.err


def test_a_seeded_ledger_is_never_overridden_by_the_export(
    ledger, scope_check, db, tmp_path, capsys
):
    """Rows are the source of truth: an export that widens scope is not consulted."""
    seed_roe(ledger, db)
    export = tmp_path / "rules-of-engagement.json"
    export.write_text(
        json.dumps({"rules": {"scope": [{"value": "path:", "reason": "everything"}]}}),
        encoding="utf-8",
    )

    code = scope_check.main(
        ["--db", str(db), "--roe-json", str(export), "--path", "website/src/main.tsx"]
    )

    captured = capsys.readouterr()
    assert code == 10
    assert "falling back" not in captured.err


def test_an_unreadable_database_is_unknown_and_does_not_fall_back(
    scope_check, db, tmp_path, capsys
):
    """Falling back needs the ledger to have SAID it holds no rules.

    A database that cannot be read said nothing, so reaching for the export
    would answer a scope question from a file while the source of truth was
    unavailable -- an answer nobody could attribute.
    """
    db.write_bytes(b"this is not a sqlite database at all")
    export = tmp_path / "rules-of-engagement.json"
    export.write_text(
        json.dumps({"rules": {"scope": [{"value": "path:src/", "reason": "wide open"}]}}),
        encoding="utf-8",
    )

    code = scope_check.main(
        ["--db", str(db), "--roe-json", str(export), "--path", "src/kiro_crew/security.py"]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out.strip().splitlines()[-1])
    assert code == 20
    assert payload["verdict"] == "UNKNOWN"
    assert "falling back" not in captured.err


def test_asking_a_scope_question_never_creates_a_ledger(scope_check, db, missing_json, capsys):
    """A read must not leave a database behind for a later write to inherit."""
    run_scope(scope_check, db, missing_json, "--path", "src/x.py")
    capsys.readouterr()

    assert not db.exists()


def test_asking_a_scope_question_does_not_rewrite_the_ledger(
    ledger, scope_check, db, missing_json, capsys
):
    """The docstring's "SELECT only" has to hold for the bytes on disk, not just the SQL.

    ``ledger.connect`` switches the journal to WAL, and that switch rewrites the
    file header of any database not already in WAL mode -- so a scope QUESTION
    that borrowed the writer's connection mutated the ledger it promised only to
    read, and left ``-wal``/``-shm`` sidecars beside it. The ledger here is put
    back into the default rollback journal first, so the switch would show up as
    changed bytes; a read-only open cannot make it.
    """
    seed_roe(ledger, db)
    plain = sqlite3.connect(str(db))
    try:
        plain.execute("PRAGMA journal_mode=DELETE")
    finally:
        plain.close()
    before = db.read_bytes()

    code = run_scope(
        scope_check, db, missing_json, "--repo", "kirodotdev/KiroCrew", "--path", "src/x.py"
    )
    capsys.readouterr()

    assert code == 0
    assert db.read_bytes() == before
    sidecars = sorted(p.name for p in db.parent.iterdir() if p.name.startswith(db.name + "-"))
    assert sidecars == []


# --------------------------------------------------------------------------- #
# finding_entry
# --------------------------------------------------------------------------- #


def test_a_second_file_of_the_same_defect_is_a_duplicate_with_the_same_id(
    ledger, finding_entry, db, missing_json, capsys
):
    """One record per real defect: a re-audit must not re-file what is recorded.

    The duplicate exit is nonzero AND still prints the id, because the caller's
    next step needs the handle and making it parse stderr for it would be the
    same information behind a worse contract.
    """
    seed_roe(ledger, db)

    first_code = file_finding(finding_entry, db, missing_json)
    first = out_json(capsys)
    second_code = file_finding(finding_entry, db, missing_json)
    second = out_json(capsys)

    assert (first_code, first["created"]) == (0, True)
    assert (second_code, second["created"]) == (3, False)
    assert second["finding_id"] == first["finding_id"]


def test_the_same_paths_in_a_different_order_are_the_same_defect(
    ledger, finding_entry, db, missing_json, capsys
):
    """Identity is the SORTED paths, so argument order cannot fork a finding."""
    seed_roe(ledger, db)
    base = [
        "--surface",
        "webhooks.ingest",
        "--severity",
        "High",
        "--title",
        "unbounded frame accepted",
        "--poc",
        "cmd::python3 -c pass",
    ]

    file_finding(finding_entry, db, missing_json, *base, "--path", "a.py", "--path", "b.py")
    first = out_json(capsys)
    code = file_finding(finding_entry, db, missing_json, *base, "--path", "b.py", "--path", "a.py")
    second = out_json(capsys)

    assert code == 3
    assert second["finding_id"] == first["finding_id"]


def test_a_severity_outside_the_scale_is_refused(ledger, finding_entry, db, missing_json, capsys):
    """The scale is the only adjudication vocabulary, so a level it does not name
    cannot be graded and must not reach the ledger."""
    seed_roe(ledger, db)
    capsys.readouterr()  # the seeding rules printed their own ids

    code = file_finding(
        finding_entry,
        db,
        missing_json,
        "--surface",
        "security.is_denied",
        "--severity",
        "Catastrophic",
        "--title",
        "invented grade",
        "--path",
        "src/kiro_crew/security.py",
        "--poc",
        "cmd::python3 -c pass",
    )

    captured = capsys.readouterr()
    assert code == 2
    assert "severity_scale" in captured.err
    assert captured.out.strip() == ""


def test_no_severity_scale_at_all_is_a_refusal_not_a_pass(
    ledger, finding_entry, db, missing_json, capsys
):
    """An unvalidatable grade is exactly what the scale exists to stop."""
    add_rule(ledger, db, "scope", "path:src/")

    code = file_finding(finding_entry, db, missing_json)

    assert code == 2
    assert "severity_scale" in capsys.readouterr().err


def test_a_finding_with_no_runnable_proof_is_refused(
    ledger, finding_entry, db, missing_json, capsys
):
    """A proof the verifier cannot re-run is prose, and prose is where
    hallucinated vulnerabilities come from."""
    seed_roe(ledger, db)

    code = file_finding(
        finding_entry,
        db,
        missing_json,
        "--surface",
        "security.is_denied",
        "--severity",
        "High",
        "--title",
        "described but not demonstrated",
        "--path",
        "src/kiro_crew/security.py",
        "--poc",
        "read the function and it is obvious",
    )

    assert code == 2
    assert "pytest::" in capsys.readouterr().err


def test_a_finding_with_no_path_is_refused(ledger, finding_entry, db, missing_json, capsys):
    seed_roe(ledger, db)

    code = file_finding(
        finding_entry,
        db,
        missing_json,
        "--surface",
        "security.is_denied",
        "--severity",
        "High",
        "--title",
        "nowhere in particular",
        "--poc",
        "cmd::python3 -c pass",
    )

    assert code == 2
    assert "paths" in capsys.readouterr().err


def test_a_json_finding_is_filed_and_an_unknown_key_is_refused(
    ledger, finding_entry, db, missing_json, tmp_path, capsys
):
    """A typo'd key is silently dropped data, so it is refused rather than ignored."""
    seed_roe(ledger, db)
    good = tmp_path / "finding.json"
    good.write_text(
        json.dumps(
            {
                "surface": "dashboard.token_auth",
                "severity": "Critical",
                "title": "session token accepted from a query string",
                "paths": ["src/kiro_crew/dashboard/token_auth.py"],
                "poc": "cmd::python3 -c pass",
                "round_id": "kirocrew-dogfood-round-1",
            }
        ),
        encoding="utf-8",
    )
    bad = tmp_path / "typo.json"
    bad.write_text(
        json.dumps(
            {
                "surface": "s",
                "severity": "Low",
                "title": "t",
                "paths": ["p"],
                "poc": "cmd::true",
                "sevrity": "Low",
            }
        ),
        encoding="utf-8",
    )

    good_code = finding_entry.main(
        ["--db", str(db), "--roe-json", str(missing_json), "--json-file", str(good)]
    )
    filed = out_json(capsys)
    bad_code = finding_entry.main(
        ["--db", str(db), "--roe-json", str(missing_json), "--json-file", str(bad)]
    )

    assert (good_code, filed["created"]) == (0, True)
    assert bad_code == 2
    assert "sevrity" in capsys.readouterr().err


def test_parallel_filings_of_one_defect_record_one_auditor_verdict(
    ledger, finding_entry, db, missing_json, tmp_path
):
    """The absence check and the auditor-row insert run under one write lock.

    Two auditors filing the same defect at once both saw "no auditor row" and
    both appended one -- into a verdict history that is INSERT-only by design, so
    the duplicate was permanent. Driven through real subprocesses against one
    SQLite file, with a barrier so both are past argument parsing before either
    touches the ledger.
    """
    seed_roe(ledger, db)
    payload = tmp_path / "finding.json"
    payload.write_text(json.dumps(finding_json(*FINDING_ARGS)), encoding="utf-8")
    script = Path(finding_entry.__file__ or "")
    argv = [
        sys.executable,
        str(script),
        "--db",
        str(db),
        "--roe-json",
        str(missing_json),
        "--json-file",
        str(payload),
    ]
    env = {k: v for k, v in os.environ.items() if not k.startswith("KIROCREW_")}
    procs = [subprocess.Popen(argv, env=env, stdout=subprocess.DEVNULL) for _ in range(6)]
    codes = [p.wait(timeout=120) for p in procs]

    assert sorted(codes) == [0, 3, 3, 3, 3, 3] or codes.count(3) == 5
    conn = ledger.connect(db)
    try:
        rows = conn.execute("SELECT COUNT(*) FROM verdicts WHERE role = 'auditor'").fetchone()[0]
        findings = conn.execute("SELECT COUNT(*) FROM findings").fetchone()[0]
    finally:
        conn.close()
    assert findings == 1
    assert rows == 1
