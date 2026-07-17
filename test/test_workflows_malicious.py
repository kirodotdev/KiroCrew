"""GATE B9 — the adversarial escape corpus must be 100% rejected.

Loads every ``*.py`` under ``tests/workflows/malicious/`` (hostile workflow
scripts — data fixtures, not collected as tests since pytest's ``testpaths`` is
``test``) and asserts ``validate()`` statically rejects each one. New escape ideas
are added by dropping a file in that directory — this test picks it up
automatically (the flywheel).

See ``docs/dynamic-workflows/GATES.md`` (B9).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew.workflows.validate import validate

_CORPUS_DIR = Path(__file__).resolve().parent.parent / "tests" / "workflows" / "malicious"


def _corpus_files() -> list[Path]:
    return sorted(_CORPUS_DIR.glob("*.py"))


def test_corpus_dir_exists_and_is_populated() -> None:
    assert _CORPUS_DIR.is_dir(), f"missing adversarial corpus dir: {_CORPUS_DIR}"
    assert _corpus_files(), "B9 corpus is empty — at least one hostile script is required"


@pytest.mark.parametrize("script_path", _corpus_files(), ids=lambda p: p.name)
def test_every_malicious_script_is_rejected(script_path: Path) -> None:
    source = script_path.read_text(encoding="utf-8")
    result = validate(source)
    assert result.ok is False, (
        f"SANDBOX ESCAPE: {script_path.name} was NOT rejected by validate() — "
        "this is a B9 regression. Tighten the validator; do not weaken this gate."
    )
    assert result.errors, f"{script_path.name}: rejected but produced no error message"
