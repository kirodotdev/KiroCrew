"""The deny-rules tier asks about each distinct string once.

A KAS shell call's title IS its command, so the normalized title, the title and the
command are one string. The deny verdict is a function of that string, so asking it
twice can only repeat the first answer, and the shell parse of a long command is the
gate's largest cost. These tests pin the count and that the verdict is unchanged.
"""

from __future__ import annotations

import pytest

from kiro_crew import security
from kiro_crew.hooks import TOOL_DENY, HookManager

_LONG = (
    "git show origin/main:src/kiro_crew/hooks.py > /tmp/h.py; sed -n 518,560p /tmp/h.py; "
    "grep -n 'await \\|to_thread\\|subprocess\\|open(' /tmp/h.py | awk -F: '$1>518 && $1<1420' | head -90; "
    'ls -la | head -20; for f in $(ls | head -5); do echo "== $f"; head -c 600 "$f" | cut -c1-600; echo; done; git status --short'
)
_MULTILINE = "python3 - <<'EOF'\nimport json\nprint(json.dumps({'a': 1}))\nEOF"

#: (label, command, expected action is a deny) -- allow, deny, long, pipelines.
COMMANDS = [
    ("allow-short", "git status --short | head -5", False),
    ("allow-pipeline", "cd /tmp && git log --oneline -5 | grep fix | wc -l", False),
    ("allow-long", _LONG, False),
    ("allow-multiline", _MULTILINE, False),
    ("deny-rm-root", "rm -rf /", True),
    ("deny-delete-stack", "aws cloudformation delete-stack --stack-name x | tee /tmp/o", True),
    ("deny-in-pipeline", "echo ok && rm -rf / ; ls", True),
    ("deny-long-tail", _LONG + "; rm -rf /", True),
]


def _judge(cmd: str, *, title: str | None = None, cwd: str | None = "/tmp"):
    raw = {"command": cmd, **({"cwd": cwd} if cwd else {})}
    return HookManager().on_tool_call(
        cmd if title is None else title,
        tool_kind="execute",
        raw_params=raw,
        command=cmd,
        is_shell=True,
        kas_builtin_ids=True,
        harness_builtin_tool="execute_bash",
        session_key="dashboard:test-deny-once",
        agent="kirocrew",
        resolved_agent="kirocrew",
    )


@pytest.fixture
def asked(monkeypatch):
    """Every string ``security.is_denied`` is asked about, in order."""
    seen: list[str] = []
    real = security.is_denied

    def spy(target, *args, **kwargs):
        seen.append(target)
        return real(target, *args, **kwargs)

    monkeypatch.setattr(security, "is_denied", spy)
    return seen


@pytest.mark.parametrize("cwd", ["/tmp", None], ids=["cwd", "no-cwd"])
def test_a_kas_shell_command_is_asked_once(asked, cwd):
    assert len(_LONG) > 300
    result = _judge(_LONG, cwd=cwd)
    assert result.action != TOOL_DENY
    assert asked.count(_LONG) == 1


def test_a_prefixed_title_still_asks_both_forms(asked):
    cmd = "git status --short"
    _judge(cmd, title=f"Running: {cmd}")
    assert asked.count(cmd) == 1
    assert asked.count(f"Running: {cmd}") == 1


@pytest.mark.parametrize("label,cmd,denied", COMMANDS, ids=[c[0] for c in COMMANDS])
def test_the_verdict_is_what_asking_twice_gave(label, cmd, denied):
    """Same action and reason as the old target list, which asked the command twice.

    The old list is this call's targets with the first one repeated in front;
    ``security.is_denied`` is asked about each in order and the first refusal wins,
    exactly as ``_tier_deny_rules`` walks them.
    """
    result = _judge(cmd)
    assert (result.action == TOOL_DENY) is denied
    again = [security.is_denied(cmd), security.is_denied(cmd)]
    assert again[0] == again[1]
    if denied:
        assert again[0] is not None
        assert again[0] in (result.reason or "")
