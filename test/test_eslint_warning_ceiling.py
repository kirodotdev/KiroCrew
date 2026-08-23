"""The frontend eslint warning ceiling has ONE numeric source.

The ceiling is a ratchet: CI runs `eslint src/ --max-warnings <n>`, so a warning
count at or below `<n>` is green and anything above is red. Two things make that
ratchet stop ratcheting, and neither shows up as a failing check:

* **Slack.** A ceiling above the measured count is a budget new warnings land
  inside, silently, until it is exhausted. Only running eslint can measure that,
  so it is not pinned here -- the gate itself is that test.
* **Transcription.** Prose that repeats the number goes stale the first time
  anyone burns the ceiling down, and then documents a gate that no longer
  exists. That is what these tests pin, because it is cheap to pin and because a
  stale ceiling in a doc is what makes the next burn-down look already done.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CI = _REPO_ROOT / ".github" / "workflows" / "ci.yml"

#: Trees whose prose must not carry a copy of the ceiling.
_PROSE = (
    _REPO_ROOT / "docs",
    _REPO_ROOT / "website" / "docs",
    _REPO_ROOT / "AGENTS.md",
    _REPO_ROOT / "website" / "AGENTS.md",
)

_CEILING = re.compile(r"--max-warnings\s+(\d+)")


def _ci_text() -> str:
    return _CI.read_text(encoding="utf-8")


def test_the_lint_gate_declares_a_ceiling() -> None:
    """Without one, `eslint` exits 0 on any number of warnings."""
    ceilings = _CEILING.findall(_ci_text())

    assert ceilings, (
        "ci.yml's Lint step no longer passes --max-warnings, so eslint reports "
        "warnings and exits 0 -- the ratchet is gone entirely"
    )
    assert len(ceilings) == 1, (
        f"ci.yml declares {len(ceilings)} eslint ceilings ({ceilings}); keep one, "
        "or a burn-down has to find them all and will miss one"
    )


def test_the_ceiling_is_not_transcribed_into_prose() -> None:
    """Docs describe the ratchet; they must not restate its current value.

    A number copied into prose is correct exactly until the ceiling moves, and
    the reader who finds the stale copy concludes the burn-down already happened.
    Refer to the gate instead, so there is one place to change.
    """
    ceiling = _CEILING.search(_ci_text())
    assert ceiling is not None
    value = ceiling.group(1)

    offenders: list[str] = []
    for root in _PROSE:
        files = sorted(root.rglob("*.md")) if root.is_dir() else [root]
        for path in files:
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if "max-warnings" in line and value in line:
                    offenders.append(f"{path.relative_to(_REPO_ROOT)}:{lineno}")

    assert offenders == [], (
        "these lines transcribe the eslint ceiling's current value "
        f"({value}) instead of referring to the gate: {offenders}. "
        "The value belongs only in .github/workflows/ci.yml, so burning the "
        "ratchet down is a one-line change that cannot leave a stale copy behind."
    )
