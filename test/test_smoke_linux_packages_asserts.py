"""The in-container assertions of ``scripts/smoke-linux-packages.sh`` render cleanly.

``common_asserts`` writes its assertions through an unquoted heredoc, so the
host shell expands every backtick and ``$(`` in the body, comment lines
included, before the container sees a byte. A comment that quotes a path in
bare backticks runs that path on the host, prints "No such file or directory"
into every smoke log, and drops the path from the comment. Each such character
in the body has to be escaped.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "smoke-linux-packages.sh"

pytestmark = pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None,
    reason="the Linux smoke script runs under bash on a POSIX runner",
)


def _function_source() -> str:
    text = SCRIPT.read_text(encoding="utf-8")
    match = re.search(r"^common_asserts\(\) \{\n.*?^\}\n", text, re.S | re.M)
    assert match, "common_asserts() not found in smoke-linux-packages.sh"
    return match.group(0)


def _heredoc_body() -> list[str]:
    lines = _function_source().splitlines()
    start = next(i for i, line in enumerate(lines) if "<<ASSERTS" in line)
    end = next(i for i, line in enumerate(lines) if line == "ASSERTS")
    return lines[start + 1 : end]


def test_heredoc_body_escapes_every_backtick_and_substitution() -> None:
    unescaped = re.compile(r"(?<!\\)(`|\$\()")
    offenders = [line for line in _heredoc_body() if unescaped.search(line)]
    assert not offenders, (
        "unescaped ` or $( in the common_asserts heredoc runs on the host "
        f"while the assertions are written: {offenders}"
    )


def test_rendering_the_assertions_is_silent_and_keeps_comment_text() -> None:
    script = _function_source() + 'common_asserts "dpkg -L kirocrew"\n'
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=FALLBACK_TIMEOUT_SECS,
        check=True,
    )
    assert result.stderr == ""
    assert "`/opt/KiroCrew Nightly/...`" in result.stdout
