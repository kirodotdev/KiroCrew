"""Read-only shell auto-approve never runs ``git``.

git reads ``.git/config`` on every invocation, and ``core.fsmonitor`` /
``core.hooksPath`` name a program there that no text classifier sees. So a
read-only ``git status`` still runs whatever that config names. The heartbeat
approver already keeps git out of its unattended auto-approve; the gate's
read-only tier (cron, subagent and app sessions, where nobody may be watching)
and the dashboard's trust-reads mode keep it out the same way.

What is pinned here:

* ``readonly_bash.command_invokes_git`` -- one pure rule for both sites: true
  when any command position runs git (any case, any path, ``.exe``, after a
  ``VAR=`` prefix or a program runner such as ``env``) or the quoting cannot be
  resolved;
* the gate's read-only tier: a git command gets no read-only verdict and falls
  through to the surface's own approval, while other reads still auto-approve;
* the classifier itself is unchanged: ``is_read_only_bash`` still reads a git
  read as read-only, so only the auto-approve sites change.

The trust-reads path is driven end to end in ``test_chat_runner_coverage_approval.py``.
"""

from __future__ import annotations

import pytest

from kiro_crew.hooks import TOOL_ALLOW, TOOL_AUTO_APPROVE, HookManager, ToolCall
from kiro_crew.security.readonly_bash import command_invokes_git, is_read_only_bash

GIT_COMMANDS = [
    "git status",
    "git log --oneline -5",
    "pwd; git status",
    "ls && git diff",
    "ls | GIT log",
    "FOO=1 git status",
    "(git status)",
    "/usr/bin/git status",
    "ls\ngit status",
    "Git branch -a",
    "ls || git status",
    "{ git status; }",
    "env -i git status",
    "timeout 5 git log",
    "ls | xargs git show",
    "C:\\Git\\cmd\\git.exe status",
    "2>/dev/null git status",
    "> out.txt git status",
    "2>&1 git log",
    "echo $(git status)",
    "echo `git status`",
    "cat <(git log)",
    "G=git; $G status",
]

#: Reads that must keep auto-approving after the change.
STILL_APPROVED = [
    "ls -la",
    "cat README.md",
    "head -20 file.py",
    "grep -rn pattern src/",
    "wc -l file.txt",
    "ls | grep digit",
]


@pytest.mark.parametrize("command", GIT_COMMANDS)
def test_every_git_position_is_seen(command: str) -> None:
    assert command_invokes_git(command) is True


@pytest.mark.parametrize(
    "command", STILL_APPROVED + ["echo git", "grep -rn git src/", "cat .gitignore", "ls git/"]
)
def test_git_as_an_argument_is_not_a_git_run(command: str) -> None:
    assert command_invokes_git(command) is False


def test_quoting_that_cannot_be_resolved_counts_as_git() -> None:
    assert command_invokes_git("ls 'unclosed") is True


def _judge(command: str) -> object:
    return HookManager().judge(
        ToolCall(title=f"Running: {command}", is_shell=True, command=command)
    )


@pytest.mark.parametrize("command", GIT_COMMANDS)
def test_the_read_only_tier_does_not_approve_git(command: str) -> None:
    verdict = _judge(command)
    assert verdict.action != TOOL_AUTO_APPROVE, (command, verdict)


@pytest.mark.parametrize("command", ["git status", "git log --oneline -5", "ls && git diff"])
def test_a_git_read_falls_through_to_the_surface_approver(command: str) -> None:
    """Not a deny: the call reaches the surface's own approval, as any other
    unproven call does, so an attended session can still approve it by hand."""
    assert is_read_only_bash(command) is True
    verdict = _judge(command)
    assert (verdict.action, verdict.read_only) == (TOOL_ALLOW, False)


@pytest.mark.parametrize("command", ["git status", "ls && git diff"])
def test_classifier_only_does_not_approve_git_either(command: str) -> None:
    verdict = HookManager().judge(
        ToolCall(title=f"Running: {command}", is_shell=True, command=command),
        classifier_only=True,
    )
    assert verdict.action != TOOL_AUTO_APPROVE


@pytest.mark.parametrize("command", STILL_APPROVED)
def test_other_reads_still_auto_approve(command: str) -> None:
    verdict = _judge(command)
    assert (verdict.action, verdict.read_only) == (TOOL_AUTO_APPROVE, True), (command, verdict)


class _RecordingProvider:
    def __init__(self) -> None:
        self.approved: list[str] = []
        self.rejected: list[str] = []
        self.deny_reasons: list[str] = []

    async def approve_tool(self, request_id: str) -> None:
        self.approved.append(request_id)

    async def reject_tool(self, request_id: str) -> None:
        self.rejected.append(request_id)

    async def steer(self, *_args: object, **_kwargs: object) -> None:
        return None


