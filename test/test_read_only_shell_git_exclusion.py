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


async def _resolve_headless(
    command: str, approver: object = None, *, shell: bool = True, mcp_server: str = ""
) -> _RecordingProvider:
    """Drive ``_resolve_permission`` as a hook-based surface with no approver
    (a Meetings dispatch) unless *approver* is given.

    ``shell=False`` sends the command the way the DeepSeek harness does: a
    ``bash`` tool streamed as kind ``other``, so no shell command is recovered
    and the command is only the tool's own ``command`` argument."""
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
        title=f"Running: {command}" if shell else "bash",
        request_id="r1",
        tool_input=json.dumps({"command": command}),
        is_shell=shell,
        tool_kind="execute" if shell else "other",
        raw_tool_params={"command": command},
        mcp_server_name=mcp_server,
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
    "this command may run git, which runs programs its repository config "
    "names, so it is never run without an approval and this surface has no "
    "approver; grant it in the hook's auto_approve_tools to allow it here"
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
@pytest.mark.parametrize("command", ["git commit -m wip", "git status", "make && git add -A"])
async def test_a_git_command_sent_as_a_non_shell_kind_is_refused_where_no_approver_exists(
    command: str,
) -> None:
    """A harness that streams its shell tool as kind ``other`` (DeepSeek) gives no
    recovered shell command; the tool's own ``command`` argument is judged instead."""
    provider = await _resolve_headless(command, shell=False)
    assert (provider.approved, provider.rejected) == ([], ["r1"])
    assert provider.deny_reasons == [_NO_APPROVER_GIT_REASON]


@pytest.mark.asyncio
async def test_a_non_git_command_sent_as_a_non_shell_kind_keeps_the_default() -> None:
    provider = await _resolve_headless("touch notes.txt", shell=False)
    assert (provider.approved, provider.rejected) == (["r1"], [])


@pytest.mark.asyncio
async def test_an_mcp_tool_command_argument_is_not_read_as_shell_text() -> None:
    """A call that names its MCP server is governed by ``@server/tool`` rules; its
    ``command``-named argument is not shell text."""
    provider = await _resolve_headless("git commit -m wip", shell=False, mcp_server="tasks")
    assert (provider.approved, provider.rejected) == (["r1"], [])


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


@pytest.mark.parametrize(
    ("command", "names_git"),
    [
        ("git status", True),
        ("ls && GIT_DIR=x git log", True),
        ("echo $(git status)", True),
        ("echo $(date)", False),
        ("$EDITOR notes", False),
        ("ls git-notes", True),
        ("xargs git add", True),
        ("timeout 5 git fetch", True),
        ("timeout --signal KILL 5 git fetch", True),
        ("sudo -u builder env -i PATH=/usr/bin git push", True),
        ("nice nohup git gc", True),
    ],
)
def test_command_names_git_reads_a_git_word(command: str, names_git: bool) -> None:
    from kiro_crew.security.readonly_bash import command_names_git

    assert command_names_git(command) is names_git


#: git written with the shell's quoting or escaping, which the shell removes before it
#: looks the program up, or as a Windows path.
QUOTED_GIT_COMMANDS = [
    'g"it" checkout -b topic',
    "g'it' status",
    "$'git' commit -m wip",
    '"git" add -A',
    "\\git fetch",
    'echo "$(printf ")"; git status)" > out.txt',
    "bash -c 'g\\it checkout -b topic'",
    "eval 'g\\it commit -m wip'",
    "echo ${a['$(git checkout -b topic)']}",
    "C:\\tools\\git.exe status",
    "gi\\\nt commit -m wip",
    "bash -c 'g\\\nit checkout -b topic'",
]

#: git at a command position past a glued redirection, a conditional or loop keyword,
#: or an fd-name redirection.
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

#: git inside a ``case`` arm or a function body, in a runner option that carries a
#: command (``env -S``), after ``rg --pre`` or ``find -exec``, in an ``eval`` or
#: ``bash -c`` script, a nested escaped backtick, or a ``printf -v`` destination
#: whose array subscript Bash evaluates.
COMPOUND_GIT_COMMANDS = [
    "case x in x) git checkout -b topic;; esac",
    "f() { git commit -m wip; }; f",
    "function f { git commit -m wip; }; f",
    'env -S "git commit -m wip"',
    "env --split-string='git checkout -b topic'",
    "env -S'git commit'",
    "env -iSgit checkout -b topic",
    "env -ivSgit commit -m wip",
    "rg --pre git pattern .",
    "find . -name '*.md' -exec git add {} +",
    "eval 'v=1 git commit -m wip'",
    'bash -c "v=1 git commit"',
    "echo `echo \\`git commit -m wip\\``",
    "printf -v 'a[$(git commit -m wip)]' %s foo",
]

