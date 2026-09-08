"""The security conductor's three evaluator scripts -- the properties, not the plumbing.

Each test pins one guarantee the RFC asks a SCRIPT to hold rather than asking an
agent to hold it: a scope verdict whose precedence is fixed, an ``UNKNOWN`` that
is never permission, one finding per real defect, and a verifier that refuses a
proof of concept the rules of engagement forbid instead of running it.

Every script is driven through ``main`` with argv, the way the skill invokes it,
because the exit code IS the interface -- a property that holds only in a helper
called directly would not be the one the conductor reads.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from skill_script_helpers import load_skill_script

SCRIPTS = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "kiro_crew"
    / "builtin_skills"
    / "security-conductor"
    / "scripts"
)


@pytest.fixture
def ledger():
    return load_skill_script("security_conductor_ledger_for_scripts", SCRIPTS / "ledger.py")


@pytest.fixture
def scope_check():
    return load_skill_script("security_conductor_scope_check", SCRIPTS / "scope_check.py")


@pytest.fixture
def finding_entry():
    return load_skill_script("security_conductor_finding_entry", SCRIPTS / "finding_entry.py")


@pytest.fixture
def verify_finding():
    return load_skill_script("security_conductor_verify_finding", SCRIPTS / "verify_finding.py")


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "findings.db"


@pytest.fixture
def missing_json(tmp_path: Path) -> Path:
    """An export path that does not exist, so no test silently reads the shipped one."""
    return tmp_path / "no-such-rules-of-engagement.json"


def out_json(capsys) -> dict:
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def add_rule(ledger, db: Path, field: str, value: str) -> int:
    code = ledger.main(
        [
            "--db",
            str(db),
            "add-rule",
            "--field",
            field,
            "--value",
            value,
            "--reason",
            "pinned by a test",
            "--approved-by",
            "test-human",
        ]
    )
    assert code == 0
    return code


SEVERITY_ROWS = (
    "Critical: a full bypass of the safety fence.",
    "High: an authorization bypass on a network-exposed surface.",
    "Low: a hardening gap with no demonstrated exploit.",
)


def seed_roe(ledger, db: Path) -> None:
    """A minimal, realistic rules-of-engagement set, in the shipped value grammar."""
    add_rule(ledger, db, "scope", "repo:kirodotdev/KiroCrew")
    add_rule(ledger, db, "scope", "path:src/")
    add_rule(ledger, db, "allowed_techniques", "static-code-review")
    add_rule(ledger, db, "allowed_techniques", "local-unit-poc")
    add_rule(ledger, db, "forbidden", "no-network-egress-from-a-poc")
    add_rule(ledger, db, "forbidden", "no-dos-or-broad-fuzzing")
    add_rule(ledger, db, "human_approval", "active-testing-beyond-static-review")
    for row in SEVERITY_ROWS:
        add_rule(ledger, db, "severity_scale", row)


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


# --------------------------------------------------------------------------- #
# finding_entry
# --------------------------------------------------------------------------- #


FINDING_ARGS = (
    "--surface",
    "security.is_denied",
    "--severity",
    "High",
    "--title",
    "echo text is classified as an executed program",
    "--path",
    "src/kiro_crew/security.py",
    "--poc",
    "pytest::test/test_security.py::test_echo_is_not_a_program",
)


def finding_json(*flags: str) -> dict:
    """Turn a flag-style spelling into the finding file's dict.

    The flag spelling is kept in the tests only because it reads well beside the
    field it varies; the script itself accepts one input, `--json-file`.
    """
    body: dict = {"paths": []}
    it = iter(flags)
    for flag in it:
        value = next(it)
        key = flag.lstrip("-").replace("-", "_")
        if key == "path":
            body["paths"].append(value)
        else:
            body[key] = value
    return body


def file_finding(finding_entry, db: Path, missing_json: Path, *extra: str) -> int:
    payload = db.parent / f"finding-{abs(hash(extra)) % 10**8}.json"
    payload.write_text(json.dumps(finding_json(*(extra or FINDING_ARGS))), encoding="utf-8")
    return finding_entry.main(
        ["--db", str(db), "--roe-json", str(missing_json), "--json-file", str(payload)]
    )


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


# --------------------------------------------------------------------------- #
# verify_finding
# --------------------------------------------------------------------------- #


FAILING_TEST = "def test_the_guard_admits_what_it_must_refuse():\n    assert False\n"
PASSING_TEST = "def test_the_guard_refuses_it():\n    assert True\n"
BROKEN_TEST = "import no_such_module_anywhere\n\n\ndef test_x():\n    assert False\n"


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    """A real git checkout, because that is what the verifier brief asks for."""
    root = tmp_path / "scratch-checkout"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(root), check=True)
    return root


def a_finding(ledger, db: Path, *, poc: str, title: str = "the guard admits a bad input") -> int:
    conn = ledger.connect(db)
    try:
        ledger.init_schema(conn)
        finding_id, _ = ledger.add_finding(
            conn,
            surface="security.is_denied",
            title=title,
            severity="High",
            paths=["src/kiro_crew/security.py"],
            poc=poc,
            round_id="r1",
        )
    finally:
        conn.close()
    return finding_id


def verdict_rows(ledger, db: Path, finding_id: int) -> list[tuple[str, str]]:
    conn = ledger.connect(db)
    try:
        rows = conn.execute(
            "SELECT role, verdict FROM verdicts WHERE finding_id = ? ORDER BY rowid ASC",
            (finding_id,),
        ).fetchall()
    finally:
        conn.close()
    return [(str(row["role"]), str(row["verdict"])) for row in rows]


def test_a_failing_pytest_proof_confirms_the_finding(ledger, verify_finding, db, worktree, capsys):
    (worktree / "test_poc.py").write_text(FAILING_TEST, encoding="utf-8")
    finding_id = a_finding(
        ledger, db, poc="pytest::test_poc.py::test_the_guard_admits_what_it_must_refuse"
    )

    code = verify_finding.main(
        ["--db", str(db), "--finding-id", str(finding_id), "--worktree", str(worktree)]
    )

    payload = out_json(capsys)
    assert code == 0
    assert payload["verdict"] == "confirmed"
    assert verdict_rows(ledger, db, finding_id) == [("verifier", "confirmed")]


def test_a_passing_pytest_proof_rejects_the_finding(ledger, verify_finding, db, worktree, capsys):
    (worktree / "test_poc.py").write_text(PASSING_TEST, encoding="utf-8")
    finding_id = a_finding(ledger, db, poc="pytest::test_poc.py::test_the_guard_refuses_it")

    code = verify_finding.main(
        ["--db", str(db), "--finding-id", str(finding_id), "--worktree", str(worktree)]
    )

    payload = out_json(capsys)
    assert code == 10
    assert payload["verdict"] == "rejected"
    assert verdict_rows(ledger, db, finding_id) == [("verifier", "rejected")]


def test_a_proof_that_errors_rather_than_fails_is_needs_human(
    ledger, verify_finding, db, worktree, capsys
):
    """pytest exits nonzero for a collection error too.

    Reading the exit status alone would confirm a finding whose proof never
    executed -- the exact false positive this pass exists to catch.
    """
    (worktree / "test_poc.py").write_text(BROKEN_TEST, encoding="utf-8")
    finding_id = a_finding(ledger, db, poc="pytest::test_poc.py::test_x")

    code = verify_finding.main(
        ["--db", str(db), "--finding-id", str(finding_id), "--worktree", str(worktree)]
    )

    payload = out_json(capsys)
    assert code == 20
    assert payload["verdict"] == "needs-human"
    assert verdict_rows(ledger, db, finding_id) == [("verifier", "needs-human")]


def test_a_nonzero_command_proof_confirms_and_a_zero_one_rejects(
    ledger, verify_finding, db, worktree, capsys
):
    reproduces = a_finding(
        ledger, db, poc='cmd::python3 -c "raise SystemExit(3)"', title="exits nonzero"
    )
    does_not = a_finding(ledger, db, poc="cmd::python3 -c pass", title="exits zero")

    confirmed = verify_finding.main(
        ["--db", str(db), "--finding-id", str(reproduces), "--worktree", str(worktree)]
    )
    capsys.readouterr()
    rejected = verify_finding.main(
        ["--db", str(db), "--finding-id", str(does_not), "--worktree", str(worktree)]
    )
    capsys.readouterr()

    assert (confirmed, rejected) == (0, 10)


def test_an_egress_proof_is_refused_unrun(ledger, verify_finding, db, worktree, capsys):
    """A forbidden shape is REFUSED, not run and then judged.

    The proof of concept below would create ``was-run`` in the worktree if it
    ever executed, so its absence is what proves the screen fired before the
    subprocess rather than after it.
    """
    finding_id = a_finding(ledger, db, poc="cmd::touch was-run curl")

    code = verify_finding.main(
        ["--db", str(db), "--finding-id", str(finding_id), "--worktree", str(worktree)]
    )

    payload = out_json(capsys)
    assert code == 20
    assert payload["verdict"] == "needs-human"
    assert "curl" in payload["reason"]
    assert not (worktree / "was-run").exists()
    assert verdict_rows(ledger, db, finding_id) == [("verifier", "needs-human")]


@pytest.mark.parametrize(
    "poc",
    [
        "cmd::cat ~/.aws/credentials",
        "cmd::cat ~/.ssh/id_rsa",
        "cmd::cat .kiro/crew/config.json",
        "cmd::python3 -c print(token)",
        "cmd::wget http://example.invalid/x",
        "cmd::ssh host true",
        "cmd::nc host 80",
    ],
)
def test_every_named_forbidden_shape_is_refused(ledger, verify_finding, db, worktree, capsys, poc):
    finding_id = a_finding(ledger, db, poc=poc, title=f"forbidden shape {poc}")

    code = verify_finding.main(
        ["--db", str(db), "--finding-id", str(finding_id), "--worktree", str(worktree)]
    )

    assert code == 20
    assert out_json(capsys)["verdict"] == "needs-human"


def test_a_word_inside_another_word_is_not_an_egress_shape(verify_finding):
    """``nc`` must not fire on ``since``, and ``token`` must not fire on ``tokenize``.

    A screen that refused every innocent PoC would route the whole round to a
    human, which is the same outcome as having no verifier at all.
    """
    assert verify_finding.refusal_reason("cmd::python3 -m tokenize since.py") is None
    assert verify_finding.refusal_reason("pytest::test/test_concurrency.py::test_since") is None


def test_a_directory_that_is_not_a_git_worktree_is_refused(
    ledger, verify_finding, db, tmp_path, capsys
):
    """A scratch checkout is the whole blast-radius bound."""
    plain = tmp_path / "just-a-directory"
    plain.mkdir()
    finding_id = a_finding(ledger, db, poc="cmd::python3 -c pass")

    code = verify_finding.main(
        ["--db", str(db), "--finding-id", str(finding_id), "--worktree", str(plain)]
    )

    payload = out_json(capsys)
    assert code == 20
    assert payload["verdict"] == "needs-human"
    assert "git worktree" in payload["reason"]


def test_a_proof_that_outlives_the_deadline_is_needs_human(
    ledger, verify_finding, db, worktree, capsys
):
    finding_id = a_finding(ledger, db, poc='cmd::python3 -c "import time; time.sleep(30)"')

    code = verify_finding.main(
        [
            "--db",
            str(db),
            "--finding-id",
            str(finding_id),
            "--worktree",
            str(worktree),
            "--timeout",
            "1",
        ]
    )

    payload = out_json(capsys)
    assert code == 20
    assert payload["verdict"] == "needs-human"
    assert "did not finish" in payload["reason"]
    assert verdict_rows(ledger, db, finding_id) == [("verifier", "needs-human")]


def test_an_unknown_finding_records_nothing(ledger, verify_finding, db, worktree, capsys):
    """A verdict about a finding that is not there has nothing to attach to."""
    a_finding(ledger, db, poc="cmd::python3 -c pass")

    code = verify_finding.main(
        ["--db", str(db), "--finding-id", "9999", "--worktree", str(worktree)]
    )

    captured = capsys.readouterr()
    assert code == 2
    assert captured.out.strip() == ""
    assert verdict_rows(ledger, db, 9999) == []


def test_a_recorded_finding_with_an_unreadable_proof_shape_records_nothing(
    ledger, verify_finding, db, worktree, capsys
):
    finding_id = a_finding(ledger, db, poc="just some prose")

    code = verify_finding.main(
        ["--db", str(db), "--finding-id", str(finding_id), "--worktree", str(worktree)]
    )

    assert code == 2
    assert verdict_rows(ledger, db, finding_id) == []


def test_a_nonpositive_timeout_is_refused(ledger, verify_finding, db, worktree, capsys):
    finding_id = a_finding(ledger, db, poc="cmd::python3 -c pass")

    code = verify_finding.main(
        [
            "--db",
            str(db),
            "--finding-id",
            str(finding_id),
            "--worktree",
            str(worktree),
            "--timeout",
            "0",
        ]
    )

    assert code == 2
    assert verdict_rows(ledger, db, finding_id) == []


def test_the_child_environment_does_not_inherit_the_operators_home(verify_finding, tmp_path):
    """``HOME`` points at the worktree, so ``~/.aws`` resolves inside the sandbox.

    The key set is asserted CLOSED, because that is what keeps the operator's
    credentials out of a child the audited checkout controls: a name arrives in
    this environment by being listed, never by being inherited.
    """
    env = verify_finding.child_env(tmp_path)

    assert env["HOME"] == str(tmp_path)
    expected = {
        "PATH",
        "HOME",
        "TMPDIR",
        "TEMP",
        "TMP",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONIOENCODING",
        "LC_ALL",
    }
    # The three Windows names are forwarded only where the host defines them, so
    # the closed set varies by platform in exactly this one way.
    expected |= {name for name in ("SYSTEMROOT", "PATHEXT", "COMSPEC") if name in os.environ}
    assert set(env) == expected


def test_the_verifier_verdict_is_appended_not_overwritten(
    ledger, verify_finding, db, worktree, capsys
):
    """The ledger keeps the disagreement; two runs leave two rows."""
    (worktree / "test_poc.py").write_text(PASSING_TEST, encoding="utf-8")
    finding_id = a_finding(ledger, db, poc="pytest::test_poc.py::test_the_guard_refuses_it")
    conn = ledger.connect(db)
    try:
        ledger.record_verdict(
            conn, finding_id=finding_id, role="auditor", verdict="confirmed", reason="I saw it"
        )
    finally:
        conn.close()

    verify_finding.main(
        ["--db", str(db), "--finding-id", str(finding_id), "--worktree", str(worktree)]
    )
    capsys.readouterr()

    assert verdict_rows(ledger, db, finding_id) == [
        ("auditor", "confirmed"),
        ("verifier", "rejected"),
    ]


def test_the_scripts_shell_out_to_nothing_but_the_proof_of_concept(scope_check, finding_entry):
    """Only ``verify_finding`` runs a subprocess; the other two are pure readers.

    Asserted on the source rather than described in a comment, because "this
    script runs nothing" is a property a later edit can quietly take away.
    """
    for module in (scope_check, finding_entry):
        source = Path(module.__file__ or "").read_text(encoding="utf-8")
        # The import, not the word: both files DISCUSS running nothing in their
        # docstrings, and a scan that matched the prose would pass for the wrong
        # reason and fail the moment someone rewrote a sentence.
        assert "import subprocess" not in source
        assert "os.system" not in source
        assert "os.popen" not in source


def test_every_script_carries_a_shebang_and_is_stdlib_only():
    for name in ("scope_check.py", "finding_entry.py", "verify_finding.py"):
        source = (SCRIPTS / name).read_text(encoding="utf-8")
        assert source.startswith("#!/usr/bin/env python3\n")
        imports = [
            line.split()[1].split(".")[0]
            for line in source.splitlines()
            if line.startswith("import ")
        ] + [
            line.split()[1].split(".")[0]
            for line in source.splitlines()
            if line.startswith("from ") and " import " in line
        ]
        assert set(imports) <= set(sys.stdlib_module_names), sorted(
            set(imports) - set(sys.stdlib_module_names)
        )


def test_a_proof_that_writes_outside_the_worktree_is_refused_unrun(
    ledger, verify_finding, db, worktree, capsys
):
    """The write-escape shape is refused, not run and then judged.

    The command below would create ``escaped`` in the worktree's PARENT if it
    ran, so that file's absence is what proves the screen fired before the
    subprocess. This is a named-shape screen, not containment: it refuses a
    ``..`` segment and an absolute path, and the module docstring says plainly
    that a program reaching outside by another spelling is bounded by the rules
    of engagement and the host's policy gate rather than by this check.
    """
    finding_id = a_finding(ledger, db, poc="cmd::touch ../escaped")

    code = verify_finding.main(
        ["--db", str(db), "--finding-id", str(finding_id), "--worktree", str(worktree)]
    )

    payload = out_json(capsys)
    assert code == 20
    assert payload["verdict"] == "needs-human"
    assert not (worktree.parent / "escaped").exists()
    assert verdict_rows(ledger, db, finding_id) == [("verifier", "needs-human")]


@pytest.mark.parametrize(
    "poc",
    [
        "cmd::python3 -c \"open('../sibling', 'w').write('x')\"",
        "cmd::cp secrets /etc/passwd",
        "cmd::python3 --config=../outside.cfg",
    ],
)
def test_every_escape_shape_is_refused(ledger, verify_finding, db, worktree, capsys, poc):
    finding_id = a_finding(ledger, db, poc=poc, title=f"escape shape {poc}")

    code = verify_finding.main(
        ["--db", str(db), "--finding-id", str(finding_id), "--worktree", str(worktree)]
    )

    assert code == 20
    assert out_json(capsys)["verdict"] == "needs-human"


def test_an_ordinary_relative_proof_is_not_read_as_an_escape(verify_finding):
    """The escape screen must not refuse the proofs the verifier exists to run."""
    assert verify_finding.refusal_reason("cmd::python3 -m pytest test/test_x.py") is None
    assert verify_finding.refusal_reason("cmd::python3 poc.py --mode=strict") is None


@pytest.mark.parametrize(
    "nodeid",
    [
        # A parametrised id: `token` then `[`, which IS a word boundary.
        "pytest::test/test_dashboard_auth.py::test_token[query-string]",
        # A module named for the thing under test.
        "pytest::test/token.py::test_rejects_a_query_string",
        # A directory named for it.
        "pytest::test/ssh/test_transport.py::test_refuses_an_unknown_host",
    ],
)
def test_a_pytest_nodeid_naming_a_screened_word_is_not_refused(verify_finding, nodeid):
    """A nodeid is a SELECTOR, not a program.

    Each id here really does contain a screened word as a WHOLE word -- which is
    what the word screen matches -- so word-screening nodeids refused exactly the
    tests this codebase's dominant finding class is written as, and bought
    nothing: what the selected test DOES was never visible to a string screen.
    The credential-PATH screen still applies to both shapes.
    """
    assert verify_finding.refusal_reason(nodeid) is None


def test_the_word_screen_still_applies_to_a_command_proof(verify_finding):
    """Narrowing the screen to ``cmd::`` must not disarm it there."""
    assert verify_finding.refusal_reason('cmd::python3 -c "print(token)"') is not None
    assert verify_finding.refusal_reason("cmd::ssh host true") is not None


def test_the_credential_path_screen_applies_to_a_nodeid_too(verify_finding):
    """No legitimate nodeid names a credential path, so both shapes keep it."""
    assert verify_finding.refusal_reason("pytest::test_x.py::test_reads_from_~/.aws")


def test_a_hostile_checkout_cannot_print_its_way_to_a_confirmation(
    ledger, verify_finding, db, worktree, capsys
):
    """The audited checkout is untrusted, so its STDOUT cannot be the verdict.

    A ``conftest.py`` in the target that prints pytest's own summary text and
    exits nonzero forged ``confirmed`` while the named test never ran. The
    verdict now comes from a JUnit report written outside the worktree and keyed
    to the requested nodeid, so this reaches a human instead.
    """
    (worktree / "conftest.py").write_text(
        "import sys\n\n\ndef pytest_sessionfinish(session, exitstatus):\n"
        "    print('1 failed, 0 passed in 0.01s')\n"
        "    sys.stdout.flush()\n"
        "    import os\n"
        "    os._exit(1)\n",
        encoding="utf-8",
    )
    (worktree / "test_poc.py").write_text(PASSING_TEST, encoding="utf-8")
    finding_id = a_finding(ledger, db, poc="pytest::test_poc.py::test_the_guard_refuses_it")

    code = verify_finding.main(
        ["--db", str(db), "--finding-id", str(finding_id), "--worktree", str(worktree)]
    )

    payload = out_json(capsys)
    assert code != 0
    assert payload["verdict"] != "confirmed"


def test_a_report_that_names_another_test_is_needs_human(verify_finding, tmp_path):
    """The report is keyed to the nodeid that was asked for."""
    report = tmp_path / "result.xml"
    report.write_text(
        '<testsuites><testsuite name="pytest" tests="1">'
        '<testcase classname="test_other" name="test_something_else">'
        "<failure>boom</failure></testcase></testsuite></testsuites>",
        encoding="utf-8",
    )

    verdict, reason = verify_finding.judge_report(report, "test_poc.py::test_the_real_one")

    assert verdict == "needs-human"
    assert "test_the_real_one" in reason


def test_a_missing_report_is_needs_human(verify_finding, tmp_path):
    """Fail closed: no report means the proof did not run to completion."""
    verdict, _ = verify_finding.judge_report(tmp_path / "absent.xml", "test_poc.py::test_x")

    assert verdict == "needs-human"


def test_a_pytest_selector_may_not_escape_the_worktree(verify_finding):
    """The path screen was wired to the cmd lane only, so a nodeid carried the same
    escape past it -- spelled as a selector instead of as an argument.

    pytest resolves a nodeid's file part against its working directory, which is the
    scratch checkout, so `..` there runs code the audited checkout does not contain.
    """
    for nodeid in (
        "pytest::../outside/test_poc.py::test_x",
        "pytest::../../etc/test_poc.py::test_x",
        "pytest::/abs/test_poc.py::test_x",
        "pytest::..\\outside\\test_poc.py::test_x",
    ):
        reason = verify_finding.refusal_reason(nodeid)
        assert reason is not None, nodeid
        assert "outside the scratch worktree" in reason, nodeid


def test_an_ordinary_nodeid_is_not_caught_by_the_escape_screen(verify_finding):
    """The screen must not cost the dominant real finding shape.

    Only the FILE part is screened, so a parametrised id carrying arbitrary text --
    including a screened word or a dotted value -- still runs.
    """
    for nodeid in (
        "pytest::test/test_security.py::test_echo_is_not_a_program",
        "pytest::test/test_security.py::test_token_auth[query]",
        "pytest::test/sub/dir/test_poc.py::test_x[a..b]",
        "pytest::test/test_poc.py::test_x[/abs/path]",
    ):
        assert verify_finding.refusal_reason(nodeid) is None, nodeid


def test_a_pytest_selector_that_names_only_a_file_is_not_runnable(verify_finding):
    """An unkeyed selector is refused as a SHAPE, before anything runs.

    A file-wide run produces a report keyed to no single test, so ANY failure in
    that file satisfies the finding. Refusing the shape is what makes that verdict
    unreachable rather than merely unlikely.
    """
    assert verify_finding.poc_argv("pytest::test_poc.py") is None
    assert verify_finding.poc_argv("pytest::test_poc.py::") is None
    assert verify_finding.poc_argv("pytest::test_poc.py::test_x") is not None


def test_a_finding_whose_proof_names_only_a_file_is_refused_at_filing(
    ledger, finding_entry, db, missing_json, capsys
):
    """Write-time validation runs the verifier's own parser, so an unkeyed
    selector never reaches the ledger at all."""
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
        "a whole file stands in for one test",
        "--path",
        "src/kiro_crew/security.py",
        "--poc",
        "pytest::test/test_security.py",
    )

    assert code == 2
    assert "pytest::" in capsys.readouterr().err


def test_an_unkeyed_selector_is_never_confirmed_by_a_report(verify_finding, tmp_path):
    """The reader refuses too: a failure belonging to some other test in the same
    file is not evidence for this finding."""
    report = tmp_path / "result.xml"
    report.write_text(
        '<testsuites><testsuite name="pytest" tests="1">'
        '<testcase classname="test_poc" name="test_something_unrelated">'
        "<failure>boom</failure></testcase></testsuite></testsuites>",
        encoding="utf-8",
    )

    verdict, reason = verify_finding.judge_report(report, "test_poc.py")

    assert verdict == "needs-human"
    assert "one test" in reason


def test_the_child_env_carries_what_a_windows_process_needs_to_start(
    verify_finding, tmp_path, monkeypatch
):
    """Withholding these does not harden the child, it stops it existing.

    Without ``SYSTEMROOT`` a Windows CPython dies seeding ``os.urandom`` during
    interpreter startup, so the proof never runs and every pytest finding reads
    ``needs-human``. ``PATHEXT`` and ``COMSPEC`` are how a bare program name
    resolves to an executable there.
    """
    monkeypatch.setenv("SYSTEMROOT", r"C:\Windows")
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT")
    monkeypatch.setenv("COMSPEC", r"C:\Windows\system32\cmd.exe")

    env = verify_finding.child_env(tmp_path)

    assert env["SYSTEMROOT"] == r"C:\Windows"
    assert env["PATHEXT"] == ".COM;.EXE;.BAT"
    assert env["COMSPEC"] == r"C:\Windows\system32\cmd.exe"


def test_a_name_the_host_does_not_define_is_not_invented(verify_finding, tmp_path, monkeypatch):
    """The passthrough is a filter, not a set of required keys, so a minimal host
    is unaffected rather than handed empty strings."""
    for name in ("SYSTEMROOT", "PATHEXT", "COMSPEC"):
        monkeypatch.delenv(name, raising=False)

    env = verify_finding.child_env(tmp_path)

    assert "SYSTEMROOT" not in env
    assert "PATHEXT" not in env
    assert "COMSPEC" not in env


def test_the_child_scratch_directory_is_the_worktree_in_every_spelling(verify_finding, tmp_path):
    """``tempfile`` reads ``TMPDIR`` on POSIX and ``TEMP``/``TMP`` on Windows, so
    pinning one spelling left a PoC's scratch files outside the throwaway
    checkout on the other platform."""
    env = verify_finding.child_env(tmp_path)

    assert env["TMPDIR"] == str(tmp_path)
    assert env["TEMP"] == str(tmp_path)
    assert env["TMP"] == str(tmp_path)


def test_filing_records_the_auditor_verdict(ledger, finding_entry, db, missing_json, capsys):
    """Filing IS the auditor's claim, and the retrospective reads it.

    ``findings.auditor_verdict`` is a materialised view of the verdict rows, so
    with no row written the column stays NULL and an auditor claim is
    indistinguishable from an absent one -- which is the false-positive signal the
    procedure obliges the conductor to report.
    """
    seed_roe(ledger, db)

    code = file_finding(finding_entry, db, missing_json)

    finding_id = out_json(capsys)["finding_id"]
    assert code == 0
    assert verdict_rows(ledger, db, finding_id) == [("auditor", "confirmed")]
    conn = ledger.connect(db)
    try:
        row = conn.execute(
            "SELECT auditor_verdict FROM findings WHERE id = ?", (finding_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row["auditor_verdict"] == "confirmed"


def test_a_duplicate_filing_does_not_append_a_second_auditor_verdict(
    ledger, finding_entry, db, missing_json, capsys
):
    """A re-report returns the original id, and the earlier row's verdict history
    is the record rather than something a second filing appends to."""
    seed_roe(ledger, db)
    assert file_finding(finding_entry, db, missing_json) == 0
    finding_id = out_json(capsys)["finding_id"]

    assert file_finding(finding_entry, db, missing_json) == 3

    assert verdict_rows(ledger, db, finding_id) == [("auditor", "confirmed")]


def test_a_filing_repairs_a_finding_that_carries_no_auditor_claim(
    ledger, finding_entry, db, missing_json, capsys
):
    """``add_finding`` commits on its own, so a crash before the verdict write is
    possible -- and keyed on ``created`` the retry would skip the repair forever.

    This drives that state directly: a finding inserted through the ledger, with no
    verdict row, is what the process would leave behind. Re-filing it must complete
    the record rather than report a duplicate and walk away.
    """
    seed_roe(ledger, db)
    finding_id = a_finding(
        ledger,
        db,
        poc="pytest::test/test_security.py::test_echo_is_not_a_program",
        title="echo text is classified as an executed program",
    )
    assert verdict_rows(ledger, db, finding_id) == []

    code = file_finding(finding_entry, db, missing_json)

    assert code == 3
    assert out_json(capsys)["finding_id"] == finding_id
    assert verdict_rows(ledger, db, finding_id) == [("auditor", "confirmed")]


def test_a_filing_does_not_overwrite_a_verdict_someone_else_recorded(
    ledger, finding_entry, db, missing_json, capsys
):
    """A human ruling outranks a re-report, so the repair is keyed on the auditor
    row's absence and never on the folded column."""
    seed_roe(ledger, db)
    assert file_finding(finding_entry, db, missing_json) == 0
    finding_id = out_json(capsys)["finding_id"]
    conn = ledger.connect(db)
    try:
        ledger.record_verdict(
            conn, finding_id=finding_id, role="human", verdict="rejected", reason="not a defect"
        )
    finally:
        conn.close()

    assert file_finding(finding_entry, db, missing_json) == 3

    assert verdict_rows(ledger, db, finding_id) == [
        ("auditor", "confirmed"),
        ("human", "rejected"),
    ]


