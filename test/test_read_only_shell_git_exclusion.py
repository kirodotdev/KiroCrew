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
