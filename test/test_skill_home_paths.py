"""Guard: skill guidance must point at the CURRENT data home, not the legacy one.

The data home moved from a top-level ``~/.kirocrew`` to ``~/.kiro/crew`` (nested
under kiro-cli's ``~/.kiro``). Skill files were missed by that move, so agents
following them ran commands against a path that no longer exists -- and the
scripts some skills execute (``prepare-pr``'s ``$SKILL_DIR/scripts/*.py``,
``self-nudge-loop``'s ``.local_secret`` read, ``feature-demo-recording``'s venv)
failed outright.

These tests keep the fix from silently regressing: a skill that reintroduces the
legacy home fails here rather than at an agent's runtime.

Deliberately NOT covered: production modules (e.g.
``apps/builtins/file_explorer/server.py``) reference the legacy home on purpose,
to migrate it and to keep detecting it. Only skill files are asserted.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

SKILL_SUFFIXES = {".md", ".sh", ".py"}

# Top-level siblings that did NOT move and must stay spelled the legacy way:
#   ~/.kirocrew-pods       -> KIROCREW_POD_ROOT default (pod/config.py)
#   ~/.kirocrew-dev        -> per-worktree dev data dir (dev-backend.sh)
#   ~/.kirocrew.breadcrumb -> recovery pointer written beside the home
# The negative lookahead below is what distinguishes them from the old home.
LEGACY_HOME = re.compile(r"\.kirocrew(?![-.])")

CURRENT_HOME_POSIX = "~/.kiro/crew"

# A skill may legitimately *mention* the legacy home when it is documenting the
# migration itself ("legacy installs auto-migrate from ~/.kirocrew on first
# launch") rather than telling an agent to use that path. Those lines are
# accurate and worth keeping, so a line carrying one of these markers is
# exempt. A line without a marker is an instruction, and an instruction
# pointing at the legacy home is the bug this guard exists to catch.
MIGRATION_CONTEXT = ("legacy", "migrat", "pre-move", "archived")


def _is_migration_prose(line: str) -> bool:
    lowered = line.lower()
    return any(marker in lowered for marker in MIGRATION_CONTEXT)


def _skill_files() -> list[Path]:
    """Every skill-authored file: top-level skills/ plus app-bundled skills/."""
    roots = [REPO_ROOT / "skills"]
    roots += sorted((REPO_ROOT / "src" / "kiro_crew" / "apps" / "builtins").glob("*/skills"))
    files: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        files += [p for p in sorted(root.rglob("*")) if p.is_file() and p.suffix in SKILL_SUFFIXES]
    return files


def test_skill_files_exist() -> None:
    """Confidence check: the guard is actually scanning something."""
    files = _skill_files()
    assert len(files) > 10, f"expected to find skill files, got {len(files)}"


def test_no_skill_references_the_legacy_data_home() -> None:
    """No skill file may *instruct* against the pre-move ``~/.kirocrew`` home."""
    offenders: list[str] = []
    for path in _skill_files():
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if LEGACY_HOME.search(line) and not _is_migration_prose(line):
                rel = path.relative_to(REPO_ROOT)
                offenders.append(f"{rel}:{lineno}: {line.strip()}")

    assert not offenders, (
        "Skill files must reference the current data home "
        f"({CURRENT_HOME_POSIX}), not the legacy ~/.kirocrew. If a line is "
        "documenting the migration rather than instructing, say so explicitly "
        "(mention 'legacy' or 'migrates'):\n  " + "\n  ".join(offenders)
    )


def test_sibling_paths_that_did_not_move_are_preserved() -> None:
    """The fix must not have rewritten ``.kirocrew-pods`` / ``.kirocrew-dev``.

    These are separate top-level names, not the data home, so the sweep that
    fixed the home must have left them alone.
    """
    joined = "\n".join(p.read_text(encoding="utf-8") for p in _skill_files())
    assert ".kirocrew-pods" in joined, "pod-root references should be unchanged"
    assert ".kiro/crew-pods" not in joined, "pod root was wrongly rewritten"
    assert ".kiro/crew-dev" not in joined, "worktree dev dir was wrongly rewritten"


def test_windows_examples_use_windows_separators() -> None:
    """A Windows path example must not end up with a mixed ``\\.kiro/crew\\`` form."""
    offenders: list[str] = []
    for path in _skill_files():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if "\\.kiro/crew" in line:
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}")
    assert not offenders, "Windows examples must use \\.kiro\\crew:\n  " + "\n  ".join(offenders)


def test_migration_exemption_is_narrow() -> None:
    """The exemption must not become a loophole for real instruction paths."""
    # An instruction line is still caught...
    assert not _is_migration_prose("SECRET=$(cat ~/.kirocrew/.local_secret)")
    assert LEGACY_HOME.search("SECRET=$(cat ~/.kirocrew/.local_secret)")
    # ...while prose about the move is allowed.
    assert _is_migration_prose("legacy installs auto-migrate from ~/.kirocrew")