def test_an_unrelated_failure_beside_the_requested_test_does_not_confirm(verify_finding, tmp_path):
    """Naming the test is not enough -- the verdict is that test's OWN result.

    A run reports tests beside the cited one (a parametrised sibling, whatever else
    the selector's file collected), and folding those in let an unrelated failure
    confirm this finding.
    """
    report = tmp_path / "result.xml"
    report.write_text(
        '<testsuites><testsuite name="pytest" tests="2">'
        '<testcase classname="test_poc" name="test_the_real_one"/>'
        '<testcase classname="test_poc" name="test_a_sibling">'
        "<failure>boom</failure></testcase></testsuite></testsuites>",
        encoding="utf-8",
    )

    verdict, reason = verify_finding.judge_report(report, "test_poc.py::test_the_real_one")

    assert verdict == "rejected"
    assert "test_the_real_one" in reason


def test_the_requested_test_failing_still_confirms(verify_finding, tmp_path):
    """The narrowing must not cost the verdict it exists to produce."""
    report = tmp_path / "result.xml"
    report.write_text(
        '<testsuites><testsuite name="pytest" tests="2">'
        '<testcase classname="test_poc" name="test_a_sibling"/>'
        '<testcase classname="test_poc" name="test_the_real_one">'
        "<failure>boom</failure></testcase></testsuite></testsuites>",
        encoding="utf-8",
    )

    verdict, _ = verify_finding.judge_report(report, "test_poc.py::test_the_real_one")

    assert verdict == "confirmed"


