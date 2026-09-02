"""Whether a NAME-based approval may be honoured for a shell command line.

Every auto-approve tier that decides from a program NAME -- a session-trusted
pattern (``head *``), a configured ``auto_approve_tools`` glob, the read-only
allowlist -- is making a statement about a PROGRAM. The shell then performs its
own ``PATH`` lookup, and a gateway's ``PATH`` legitimately leads with
directories the agent itself can write (a worktree venv's ``bin``, mise shims,
``~/.local/bin``). A file the agent planted at ``~/.local/bin/head`` therefore
wins the lookup over ``/usr/bin/head``, and a grant made because the command
"is just ``head``" runs it.

:func:`name_grant_refusal` answers the one question those tiers need: does the
name still identify the program it appears to name? A refusal does NOT block the
command and does NOT rewrite it -- the request falls through to the ordinary
interactive approval card, where the human decides on this specific command.
That is the whole point: the tier's job is to skip a prompt the user has already
answered in general, and a shadowed name is a case the user has not answered.

Three shapes are refused, and nothing else:

* **A shadowing resolution.** The name resolves somewhere other than the
  same-named program in the trusted system directories
  (:func:`platform_compat.trusted_system_bin`). ``head`` found at
  ``~/.local/bin/head`` while ``/usr/bin/head`` exists is the reported attack.
* **A resolution inside a tree the agent writes** -- the project checkout, the
  LLM workspace root (:func:`github_runner.agent_writable_roots`), or a
  project-local tool directory (``.venv/bin``, ``node_modules/.bin``). No
  shadowing is needed for this one to be suspicious: it is the same class
  ``github_runner.validate_provider_executable`` and the terminal panel's
  command probe already refuse.
* **A name no approval has identified.** The two rules above cannot help a name
  the system directories do not carry -- ``gh``, ``node``, ``kirocrew``, a
  version manager's ``python`` -- because such a program legitimately lives
  where the user installed it, which is also where the agent can write. For
  those the tiers require a WITNESS: a human answering an approval card has seen
  the command and said yes, and that moment records the file's identity
  (:func:`pin_human_approval`). A grant naming the program is then honoured only
  while the same file answers to the name. No pin means refuse -- pinning on
  first SIGHT would bless whatever is there the first time a tier looks, and a
  tier looks precisely when it is about to auto-approve without asking anyone.

A command carrying a construct whose programs cannot be enumerated -- a
substitution inside quotes, a backtick, a process substitution -- is refused
whole, and so is a program token the shell expands (``$CMD``). Seeing part of a
command's program set is not a basis for vouching for the command.

What this deliberately does NOT do, stated plainly so the boundary is not
mistaken for a stronger one:

* It does not make a decision BINDING on the exec. The check runs when the
  approval is decided and the shell resolves again when it runs, so a second
  agent writing the shim in that window still wins. Closing that needs the
  child's ``PATH`` to stop leading with agent-writable directories, which
  changes the execution environment of every command the agent runs and is a
  separate change with its own compatibility surface (upstream issue #4438
  names it).
* It does not decide that a user-owned directory is untrustworthy. A program
  the user installed into ``~/.local/bin`` is theirs, and refusing it outright
  would leave the auto-approve tiers dead on the most common developer host --
  an unused code path, not a security win. It is admitted on a human's say-so
  and only while it stays the same file, which costs one approval card per
  program (and one more after an upgrade) rather than the whole tier.
* It says nothing about full-trust or YOLO mode, which approve everything by
  construction and are not name-based grants.
* The witness is recorded on the dashboard's approval card. Another surface's
  approval does not pin, so a non-system program there keeps prompting -- more
  prompts, never fewer, which is the safe direction to be incomplete in.

Resolution runs against the same ``PATH`` value the spawn code hands the
child (:func:`env.augmented_path`), not this process's own ``PATH``: the child's
is a superset with the version-manager directories PREPENDED, so resolving
against ours would answer for a search order the command will not use.

Cost is a ``which`` walk plus a handful of ``stat`` calls per decision, on the
same order as ``trusted_system_bin``'s own lookup. The filesystem work runs on
a worker thread via :func:`refusal_for_command_off_loop` — never on the event
loop where the approval is decided; only the constant Windows decline is
answered on-loop, because it needs no filesystem access at all. Building the
search path is cheap wherever it runs because :func:`env.augmented_path` is
string work over a glob that ``env._node_all_bin_dirs`` caches for the process
lifetime, and that cache is already warm: the same call builds the ``PATH``
handed to the agent process at session start, long before any tool approval.

The verdict itself is deliberately uncached: a cached "trusted" answer is a
substitution window, and this must reflect the filesystem as it is when the tier
decides.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import shlex
import shutil
import stat
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable

from kiro_crew import platform_compat
from kiro_crew.env import augmented_path
from kiro_crew.github_runner import agent_writable_roots
from kiro_crew.platform.context import redact_log_via_context

logger = logging.getLogger(__name__)

#: Path segments that mark a directory as PROJECT-LOCAL tooling rather than an
#: installed program. A binary under one of these is writable by whatever can
#: write the project -- which includes the agent -- so a name that resolves into
#: one is not a name a grant can vouch for.
PROJECT_LOCAL_SEGMENTS = frozenset(
    {
        ".venv",
        "venv",
        ".virtualenv",
        "virtualenv",
        "node_modules",
        ".tox",
        ".nox",
        "vendor",
        ".direnv",
        "target",
        "build",
        "dist",
        ".git",
    }
)

#: Tokens after which the NEXT word is a program name rather than an operand.
#: Redirects are deliberately absent: what follows them is a file, and treating
#: it as a program would resolve an operand.
_COMMAND_STARTERS = frozenset({"|", "||", "&&", ";", ";;", "&", "|&", "(", ")", "\n"})

#: Redirection operators, EXACTLY. ``shlex`` groups a run of punctuation into one
#: token, so a composite like ``;(`` or ``;>`` arrives whole -- and a membership
#: test against these two sets is what makes such a token unrecognized instead of
#: silently skipped. ``head x;(payload)`` used to yield only ``head``.
_REDIRECT_OPERATORS = frozenset(
    {"<", ">", ">>", "<<", "<<<", "<&", ">&", "<>", ">|", "&>", "&>>", ">>&"}
)

#: Every character ``shlex`` may hand back as punctuation with
#: ``punctuation_chars=True``. A token made only of these is an operator, and if
#: it is not one of the two sets above it is grammar this walk does not model.
_PUNCTUATION = "();<>|&"


#: Constructs that RUN a program in a position this tokenizer cannot enumerate.
#: ``shlex`` in POSIX mode consumes quotes, so a substitution inside double
#: quotes (``echo "$(head x)"``) collapses into one ordinary token and its inner
#: program disappears from the walk entirely. Refusing the whole command line is
#: the only honest answer: the tier cannot vouch for a program it cannot see.
_UNENUMERABLE = ("$(", "`", "<(", ">(")

#: Characters that make a PROGRAM token something other than a literal name --
#: the shell expands them, so what runs is decided after this check reads it.
_EXPANDING_CHARS = ("$", "`", "*", "?", "[")

#: ``(program name, directory) -> identity`` for the first file each name
#: resolved to. See :func:`_pin_refusal`; bounded so a long-lived gateway cannot
#: accumulate an entry per name it has ever seen.
_PINS: "OrderedDict[tuple[str, str], tuple]" = OrderedDict()
_PIN_LIMIT = 512

#: Guards :data:`_PINS`. Every check runs on a worker thread (the tiers call
#: through ``asyncio.to_thread``), so read-compare-touch has to be one critical
#: section: otherwise a concurrent insert can evict the key a check is holding.
_PIN_LOCK = threading.Lock()

# ── Refusal reasons ──
#
# Each refusal carries a CODE as well as its human detail, because the detail is
# built from the command line and from resolved paths: logging it is a dataflow
# from tool input into a log sink, which CodeQL's
# `py/clear-text-logging-sensitive-data` query reports at high severity (and it
# is right to -- a resolved path discloses more than the user typed). Callers log
# ``Refusal.log_text``, which reads a constant OUT of a table below; that severs
# the flow in a way the analysis can verify, where returning the caller's own
# string after checking it would not.

UNENUMERABLE = "unenumerable_construct"
UNTOKENIZABLE = "untokenizable"
EXPANDED = "expanded_program_token"
RELATIVE_PATH = "relative_path_program"
AGENT_TREE = "agent_writable_tree"
SHADOWED = "shadows_system_program"
IDENTITY_CHANGED = "identity_changed"
UNWITNESSED = "no_approval_identified_this_file"
DISPATCHER = "program_dispatches_another"
AMBIGUOUS_PATH = "search_path_has_a_relative_entry"
WINDOWS_UNMODELLED = "windows_lookup_not_modelled"
UNINSPECTABLE = "uninspectable"
UNKNOWN_COMMAND = "unresolved_command_word"
AMBIGUOUS_ENV = "inherited_env_can_redefine_programs"

_REFUSAL_LOG_TEXT = {
    UNENUMERABLE: "the command carries a construct whose programs cannot be enumerated",
    UNTOKENIZABLE: "the command line could not be tokenized, or uses shell grammar "
    "this check does not model",
    EXPANDED: "a program token is expanded by the shell",
    RELATIVE_PATH: "a program is named by relative path",
    AGENT_TREE: "a program resolves inside a tree the agent can write",
    SHADOWED: "a program name shadows the system program of that name",
    IDENTITY_CHANGED: "a program name resolves to a different file than an approval identified",
    UNWITNESSED: "a non-system program has no file identified by an approval",
    DISPATCHER: "a program runs another program named in its arguments",
    AMBIGUOUS_PATH: "the search path contains an empty or relative entry",
    WINDOWS_UNMODELLED: "Windows tokenization and shell lookup are not modelled",
    UNINSPECTABLE: "a program could not be inspected",
    UNKNOWN_COMMAND: "a command word resolves to no program and is not a known inert builtin",
    AMBIGUOUS_ENV: "the inherited environment can redefine a program name as a shell function",
}


@dataclass(frozen=True)
class Refusal:
    """Why a name-based auto-approve must not be honoured.

    ``detail`` names the program and the paths involved and is meant for the
    person deciding at the approval card. ``log_text`` is the constant to log --
    see the note above on why the two are separate.
    """

    code: str
    detail: str

    @property
    def log_text(self) -> str:
        return _REFUSAL_LOG_TEXT.get(self.code, "a program name could not be vouched for")


#: Shell RESERVED WORDS and grouping tokens. This walk models one grammar --
#: simple commands joined by pipes, ``&&``/``||``/``;`` and subshells -- and a
#: reserved word means the command is using grammar it does NOT model, where the
#: real program hides behind a syntax word: in ``head x | { evil; }`` a walk that
#: reads ``{`` as the program never sees ``evil``. Meeting one in a command
#: position refuses the whole line rather than vouching for what it could see.
#:
#: ``test`` and ``[`` are absent on purpose: those are real programs, not
#: grammar. ``time`` and ``!`` are here because they PREFIX a command, which is
#: the same hiding shape.
_RESERVED_WORDS = frozenset(
    {
        "{",
        "}",
        "!",
        "time",
        "if",
        "then",
        "elif",
        "else",
        "fi",
        "for",
        "while",
        "until",
        "do",
        "done",
        "case",
        "esac",
        "select",
        "function",
        "coproc",
        "[[",
        "]]",
    }
)


def _is_redirect(token: str) -> bool:
    """Whether a token is EXACTLY a redirection operator.

    Exact membership, not "contains ``<`` or ``>``": ``shlex`` groups a run of
    punctuation into one token, so a composite such as ``;>`` both separates
    commands and redirects, and consuming it as a plain redirect would swallow
    the program that follows it.
    """

    return token in _REDIRECT_OPERATORS


def is_project_local(entry: str) -> bool:
    """Whether a path belongs to a project tree rather than an install.

    Segment-wise, not substring: ``/opt/venv-tools/bin`` is an installed prefix
    that merely CONTAINS the text, while ``/home/u/proj/.venv/bin`` genuinely is
    project-local.

    Both separators are honoured regardless of host. ``os.sep`` alone would make
    this silently useless for POSIX-shaped input on Windows (and vice versa),
    and a security filter that quietly stops matching is worse than one that is
    absent, because the tests covering it keep passing on the host that wrote
    them.
    """

    parts = entry.replace("\\", "/").split("/")
    return any(part in PROJECT_LOCAL_SEGMENTS for part in parts)


def _path_is_ambiguous() -> bool:
    """Whether the agent's ``PATH`` contains an entry this check cannot resolve.

    An empty entry (``PATH=/usr/bin:``) and a relative one (``PATH=.:...``) both
    mean "the current directory", and the two processes disagree about which
    directory that is: the check runs in the gateway's, the command runs in the
    session's. Dropping such entries is not enough -- it makes the check resolve
    ``head`` to ``/usr/bin/head`` and vouch for it while the child, which still
    has the entry, runs a planted ``./head`` from its own directory. Since
    neither keeping nor dropping the entry can answer the question, a ``PATH``
    carrying one refuses every name-based auto-approve outright.
    """

    raw = augmented_path(os.environ.get("PATH", ""))
    return any(not entry or not os.path.isabs(entry) for entry in raw.split(os.pathsep))


def _agent_search_path() -> str:
    """The ``PATH`` a spawned agent command searches, absolute entries only.

    Only ever consulted once :func:`_path_is_ambiguous` has answered ``False``,
    so the filter here is belt-and-braces rather than the guarantee.
    """

    raw = augmented_path(os.environ.get("PATH", ""))
    return os.pathsep.join(
        entry for entry in raw.split(os.pathsep) if entry and os.path.isabs(entry)
    )


def _agent_writable_roots() -> tuple[str, ...] | None:
    """Trees the agent itself writes, or ``None`` when that cannot be decided.

    ``None`` is fail-closed at every caller ("assume the path IS inside one"):
    a filter that silently dropped a root it could not resolve would admit
    exactly the trees it exists to refuse.

    Read live rather than cached, because a session can retarget its project
    directory between two tool calls.
    """

    try:
        return tuple(os.path.normcase(str(root)) for root in agent_writable_roots())
    except Exception:
        logger.warning(
            "agent-writable roots unavailable; refusing to honour a name-based "
            "grant until they can be resolved",
            exc_info=True,
        )
        return None


def _within(path: str, roots: tuple[str, ...] | None) -> bool:
    """Whether *path* sits inside one of *roots*, refusing when *roots* is None.

    Compared against ``root + os.sep`` rather than by bare prefix, so a sibling
    that merely starts with the same characters (``…/workspace-other`` next to
    ``…/workspace``) is outside.
    """

    if roots is None:
        return True
    real = os.path.normcase(path)
    return any(real == root or real.startswith(root + os.sep) for root in roots)


def program_names(command: str) -> list[str] | None:
    """Program tokens of *command*, or ``None`` when it cannot be tokenized.

    Every command position is collected -- each stage of a pipeline, each side
    of ``&&``/``||``/``;``, each LINE, and the inside of a subshell -- so a grant
    cannot be honoured on the strength of its first word alone.

    Lines are split before tokenizing because ``shlex`` treats a newline as
    ordinary whitespace: in ``head file\\npayload`` it would hand back
    ``['head', 'file', 'payload']``, leaving ``payload`` in operand position and
    invisible. A newline inside quotes makes its line's quoting unbalanced, which
    tokenizes to ``None`` and refuses -- the safe direction.

    A ``VAR=value`` prefix keeps the position open: it assigns into the
    command's environment, and the program is the token after it.

    ``None`` (unbalanced quotes, an unterminated construct, grammar this walk
    does not model) means the program set could not be established, which callers
    treat as a refusal rather than as "no programs found".
    """

    names: list[str] = []
    for line in command.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not line.strip():
            continue
        found = _program_names_line(line)
        if found is None:
            return None
        names.extend(found)
    return names


def _program_names_line(command: str) -> list[str] | None:
    """:func:`program_names` for a single line."""

    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    # `shlex` DISCARDS THE REST OF THE LINE AFTER `#` BY DEFAULT. Bash does not:
    # `#` only opens a comment at the start of a word, so `head file#x; cat secret`
    # runs BOTH commands, while the default lexer handed this walk `['head',
    # 'file']` and the `cat` was never checked at all. Measured, not assumed --
    # bash prints both halves. Turning commenters off is what makes the token
    # stream describe the command bash will actually run; a genuine trailing
    # comment then arrives as an ordinary operand, which is harmless here because
    # only command POSITIONS are inspected.
    lexer.commenters = ""
    try:
        tokens = list(lexer)
    except ValueError:
        return None
    names: list[str] = []
    expect_program = True
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if not token:
            continue
        if token in _COMMAND_STARTERS:
            expect_program = True
            continue
        # A REDIRECT may appear anywhere in a simple command, INCLUDING BEFORE
        # the program: `2>/dev/null head x` runs `head`. So consume the operator
        # and the file it names, and leave the command position as it was.
        # Closing the position here would make `head` invisible; opening it would
        # judge the FILE in `head x > out`. `2>` and `2>&1` arrive as a digit
        # token followed by the operator, so that fd prefix is consumed too.
        if _is_redirect(token):
            index += 1  # its target, if any
            continue
        if token.isdigit() and index < len(tokens) and _is_redirect(tokens[index]):
            index += 2  # the operator and its target
            continue
        if all(ch in _PUNCTUATION for ch in token):
            # Punctuation that is neither a starter nor a redirect: `shlex` groups
            # a run of it into one token, so `;(` and `;>out` arrive whole and
            # match no operator. Skipping such a token loses the command it
            # introduces, so report "unknown" instead.
            return None
        if not expect_program:
            continue
        if token in _RESERVED_WORDS:
            # Grammar this walk does not model. The program is elsewhere in a
            # shape it cannot follow, so report "unknown" rather than the subset
            # it managed to see.
            return None
        # `VAR=value cmd` assigns into the environment; the program follows it.
        # Only a STRICT `NAME=` prefix is skipped, and only for a variable that
        # does not decide what runs. Anything else carrying `=` in a command
        # position -- `PATH+=:.`, `A[0]=x`, a quoted oddity -- is a state change
        # this walk cannot evaluate, and skipping it would leave the program that
        # follows unchecked, so the line is refused.
        if "=" in token:
            head = token.split("=", 1)[0]
            if _decides_execution(head) or not _ASSIGN_NAME_RE.fullmatch(head):
                return None
            continue
        names.append(token)
        expect_program = False
    return names


#: Programs whose JOB is to run another program named in their arguments. The
#: walk sees ``env head file`` as the single program ``env`` with two operands, so
#: it would vouch for ``/usr/bin/env`` while ``head`` is resolved from ``PATH`` at
#: exec time -- the same substitution the shebang chain closes, reached through
#: argv instead. Each one has its own flag grammar (``env -i -u X CMD``,
#: ``timeout -s KILL 5 CMD``, ``xargs -I{} CMD``), so rather than model them the
#: walk refuses: a grant naming a dispatcher cannot identify what it dispatches.
_DISPATCHERS = frozenset(
    {
        # Command SHELLS. `sh -c 'head file'` runs an arbitrary command string, so
        # vouching for `/bin/sh` says nothing about what executes. Scoped out in
        # round 9 and asked for in round 12: a grant naming a shell is a grant to
        # run anything, which is a decision for the approval card, not for a name
        # check. Interpreters that take CODE (`python3 -c`) are deliberately NOT
        # here -- the read-only tier already restricts them through its own
        # denied-programs list, and listing them would refuse `python3 --version`,
        # which that tier grants on purpose.
        "sh",
        "bash",
        "dash",
        "zsh",
        "ksh",
        "csh",
        "tcsh",
        "ash",
        "fish",
        "busybox",
        "cmd",
        "cmd.exe",
        "powershell",
        "powershell.exe",
        "pwsh",
        "pwsh.exe",
        # Shell BUILTINS that CHANGE HOW A LATER NAME RESOLVES. `export
        # PATH=/agent/bin:$PATH && head file` re-points the lookup for every
        # command after it, and `hash`/`alias` re-point one name directly -- so
        # the resolution this check performs describes the PREVIOUS state, not
        # the one the later command will use. They cannot be evaluated by
        # resolving a name, so a line carrying one is refused. (A bare
        # `PATH=... cmd` PREFIX is caught separately by `_EXEC_ENV_VARS`.)
        "export",
        "hash",
        "alias",
        "unalias",
        "declare",
        "typeset",
        "readonly",
        "local",
        "set",
        "shopt",
        # Builtins that WRITE A VARIABLE from their arguments, so they can set
        # `PATH` without an assignment token: `printf -v PATH /writable; payload`
        # leaves the walk checking `payload` against the previous search path.
        "printf",
        "read",
        "mapfile",
        "readarray",
        "getopts",
        "let",
        # Shell BUILTINS that hand off to a program named in their arguments.
        # These are the sharpest case because `shutil.which` cannot see them at
        # all: an unresolvable name is otherwise treated as "nothing to shadow,
        # nothing to vouch for", so `exec head file` passed while `head` was
        # resolved from PATH at exec time and never inspected.
        "exec",
        "eval",
        "builtin",
        "source",
        ".",
        # External wrappers whose whole job is to run something else.
        "env",
        "nohup",
        "nice",
        "ionice",
        "chrt",
        "stdbuf",
        "unbuffer",
        "setsid",
        "timeout",
        "xargs",
        "command",
        "watch",
        "parallel",
        "sudo",
        "doas",
        "su",
        "runuser",
        "pkexec",
        "systemd-run",
        "script",
        "strace",
        "ltrace",
    }
)


#: Environment variables that, when INHERITED (not written in the command line),
#: make bash run code before the named program and can define a shell FUNCTION
#: that shadows it. `BASH_ENV=/writable/rc` holding `head() { payload; }` means
#: `bash -c 'head file'` runs the function, while this check resolves the name to
#: `/usr/bin/head` and calls it a trusted system program.
#:
#: This is the same threat as the command-line `BASH_ENV=...` prefix that
#: `_EXEC_ENV_VARS` already refuses, arriving through the process environment
#: instead. Neither is visible in the command line, so the check reads its own
#: environment -- the one a child shell inherits by default.
_ENV_PRELOAD_VARS = ("BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS")


#: An EXPORTED SHELL FUNCTION, which shadows a program name directly rather than
#: via a file bash is told to source. Bash exports `head() { payload; }` as an
#: environment entry and re-imports it in the child, so `bash -c 'head file'`
#: runs the function while this check resolves the name to `/usr/bin/head` and
#: calls it a trusted system program -- the same unsoundness as
#: :data:`_ENV_PRELOAD_VARS`, needing no writable file at all.
#:
#: The key is matched on its `BASH_FUNC_` PREFIX alone, deliberately: the suffix
#: has been spelled `()` and `%%` by different bash versions, and pinning a
#: spelling would hand the bypass back on any build that picks another one.
#:
#: The value form is matched too, for the pre-2014 spelling where the key is the
#: bare function name and only the `() {` value marks it. Supported bash no
#: longer imports that, so this is belt-and-braces rather than the live vector.
_BASH_FUNC_KEY_PREFIX = "BASH_FUNC_"
_BASH_FUNC_VALUE_PREFIX = "() {"


def _inherited_preload() -> str | None:
    """The inherited variable that makes a name-based grant unsound, if any."""

    for var in _ENV_PRELOAD_VARS:
        if os.environ.get(var):
            return var
    for key, value in os.environ.items():
        if key.startswith(_BASH_FUNC_KEY_PREFIX) or value.startswith(_BASH_FUNC_VALUE_PREFIX):
            # The FAMILY, not the key: the key embeds an attacker-chosen function
            # name and this string reaches a log sink and the dashboard card.
            return f"{_BASH_FUNC_KEY_PREFIX}*"
    return None


#: Shell builtins that resolve to NO file and are still allowed, because they can
#: neither run a program named in their arguments nor change how a later name
#: resolves. Anything not here that fails to resolve is refused -- see the
#: `not found` branch in :func:`_program_refusal` for why the default is refuse.
#:
#: Deliberately NOT here, and each for a measured reason: `time` and `command`
#: run a program named in their arguments; `trap` and `enable` install code that
#: runs later (`trap 'payload' DEBUG` before every command, `enable -f` loading a
#: shared object as a builtin); `:` is here because it is a true no-op, while
#: `eval`/`exec`/`source`/`.` are the sharpest dispatchers there are.
_INERT_BUILTINS = frozenset(
    {
        ":",
        "cd",
        "echo",
        "pwd",
        "true",
        "false",
        "test",
        "[",
        "wait",
    }
)

#: A STRICT shell assignment name. Anything else carrying `=` in a command
#: position is not a plain `NAME=value` prefix and is refused rather than skipped.
_ASSIGN_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

#: Environment variables whose value in a COMMAND POSITION decides which file
#: runs, or what code runs inside it. `PATH=/writable/bin head file` re-points
#: the very lookup this module vouches for, and the loader variables inject code
#: into whatever does run, so a command carrying one cannot be answered for by
#: resolving its program name. An ordinary assignment (`FOO=bar head x`) is left
#: alone: it changes the program's inputs, not its identity.
_EXEC_ENV_VARS = frozenset(
    {
        "PATH",
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "LD_AUDIT",
        "DYLD_INSERT_LIBRARIES",
        "DYLD_LIBRARY_PATH",
        "DYLD_FRAMEWORK_PATH",
        "BASH_ENV",
        "ENV",
        "SHELL",
        "IFS",
        # A TOOL that runs a helper command named by its environment. The base
        # program is genuinely the trusted system one, so resolving its name
        # answers nothing about what executes: `GIT_SSH_COMMAND=/writable/evil
        # git fetch ssh://x` vouches for `/usr/bin/git` and runs the planted
        # file. Same shape as `LD_PRELOAD`, reached through a tool's own config.
        "GIT_SSH_COMMAND",
        "GIT_SSH",
        "GIT_EXTERNAL_DIFF",
        "GIT_PAGER",
        "GIT_EDITOR",
        "GIT_ASKPASS",
        "SSH_ASKPASS",
        # An INTERPRETER told to load extra code before the script it was given.
        # `PYTHONPATH=/writable python3 x` with a planted `sitecustomize.py`, or
        # `NODE_OPTIONS=--require=/writable/evil node x`, both run attacker code
        # inside a program this check called trusted.
        "NODE_OPTIONS",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "PYTHONHOME",
        "PERL5OPT",
        "PERL5LIB",
        "PERLLIB",
        "RUBYOPT",
        "RUBYLIB",
        "GEM_PATH",
        "GEM_HOME",
        "JAVA_TOOL_OPTIONS",
        "_JAVA_OPTIONS",
        "CLASSPATH",
    }
)

#: Assignment-name FAMILIES that decide what code runs. Named as families rather
#: than as more exact spellings because this is the shape that kept recurring:
#: every round found one more interpreter or tool with its own way of being told
#: to load code, and an exact list can only ever be as complete as the last
#: person to think about it. A loader prefix (``LD_*``, ``DYLD_*``) and the
#: option/search-path suffixes cover the families themselves.
#:
#: Over-refusing is the safe direction and its cost is bounded: an assignment
#: like ``PYTHONUNBUFFERED=1`` or ``MY_CONFIG_PATH=/x`` costs ONE approval
#: prompt, because a refusal here never blocks and never rewrites the command.
_EXEC_ENV_PREFIXES = ("LD_", "DYLD_")
_EXEC_ENV_SUFFIXES = ("_OPTIONS", "OPT", "PATH", "LIB", "_PRELOAD")


def _decides_execution(head: str) -> bool:
    """Whether assigning *head* decides WHAT runs, not merely a program's inputs."""

    return (
        head in _EXEC_ENV_VARS
        or head.startswith(_EXEC_ENV_PREFIXES)
        or head.endswith(_EXEC_ENV_SUFFIXES)
    )


