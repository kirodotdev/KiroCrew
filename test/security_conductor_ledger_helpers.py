"""Fixtures and builders shared by the security conductor's findings-ledger tests.

``test_security_conductor_ledger.py`` and its ``_cli``, ``_golden_paths`` and ``_rebuilds``
siblings import these by name: the ledger script loaded as a module (``mod``), a per-test
database path (``db``), the ``main`` driver and the JSON line it prints, the finding and lesson
builders, and the script's source with implicit string concatenation collapsed for the
structural scans.
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


@pytest.fixture(name="mod")
def mod_fixture():
    return load_skill_script("security_conductor_ledger", SCRIPT)


@pytest.fixture(name="db")
def db_fixture(tmp_path: Path) -> Path:
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