def test_the_verifier_refuses_the_checkout_it_is_running_from(verify_finding, tmp_path):
    """The checkable half of "the sandbox must be disposable".

    Provenance in general is not checkable from here -- the caller picks the path and
    so controls what is inside it -- but "you pointed me at my own source tree" is
    decidable without trusting the caller, and it is the case an operator reaches by
    accident.
    """
    repo_root = Path(verify_finding.__file__ or "").resolve().parents[5]

    assert verify_finding.is_own_checkout(repo_root)
    assert not verify_finding.is_own_checkout(tmp_path)


def test_a_proof_is_refused_when_the_sandbox_is_the_verifiers_own_repository(verify_finding):
    """Wired into the verdict path, not just available as a helper."""
    repo_root = Path(verify_finding.__file__ or "").resolve().parents[5]

    verdict, reason = verify_finding.decide(
        {"poc": "pytest::test/test_security.py::test_echo_is_not_a_program"}, repo_root, 5
    )

    assert verdict == "needs-human"
    assert "running from" in reason


def test_a_missing_command_records_needs_human_instead_of_crashing(
    ledger, verify_finding, db, worktree, capsys
):
    """A mistyped or hallucinated program name is the verifier's ordinary input.

    ``subprocess.run`` raises ``FileNotFoundError`` for it rather than returning
    a status, and letting that propagate killed the process outside the
    documented ``{0,10,20,2}`` contract with no verdict recorded at all.
    """
    finding_id = a_finding(ledger, db, poc="cmd::definitely-no-such-command-anywhere --flag")

    code = verify_finding.main(
        ["--db", str(db), "--finding-id", str(finding_id), "--worktree", str(worktree)]
    )

    payload = out_json(capsys)
    assert code == 20
    assert payload["verdict"] == "needs-human"
    assert "could not be started" in payload["reason"]
    assert verdict_rows(ledger, db, finding_id) == [("verifier", "needs-human")]