#: Returned by :func:`_shebang_interpreter` for an ``env`` shebang carrying more
#: than one bare name. A sentinel rather than ``None`` or a guess: ``None`` means
#: "no shebang, nothing to follow" and would ALLOW the command, and guessing the
#: interpreter validates the wrong file while vouching for the right one. Not a
#: possible filename, so it cannot collide with a real interpreter.
_COMPLEX_ENV_SHEBANG = "\x00complex-env-shebang"

#: How deep an interpreter chain is followed. A shebang normally names an ELF
#: binary, so one step ends it; two allows for a wrapper script in between.
_INTERPRETER_DEPTH = 2


def _shebang_interpreter(real: str) -> str | None:
    """The interpreter a script hands itself to, or ``None`` for a binary.

    Pinning a SCRIPT binds its bytes, and its bytes may say
    ``#!/usr/bin/env node`` -- which resolves ``node`` from ``PATH`` at exec time,
    exactly the lookup this module exists to distrust. The script can stay
    byte-identical while the program that actually runs is replaced underneath
    it, so the interpreter has to face the same questions the script did.

    Returns the interpreter as written: an absolute path (``/bin/sh``) or a bare
    name when the shebang goes through ``env``. Flags are skipped, so
    ``#!/usr/bin/env -S node --flag`` yields ``node``. ``None`` means there is no
    shebang to follow -- a binary, or a file whose first bytes are not ``#!``.
    """

    if not _is_regular_file(real):
        # Same reason the digest refuses one: opening a FIFO here would block a
        # worker thread in the kernel forever.
        return None
    try:
        with open(real, "rb") as handle:
            first = handle.readline(256)
    except (OSError, ValueError):
        return None
    if not first.startswith(b"#!"):
        return None
    try:
        tokens = first[2:].decode("utf-8", "replace").strip().split()
    except ValueError:  # pragma: no cover - decode with errors= cannot raise
        return None
    if not tokens:
        return None
    interpreter = tokens[0]
    if os.path.basename(interpreter) in ("env", "env.exe"):
        # The `env` BINARY is what the kernel runs, so it decides what executes
        # no matter which name follows it. Reading the name and forgetting the
        # path would validate `node` while `#!~/.local/bin/env node` runs a
        # planted `env` -- and because a pin binds the SCRIPT's bytes, the script
        # keeps matching while the file behind its shebang is swapped. So the
        # path is held to the same standard as any other program: it must BE the
        # system `env`, not merely be spelled like it.
        try:
            real_env = os.path.realpath(interpreter)
        except (OSError, ValueError):
            return _COMPLEX_ENV_SHEBANG
        if not _is_trusted_system_file(os.path.basename(interpreter), real_env):
            return _COMPLEX_ENV_SHEBANG
        # ONLY the bare `#!/usr/bin/env NAME` form is read. `env`'s options take
        # operands (`-u VAR`, `-S 'cmd args'`, `--chdir=DIR`), so picking the
        # first non-flag token mistakes `VAR` for the interpreter -- which is
        # worse than not answering, because it validates the wrong file and
        # vouches for the command. Anything more complex than one bare name is
        # refused by returning a token that cannot resolve.
        rest = tokens[1:]
        if len(rest) == 1 and not rest[0].startswith("-") and "=" not in rest[0]:
            return rest[0]
        return _COMPLEX_ENV_SHEBANG
    return interpreter


