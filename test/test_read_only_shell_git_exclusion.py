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
    "this command runs git, which runs programs its repository config "
    "names, so it is never run without an approval and this surface has no "
    "approver; grant it in the hook's auto_approve_tools to allow it here"
)


def _unreadable_reason(reason: str) -> str:
    """The refusal for a command whose program cannot be read, for *reason*."""
    return (
        f"this command's program cannot be read ({reason}), so it may run git, which "
        "runs programs its repository config names, so it is never run without an "
        "approval and this surface has no approver; a hook grant cannot lift this, "
        "because the name check refuses the same command; name the program directly, "
        "or run it where an approver exists"
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("posix_model")  # PowerShell reads ``FOO=1`` as a command
@pytest.mark.parametrize(
    "command",
    ["git commit -m wip", "git checkout -b topic", "make && git add -A", "FOO=1 git merge topic"],
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
@pytest.mark.parametrize(
    "command",
    [
        "pytest $(ls tests)",
        "cat <(ls) > out.txt",
        "$EDITOR notes",
        "echo $(git rev-parse HEAD) > out.txt",
        "make `git describe`",
    ],
)
async def test_an_unreadable_program_is_refused_with_its_own_reason(command: str) -> None:
    """A program that cannot be read from the text (an expansion, a substitution)
    may be git, so with no approver it is refused, naming why it cannot be read."""
    from kiro_crew.name_grant import RUNS_GIT
    from kiro_crew.name_grant import no_approver_git_refusal as command_names_git

    reason = command_names_git(command)
    assert reason and reason != RUNS_GIT
    provider = await _resolve_headless(command)
    assert (provider.approved, provider.rejected) == ([], ["r1"])
    assert provider.deny_reasons == [_unreadable_reason(reason)]


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["$EDITOR notes", "/usr/bin/g?t commit -m wip"])
async def test_an_unreadable_program_still_reaches_an_approver_that_exists(command: str) -> None:
    async def _approve(_event: object) -> bool:
        return True

    provider = await _resolve_headless(command, _approve)
    assert (provider.approved, provider.rejected, provider.deny_reasons) == (["r1"], [], [])


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


#: git as the program by name: plain, by path and in any case (macOS and Windows
#: filesystems are case-insensitive, so ``GIT`` runs git there), with a Windows
#: suffix, after a ``VAR=`` prefix, and at each side of a pipe or list.
NAMED_GIT_COMMANDS = [
    "git status",
    "/usr/bin/git status",
    "GIT status",
    "Git.EXE status",
    "git.cmd status",
    "ls && GIT_DIR=x git log",
    "ls | GIT log",
]

#: git written with the shell's quoting or escaping, which the shell removes before
#: it looks the program up.
QUOTED_GIT_COMMANDS = [
    'g"it" checkout -b topic',
    "g'it' status",
    '"git" add -A',
    "\\git fetch",
]

#: git at a command position past a glued redirection.
POSITIONED_GIT_COMMANDS = [
    "git>&2 checkout -b topic",
    "git<msg.txt commit -F -",
    '"git">out.txt commit -m wip',
    "2>/dev/null git status",
]

#: git as the value of an option that names a program, each one read, not only
#: the first: ``rg --pre``, ``find -exec`` (also after an escaped ``\;``).
OPTION_GIT_COMMANDS = [
    "rg --pre git pattern .",
    "rg --pre=git pattern .",
    "rg --pre cat --pre git pattern README.md",
    "find . -name '*.md' -exec git add {} +",
    "find . -exec ls {} \\; -exec git add {} \\;",
    # A redirect glued to the program name is split off, so git is still seen.
    "find . -maxdepth 0 -exec git>/dev/null commit -m wip \\;",
    "find . -execdir git>/dev/null commit -m wip \\;",
    "rg --pre git>/dev/null pattern .",
    "rg --pre=git>/dev/null pattern .",
]

#: Every line is walked, a heredoc body's included, so git after the terminator,
#: on a body line, or after a ``<<`` bash does not open a heredoc for (in a
#: comment, behind a backslash, inside ``${...}`` or ``$[...]``) is read. Leaving a
#: body out of the walk would hide whatever a misread terminator makes a body.
HEREDOC_GIT_COMMANDS = [
    "cat <<EOF\nhi\nEOF\ngit status",
    "cat <<-EOF\n\thi\n\tEOF\ngit status",
    "cat << EOF\nhi\nEOF\ngit status",
    'cat <<"EOF" > out.txt\nhi\nEOF\ngit status',
    "cat <<<hi\ngit status",
    "cat <<'EOF'\ngit status\nEOF",
    "echo hi # <<EOF\ngit commit -m wip",
    "echo a\\<<EOF\ngit commit -m wip\nEOF",
    "echo '<<EOF'\ngit commit -m wip\nEOF",
    "cat <<'E'OF\nhi\nEOF\ngit commit -m wip\nE'OF'",
    "echo ${x:-<<EOF }\ngit commit -m x",
    "echo $[1<<2 ]\ngit commit -m x",
]

#: A quoted or oddly-spaced executable path whose basename is git. A no-break space
#: is not a word break for bash either, so ``./echo\u00a0/git`` is one path to ``git``.
QUOTED_PATH_GIT_COMMANDS = [
    '"./echo project/git" commit -m wip',
    '"/usr/bin/echo /git" status',
    "./echo\u00a0/git commit",
]

#: Commands that run git by name: refused with the git reason.
RUNS_GIT_COMMANDS = (
    NAMED_GIT_COMMANDS
    + QUOTED_GIT_COMMANDS
    + POSITIONED_GIT_COMMANDS
    + OPTION_GIT_COMMANDS
    + HEREDOC_GIT_COMMANDS
    + QUOTED_PATH_GIT_COMMANDS
)

#: A dispatcher runs a program named in its own arguments (``name_grant``'s
#: ``_DISPATCHERS`` plus ``flock``), and each one has its own flag grammar, so the
#: program cannot be read: ``env -u bash git``, ``bash -c -e '...'`` and ``env -S
#: 'sh -c' '...'`` all hide git behind an option this does not model. Refused with
#: that reason, whatever follows, and in any case (macOS and Windows filesystems are
#: case-insensitive, so ``/bin/BASH`` runs bash).
DISPATCHER_COMMANDS = [
    "/bin/BASH -c 'git commit -m wip'",
    "/bin/SH -c 'git commit -m wip'",
    "/usr/bin/ENV git commit -m wip",
    "XARGS git add",
    "xargs git add",
    "timeout 5 git fetch",
    "nice nohup git gc",
    "cmd /c git status",
    "pwsh -Command git status",
    "sh -c 'git commit -m wip'",
    'bash -lc "git commit -m wip"',
    "bash -c -e 'git commit -m wip'",
    "bash -c 'g\\it checkout -b topic'",
    "eval 'v=1 git commit -m wip'",
    "watch -n5 git fetch",
    "flock /tmp/l git commit -m wip",
    "ionice -c3 git gc",
    "parallel git -C {} pull ::: a b",
    'env -S "git commit -m wip"',
    "env -iSgit checkout -b topic",
    "env -u bash git commit -m wip",
    "env -S 'bash -c' 'git commit -m wip'",
    "xargs -I sh git commit -m wip",
    "timeout 60 rg -n git docs/",
    "find . -exec sh -c true {} + -exec git status {} +",
    "rg --pre sh --pre git pattern README.md",
    "bash scripts/build.sh",
    "timeout 60 pytest",
    "bash <<EOF\ngit commit -m wip\nEOF",
    "hash -p /usr/bin/git ls",
]

#: A redirection between a program-valued option and its program hides which word
#: is the program, so it refuses as unreadable.
OPTION_REDIRECT_COMMANDS = [
    "find . -maxdepth 0 -exec </dev/null git commit -m wip \\;",
    "find . -exec 2>&1 git status \\;",
    "find . -exec>/dev/null git status \\;",
    "find . -execdir >/dev/null git commit -m wip \\;",
    "find . -ok </dev/null git status \\;",
    "find . -okdir 2>/dev/null git status \\;",
    "rg --pre </dev/null git pattern .",
    "rg --pre=</dev/null git pattern .",
]

#: Grammar ``program_names`` does not model: a keyword, a function, a line
#: continuation it reads one line at a time.
UNMODELLED_COMMANDS = [
    "if git checkout -b topic; then echo done; fi",
    "while git fetch; do sleep 1; done",
    "coproc git fetch",
    "case x in x) git checkout -b topic;; esac",
    "f() { git commit -m wip; }; f",
    "function f { git commit -m wip; }; f",
    "time make docs VCS=git",
    "if [ -f a.txt ]; then rm a.txt; fi",
    "gi\\\nt commit -m wip",
    # A heredoc terminator continued with a backslash-newline is a continuation too.
    "cat <<EOF\nEO\\\nF\ngit commit -m wip\n",
    "cat <<-EOF\n\tEO\\\n\tF\ngit commit -m wip\n",
]

#: Programs that cannot be read from the text, so they may be git: a dispatcher,
#: unmodelled grammar, a glob, brace list or extglob, an ANSI-C quote or an
#: expansion as the program, or a command or process substitution anywhere (single
#: quotes included: the check is on the raw text, so a quoted ``$(`` refuses too,
#: the safe side), or quoting that cannot be resolved. Each refuses with its own
#: reason, not the git one.
UNREADABLE_COMMANDS = (
    DISPATCHER_COMMANDS
    + OPTION_REDIRECT_COMMANDS
    + UNMODELLED_COMMANDS
    + [
        "{fd}>log.txt git commit -m wip",
        'cat <<< "$(git status --porcelain)"',
        "cat < `git rev-parse --git-path x`",
        "/usr/bin/g?t checkout -b t",
        "/usr/bin/[g]it commit -m wip",
        "{g,}it commit -m wip",
        "bash -O extglob -c '/usr/bin/+(gi)t checkout -b topic'",
        "$'\\x67it' commit -m wip",
        "$'git' commit -m wip",
        "${x}t status",
        "$EDITOR notes",
        "G=git; $G status",
        "echo $(git status)",
        "echo $(date)",
        'echo "$(printf ")"; git status)" > out.txt',
        "echo ${a['$(git checkout -b topic)']}",
        "echo `echo \\`git commit -m wip\\``",
        "printf -v 'a[$(git commit -m wip)]' %s foo",
        "echo '$(git log)' > out.txt",
        "cat <(ls) > out.txt",
        "cat <<EOF\n$(git status)\nEOF",
        # A heredoc body is walked, so its text counts even behind a quoted delimiter.
        "cat > a.sh <<'EOF'\n$(git status)\nEOF",
        "cat > a.py <<'EOF'\nif x:\n    pass\nEOF",
        "echo ready # <<'EOF'\n$(git commit -m wip)",
        "echo 'unterminated",
    ]
)

#: git only as an argument, or a word that merely contains the letters: the
#: program is not git, so the command keeps the surface's default.
MENTIONS_GIT_COMMANDS = [
    "grep digit README.md",
    "make docs VCS=git",
    "rg -n git docs/",
    "pytest tests/test_git_helpers.py",
    "C:\\tools\\rg.exe -n git docs",
    "GREP.EXE -c git README.md",
    "findstr /n git README.md",
    "grep -rn git docs/",
    "echo 'use git here'",
    "cat .gitignore",
    "ls git-notes",
    'echo "ordinary; git documentation" > notes.txt',
    "echo 'a | git b' > out.txt",
    'cat "x && git y" > out.txt',
    "grep -c git>count.txt README.md",
    "touch legit.txt digital.txt",
]

#: Commands with no git program, so they keep the surface's default.
KEEPS_DEFAULT_COMMANDS = MENTIONS_GIT_COMMANDS + [
    "echo done>notes.txt",
    "cat a.txt>b.txt",
    "test -f a.txt && echo yes>out.txt",
    "pytest -q 2>&1 > out.txt",
    "ls -la",
    "cat README.md",
    "find . -name x -exec touch {} +",
    "echo ${HOME}",
    # A glob, brace list, expansion or JSON payload outside the program position:
    # the program is readable and is not git.
    "rm *.tmp",
    "pytest tests/test_*.py",
    "echo {a,b}",
    'curl -d \'{"a":1,"b":2}\' http://localhost:8000/x',
    "[ -f a.txt ] && rm a.txt",
    'grep "(foo" file.txt',
    # A heredoc body is walked, so one whose lines read as plain programs passes.
    "cat > notes.md <<'EOF'\nhello world\nEOF",
    # Builtins that run no named program are not dispatchers.
    "export FOO=1 && make",
    "source .venv/bin/activate && pytest",
    "set -e; make test",
    "printf 'hi\\n' > out.txt",
]

#: The same commands read by the Windows model, which has no backslash escape, so a
#: path to ``git.exe`` keeps its separators.
WINDOWS_GIT_COMMANDS = [
    "C:\\tools\\git.exe status",
    '"C:\\Program Files\\Git\\bin\\git.exe" status',
    "GIT.EXE status",
]


@pytest.fixture
def posix_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Read the shared rows as the POSIX shell model on every host: the Windows model
    is PowerShell's, which reads ``VAR=`` prefixes and backslashes differently and
    has its own rows below."""
    from kiro_crew import platform_compat

    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)


@pytest.mark.parametrize("command", WINDOWS_GIT_COMMANDS)
def test_the_windows_model_reads_a_git_exe_path_as_git(
    command: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew import platform_compat
    from kiro_crew.name_grant import RUNS_GIT, no_approver_git_refusal

    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    assert no_approver_git_refusal(command) == RUNS_GIT
    assert no_approver_git_refusal("findstr /n git README.md") == ""


@pytest.mark.usefixtures("posix_model")
@pytest.mark.parametrize("command", RUNS_GIT_COMMANDS)
def test_command_names_git_reads_git_as_the_program(command: str) -> None:
    from kiro_crew.name_grant import RUNS_GIT
    from kiro_crew.name_grant import no_approver_git_refusal as command_names_git

    assert command_names_git(command) == RUNS_GIT


@pytest.mark.usefixtures("posix_model")
@pytest.mark.parametrize("command", UNREADABLE_COMMANDS)
def test_command_names_git_names_why_a_program_cannot_be_read(command: str) -> None:
    from kiro_crew.name_grant import RUNS_GIT
    from kiro_crew.name_grant import no_approver_git_refusal as command_names_git

    reason = command_names_git(command)
    assert reason and reason != RUNS_GIT


@pytest.mark.usefixtures("posix_model")
@pytest.mark.parametrize("command", KEEPS_DEFAULT_COMMANDS)
def test_command_names_git_passes_a_command_whose_program_is_not_git(command: str) -> None:
    from kiro_crew.name_grant import no_approver_git_refusal as command_names_git

    assert command_names_git(command) == ""


@pytest.mark.asyncio
@pytest.mark.usefixtures("posix_model")
@pytest.mark.parametrize("command", RUNS_GIT_COMMANDS)
async def test_a_command_that_runs_git_is_refused_where_no_approver_exists(
    command: str,
) -> None:
    provider = await _resolve_headless(command)
    assert (provider.approved, provider.rejected) == ([], ["r1"])
    assert provider.deny_reasons == [_NO_APPROVER_GIT_REASON]


@pytest.mark.asyncio
@pytest.mark.usefixtures("posix_model")
@pytest.mark.parametrize("command", UNREADABLE_COMMANDS)
async def test_a_command_whose_program_cannot_be_read_is_refused_where_no_approver_exists(
    command: str,
) -> None:
    from kiro_crew.name_grant import no_approver_git_refusal as command_names_git

    provider = await _resolve_headless(command)
    assert (provider.approved, provider.rejected) == ([], ["r1"])
    assert provider.deny_reasons == [_unreadable_reason(command_names_git(command))]


@pytest.mark.asyncio
@pytest.mark.usefixtures("posix_model")
@pytest.mark.parametrize("command", KEEPS_DEFAULT_COMMANDS)
async def test_a_command_whose_program_is_not_git_keeps_the_hook_based_default(
    command: str,
) -> None:
    provider = await _resolve_headless(command)
    assert (provider.approved, provider.rejected) == (["r1"], [])


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["GIT status", "/usr/bin/git status"])
async def test_git_by_name_still_reaches_an_approver_that_exists(command: str) -> None:
    async def _approve(_event: object) -> bool:
        return True

    provider = await _resolve_headless(command, _approve)
    assert (provider.approved, provider.rejected, provider.deny_reasons) == (["r1"], [], [])


@pytest.mark.timeout(60)
@pytest.mark.usefixtures("posix_model")
def test_command_names_git_stays_linear() -> None:
    from kiro_crew.name_grant import RUNS_GIT
    from kiro_crew.name_grant import no_approver_git_refusal as command_names_git

    assert command_names_git("echo $(echo " * 20000 + "x" + ")" * 20000)
    assert command_names_git("echo hi; " * 40000) == ""
    assert command_names_git("x>" * 200000) == ""
    assert command_names_git("echo a; " * 40000 + "git status") == RUNS_GIT
    assert command_names_git("ls " + "a " * 200000) == ""
    assert command_names_git("find . " + "-exec a " * 100000) == ""