@pytest.mark.parametrize(
    "returncode,verdict",
    [
        (0, "rejected"),
        (1, "needs-human"),
        (2, "confirmed"),
        (3, "confirmed"),
        (125, "confirmed"),
        (126, "needs-human"),
        (127, "needs-human"),
        (137, "needs-human"),
        (139, "needs-human"),
        (255, "needs-human"),
        (3221225477, "needs-human"),
        (-9, "needs-human"),
    ],
)
def test_a_command_that_never_ran_does_not_confirm(verify_finding, returncode, verdict):
    """``cmd::`` may not read its verdict off "nonzero" alone.

    Nonzero covers "the defect reproduced" AND "the command never ran" -- a
    missing interpreter, a wrong cwd, a death by signal. Confirming on the second
    is the errored-proof false positive the pytest lane already rejects, so the
    two lanes now hold the same bar. Exit 1 is in the second set: it is what a
    model-authored PoC exits when it dies on a hallucinated import, and nothing
    in the status separates that from a reproduction.
    """
    assert verify_finding.judge_cmd(returncode)[0] == verdict


def test_a_command_proof_that_crashes_is_not_confirmed(
    ledger, verify_finding, db, worktree, capsys
):
    """End to end: a PoC dying on a missing import must not persist `confirmed`."""
    finding_id = a_finding(ledger, db, poc='cmd::python3 -c "import no_such_module_anywhere_xyz"')

    code = verify_finding.main(
        ["--db", str(db), "--finding-id", str(finding_id), "--worktree", str(worktree)]
    )

    payload = out_json(capsys)
    assert code == 20
    assert payload["verdict"] == "needs-human"
    assert "exited 1" in payload["reason"]
    assert verdict_rows(ledger, db, finding_id) == [("verifier", "needs-human")]


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="SIGKILL is POSIX-only. Windows reports no negative returncode, so the"
    " signal-death shape this drives end to end cannot arise there; judge_cmd's"
    " mapping for it is covered directly by the unit test above.",
)
def test_a_signal_killed_command_records_needs_human(ledger, verify_finding, db, worktree, capsys):
    """The launch-failure mapping, driven end to end rather than unit-only."""
    finding_id = a_finding(
        ledger, db, poc='cmd::python3 -c "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"'
    )

    code = verify_finding.main(
        ["--db", str(db), "--finding-id", str(finding_id), "--worktree", str(worktree)]
    )

    payload = out_json(capsys)
    assert code == 20
    assert payload["verdict"] == "needs-human"
    assert verdict_rows(ledger, db, finding_id) == [("verifier", "needs-human")]


