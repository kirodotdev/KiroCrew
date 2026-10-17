"""The prose in ``golden-paths.json`` must agree with the code that reads the file.

The ``_about`` header tells a fixer how a ``shell`` row is checked, and each
row's ``reason`` says whether the row can be checked yet. Both are prose, so
nothing fails when the code moves. These tests tie the prose to the code.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = REPO_ROOT / "src" / "kiro_crew" / "builtin_skills" / "security-conductor"
CORPUS = SKILL_DIR / "golden-paths.json"
VERIFY_FIX = SKILL_DIR / "scripts" / "verify_fix.py"

_NUMBER_WORDS = {"two": 2, "three": 3, "four": 4, "five": 5}


def _verify_fix_tier_names() -> list[str]:
    """The tier names in ``verify_fix.TIERS``, read from source without importing it."""
    tree = ast.parse(VERIFY_FIX.read_text(encoding="utf-8"))
    for node in tree.body:
        target = getattr(node, "target", None)
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(target, ast.Name)
            and target.id == "TIERS"
        ):
            value = ast.literal_eval(node.value)
            return [name for name, _ in value]
    raise AssertionError("verify_fix.py has no module-level TIERS")


def _about_text() -> str:
    data = json.loads(CORPUS.read_text(encoding="utf-8"))
    return " ".join(data["_about"])


def test_about_counts_the_shell_checks_verify_fix_runs() -> None:
    about = _about_text()
    match = re.search(r"all (\w+) checks it applies to a shell command", about)
    assert match, "the _about header no longer states how many checks a shell row faces"
    stated = _NUMBER_WORDS.get(match.group(1))
    assert stated == len(_verify_fix_tier_names()), (
        f"_about says {match.group(1)} checks; verify_fix.TIERS has "
        f"{len(_verify_fix_tier_names())}"
    )


def test_about_names_no_path_fence_tier() -> None:
    assert "path" not in " ".join(_verify_fix_tier_names())
    assert "(path fence," not in _about_text()


def test_no_row_waits_on_a_node_that_already_exists() -> None:
    """A row that says it 'becomes checkable when that PR lands' must name a missing node."""
    rows = json.loads(CORPUS.read_text(encoding="utf-8"))["golden_paths"]
    stale = []
    for row in rows:
        if row.get("kind") != "test" or "becomes checkable when" not in row.get("reason", ""):
            continue
        path, _, name = row["command_or_flow"].partition("::")
        test_file = REPO_ROOT / path
        if test_file.is_file() and re.search(
            rf"^def {re.escape(name)}\(", test_file.read_text(encoding="utf-8"), re.M
        ):
            stale.append(row["command_or_flow"])
    assert not stale, f"rows still wait on nodes that exist: {stale}"