async def _resolve_headless(command: str, approver: object = None) -> _RecordingProvider:
    """Drive ``_resolve_permission`` as a hook-based surface with no approver
    (a Meetings dispatch) unless *approver* is given."""
    import json
    from unittest.mock import MagicMock, patch

    import kiro_crew.sel as sel_mod
    from kiro_crew import name_grant
    from kiro_crew.llm_helpers import ToolApprovalPolicy, _resolve_permission
    from kiro_crew.providers.base import EVENT_PERMISSION_REQUEST, LLMEvent

    async def _clean(_event: object) -> None:
        return None

    provider = _RecordingProvider()

    async def _record_deny(_provider: object, _event: object, reason: str, **_kw: object) -> None:
        provider.deny_reasons.append(reason)

    import kiro_crew.llm_helpers as llm_helpers_mod

    event = LLMEvent(
        kind=EVENT_PERMISSION_REQUEST,
        title=f"Running: {command}",
        request_id="r1",
        tool_input=json.dumps({"command": command}),
        is_shell=True,
    )
    with (
        patch.object(sel_mod, "sel", lambda: MagicMock()),
        patch.object(name_grant, "refusal_for_event", _clean),
        patch.object(llm_helpers_mod, "_steer_host_deny", _record_deny),
    ):
        await _resolve_permission(
            provider,  # type: ignore[arg-type]
            event,
            ToolApprovalPolicy.HOOK_BASED,
            HookManager(),
            on_tool_approval=approver,  # type: ignore[arg-type]
        )
    return provider


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["git status", "ls && git diff", "git log --oneline -5"])
async def test_a_git_read_is_refused_where_no_approver_exists(command: str) -> None:
    """With no approver the fall-through would reach the default approve and run
    git unchecked; it is refused instead."""
    provider = await _resolve_headless(command)
    assert (provider.approved, provider.rejected) == ([], ["r1"])
    assert provider.deny_reasons == [_NO_APPROVER_GIT_REASON]


#: The refusal the no-approver git branch sends, so a test can tell it from a hook deny.
_NO_APPROVER_GIT_REASON = (
    "a git command is never run without an approval (git runs programs "
    "its repository config names) and this surface has no approver"
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    ["git commit -m wip", "git checkout -b topic", "make && git add -A", "env git merge topic"],
)
async def test_a_git_write_is_refused_where_no_approver_exists(command: str) -> None:
    """A git command that is not a read runs repository-configured programs too
    (hooks), so with no approver it is refused like a git read."""
    assert is_read_only_bash(command) is False
    provider = await _resolve_headless(command)
    assert (provider.approved, provider.rejected) == ([], ["r1"])
    assert provider.deny_reasons == [_NO_APPROVER_GIT_REASON]


@pytest.mark.asyncio
async def test_a_git_write_still_reaches_an_approver_that_exists() -> None:
    async def _approve(_event: object) -> bool:
        return True

    provider = await _resolve_headless("git commit -m wip", _approve)
    assert (provider.approved, provider.rejected) == (["r1"], [])


@pytest.mark.asyncio
async def test_a_non_git_write_keeps_the_hook_based_default_where_no_approver_exists() -> None:
    """Only git is refused: a non-git command the hook allows keeps the surface's
    default, so the refusal is scoped to what runs repository-configured programs."""
    provider = await _resolve_headless("touch notes.txt")
    assert (provider.approved, provider.rejected) == (["r1"], [])


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["pytest $(ls tests)", "cat <(ls) > out.txt", "$EDITOR notes"])
async def test_an_unreadable_non_git_write_keeps_the_hook_based_default(command: str) -> None:
    """A command whose program cannot be read from its text, and that is not a read,
    is not refused as git: it keeps the surface's default, as before."""
    assert is_read_only_bash(command) is False
    provider = await _resolve_headless(command)
    assert (provider.approved, provider.rejected) == (["r1"], [])


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["echo $(git rev-parse HEAD) > out.txt", "make `git describe`"])
async def test_git_inside_a_substitution_is_refused_where_no_approver_exists(command: str) -> None:
    provider = await _resolve_headless(command)
    assert (provider.approved, provider.rejected) == ([], ["r1"])
    assert provider.deny_reasons == [_NO_APPROVER_GIT_REASON]


@pytest.mark.parametrize(
    ("command", "names_git"),
    [
        ("git status", True),
        ("ls && GIT_DIR=x git log", True),
        ("echo $(git status)", True),
        ("echo $(date)", False),
        ("$EDITOR notes", False),
        ("ls git-notes", False),
        ("xargs git add", True),
        ("timeout 5 git fetch", True),
        ("timeout --signal KILL 5 git fetch", True),
        ("sudo -u builder env -i PATH=/usr/bin git push", True),
        ("nice nohup git gc", True),
    ],
)
def test_command_names_git_reads_only_what_it_can_see(command: str, names_git: bool) -> None:
    from kiro_crew.security.readonly_bash import command_names_git

    assert command_names_git(command) is names_git


