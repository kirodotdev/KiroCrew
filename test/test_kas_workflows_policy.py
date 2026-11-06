"""The deny-rule screen a workflow step's command passes before a person sees it."""

from __future__ import annotations

from kiro_crew.kas_workflows import policy


def _text(params):
    return policy.resolve_step_command(params).text


def _unreadable(params):
    return policy.resolve_step_command(params).unreadable


def test_the_command_is_read_from_raw_input_and_from_kas_meta():
    assert _text({"toolCall": {"rawInput": {"command": "ls -la"}}}) == "ls -la"
    assert _text({"toolCall": {"rawInput": {"cmd": ["git", "status"]}}}) == "git status"
    native = {"_meta": {"kiro": {"command": "npm test"}}, "toolCall": {"rawInput": {}}}
    assert _text(native) == "npm test"
    same = {"_meta": {"kiro": {"command": "ls"}}, "toolCall": {"rawInput": {"command": "ls"}}}
    assert _text(same) == "ls"


def test_a_call_naming_no_command_has_none():
    for params in ({"toolCall": {"rawInput": {"path": "a.txt"}}}, {}, {"_meta": {"kiro": {}}}):
        assert _text(params) is None and not _unreadable(params)


def test_a_command_in_a_shape_it_cannot_read_is_unreadable():
    for params in (
        {"toolCall": "nope"},
        {"toolCall": {"rawInput": "ls"}},
        {"toolCall": {"rawInput": {"command": 3}}},
        {"toolCall": {"rawInput": {"command": "  "}}},
        {"toolCall": {"rawInput": {"cmd": []}}},
        {"_meta": "x"},
        {"_meta": {"kiro": {"command": None}}},
        {"_meta": {"kiro": {"command": "ls"}}, "toolCall": {"rawInput": {"command": "make"}}},
    ):
        assert _unreadable(params) and _text(params) is None, params


def test_real_deny_rules_refuse_a_builtin_destructive_command():
    assert policy.deny_reason("rm -rf /") is not None


def test_real_deny_rules_pass_an_ordinary_command():
    assert policy.deny_reason("git status") is None


def test_oversized_command_is_refused_unscreened():
    assert policy.deny_reason("x" * (policy.MAX_SCREENED_COMMAND_CHARS + 1)) is not None
