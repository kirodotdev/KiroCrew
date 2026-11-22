"""The EC2 bootstrap writes its helper files without running anything inside them.

UserData writes ``kcfetch.sh`` and the gateway unit through UNQUOTED heredocs
(``<<KCFETCH``, ``<<UNIT``) so ``$RUN_HOME`` and friends land at write time. Bash
still runs command substitution in such a body, comment lines included, so a
backtick pair there runs its contents while the file is written: a backticked
``--`` in a comment prints ``--: command not found`` in the launch output.
"""

from __future__ import annotations

import re

from kiro_crew.cloud import ec2

# ``<<WORD`` at the end of a line; ``<<'WORD'`` (quoted, no expansion) never matches.
_UNQUOTED_OPENER = re.compile(r"<<-?([A-Za-z_][A-Za-z0-9_]*)\s*$")


def _unquoted_heredocs(text: str) -> list[tuple[str, list[str]]]:
    lines = text.splitlines()
    found = []
    i = 0
    while i < len(lines):
        m = _UNQUOTED_OPENER.search(lines[i])
        i += 1
        if m:
            start = i
            while lines[i].strip() != m.group(1):
                i += 1
            found.append((m.group(1), lines[start:i]))
    return found


def test_no_backtick_inside_an_unquoted_heredoc_body():
    heredocs = _unquoted_heredocs(ec2.load_template())
    # Guard the scan itself: a parser that found nothing would pass vacuously.
    assert "KCFETCH" in [word for word, _ in heredocs]
    bad = [(word, line.strip()) for word, body in heredocs for line in body if "`" in line]
    assert not bad, (
        "a backtick in an unquoted heredoc runs as a command while UserData writes "
        f"the file; reword the line without it: {bad}"
    )
