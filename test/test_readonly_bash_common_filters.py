"""Common stdout-only filters auto-approve as reads; their write forms still prompt."""

from __future__ import annotations

import pytest

from kiro_crew.security.readonly_bash import is_read_only_bash, unsafe_bash_reason

READ_ONLY_LEADS = [
    "tr a-z A-Z",
    "nl -ba src/app.py",
    "rev notes.txt",
    "comm -12 a.txt b.txt",
    "od -c data.bin",
    "column -t table.txt",
]

READ_ONLY_PIPES = [
    "cat f | cut -d: -f1",
    "cat f | tr -d '\\r'",
    "cat f | nl",
    "cat f | rev",
    "cat f | od -An -tx1",
    "cat f | column -t",
    "ls | comm -23 - other.txt",
]


@pytest.mark.parametrize("cmd", READ_ONLY_LEADS + READ_ONLY_PIPES)
def test_common_filter_is_read_only(cmd: str) -> None:
    assert unsafe_bash_reason(cmd) == ""


@pytest.mark.parametrize(
    "cmd",
    [
        # rg stays off: `--pre` runs a program on every file it searches.
        "rg --pre ./payload pattern",
        "rg pattern",
        "cat f | rg --pre ./payload x",
        # cut is a pipe target only.
        "cut -d: -f1 /etc/passwd",
        # A real-file redirect still trips the shell check.
        "tr a-z A-Z > out.txt",
        "cat f | tr -d x > out",
        # xxd stays off the allowlist: `-r` writes a file.
        "xxd data.bin",
        "cat f | xxd",
        # jq stays off the allowlist: it evaluates a program.
        "jq -n env",
        "jq . package.json",
        "cat package.json | jq .",
        # printf stays off the allowlist: `-v` assigns a shell variable.
        "printf -v PATH /tmp/x && ls",
        "printf '%s' x",
    ],
)
def test_write_or_exec_form_still_prompts(cmd: str) -> None:
    assert not is_read_only_bash(cmd)