RUNNER_ARGUMENT_COMMANDS = [
    "find . -name '*.md' | xargs grep -ln git",
    "xargs -a files.txt grep -n 'git'",
    "timeout 60 rg -n git docs/",
    "nice -n 10 grep -rn git src/",
    "env LC_ALL=C grep -c git CHANGELOG.md",
    "time make docs VCS=git",
]


@pytest.mark.parametrize("command", RUNNER_ARGUMENT_COMMANDS)
def test_a_word_after_the_program_a_runner_runs_is_not_read_as_git(command: str) -> None:
    """After a runner, only the program it runs is read; ``git`` as that program's
    argument runs no git."""
    from kiro_crew.security.readonly_bash import command_names_git

    assert command_names_git(command) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("command", RUNNER_ARGUMENT_COMMANDS)
async def test_git_as_a_runner_programs_argument_keeps_the_hook_based_default(command: str) -> None:
    provider = await _resolve_headless(command)
    assert (provider.approved, provider.rejected) == (["r1"], [])


@pytest.mark.asyncio
async def test_a_git_read_still_reaches_an_approver_that_exists() -> None:
    async def _approve(_event: object) -> bool:
        return True

    provider = await _resolve_headless("git status", _approve)
    assert (provider.approved, provider.rejected) == (["r1"], [])


@pytest.mark.asyncio
async def test_a_plain_read_still_auto_approves_where_no_approver_exists() -> None:
    provider = await _resolve_headless("ls -la")
    assert (provider.approved, provider.rejected) == (["r1"], [])


@pytest.mark.parametrize("command", GIT_COMMANDS)
def test_the_heartbeat_approver_refuses_the_same_git_commands(command: str) -> None:
    """The heartbeat approver keeps its own git check; the same table pins that
    it refuses every shape the shared helper refuses."""
    from test_heartbeat_safe_tools import _shell

    from kiro_crew.slack.gateway import _is_heartbeat_safe_tool

    assert not _is_heartbeat_safe_tool(_shell(command))


@pytest.mark.parametrize("command", STILL_APPROVED)
def test_the_heartbeat_approver_still_approves_plain_reads(command: str) -> None:
    from test_heartbeat_safe_tools import _shell

    from kiro_crew.slack.gateway import _is_heartbeat_safe_tool

    assert _is_heartbeat_safe_tool(_shell(command))


#: git written with the shell's quoting or escaping, which the shell removes before it
#: looks the program up.
QUOTED_GIT_COMMANDS = [
    'g"it" checkout -b topic',
    "g'it' status",
    "$'git' commit -m wip",
    '"git" add -A',
    "\\git fetch",
    'echo "$(printf ")"; git status)" > out.txt',
]

#: Commands whose ``git`` and separators sit inside quotes, so they are argument text.
QUOTED_PROSE_COMMANDS = [
    'echo "ordinary; git documentation" > notes.txt',
    "echo 'a | git b' > out.txt",
    "echo '$(git log)' > out.txt",
    'printf "%s" "x && git y" > out.txt',
]


@pytest.mark.parametrize("command", QUOTED_GIT_COMMANDS)
def test_command_names_git_reads_the_program_after_quote_removal(command: str) -> None:
    from kiro_crew.security.readonly_bash import command_names_git

    assert command_names_git(command) is True


@pytest.mark.parametrize("command", QUOTED_PROSE_COMMANDS)
def test_command_names_git_keeps_quoted_text_one_word(command: str) -> None:
    from kiro_crew.security.readonly_bash import command_names_git

    assert command_names_git(command) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("command", QUOTED_GIT_COMMANDS)
async def test_quoted_git_is_refused_where_no_approver_exists(command: str) -> None:
    provider = await _resolve_headless(command)
    assert (provider.approved, provider.rejected) == ([], ["r1"])
    assert provider.deny_reasons == [_NO_APPROVER_GIT_REASON]


@pytest.mark.asyncio
@pytest.mark.parametrize("command", QUOTED_PROSE_COMMANDS)
async def test_quoted_git_text_keeps_the_hook_based_default(command: str) -> None:
    assert is_read_only_bash(command) is False
    provider = await _resolve_headless(command)
    assert (provider.approved, provider.rejected) == (["r1"], [])


@pytest.mark.timeout(60)
def test_command_names_git_stays_fast_on_deeply_nested_substitutions() -> None:
    from kiro_crew.security.readonly_bash import command_names_git

    nested = "echo " + "$(echo " * 2000 + "x" + ")" * 2000
    assert command_names_git(nested) is True  # past the nesting bound reads as git
    flat = "echo $(date) " * 20000
    assert command_names_git(flat) is False