def test_the_child_path_fallback_names_no_posix_only_directory(
    verify_finding, tmp_path, monkeypatch
):
    """A hardcoded ``/usr/bin:/bin`` fallback names nothing on Windows."""
    monkeypatch.delenv("PATH", raising=False)

    assert verify_finding.child_env(tmp_path)["PATH"] == os.defpath


def test_the_ledger_is_loaded_once_per_invocation(finding_entry, scope_check, verify_finding):
    """Three scripts, one ledger module -- not one load per sibling.

    ``finding_entry`` loads both siblings, and a module-level ledger load in each
    executed ``ledger.py`` three times for one filing. The siblings load it
    lazily and ``finding_entry`` takes ``scope_check``'s instance.
    """
    assert finding_entry._ledger is finding_entry._scope_check.ledger()
    assert scope_check.ledger() is scope_check.ledger()
    assert verify_finding.ledger() is verify_finding.ledger()


def test_the_proof_shapes_are_declared_once(finding_entry, verify_finding):
    """Write-time validation and the verifier cannot drift apart.

    ``finding_entry`` validates a PoC with ``verify_finding``'s own parser, so a
    third proof shape cannot leave filing rejecting what the verifier runs.
    """
    source = Path(finding_entry.__file__ or "").read_text(encoding="utf-8")
    assert "POC_PREFIXES = (" not in source
    assert finding_entry.validate_poc("pytest::test_x.py::test_y") is None
    assert finding_entry.validate_poc("cmd::python3 -c pass") is None
    assert finding_entry.validate_poc("prose about the bug") is not None
    assert verify_finding.POC_PREFIXES == ("pytest::", "cmd::")


