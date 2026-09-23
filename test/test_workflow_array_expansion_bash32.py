"""Every array expansion in a workflow script must survive bash 3.2.

Bash treats an EMPTY array's expansion as an unset variable before 4.4. Under
``set -u``::

    a=(); for x in "${a[@]}"; do :; done   # bash 3.2: a[@]: unbound variable
    a=(); echo "${#a[@]}"                  # fine in every version -- a length

macOS ships bash 3.2.57 as ``/bin/bash`` and that is the ``bash`` on a GitHub
macOS runner's PATH. That matters here because these workflow scripts are not
only run by Actions on Linux: a dozen tests EXTRACT a ``run:`` block and execute
it locally (test_pr_readiness_evaluate.py, test_fork_pr_description_workflow.py,
test_release_macos_fail_closed.py, ...), so the macOS Platform Tests run this
shell under 3.2. One such expansion is enough to lose a step: readiness'
evaluate step empties ``workflow_specs`` on a transport failure precisely so its
lane loop runs over no specs, and on 3.2 that loop aborts the step instead --
turning the deliberately non-terminal "could not be evaluated" verdict into the
red check it exists to prevent.

A count guard is not what this pin asks for, because a guard and its expansion
drift: the array that cost a nightly WAS guarded, and then emptied again after
the guard. The asked-for property is local to the expansion and needs no
reasoning about reachability::

    ${a[@]+"${a[@]}"}

That is a no-op on bash >= 4.4, so nothing about how Actions runs these scripts
changes. It is only ever the difference between zero words and a fatal error.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"

# Only the three checks at the end of this file EXECUTE bash. The sweep does
# not, and must keep running on every platform -- it is the pin, and the
# property it reads is in the workflow text, not in a shell. So the guard goes
# on those three rather than on the module.
#
# A bare "bash" on Windows resolves off PATH to System32\bash.exe, the WSL
# launcher, which exits non-zero when no distribution is installed -- the reason
# conftest's _find_posix_test_shell refuses to treat it as a shell. conftest's
# posix_test_shell fixture is still not the seam for these: it resolves `sh`, and
# what they assert is a behaviour that differs BETWEEN BASH VERSIONS, so argv[0]
# has to be a bash. Same predicate as test_pr_readiness_evaluate.py's.
needs_bash = pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None,
    reason="executes bash; needs a POSIX bash on PATH",
)

# The forms that already tolerate an empty array, retired from the text before
# the sweep looks for the ones that do not. `_FULL` first and by itself: it is
# the shape this pin asks for, and its OWN inner `${a[@]}` carries no tail, so
# leaving it behind would make the fix look like the defect. The backreferences
# keep it to one array -- `${a[@]+"${b[@]}"}` is not this idiom.
_SAFE_FULL = re.compile(r'\$\{(?P<n>[A-Za-z_]\w*)\[(?P<s>[@*])\]\+"\$\{\1\[\2\]\}"\}')
# Any other `+`/`:-` tail: ${a[@]:-}, ${FOUND[@]:-<none>}, ${a[@]+--flag}.
_SAFE_TAIL = re.compile(r"\$\{[A-Za-z_]\w*\[[@*]\][+:][^{}]*\}")
# A VALUE expansion left over is the defect. `${#a[@]}` is a length, and
# `${a[0]}` an indexed read that errors identically on bash 5, so neither is
# this pin's business.
_UNSAFE = re.compile(r"(?<!\$\{#)\$\{([A-Za-z_]\w*)\[([@*])\]\}")
# `${{ ... }}` belongs to Actions, not bash, and can hold text shaped like an
# expansion. The shell only ever sees its interpolated result.
_GH_EXPR = re.compile(r"\$\{\{[^}]*\}\}")


def unsafe_sites(script: str) -> list[tuple[str, str]]:
    """The ``(name, sigil)`` of every array expansion bash 3.2 would reject."""
    text = _GH_EXPR.sub("GHEXPR", script)
    # A full-line comment is prose, not code, and these scripts EXPLAIN the
    # idiom in their comments -- the readiness script quotes both the safe and
    # the unsafe form to say which to write. Scanning that text flags the
    # documentation of the fix as the defect. Only whole-line comments are
    # dropped: a code line keeps its trailing comment, so the pin still errs
    # toward flagging rather than toward missing a real expansion.
    text = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    text = _SAFE_FULL.sub("SAFE", text)
    text = _SAFE_TAIL.sub("SAFE", text)
    return [(m.group(1), m.group(2)) for m in _UNSAFE.finditer(text)]


def _run_blocks(path: Path):
    """Every ``run:`` script in a workflow, with a label to name it by."""
    spec = yaml.safe_load(path.read_text(encoding="utf-8"))
    stack = [spec]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            run = node.get("run")
            if isinstance(run, str):
                yield node.get("id") or node.get("name") or "<unnamed>", run
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)


def test_the_detector_flags_the_unsafe_form_and_spares_the_safe_ones():
    """Positive control.

    A silently-zero matcher is indistinguishable from a clean tree, so assert
    both directions against literal samples before trusting the sweep. The
    third "spared" case is the one that matters: the fix must not read as the
    defect, or the pin fails the moment it is satisfied.
    """
    assert unsafe_sites('for x in "${a[@]}"; do :; done') == [("a", "@")]
    assert unsafe_sites('printf "%s\\n" "${a[*]}"') == [("a", "*")]
    assert unsafe_sites('b=("${failed[@]}")') == [("failed", "@")]

    assert unsafe_sites('[ "${#a[@]}" -gt 0 ]') == []
    assert unsafe_sites('for x in ${a[@]+"${a[@]}"}; do :; done') == []
    assert unsafe_sites('printf "%s\\n" ${a[*]+"${a[*]}"}') == []
    assert unsafe_sites('echo "${a[*]:-}"') == []
    assert unsafe_sites('echo "${FOUND[@]:-<none>}"') == []
    assert unsafe_sites('echo "${a[0]}"') == []

    # A whole-line comment is prose. These scripts quote BOTH forms to explain
    # which to write, so scanning comment text reports the documentation of the
    # fix as the defect.
    assert unsafe_sites('  # write ${a[@]+"${a[@]}"} rather than "${a[@]}"') == []
    # A code line keeps its trailing comment, so a real expansion is never lost
    # to a `#` later on the line.
    assert unsafe_sites('for x in "${a[@]}"; do :; done  # loop') == [("a", "@")]

    # A mismatched pair is not the idiom, so the inner read still counts.
    assert unsafe_sites('${a[@]+"${b[@]}"}') == [("b", "@")]


def test_the_sweep_actually_reads_the_workflows():
    """Guard the sweep's own inputs: a glob that matches nothing, or a parse
    that yields no scripts, would make the pin below vacuously green."""
    blocks = [run for path in WORKFLOWS.glob("*.yml") for _, run in _run_blocks(path)]
    assert len(blocks) > 100, f"only {len(blocks)} run: blocks found"
    assert any("[@]" in run for run in blocks), "no array expansion seen at all"


def test_no_workflow_expands_an_array_in_a_form_bash_32_rejects():
    offenders: list[str] = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        for label, run in _run_blocks(path):
            for name, sigil in unsafe_sites(run):
                offenders.append(
                    f"{path.name} :: {label} :: ${{{name}[{sigil}]}} "
                    f'-- write ${{{name}[{sigil}]+"${{{name}[{sigil}]}}"}}'
                )
    assert not offenders, (
        "workflow array expansion(s) that abort under bash 3.2 + set -u when "
        "the array is empty (macOS /bin/bash, which the extraction tests run "
        "this shell under):\n  " + "\n  ".join(offenders)
    )


@needs_bash
@pytest.mark.parametrize("sigil", ["@", "*"])
def test_the_safe_form_keeps_elements_that_contain_spaces(sigil: str) -> None:
    """The rewrite must not re-split an element.

    ``${a[@]+"${a[@]}"}`` looks unquoted, and the outer braces really are -- but
    the expansion inside them is quoted, so each element stays one word. A
    readiness spec like ``code-review.yml|Code Review`` would otherwise arrive
    as two, which is the failure mode a reviewer would reasonably suspect.
    """
    proc = subprocess.run(
        [
            "bash",
            "-c",
            "set -u\n"
            'a=("one two" "three")\n'
            f'for x in ${{a[{sigil}]+"${{a[{sigil}]}}"}}; do echo "[$x]"; done\n',
        ],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=True,
    )
    expected = "[one two]\n[three]\n" if sigil == "@" else "[one two three]\n"
    assert proc.stdout == expected


@needs_bash
def test_the_safe_form_expands_an_empty_array_to_nothing() -> None:
    """And the property the pin exists for, on whatever bash is running: an
    empty array must contribute zero words, not one empty one.

    Counted with ``$#`` rather than printed: ``printf`` runs its format once
    even with no arguments, so a print would show one word where there are
    none and say nothing about the expansion.
    """
    proc = subprocess.run(
        [
            "bash",
            "-c",
            'set -u\na=()\nset -- ${a[@]+"${a[@]}"}\necho "words=$#"\n',
        ],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=True,
    )
    assert proc.stdout == "words=0\n"