#: git at a command position the shell reads past a redirection glued to it, a
#: conditional or loop keyword, or an fd-name redirection.
POSITIONED_GIT_COMMANDS = [
    "git>&2 checkout -b topic",
    "git<msg.txt commit -F -",
    '"git">out.txt commit -m wip',
    "if git checkout -b topic; then echo done; fi",
    "while git fetch; do sleep 1; done",
    "until git pull; do sleep 1; done",
    "coproc git fetch",
    "{fd}>log.txt git commit -m wip",
]

#: Glued redirections and conditionals around commands that are not git.
POSITIONED_PLAIN_COMMANDS = [
    "echo done>notes.txt",
    "cat a.txt>b.txt",
    "grep -c git>count.txt README.md",
    "if test -f a.txt; then echo yes>out.txt; fi",
    "pytest -q 2>&1 > out.txt",
]


@pytest.mark.parametrize("command", POSITIONED_GIT_COMMANDS)
def test_both_git_readers_find_git_past_a_glued_redirection_or_keyword(command: str) -> None:
    from kiro_crew.security.readonly_bash import command_invokes_git, command_names_git

    assert command_names_git(command) is True
    assert command_invokes_git(command) is True


@pytest.mark.parametrize("command", POSITIONED_PLAIN_COMMANDS)
def test_a_glued_redirection_on_a_plain_command_is_not_git(command: str) -> None:
    from kiro_crew.security.readonly_bash import command_names_git

    assert command_names_git(command) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("command", POSITIONED_GIT_COMMANDS)
async def test_positioned_git_is_refused_where_no_approver_exists(command: str) -> None:
    provider = await _resolve_headless(command)
    assert (provider.approved, provider.rejected) == ([], ["r1"])
    assert provider.deny_reasons == [_NO_APPROVER_GIT_REASON]


@pytest.mark.asyncio
@pytest.mark.parametrize("command", POSITIONED_PLAIN_COMMANDS)
async def test_positioned_plain_commands_keep_the_hook_based_default(command: str) -> None:
    assert is_read_only_bash(command) is False
    provider = await _resolve_headless(command)
    assert (provider.approved, provider.rejected) == (["r1"], [])


@pytest.mark.timeout(60)
def test_glued_redirections_stay_linear() -> None:
    from kiro_crew.security.readonly_bash import command_names_git

    assert command_names_git("echo a>b; " * 40000) is False
    assert command_names_git("x>" * 200000) is False


#: git the position walk does not place: inside a ``case`` arm or a function body, or in
#: a runner option that carries a command (``env -S``), or after ``rg --pre``.
COMPOUND_GIT_COMMANDS = [
    "case x in x) git checkout -b topic;; esac",
    "f() { git commit -m wip; }; f",
    "function f { git commit -m wip; }; f",
    'env -S "git commit -m wip"',
    "env --split-string='git checkout -b topic'",
    "env -S'git commit'",
    "rg --pre git pattern .",
    "find . -name '*.md' -exec git add {} +",
    "eval 'v=1 git commit -m wip'",
    'bash -c "v=1 git commit"',
    "echo `echo \\`git commit -m wip\\``",
]

#: ``git`` as text of a program that runs none of its arguments, or a variable value.
INERT_GIT_TEXT_COMMANDS = [
    "grep -rn git docs/",
    "echo 'use git here'",
    "make docs VCS=git",
    "pytest tests/test_git_helpers.py",
]


@pytest.mark.parametrize("command", COMPOUND_GIT_COMMANDS)
def test_command_names_git_reads_git_it_cannot_place_as_git(command: str) -> None:
    from kiro_crew.security.readonly_bash import command_names_git

    assert command_names_git(command) is True


@pytest.mark.parametrize("command", INERT_GIT_TEXT_COMMANDS)
def test_git_as_text_of_an_inert_program_is_not_git(command: str) -> None:
    from kiro_crew.security.readonly_bash import command_names_git

    assert command_names_git(command) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("command", COMPOUND_GIT_COMMANDS)
async def test_compound_git_is_refused_where_no_approver_exists(command: str) -> None:
    provider = await _resolve_headless(command)
    assert (provider.approved, provider.rejected) == ([], ["r1"])
    assert provider.deny_reasons == [_NO_APPROVER_GIT_REASON]


@pytest.mark.timeout(60)
def test_case_arms_stay_linear() -> None:
    from kiro_crew.security.readonly_bash import command_names_git

    assert command_names_git("case x in x) echo hi;; esac; " * 40000) is False
    assert command_names_git("case x in x) echo git;; esac; " * 40000) is True