#: Commands that only mention git but are more than one simple command of a program
#: that runs none of its arguments: a pipe, a runner, a redirection, or a separator
#: inside quotes. Refused on purpose, as the fail-closed cost; a hook grant lifts it.
MENTIONS_GIT_COMMANDS = [
    "find . -name '*.md' | xargs grep -ln git",
    "xargs -a files.txt grep -n 'git'",
    "timeout 60 rg -n git docs/",
    "nice -n 10 grep -rn git src/",
    "env LC_ALL=C grep -c git CHANGELOG.md",
    'echo "ordinary; git documentation" > notes.txt',
    "echo 'a | git b' > out.txt",
    "echo '$(git log)' > out.txt",
    'cat "x && git y" > out.txt',
    "grep -c git>count.txt README.md",
    "findstr /n git README.md",
]

#: A quoted or oddly-spaced executable path pointing at git, and Unicode whitespace
#: before a git word: the word-and-program parsing the old reader relied on is gone,
#: so git anywhere in the text refuses. Each slipped past the reader on f13a81b9 (the
#: quotes split the path and its program read as an inert ``echo``, or ``str.split``
#: broke the command at the Unicode space).
QUOTED_PATH_GIT_COMMANDS = [
    '"./echo project/git" commit -m wip',
    '"/usr/bin/echo /git" status',
    "./echo\u00a0/git commit",
]

#: Spellings whose letters the shell writes itself: an ANSI-C ``$'...'`` escape, a
#: glob or a brace list that expands to ``git``. No ``git`` is in the text, so each
#: slipped past the plain substring check; they refuse whatever they spell.
SHELL_WRITTEN_GIT_COMMANDS = [
    "$'\\x67it' commit -m wip",
    "$'\\147it' checkout -b topic",
    "/usr/bin/g?t checkout -b t",
    "/usr/bin/gi* commit -m wip",
    "/usr/bin/[g]it commit -m wip",
    "{g,}it commit -m wip",
    "bash -O extglob -c '/usr/bin/+(gi)t checkout -b topic'",
    "bash -O extglob -c '/usr/bin/@(g|gi)t checkout -b topic'",
    "bash -O extglob -c '/usr/bin/!(x)it commit -m wip'",
    "{g,'--namespace=a b'}it status",
    '{g,"--namespace=a b"}it status',
    "{g,--namespace=a\\ b}it status",
    'bash -c "$""\'\\x67it\' status"',
    "bash -c \"\\$'\\x67it' status\"",
    # An empty quote pair between an extglob's operator and its ``(``, which the
    # shell joins away; slipped past on 053bee62, where the extglob test read only
    # the text as written.
    'bash -O extglob -c "/usr/bin/@""(g|gi)t commit -m wip"',
    'bash -O extglob -c "/usr/bin/+""(gi)t checkout -b topic"',
    "bash -O extglob -c '/usr/bin/@''(g|gi)t commit -m wip'",
    "bash -O extglob -c '/usr/bin/+''(gi)t checkout -b topic'",
]

#: ALLOW cases that flip to refused with the fail-closed text check but are NOT a
#: proven read, so they reach the no-approver git check and are refused there. Allowed
#: on f13a81b9 (its word reader saw an inert program or no bare ``git`` word); now git
#: anywhere in the text refuses. Accepted cost on the no-approver surface, lifted by a
#: hook grant. ``findstr`` already sits in MENTIONS above.
NONREAD_MENTIONS_GIT_COMMANDS = [
    "rg -n git docs/",
    "make docs VCS=git",
    "time make docs VCS=git",
    "pytest tests/test_git_helpers.py",
    "grep.exe -rn git docs\\",
    "C:\\tools\\rg.exe -n git docs",
    "GREP.EXE -c git README.md",
]

