"""The diagnostics redaction tally counts each secret once.

A header line such as ``Authorization: Bearer <secret>`` is matched by two
passes in ``diagnostics._scrub``: the credential redactor replaces the value
with a credential tag, and the header rule then replaces the rest of the line.
Only the first of those is a new secret, so the line must count as one.
"""

from __future__ import annotations

import pytest

from kiro_crew import diagnostics


@pytest.mark.parametrize(
    "line",
    [
        "Authorization: Bearer abcdefgh12345678",
        "x-api-key: abcdefgh12345678",
        "Cookie: session=abcdefgh12345678",
        "Authorization: abcdefgh12345678",
        "request used Bearer abcdefgh12345678 mid-line",
    ],
)
def test_one_secret_on_a_line_counts_once(line):
    clean, count = diagnostics._scrub(line)

    assert "abcdefgh12345678" not in clean
    assert count == 1, clean


def test_separate_lines_each_count():
    text = "Authorization: Bearer abcdefgh12345678\n" "x-api-key: zyxwvuts87654321\n"

    clean, count = diagnostics._scrub(text)

    assert "abcdefgh12345678" not in clean and "zyxwvuts87654321" not in clean
    assert count == 2, clean