def test_an_output_flood_does_not_buffer_into_the_verifier(
    ledger, verify_finding, db, worktree, capsys
):
    """The PoC's output is discarded, so a printing PoC cannot exhaust memory.

    No verdict reads the child's output -- the pytest lane reads the report file
    and the command lane reads the status -- but capturing it through a pipe
    buffered the whole stream in this process, and ``--timeout`` bounds wall time,
    never bytes. This PoC prints ~64MB and must still reach a verdict.
    """
    program = (
        "import sys\n"
        "block = 'x' * 65536\n"
        "for _ in range(1000):\n"
        "    sys.stdout.write(block)\n"
        "raise SystemExit(7)\n"
    )
    (worktree / "flood.py").write_text(program, encoding="utf-8")
    finding_id = a_finding(ledger, db, poc="cmd::python3 flood.py")

    code = verify_finding.main(
        ["--db", str(db), "--finding-id", str(finding_id), "--worktree", str(worktree)]
    )

    payload = out_json(capsys)
    assert code == 0
    assert payload["verdict"] == "confirmed"
    assert verdict_rows(ledger, db, finding_id) == [("verifier", "confirmed")]


def test_run_poc_returns_a_bare_status_and_captures_nothing(verify_finding):
    """Asserted on the source: a pipe re-added is the whole defect coming back."""
    source = Path(verify_finding.__file__ or "").read_text(encoding="utf-8")
    assert "stdout=subprocess.DEVNULL" in source
    assert "stderr=subprocess.DEVNULL" in source
    assert "subprocess.PIPE" not in source


