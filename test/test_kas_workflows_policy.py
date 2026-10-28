"""The deny-rule screen a workflow step's command passes before a person sees it."""

from __future__ import annotations

from kiro_crew.kas_workflows import policy


def test_step_command_reads_shell_shapes():
    assert policy.step_command({"toolCall": {"rawInput": {"command": "ls -la"}}}) == "ls -la"
    assert policy.step_command({"toolCall": {"rawInput": {"cmd": ["git", "status"]}}}) == (
        "git status"
    )
    assert policy.step_command({"toolCall": {"rawInput": {"path": "a.txt"}}}) is None
    assert policy.step_command({"toolCall": "nope"}) is None
    assert policy.step_command({}) is None


def test_real_deny_rules_refuse_a_builtin_destructive_command():
    assert policy.deny_reason("rm -rf /") is not None


def test_real_deny_rules_pass_an_ordinary_command():
    assert policy.deny_reason("git status") is None


def test_oversized_command_is_refused_unscreened():
    assert policy.deny_reason("x" * (policy.MAX_SCREENED_COMMAND_CHARS + 1)) is not None
