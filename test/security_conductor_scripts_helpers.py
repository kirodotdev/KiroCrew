"""Fixtures and builders the security conductor script test modules share.

The ``ledger`` / ``scope_check`` / ``finding_entry`` script modules, a scratch
``db`` path and an absent ``missing_json`` rules-of-engagement export (fixtures,
declared ``@pytest.fixture(name=...)`` and imported under their function names),
the rules-of-engagement seeding, and the finding builders that file one finding
through ``finding_entry``. ``test_security_conductor_scripts.py`` holds the
``scope_check`` and ``finding_entry`` sections, and
``test_security_conductor_scripts_verify_finding.py`` the ``verify_finding`` section.
"""

from __future__ import annotations

import json
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


@pytest.fixture(name="ledger")
def ledger_fixture():
    return load_skill_script("security_conductor_ledger_for_scripts", SCRIPTS / "ledger.py")


@pytest.fixture(name="scope_check")
def scope_check_fixture():
    return load_skill_script("security_conductor_scope_check", SCRIPTS / "scope_check.py")


@pytest.fixture(name="finding_entry")
def finding_entry_fixture():
    return load_skill_script("security_conductor_finding_entry", SCRIPTS / "finding_entry.py")


@pytest.fixture(name="db")
def db_fixture(tmp_path: Path) -> Path:
    return tmp_path / "findings.db"


@pytest.fixture(name="missing_json")
def missing_json_fixture(tmp_path: Path) -> Path:
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