def test_a_test_named_like_an_outcome_element_cannot_forge_one(verify_finding, tmp_path):
    """The test file is in the untrusted checkout, so a test NAME is attacker text.

    XML escapes a ``<`` inside an attribute as ``&lt;``, so a raw ``<failure`` can
    only be markup -- which is why the reader matches the tag and not the word.
    """
    report = tmp_path / "result.xml"
    report.write_text(
        '<testsuites><testsuite><testcase classname="c" name="test_x&lt;failure/&gt;"'
        ' time="0.0"></testcase></testsuite></testsuites>',
        encoding="utf-8",
    )

    verdict, _ = verify_finding.judge_report(report, "test_poc.py::test_x&lt;failure/&gt;")

    assert verdict == "rejected"


def test_the_case_name_is_not_read_off_classname(verify_finding, tmp_path):
    """``name="`` is a SUBSTRING of ``classname="``.

    An unanchored search returned the module name for every case, so the
    nodeid check compared the wrong string and every real run came back
    ``needs-human``. Caught by the suite before it shipped.
    """
    chunk = ' classname="test_poc" name="test_boom" time="0.001"><failure>E</failure></testcase>'

    assert verify_finding.case_name(chunk) == "test_boom"


def test_a_name_inside_a_nested_element_is_not_the_case_name(verify_finding):
    """Only the start tag is searched, so a nested attribute cannot supply one."""
    chunk = ' classname="c" time="0.0"><failure name="not-the-test">E</failure></testcase>'

    assert verify_finding.case_name(chunk) == "c" or verify_finding.case_name(chunk) is None


