"""Bash conformance for the span gate's non-arming ``case`` positions.

``_arms_case_context`` declines to arm the pattern-paren rule in a few positions
because bash, measured, reads ``case`` there as data or refuses the line. This
module re-measures each of those positions under the host's own bash, so a bash
that starts running the clause body in one of them turns a test red instead of
going unnoticed.

Each vector puts a ``case`` clause inside ``$( )``. If bash read that ``case`` as
the reserved word, the clause body after the pattern ``)`` would run and print the
sentinel; the sentinel is assembled by ``printf``, so echoing the text as an
argument never prints it. The empty head is the control: a bare ``case`` in
command position, which the gate arms and bash spans. The blank after ``$(`` keeps
a head that opens with ``(`` from reading as ``$((`` arithmetic.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import _find_posix_test_shell
from kiro_crew.security.shell_normalizer import _arms_case_context
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

_SENTINEL = "SENTINEL"
_TAIL = "printf '%s%s\\n' SENT INEL;; esac)"


@pytest.fixture(scope="module")
def bash() -> str:
    """A bash that parses ``case`` inside ``$( )``.

    bash 3.2 (macOS ``/bin/bash``) closes the substitution at every pattern
    paren, so the control would fail and every other row would pass vacuously.
    """
    path = _find_posix_test_shell() if os.name == "nt" else shutil.which("bash")
    if path is None:
        pytest.skip("needs bash")
    probe = subprocess.run(
        [path, "-c", "echo ${BASH_VERSINFO[0]}"],
        capture_output=True,
        encoding="utf-8",
        timeout=FALLBACK_TIMEOUT_SECS,
    )
    major = probe.stdout.strip()
    if not major.isdigit() or int(major) < 4:
        pytest.skip(f"needs bash 4 or newer, found {major!r}")
    return path


@pytest.mark.parametrize(
    ("head", "arms"),
    [
        pytest.param("", True, id="control-command-position"),
        pytest.param("echo", False, id="argument"),
        pytest.param("echo then", False, id="keeper-as-argument"),
        pytest.param("v=1", False, id="assignment-prefix"),
        pytest.param("command", False, id="command-operand"),
        pytest.param("eval", False, id="eval-operand"),
        pytest.param("> f", False, id="redirect-prefix"),
        pytest.param(">", False, id="redirect-target"),
        pytest.param("if :; then :; fi", False, id="after-fi"),
        pytest.param("for i in 1; do :; done", False, id="after-done"),
        pytest.param("{ :; }", False, id="after-brace"),
        pytest.param("case y in y) :;; esac", False, id="after-esac"),
        pytest.param("echo -n", False, id="option-word"),
        pytest.param("(:)", False, id="after-subshell"),
        pytest.param("echo $(:)", False, id="after-substitution"),
    ],
)
def test_bash_never_runs_the_clause_body_where_the_gate_declines_to_arm(
    bash: str, head: str, arms: bool, tmp_path: Path
) -> None:
    text = f"echo $( {head} case x in x) {_TAIL}"
    assert _arms_case_context(text, text.index("case x in x)")) is arms
    result = subprocess.run(
        [bash, "-c", text],
        cwd=tmp_path,
        capture_output=True,
        encoding="utf-8",
        timeout=FALLBACK_TIMEOUT_SECS,
    )
    assert (_SENTINEL in result.stdout) is arms, (
        result.returncode,
        result.stdout,
        result.stderr,
    )
