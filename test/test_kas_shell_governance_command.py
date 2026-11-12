"""The governance row judges a KAS shell call on its recovered command.

KAS titles its shell call ``Run Command``, so a ``commands`` rule read off the
title alone never met the command: a policy deny such as ``*backdoor*`` let
``echo backdoor`` through to the read-only row's auto-approve. The row also asks
the recovered command, so the deny refuses it before any grant or classify row.
"""

from __future__ import annotations

import pytest
from test_kas_allowed_commands import (  # noqa: F401 - _no_profiles is an autouse fixture
    _install_governance,
    _judge,
    _no_profiles,
    _shell,
)
from test_tool_call_classification_kas import _kas_builtin_flow

from kiro_crew.hooks import TOOL_ALLOW, TOOL_AUTO_APPROVE, TOOL_DENY


def _deny_backdoor() -> None:
    _install_governance({"commands": {"mode": "deny", "deny": ["*backdoor*"]}})


def test_a_policy_deny_refuses_the_command_a_read_only_call_runs() -> None:
    _deny_backdoor()
    verdict = _judge(_shell("echo backdoor"), [])
    assert (verdict.action, verdict.tier) == (TOOL_DENY, "governance")


def test_a_policy_deny_refuses_a_granted_chain_holding_the_command() -> None:
    _deny_backdoor()
    verdict = _judge(_shell("git status && echo backdoor"), ["git status", "echo .*"])
    assert (verdict.action, verdict.tier) == (TOOL_DENY, "governance")


def test_a_harmless_read_only_command_is_still_auto_approved() -> None:
    _deny_backdoor()
    verdict = _judge(_shell("ls"), [])
    assert (verdict.action, verdict.tier) == (TOOL_AUTO_APPROVE, "read-only")


def test_a_command_the_deny_does_not_name_still_asks() -> None:
    _deny_backdoor()
    verdict = _judge(_shell("git status --porcelain"), ["git status.*"])
    assert verdict.action == TOOL_ALLOW


# ── an allow-mode ``commands`` policy ─────────────────────────────────────────


def _allow_git_status() -> None:
    _install_governance(
        {"commands": {"mode": "allow", "allow": ["git status", "git status --porcelain"]}}
    )


def test_an_allow_mode_policy_admits_a_listed_command() -> None:
    """The title ``Run Command`` is not on the list, and it names no command: the
    recovered command is what the list is about. The call then prompts as usual."""
    _allow_git_status()
    verdict = _judge(_shell("git status --porcelain"), [])
    assert verdict.action == TOOL_ALLOW
    assert verdict.tier != "governance"


def test_an_allow_mode_policy_lets_a_listed_command_take_its_grant() -> None:
    _allow_git_status()
    verdict = _judge(_shell("git status"), ["git status"])
    assert verdict.action != TOOL_DENY


def test_an_allow_mode_policy_still_refuses_an_unlisted_command() -> None:
    _allow_git_status()
    verdict = _judge(_shell("ls"), [])
    assert (verdict.action, verdict.tier) == (TOOL_DENY, "governance")


def test_an_allow_mode_policy_refuses_a_chain_with_an_unlisted_part() -> None:
    _allow_git_status()
    verdict = _judge(_shell("git status && echo x"), ["git status", "echo .*"])
    assert (verdict.action, verdict.tier) == (TOOL_DENY, "governance")


def test_an_explicit_deny_on_the_title_still_refuses() -> None:
    _install_governance({"commands": {"mode": "deny", "deny": ["Run Command"]}})
    verdict = _judge(_shell("git status"), [])
    assert (verdict.action, verdict.tier) == (TOOL_DENY, "governance")


# ── an allow-mode glob is checked against each chained command ────────────────


def _allow_git_glob() -> None:
    _install_governance({"commands": {"mode": "allow", "allow": ["git *"]}})


def test_an_allow_mode_glob_refuses_a_chain_whose_glob_spans_the_separator() -> None:
    """``git *`` matches the whole ``git status && python3 -c x`` because ``*``
    crosses ``&&``; the chained ``python3 -c x`` is not listed, so it refuses."""
    _allow_git_glob()
    verdict = _judge(_shell("git status && python3 -c x"), [])
    assert (verdict.action, verdict.tier) == (TOOL_DENY, "governance")


def test_an_allow_mode_glob_admits_a_chain_whose_every_part_is_listed() -> None:
    _allow_git_glob()
    verdict = _judge(_shell("git status && git log"), [])
    assert verdict.tier != "governance"
    assert verdict.action != TOOL_DENY


def test_an_allow_mode_glob_refuses_a_command_that_cannot_be_split() -> None:
    _allow_git_glob()
    verdict = _judge(_shell("git status > out.txt"), [])
    assert (verdict.action, verdict.tier) == (TOOL_DENY, "governance")


def test_an_allow_glob_matching_the_marker_refuses_an_unsplittable_chain() -> None:
    _install_governance({"commands": {"mode": "allow", "allow": ["git *", "*sh*"]}})
    verdict = _judge(_shell('python3 -c "print(1)"'), [])
    assert (verdict.action, verdict.tier) == (TOOL_DENY, "governance")
    verdict = _judge(_shell('git status && python3 -c "print(1)"'), [])
    assert (verdict.action, verdict.tier) == (TOOL_DENY, "governance")