def test_an_element_name_must_be_a_whole_tag(verify_finding, tmp_path):
    """``<failures>`` is not ``<failure``."""
    report = tmp_path / "result.xml"
    report.write_text(
        '<testsuites><testsuite><testcase classname="c" name="test_x">'
        "<failures>0</failures></testcase></testsuite></testsuites>",
        encoding="utf-8",
    )

    verdict, _ = verify_finding.judge_report(report, "test_poc.py::test_x")

    assert verdict == "rejected"


def test_verifying_against_a_missing_ledger_creates_nothing(
    verify_finding, tmp_path, worktree, capsys
):
    """Verifying never CREATES a ledger.

    A finding must be filed to be verified, and filing is what creates the
    database -- so a missing one is a mistyped ``--db``. Creating it there left an
    empty ledger behind, which ``scope_check`` then reads as a ledger with zero
    rules rather than as no ledger at all.
    """
    absent = tmp_path / "nested" / "findings.db"

    code = verify_finding.main(
        ["--db", str(absent), "--finding-id", "1", "--worktree", str(worktree)]
    )

    captured = capsys.readouterr()
    assert code == 2
    assert not absent.exists()
    assert not absent.parent.exists()
    assert "no ledger at" in captured.err


def test_a_ledger_that_is_not_a_database_exits_two(verify_finding, tmp_path, worktree, capsys):
    """A file that is not a ledger is an input error, not a verdict."""
    bogus = tmp_path / "findings.db"
    bogus.write_bytes(b"not a sqlite database")

    code = verify_finding.main(
        ["--db", str(bogus), "--finding-id", "1", "--worktree", str(worktree)]
    )

    assert code == 2
    assert "cannot read findings" in capsys.readouterr().err


def test_a_json_file_alone_still_carries_its_own_round_id(
    ledger, finding_entry, db, missing_json, tmp_path, capsys
):
    """Refusing the COMBINATION must not refuse the file's own round id."""
    seed_roe(ledger, db)
    payload = tmp_path / "finding.json"
    payload.write_text(
        json.dumps(
            {
                "surface": "webhooks.ingest",
                "severity": "High",
                "title": "unbounded frame accepted",
                "paths": ["src/kiro_crew/webhooks.py"],
                "poc": "cmd::python3 -c pass",
                "round_id": "kirocrew-dogfood-round-1",
            }
        ),
        encoding="utf-8",
    )

    code = finding_entry.main(
        ["--db", str(db), "--roe-json", str(missing_json), "--json-file", str(payload)]
    )
    filed = out_json(capsys)

    conn = ledger.connect(db)
    try:
        row = conn.execute(
            "SELECT round_id FROM findings WHERE id = ?", (filed["finding_id"],)
        ).fetchone()
    finally:
        conn.close()

    assert code == 0
    assert row["round_id"] == "kirocrew-dogfood-round-1"
