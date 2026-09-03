"""The frontend eslint warning ceiling has ONE numeric source, and it is zero.

CI runs `eslint src/ --max-warnings <n>`, so a warning count at or below `<n>` is
green and anything above is red. The tree carries no warnings, which makes `<n>`
zero the only ceiling that needs no bookkeeping. Two things would quietly undo
that, and neither shows up as a failing check of its own:

* **Slack.** A ceiling above the measured count is a budget new warnings land
  inside, silently, until it is exhausted -- and a warning admitted that way is
  indistinguishable from the rest. With the tree at zero, any non-zero ceiling
  IS slack, so that is pinned here directly rather than left to prose in the
  workflow asking the next author not to lift it.
* **Transcription.** Prose that repeats a *drifting* number goes stale the first
  time anyone moves the ceiling, and then documents a gate that no longer
  exists; a stale ceiling in a doc is also what makes the next burn-down look
  already done. That is pinned too, but only while the ceiling can drift --
  see `test_the_ceiling_is_not_transcribed_into_prose`.
* **A second gate.** The number copied into ANOTHER workflow is not prose that
  goes stale, it is a ceiling of its own that keeps enforcing the old value: on
  the first burn-down it reports green on a tree the real gate reds. Pinned
  unconditionally, because unlike a doc there is no value at which a second copy
  is harmless -- see `test_no_other_workflow_transcribes_the_ceiling`.
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


def test_the_ceiling_is_zero() -> None:
    """The tree is at zero warnings, so any non-zero ceiling is pure slack.

    The workflow comment asks the next author not to lift it; this is the half
    that does not depend on the comment being read. A ceiling raised to admit one
    warning re-creates the drifting baseline the hard zero replaced, and nothing
    else in CI reports that -- eslint exits 0 either way.
    """
    ceiling = _CEILING.search(_ci_text())
    assert ceiling is not None

    assert ceiling.group(1) == "0", (
        f"ci.yml's eslint ceiling is {ceiling.group(1)}, not 0. The frontend tree "
        "carries no warnings, so anything above zero is a budget new warnings land "
        "inside unseen. Fix the warning, or suppress the one line it names with "
        "`// eslint-disable-next-line <rule> -- <why the code is correct>`, which is "
        "reviewable in the diff where a lifted ceiling is not"
    )


def test_no_other_workflow_transcribes_the_ceiling() -> None:
    """One workflow declares the ceiling; the rest read it out of that one.

    A copy in a second workflow is a second *enforced* ceiling, so it cannot be
    excused the way a doc's stale number can: on the first burn-down the copy
    still admits the old budget, and the job carrying it reports green on a tree
    `ci.yml` reds. That is a false all-clear on `main`, which is exactly what a
    lane auditing `main`'s ratchets exists to prevent. Read the value instead --
    `main-ratchet-audit.yml` greps it out of `ci.yml` at run time, the same way
    the prepare-pr profile does.

    Checked at every value, including zero: the early return in
    `test_the_ceiling_is_not_transcribed_into_prose` turns on a doc quoting the
    gate's real invocation being accurate prose, and a second gate is never that.
    """
    offenders: list[str] = []
    for path in sorted((_REPO_ROOT / ".github" / "workflows").glob("*.yml")):
        if path == _CI:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if _CEILING.search(line):
                offenders.append(f"{path.relative_to(_REPO_ROOT)}:{lineno}")

    assert offenders == [], (
        "these workflow lines declare an eslint ceiling of their own instead of "
        f"reading ci.yml's: {offenders}. A second copy keeps enforcing the old "
        "value after a burn-down, so the job holding it goes green on a tree the "
        "real gate fails. Extract the value at run time instead: "
        "grep -oE 'npx eslint src/ --max-warnings [0-9]+' .github/workflows/ci.yml"
    )


def test_the_ceiling_is_not_transcribed_into_prose() -> None:
    """Docs describe the ratchet; they must not restate a value that can drift.

    A number copied into prose is correct exactly until the ceiling moves, and
    the reader who finds the stale copy concludes the burn-down already happened.
    Refer to the gate instead, so there is one place to change.

    This applies only while the ceiling is a *stored count*. At zero there is
    nothing to go stale -- `test_the_ceiling_is_zero` is what holds the value, so
    the ceiling cannot move without that test failing first -- and a doc quoting
    the gate's real invocation (`--max-warnings 0`) is accurate prose, not a
    transcription. Scanning for the literal `0` would ban exactly that, and would
    also fire on any unrelated digit in a `max-warnings` sentence.
    """
    ceiling = _CEILING.search(_ci_text())
    assert ceiling is not None
    value = ceiling.group(1)
    if value == "0":
        return

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