#: Read size for the identity digest. The WHOLE file is digested -- this is only
#: the chunk size. An earlier version capped the digest at 1 MiB and hashed a
#: large file's head and tail, which left a middle-only rewrite of a big binary
#: undetected when it also preserved the size and landed inside one ctime tick.
#: Refusing large files instead would have been worse: `node`, `gh` and `docker`
#: are all above any sane cap, and they are exactly what people grant.
_DIGEST_CHUNK = 1 << 20


def _is_regular_file(real: str) -> bool:
    """Whether *real* is a REGULAR file, so reading it cannot block forever.

    A FIFO passes every test that matters to a ``PATH`` lookup -- it exists, it
    can carry the execute bit, it is not a directory -- so ``shutil.which``
    returns it and the digest below would ``open()`` it ``O_RDONLY``, which blocks
    in the kernel until a writer appears. On a worker thread that is permanent:
    an outer timeout can cancel the await but cannot free a syscall-blocked
    thread, so repeating it drains the shared executor and stalls every other
    session's approvals. A character or block device is the same shape. Only a
    regular file is read.
    """

    try:
        return stat.S_ISREG(os.stat(real).st_mode)
    except (OSError, ValueError):
        return False


def _content_digest(real: str, size: int) -> str | None:
    """A digest of ALL of *real*'s bytes, with the size mixed in.

    Metadata alone cannot answer "is this the same program": ``mtime`` and
    ``size`` are both under the writer's control (a same-size rewrite followed by
    ``os.utime`` restores the pair exactly), and while ``st_ctime_ns`` is
    kernel-set and unrestorable, its clock has a tick -- a rewrite inside the
    same tick as the pin leaves it equal, measured on both tmpfs and xfs. So the
    digest is what actually decides, and the metadata rides along to catch the
    cheap cases first.

    The whole file, not a window: hashing only the head and tail left a
    middle-only rewrite of a large binary undetected when it preserved the size
    and landed in one ctime tick. The cost is read bandwidth on an auto-approve
    decision -- for a ~100 MB interpreter roughly a quarter second, page-cached
    after the first pass, and always on a worker thread, never the event loop.
    Only a NON-system program reaches here (a system-resolved one is identified
    by its own directory), so the ordinary read-only allowlist never pays it.
    """

    if not _is_regular_file(real):
        return None
    try:
        digest = hashlib.sha256()
        digest.update(str(size).encode())
        with open(real, "rb") as handle:
            while True:
                chunk = handle.read(_DIGEST_CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()
    except (OSError, ValueError):
        return None


def _identity(real: str) -> tuple | None:
    """A value that changes whenever the file behind a name changes.

    Content first (:func:`_content_digest`), with the metadata that a writer
    cannot restore -- inode, device, and the kernel-set ``st_ctime_ns`` -- as
    corroboration. ``mtime`` and ``size`` are included for completeness but are
    NOT what the guarantee rests on: both are forgeable by the same-uid process
    this pin exists to catch.
    """

    try:
        st = os.stat(real)
    except (OSError, ValueError):
        return None
    digest = _content_digest(real, st.st_size)
    if digest is None:
        # Unreadable, or not a regular file at all (a planted FIFO resolves like
        # a program but cannot be identified as one). Either way there is nothing
        # to pin, and the caller turns this into a refusal.
        return None
    return (
        real,
        digest,
        st.st_mtime_ns,
        st.st_ctime_ns,
        st.st_size,
        st.st_ino,
        st.st_dev,
    )


def _pin_refusal(name: str, found: str, real: str, witness: bool) -> Refusal | None:
    """Vouch for a non-system program only on the file a HUMAN approved.

    The shadowing rule cannot help a name the trusted system directories do not
    carry -- ``gh``, ``node``, ``kirocrew``, a version manager's ``python``.
    Those live where the user installed them, which is also where the agent can
    write, so the name alone says nothing about the file.

    Refusing them outright is not a trade worth taking: it would leave the trust
    tiers dead for most of what a developer actually grants, and a "Trust all gh
    commands" button that never takes effect pushes people to blanket trust. What
    the tiers can require instead is a WITNESS. A human answering an approval
    card has seen the command and said yes to it, so that moment records the
    file's identity (:func:`pin_human_approval`); afterwards a grant naming it is
    honoured only while the same file answers to the name.

    That ordering is the point. Pinning on first SIGHT would bless whatever is
    there the first time a tier looks -- and a tier looks precisely when it is
    about to auto-approve without asking anyone, so a file planted before that
    moment would pin itself. No pin therefore means refuse, not adopt.

    Keyed by ``(name, directory)``, so two projects that ship a same-named tool
    do not invalidate each other; only a swap in place is a mismatch.

    A mismatch does NOT re-pin from a check -- re-pinning there would mean "one
    prompt, then trusted", and this code cannot see whether the human answered
    that prompt with yes. The next human approval re-pins, which is how an
    upgraded tool becomes auto-approvable again.

    Bounded LRU: a long-lived gateway must not accumulate an entry per name it
    has ever seen.
    """

    identity = _identity(real)
    if identity is None:
        return Refusal(UNINSPECTABLE, f"{name} could not be inspected")
    key = (name, os.path.normcase(os.path.dirname(found)))
    # The read, the comparison and the LRU touch are one decision, and this runs
    # on a worker thread per approval -- several at once across sessions. Without
    # the lock a concurrent insert can evict the key between `get` and
    # `move_to_end`, and that `KeyError` would surface as an aborted turn rather
    # than as a refusal. The digest above is deliberately OUTSIDE the lock: it
    # does I/O, and holding a mutex across a file read would serialize every
    # session's approvals behind the slowest disk.
    with _PIN_LOCK:
        pinned = _PINS.get(key)
        if witness:
            # A human just approved this command: record what they approved, and
            # replace a stale entry (an upgraded tool) with it.
            _PINS[key] = identity
            _PINS.move_to_end(key)
            if len(_PINS) > _PIN_LIMIT:
                _PINS.popitem(last=False)
            return None
        if pinned is None:
            return Refusal(
                UNWITNESSED,
                f"{name} at {found} is not a system program and no approval has "
                "identified this file, so a grant naming it cannot be honoured yet",
            )
        if pinned != identity:
            return Refusal(
                IDENTITY_CHANGED,
                f"{name} at {found} is not the file an approval identified earlier, "
                "so a grant made about it no longer identifies it",
            )
        try:
            _PINS.move_to_end(key)
        except KeyError:
            # The lock makes this unreachable through the module's own paths, and
            # it is caught anyway: this sits on the approval path, where an
            # exception aborts the user's turn while a refusal only costs a
            # prompt. The verdict does not depend on the touch -- the identity
            # comparison above already succeeded -- but the safe answer when the
            # pin has vanished is that nothing identifies this file.
            return Refusal(
                UNWITNESSED,
                f"{name} at {found} lost its recorded identity, so a grant naming "
                "it cannot be honoured until an approval identifies it again",
            )
    return None


def _program_refusal(
    name: str, witness: bool = False, depth: int = 0, as_interpreter: bool = False
) -> Refusal | None:
    """Why a name-based grant must not be honoured for *name*, else ``None``.

    ``as_interpreter`` marks the recursive step that judges a script's shebang
    target, and it SKIPS the dispatcher rule. That rule exists because the
    program to run is named in ARGUMENTS this check cannot identify; for a
    shebang the program to run is the pinned script itself, whose bytes were
    verified. So `sh -c 'head file'` on a command line is refused, while
    `#!/bin/sh` atop a script whose identity is pinned is not.
    """

    if any(ch in name for ch in _EXPANDING_CHARS):
        # `$CMD arg`, `./*.sh`: the shell decides what this names after this
        # check has read it, so no grant can identify the program.
        return Refusal(EXPANDED, f"{name} is expanded by the shell rather than naming a program")
    if not as_interpreter and os.path.basename(name) in _DISPATCHERS:
        return Refusal(
            DISPATCHER,
            f"{name} runs a program named in its own arguments, which this check "
            "cannot identify from the command line",
        )
    if "/" in name or (os.sep != "/" and os.sep in name) or (os.altsep and os.altsep in name):
        if not os.path.isabs(name):
            # A relative program is resolved against the command's working
            # directory, which the approval never saw, so no name-based grant
            # can identify what it will run.
            return Refusal(
                RELATIVE_PATH,
                f"{name} names a program by relative path, which the grant cannot identify",
            )
        try:
            real = os.path.realpath(name)
        except (OSError, ValueError):
            return Refusal(UNINSPECTABLE, f"{name} could not be resolved")
        roots = _agent_writable_roots()
        if is_project_local(name) or _within(name, roots) or _within(real, roots):
            return Refusal(AGENT_TREE, f"{name} resolves inside a tree the agent can write")
        if _is_trusted_system_file(os.path.basename(name), real):
            # Spelling the system program's own path out is still the system
            # program; it needs no witness.
            return _interpreter_refusal(name, real, witness, depth)
        dispatched = _dispatcher_target_refusal(name, real, as_interpreter)
        if dispatched is not None:
            return dispatched
        pinned = _pin_refusal(name, name, real, witness)
        if pinned is not None:
            return pinned
        return _interpreter_refusal(name, real, witness, depth)

    found = shutil.which(name, path=_agent_search_path())
    if not found:
        # NOTHING ON THE SEARCH PATH ANSWERS TO THIS NAME, SO IT IS A SHELL
        # BUILTIN (or a typo), AND IT IS REFUSED UNLESS PROVABLY INERT.
        #
        # This branch used to allow every unresolved name, reasoning that there
        # was no shadowed program and so nothing to vouch for. That reasoning is
        # wrong, and it generated a review finding per round for four rounds:
        # `exec`, then `export`, then `set`, then `printf -v`, then `trap
        # 'payload' DEBUG`. A builtin does not need to SHADOW a program to decide
        # what runs -- it IS the mechanism, and `shutil.which` cannot see it at
        # all. Bash has around seventy builtins, so enumerating the dangerous
        # ones was never going to converge; the ALLOWLIST below is the whole
        # inversion, and it is short because very few builtins can neither run a
        # program nor change how a later name resolves.
        #
        # The cost is that an unknown command word now prompts instead of being
        # waved through: a shell function or alias from the user's rc file, and a
        # typo (which would have failed anyway). That is the correct direction for
        # a check whose entire job is to say which file will run.
        if os.path.basename(name) in _INERT_BUILTINS:
            return None
        return Refusal(
            UNKNOWN_COMMAND,
            f"{name} is not a program on the search path, so it is a shell "
            "builtin this check cannot identify or vouch for",
        )
    try:
        real = os.path.realpath(found)
    except (OSError, ValueError):
        return Refusal(UNINSPECTABLE, f"{name} could not be resolved")
    # `is_project_local` reads the location the name was FOUND in, never the
    # symlink target: a real system install can legitimately resolve THROUGH a
    # segment on that list (`/usr/bin/npm` -> `…/node_modules/npm/bin/npm-cli.js`),
    # and judging the target would refuse it. Where the target LEADS is covered
    # by the agent-writable roots below, which compare whole paths instead of
    # guessing from a segment name.
    roots = _agent_writable_roots()
    if is_project_local(found) or _within(found, roots) or _within(real, roots):
        return Refusal(AGENT_TREE, f"{name} resolves inside a tree the agent can write ({found})")
    system = platform_compat.trusted_system_bin(name)
    if system is not None:
        if not _is_trusted_system_file(name, real):
            return Refusal(
                SHADOWED,
                f"{name} resolves to {found}, which shadows the system program at {system}",
            )
        # The system program itself. The name identifies it by construction, so
        # no witness is needed -- this is what keeps coreutils and the read-only
        # allowlist working with no approval history at all.
        return _interpreter_refusal(name, real, witness, depth)
    dispatched = _dispatcher_target_refusal(name, real, as_interpreter)
    if dispatched is not None:
        return dispatched
    pinned = _pin_refusal(name, found, real, witness)
    if pinned is not None:
        return pinned
    return _interpreter_refusal(name, real, witness, depth)


def _dispatcher_target_refusal(name: str, real: str, as_interpreter: bool) -> Refusal | None:
    """Refuse a name whose RESOLVED file is a dispatcher, however it is spelled.

    The check above this one reads the name as WRITTEN, so it catches `env foo`
    and misses an alias for it: an agent plants `runner -> /usr/bin/env`, a human
    approves `runner` once and pins it, and every later `runner <payload>` is
    auto-approved while `env` runs the payload. The dispatcher rule is about the
    FILE's behaviour, so it has to be asked of the file.

    Deliberately reached only AFTER the trusted-system branch. On a BusyBox
    install every coreutils name resolves to `/bin/busybox`, which is a
    dispatcher by basename; those names are already recognised as the system
    program they are, so asking this question later leaves them alone and still
    catches a planted alias, which is never a trusted system file.
    """

    if as_interpreter:
        # A shebang's interpreter runs the pinned script, not a program named in
        # a command line, which is the distinction the caller already draws.
        return None
    if os.path.basename(real) not in _DISPATCHERS:
        return None
    return Refusal(
        DISPATCHER,
        f"{name} resolves to a program that runs whatever its arguments name, "
        "which this check cannot identify from the command line",
    )


def _interpreter_refusal(name: str, real: str, witness: bool, depth: int) -> Refusal | None:
    """Apply the same questions to the interpreter *real* hands itself to.

    Measured on a stock host, the read-only allowlist's own programs are binaries
    except ``egrep`` and ``fgrep``, which are scripts naming ``sh`` by ABSOLUTE
    path -- a trusted system file, so their chain ends immediately and needs no
    witness.
    """

    if depth >= _INTERPRETER_DEPTH:
        return Refusal(
            UNTOKENIZABLE,
            f"{name} chains through more interpreters than this check follows",
        )
    interpreter = _shebang_interpreter(real)
    if interpreter is None:
        return None
    if interpreter == _COMPLEX_ENV_SHEBANG:
        return Refusal(
            UNENUMERABLE,
            f"{name} hands itself to `env` with options, so which interpreter runs "
            "cannot be read off the shebang line",
        )
    return _program_refusal(interpreter, witness=witness, depth=depth + 1, as_interpreter=True)


def _is_trusted_system_file(name: str, real: str) -> bool:
    """Whether *real* IS the trusted system program called *name*."""

    system = platform_compat.trusted_system_bin(name)
    if system is None:
        return False
    try:
        return os.path.normcase(os.path.realpath(system)) == os.path.normcase(real)
    except (OSError, ValueError):
        return False


def pin_human_approval(command: str) -> None:
    """Record the programs in a command a HUMAN just approved.

    This is what makes a later name-based grant honourable for a program the
    trusted system directories do not carry: the person saw this command on the
    approval card and said yes, so the file behind each of its program names is
    the file their decision was about. :func:`_pin_refusal` refuses such a name
    until this has run, and refuses it again once a DIFFERENT file answers to it.

    Call it only on a genuine human answer -- never from an auto-approve path,
    which is the very thing the pin exists to constrain. Failures are swallowed:
    a missing pin costs one prompt, and an approval must not fail because a
    program could not be stat-ed.
    """

    try:
        for name in program_names(command) or []:
            _program_refusal(name, witness=True)
    except Exception:
        logger.debug("could not record approved program identities", exc_info=True)


def name_grant_refusal(command: str) -> Refusal | None:
    """Why *command* may not be auto-approved by NAME, or ``None`` when it may.

    The result is a diagnostic, not a denial: the caller falls through to
    interactive approval, so a refusal costs one prompt and never blocks the
    command. Log ``Refusal.log_text`` (a constant) and show ``Refusal.detail``
    to the person deciding.

    CALL THIS OFF THE EVENT LOOP. It resolves names against ``PATH`` and digests
    the file behind each one, so a stalled network mount or a large binary would
    stall the gateway. The tiers reach it through ``asyncio.to_thread``; there is
    deliberately no cheaper on-loop mode, because a mode that cannot read a file
    cannot answer the question and would only look like it had.

    An empty command returns ``None`` -- there is no name to vouch for, and the
    tiers that call this have already established they have a command.
    """

    if not command.strip():
        return None
    if platform_compat.IS_WINDOWS:
        # FAIL CLOSED ON WINDOWS, for two reasons that compound.
        #
        # Tokenization: POSIX mode reads a backslash as an ESCAPE, so
        # `C:\workspace\tool.exe` arrives as `C:workspacetool.exe`. That is no
        # longer a path, so the path-form branch never runs; it is a bare name
        # that resolves nowhere, and an unresolvable name is otherwise ALLOWED
        # ("nothing to shadow" -- the branch that lets `cd /tmp && ls` work).
        #
        # Resolution: `cmd.exe` searches the CURRENT DIRECTORY before `PATH`,
        # which POSIX shells do not. The directory it searches is the session's,
        # while this check runs in the gateway's, so a planted `find.exe` in the
        # session's work directory wins a lookup this code cannot even see. Add
        # `PATHEXT` and the search order is a second set of semantics to model.
        #
        # Neither is a name the check can identify, so it identifies none of them:
        # on Windows a name-based auto-approve is declined and the request goes to
        # the approval card. That is a functional cost -- Windows users lose
        # auto-approve for shell commands entirely -- and it is the honest state
        # given that this module's own tests are POSIX-only. Modelling the child's
        # working directory and the shell's search order is the fix, and it is its
        # own change with its own tests.
        return Refusal(
            WINDOWS_UNMODELLED,
            "on Windows this check cannot identify which file a program name "
            "runs: the tokenizer cannot preserve a backslash path, and the shell "
            "searches the command's own directory before the search path",
        )
    if _path_is_ambiguous():
        return Refusal(
            AMBIGUOUS_PATH,
            "the agent's search path contains an empty or relative entry, so which "
            "file a program name resolves to depends on a working directory this "
            "check cannot see",
        )
    preload = _inherited_preload()
    if preload is not None:
        # The environment this process passes to a child shell can redefine any
        # program name as a shell function, so resolving the name says nothing
        # about what will run. Refusing every name grant while that is set is the
        # honest answer; it costs auto-approve for a session whose environment
        # carries one of these, which is rare and already unusual.
        return Refusal(
            AMBIGUOUS_ENV,
            f"{preload} is set in the inherited environment, so a shell function "
            "can replace any program this check resolves",
        )
    for construct in _UNENUMERABLE:
        if construct in command:
            # A substitution runs a program in a position the tokenizer cannot
            # reach (POSIX quote handling swallows `"$(head x)"` whole), so the
            # command's program set is not knowable here. Refuse rather than
            # vouch for the part that happens to be visible.
            return Refusal(
                UNENUMERABLE,
                f"the command line contains {construct!r}, whose programs cannot be enumerated",
            )
    names = program_names(command)
    if names is None:
        return Refusal(
            UNTOKENIZABLE,
            "the command line could not be reduced to a known set of program names",
        )
    for name in names:
        refusal = _program_refusal(name)
        if refusal is not None:
            return refusal
    return None


def shell_command_for_event(event: object) -> str | None:
    """The shell command a name-based grant for *event* would be vouching for.

    ``None`` for a non-shell tool or a shell event with no recoverable command:
    there is no program name to vouch for there, and those tiers are a
    different question this module does not answer.

    Duck-typed on ``is_shell`` / ``shell_command`` because every surface's
    permission event carries those two fields, and this module must not import
    a provider type from any of them.
    """

    if not getattr(event, "is_shell", False):
        return None
    command = getattr(event, "shell_command", None)
    if not command:
        return None
    return command


async def refusal_for_command_off_loop(command: str) -> Refusal | None:
    """The ONE place the auto-approve tiers reach the name-grant check.

    It resolves names against ``PATH`` and digests the file behind each one, so
    it runs on a worker thread: the gateway's loop must not stat a stalled
    network mount or read a large binary. Every tier on every surface — the
    dashboard rungs, the task runner, subagents, and the channel turn driver —
    goes through here rather than calling ``asyncio.to_thread`` itself, so
    there is a single place to reason about (and, for the rung tests, a single
    place to stub — three tiers each spawning their own thread is what crashed
    the Windows xdist workers).

    NEVER raises (cancellation excepted). The callers sit inside provider
    event loops where an escaped exception would leave the ACP permission
    request unanswered — a wedged turn, which is strictly worse than either
    verdict. An unexpected failure inside the check is answered as an
    ``UNINSPECTABLE`` refusal: the check could not vouch for the names, so the
    grant is declined and the request takes the surface's normal path. The
    guard lives HERE, at the chokepoint, so every tier inherits it — a guard
    per caller is two copies that drift.

    Windows is answered ON the loop, because there the verdict needs no
    filesystem access at all: the check declines every name-based grant outright
    (neither ``cmd.exe`` search order nor POSIX-mode tokenization is modelled),
    so the thread would do nothing but hand back a constant. Paying a hop for it
    is not merely waste — the worker can outlive a caller's event loop, which is
    what crashes an xdist worker rather than merely failing its test.

    An empty command answers ``None`` — the same deliberate contract as
    :func:`name_grant_refusal`: there is no name to vouch for, and every tier
    that calls this has already established it holds a command
    (:func:`shell_command_for_event` is that pre-filter). A new caller must
    route through :func:`refusal_for_event` rather than passing a value it has
    not established is a command.
    """

    try:
        if not command:
            return None
        if platform_compat.IS_WINDOWS:
            return name_grant_refusal(command)
        return await asyncio.to_thread(name_grant_refusal, command)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("name-grant check failed; declining the grant")
        return Refusal(
            UNINSPECTABLE,
            "the name-grant check itself failed, so no program name can be vouched for",
        )


async def refusal_for_event(event: object) -> Refusal | None:
    """Why a shell *event* may not be auto-approved by program NAME, or ``None``.

    Every auto-approve tier is a statement about a PROGRAM, and the shell
    resolves the name itself afterwards through a ``PATH`` that legitimately
    leads with directories the agent can write. This is the surface-agnostic
    entry point: a surface that honours a name-based grant awaits it at the
    point of honour, and on a refusal DOWNGRADES to its own normal
    non-auto-approve path (interactive card, deny-by-default) — a refusal
    costs one prompt and never blocks the command. The decline-not-raise
    guard lives in :func:`refusal_for_command_off_loop`, so this never raises
    either (cancellation excepted).

    ``None`` for a non-shell tool or an unrecoverable command: there is no
    program name to vouch for, and those tiers are unchanged.
    """

    command = shell_command_for_event(event)
    if command is None:
        return None
    return await refusal_for_command_off_loop(command)


def log_decline(
    *,
    source: str,
    session_key: str,
    event: object,
    refusal: Refusal,
    tier: str,
    sel_factory: Callable[[], Any],
    agent: str = "kirocrew",
    metadata: dict | None = None,
) -> None:
    """Record that a name-based auto-approve was DECLINED, and on which tier.

    Declining is a security decision, so it belongs in the audit log beside the
    approvals and denials. Without it the log shows a command arriving at the
    interactive card (or, headless, at the deny-by-default reject) and never
    says that a grant was withheld, or why. This is the ONE writer for every
    surface, so the disclosure rule below is maintained in one place rather
    than re-implemented per surface.

    The CODE, never the ``detail``: the detail names the program and the
    resolved paths, and an audit sink is exactly where that becomes a
    disclosure. Both ``code`` and ``log_text`` are constants read out of a
    module table. ``event.title`` is model-authored — often the command itself
    for a shell tool — so it passes through the credential and
    exfiltration-URL redactors before reaching the sink.

    Not ``critical=True``. That flag is for audit-or-deny, where a caller must
    refuse rather than run something unaudited. Nothing runs unaudited here:
    declining sends the request to the surface's normal path, whose own answer
    is audited in turn.

    *sel_factory* is REQUIRED: each caller passes its own module-level ``sel``
    binding so that module's audit test seam still observes the row — an
    optional default would let a new surface compile while its decline-audit
    test observes nothing.  *metadata* entries are merged in, with the
    ``reason``/``code``/``tier`` convention keys authoritative.
    """

    md: dict = dict(metadata or {})
    md.update({"reason": "name_grant", "code": refusal.code, "tier": tier})
    title = str(getattr(event, "title", "") or "")
    # Through the CONTEXT so a loaded companion's extra credential regexes apply.
    # This one persists: the title is model-authored and lands in a shared SEL
    # audit row, so a host-specific token shape the OSS baseline does not know
    # would be durable rather than rotating out of a log window. The `_log_`
    # spelling because an audit write must not raise, and because on a process
    # with no composed context the baseline is still the right answer.
    title = redact_log_via_context(title)
    sel_factory().log_tool_invocation(
        session_key=session_key,
        agent=agent,
        source=source,
        tool_name=title,
        tool_kind=str(getattr(event, "tool_kind", "") or ""),
        outcome="auto_approve_declined",
        request_id=getattr(event, "request_id", ""),
        error=refusal.log_text,
        metadata=md,
    )
