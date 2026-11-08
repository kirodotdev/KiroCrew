"""Memory and load counters auto-approve as reads; a write after them still prompts."""

from __future__ import annotations

import pytest

from kiro_crew.security.readonly_bash import is_read_only_bash, unsafe_bash_reason

READ_ONLY = [
    "vm_stat",
    "vm_stat 1",
    "uptime",
    "uptime -p",
    "free",
    "free -h",
    "free -m -s 1 -c 3",
    "free -h | grep Mem",
    "vm_stat 2>/dev/null | head -5",
]


@pytest.mark.parametrize("cmd", READ_ONLY)
def test_memory_counter_is_read_only(cmd: str) -> None:
    assert unsafe_bash_reason(cmd) == ""
    assert is_read_only_bash(cmd)


STILL_PROMPTS = [
    # A real-file redirect still trips the shell check.
    "vm_stat > out.txt",
    "uptime >> log.txt",
    "free -h > /tmp/mem",
    # A chained write still needs its own verb on the allowlist.
    "vm_stat; rm -rf build",
    "uptime && touch marker",
    "free -h || mv a b",
    "free | tee mem.txt",
    # Kept off on purpose: sysctl writes with -w / key=value, ps e leaks env.
    "sysctl -n hw.memsize",
    "sysctl -w kern.maxfiles=1",
    "ps e",
    "top -l 1",
    # A longer word is a different program, not the allowlisted verb.
    "freeze now",
    "uptimer",
]


@pytest.mark.parametrize("cmd", STILL_PROMPTS)
def test_write_or_unlisted_form_still_prompts(cmd: str) -> None:
    assert unsafe_bash_reason(cmd) != ""
    assert not is_read_only_bash(cmd)