#: git mentioned in a PROVEN read that invokes no git (``cat .gitignore``, ``grep
#: digit``). ``command_names_git`` refuses these in isolation, but the read-only tier
#: auto-approves them BEFORE the no-approver git check, so the resolver keeps the
#: default: the fail-closed cost is not actually paid for a proven read.
READ_MENTIONS_GIT_COMMANDS = [
    "grep -rn git docs/",
    "echo 'use git here'",
    "cat .gitignore",
    "grep digit README.md",
    "ls git-notes",
]

#: Every command whose text mentions git: ``command_names_git`` refuses all of them.
MENTIONS_ANY_GIT = (
    QUOTED_PATH_GIT_COMMANDS + NONREAD_MENTIONS_GIT_COMMANDS + READ_MENTIONS_GIT_COMMANDS
)

#: Commands the no-approver resolver refuses: they reach the git check (not a proven
#: read auto-approved before it).
REFUSED_COMMANDS = (
    QUOTED_GIT_COMMANDS
    + POSITIONED_GIT_COMMANDS
    + COMPOUND_GIT_COMMANDS
    + MENTIONS_GIT_COMMANDS
    + QUOTED_PATH_GIT_COMMANDS
    + NONREAD_MENTIONS_GIT_COMMANDS
    + SHELL_WRITTEN_GIT_COMMANDS
)

#: Commands with no ``git`` anywhere in their text, so they keep the surface's default.
KEEPS_DEFAULT_COMMANDS = [
    "echo done>notes.txt",
    "cat a.txt>b.txt",
    "if test -f a.txt; then echo yes>out.txt; fi",
    "pytest -q 2>&1 > out.txt",
    "ls -la",
    "cat README.md",
    "find . -name x -exec touch {} +",
    "echo ${HOME}",
]


@pytest.mark.parametrize("command", REFUSED_COMMANDS + READ_MENTIONS_GIT_COMMANDS)
def test_command_names_git_refuses_any_text_that_mentions_git(
    command: str,
) -> None:
    from kiro_crew.security.readonly_bash import command_names_git

    assert command_names_git(command) is True


@pytest.mark.parametrize("command", KEEPS_DEFAULT_COMMANDS)
def test_command_names_git_passes_text_with_no_git(command: str) -> None:
    from kiro_crew.security.readonly_bash import command_names_git

    assert command_names_git(command) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("command", REFUSED_COMMANDS)
async def test_a_command_that_may_run_git_is_refused_where_no_approver_exists(
    command: str,
) -> None:
    provider = await _resolve_headless(command)
    assert (provider.approved, provider.rejected) == ([], ["r1"])
    assert provider.deny_reasons == [_NO_APPROVER_GIT_REASON]


@pytest.mark.asyncio
@pytest.mark.parametrize("command", KEEPS_DEFAULT_COMMANDS)
async def test_text_with_no_git_keeps_the_hook_based_default(
    command: str,
) -> None:
    provider = await _resolve_headless(command)
    assert (provider.approved, provider.rejected) == (["r1"], [])


@pytest.mark.asyncio
@pytest.mark.parametrize("command", READ_MENTIONS_GIT_COMMANDS)
async def test_a_proven_read_that_mentions_git_still_auto_approves(
    command: str,
) -> None:
    """``command_names_git`` refuses these in isolation, but the read-only tier
    auto-approves a proven read that runs no git before the no-approver git check,
    so the fail-closed cost is not paid for a real read (``cat .gitignore``)."""
    provider = await _resolve_headless(command)
    assert (provider.approved, provider.rejected) == (["r1"], [])


@pytest.mark.timeout(60)
def test_command_names_git_stays_linear() -> None:
    from kiro_crew.security.readonly_bash import command_names_git

    assert command_names_git("echo $(echo " * 20000 + "x" + ")" * 20000) is False
    assert command_names_git("case x in x) echo hi;; esac; " * 40000) is False
    assert command_names_git("x>" * 200000) is False
    assert command_names_git("echo a; " * 40000 + "git status") is True


@pytest.mark.timeout(60)
def test_command_names_git_is_linear_on_an_unclosed_brace_run() -> None:
    """A ``{`` then a long run of commas with no ``}``: a backtracking brace pattern
    is quadratic here, so 2,000,000 commas would not finish inside the lost-run
    ceiling; the substring test reads each character a fixed number of times."""
    from kiro_crew.security.readonly_bash import command_names_git

    assert command_names_git("echo {" + "," * 2_000_000) is False
    assert command_names_git("echo {" + "," * 2_000_000 + "}") is True