@pytest.mark.parametrize(
    "command",
    [
        "git status > out.txt",
        'git add -A && git commit -m "x"',
        'git log --format="%h %s" -n 5 | tail -n 1',
        "cd C:\\work\\repo && git status",
    ],
)
def test_an_allow_all_glob_admits_what_it_admitted_before(command: str) -> None:
    """Under ``*`` the title and the whole command both match, which is how the
    gate judged a KAS call before, so even a command that cannot be split passes."""
    _install_governance({"commands": {"mode": "allow", "allow": ["*"]}})
    verdict = _judge(_shell(command), [])
    assert verdict.tier != "governance"
    assert verdict.action != TOOL_DENY, command


@pytest.mark.parametrize(
    ("command", "refused"),
    [
        ("git status", False),
        ("git status && git log", False),
        ('git add -A && git commit -m "x"', True),
        ("git status && python3 -c x", True),
        ("git status > out.txt", True),
        ("ls", True),
    ],
)
def test_an_allow_glob_off_the_title_is_judged_strictly(command: str, refused: bool) -> None:
    """``git *`` does not list ``Run Command``, so the call is new permission and
    every part must be listed; a command that cannot be split is refused."""
    _allow_git_glob()
    verdict = _judge(_shell(command), [])
    assert (verdict.action == TOOL_DENY and verdict.tier == "governance") is refused, command


def test_a_deny_mode_policy_permits_a_command_that_cannot_be_split() -> None:
    _deny_backdoor()
    verdict = _judge(_shell("git status > out.txt"), [])
    assert verdict.tier != "governance"


# ── quoted and Windows-path chains cannot be split, so allow mode refuses ─────


def test_an_allow_mode_glob_refuses_a_quoted_chain_with_an_unlisted_part() -> None:
    _allow_git_glob()
    verdict = _judge(_shell('git status && python3 -c "print(1)"'), [])
    assert (verdict.action, verdict.tier) == (TOOL_DENY, "governance")


def test_an_allow_mode_glob_refuses_a_separator_inside_a_quoted_argument() -> None:
    _allow_git_glob()
    verdict = _judge(_shell('git log --format="%h; rm -rf x"'), [])
    assert (verdict.action, verdict.tier) == (TOOL_DENY, "governance")


def test_an_allow_mode_glob_refuses_a_substitution_or_a_redirect() -> None:
    _allow_git_glob()
    for command in ("git log $(rm -rf x)", "git status > out.txt"):
        verdict = _judge(_shell(command), [])
        assert (verdict.action, verdict.tier) == (TOOL_DENY, "governance"), command


def test_an_allow_mode_glob_refuses_code_hidden_between_quoted_separators() -> None:
    """Quotes that hold a separator and the listed prefix must not make the
    PowerShell sub-expression or the zsh glob qualifier between them look like
    a listed part."""
    _allow_git_glob()
    for command in (
        'git log "x;git " (Remove-Item -Recurse x) ";git y"',
        'git log "x;git " *(e:reboot:) ";git y"',
    ):
        verdict = _judge(_shell(command), [])
        assert (verdict.action, verdict.tier) == (TOOL_DENY, "governance"), command


def test_an_allow_mode_glob_refuses_a_listed_quoted_or_windows_chain() -> None:
    """Today allow mode refuses these chains even though every part is listed,
    because they cannot be split safely. shortcut: admit them once a shell-aware
    splitter exists that keeps every refusal in this file."""
    _install_governance({"commands": {"mode": "allow", "allow": ["cd *", "git *"]}})
    verdict = _judge(_shell("cd C:\\work\\repo && git status"), [])
    assert (verdict.action, verdict.tier) == (TOOL_DENY, "governance")
    _allow_git_glob()
    verdict = _judge(_shell('git add -A && git commit -m "fix: example"'), [])
    assert (verdict.action, verdict.tier) == (TOOL_DENY, "governance")


# ── kiro-cli's title carries the command, so it is judged as before ───────────


def _kiro_cli_shell(command: str):
    """A kiro-cli shell call: its title carries the command it runs, and its
    permission frame names no tool id."""
    event = _kas_builtin_flow(
        "",
        title=f"Running: {command}",
        kind="execute",
        raw_input={"command": command},
        on_kas=False,
    )
    return _judge(event, [])


@pytest.mark.parametrize(
    ("allow", "command"),
    [
        (["git *"], "git status > out.txt"),
        (["git *"], 'git add -A && git commit -m "x"'),
        (["*"], "git status > out.txt"),
        (["*"], 'git log --format="%h; x" | tail -n 1'),
    ],
)
def test_a_kiro_cli_shell_call_is_judged_on_its_title_as_before(
    allow: list[str], command: str
) -> None:
    """Splitting the chain and refusing an unsplittable command apply only where
    the title does not carry the command; kiro-cli's ``Running:`` title does."""
    _install_governance({"commands": {"mode": "allow", "allow": allow}})
    assert _kiro_cli_shell(command).action != TOOL_DENY


def test_a_kiro_cli_shell_call_the_allow_list_misses_is_still_refused() -> None:
    _allow_git_glob()
    assert _kiro_cli_shell("ls").action == TOOL_DENY


# ── only the minted marker is unmatchable; a real command is matched as text ──


@pytest.mark.parametrize("harness", ["kas", "kiro-cli"])
def test_a_policy_deny_still_meets_a_command_that_holds_a_nul(harness: str) -> None:
    _deny_backdoor()
    command = "echo backdoor\x00"
    verdict = _judge(_shell(command), []) if harness == "kas" else _kiro_cli_shell(command)
    assert (verdict.action, verdict.tier) == (TOOL_DENY, "governance")


def test_a_deny_all_policy_still_refuses_a_command_that_holds_a_nul() -> None:
    _install_governance({"commands": {"mode": "deny", "deny": ["*"]}})
    verdict = _kiro_cli_shell("ls\x00")
    assert (verdict.action, verdict.tier) == (TOOL_DENY, "governance")
