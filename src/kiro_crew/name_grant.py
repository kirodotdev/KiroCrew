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
  separate change with its own compatibility surface.
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

On Windows the shell is PowerShell -- kiro-cli's shell tool spawns
``powershell -Command <text>`` there and offers no other shell
(kirodotdev/Kiro#9537) -- so that is the lookup this module models, measured on
Windows PowerShell 5.1 rather than read off cmd.exe's documentation:

* A bare name never searches the working directory. PowerShell requires an
  explicit ``.\\`` prefix for that, so cmd.exe's current-directory lookup does
  not arise in the shell that actually runs the command.
* Session ALIASES and FUNCTIONS resolve before anything on ``PATH``. The default
  alias table is fixed by the PowerShell build (``ls``, ``cat``, ``where``,
  ``sort``, ``curl``, ``sc`` ... all name cmdlets, whatever ``PATH`` holds), and a
  per-user profile script can define any function it likes. So a default alias
  is judged as the built-in it is (:data:`_WINDOWS_INERT_BUILTINS`, else
  refused), and while a per-user profile exists every grant is refused -- the
  same shape as ``BASH_ENV`` on POSIX.
* A FULL cmdlet name of the modules that ship as PowerShell itself is refused
  unless it is inert (:data:`_POWERSHELL_CORE_COMMANDS`). Whether such a name or
  a same-named file on ``PATH`` runs is not a property of the name: measured,
  ``Microsoft.PowerShell.Core`` is loaded always and beats any file, while
  ``.Management``/``.Utility`` are auto-loaded by their first use, so one earlier
  command in the same line flips every later name in that module.
* ``PATH`` is walked directory-major; inside each directory ``.ps1`` is tried
  first, then ``PATHEXT`` in order (:func:`_windows_which`). A hit whose
  extension Windows runs through a registered file association (``.py``,
  ``.js``, ``.vbs`` ...) is refused: the interpreter is chosen by a registry key
  the user can write, not by the file this check can pin.
* A backslash is a path separator, never an escape, and a path is absolute only
  with a drive or UNC prefix -- ``\tool.exe`` and ``C:tool.exe`` are resolved
  against a working directory the approval never saw.
* Module auto-loading is not a shadowing vector BEYOND that cmdlet set: measured,
  an application found on ``PATH`` wins over a command of an auto-loadable module
  (a lone ``Get-NetAdapter`` runs the file), a module is auto-loaded only by one
  of its own commands, and the only names this walk lets past without a refusal
  are inert ones -- which are all Core/Management/Utility, so no permitted prefix
  can load anything else. A name found nowhere is refused anyway.

Cost is a ``which`` walk plus a handful of ``stat`` calls per decision, on the
same order as ``trusted_system_bin``'s own lookup. The filesystem work runs on
a worker thread via :func:`refusal_for_command_off_loop` — never on the event
loop where the approval is decided. Building the search path is cheap wherever
it runs because :func:`env.augmented_path` is string work over a glob that
``env._node_all_bin_dirs`` caches for the process lifetime, and that cache is
already warm: the same call builds the ``PATH`` handed to the agent process at
session start, long before any tool approval.

The verdict itself is deliberately uncached: a cached "trusted" answer is a
substitution window, and this must reflect the filesystem as it is when the tier
decides.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import shutil
import threading
from collections import OrderedDict
from typing import Any, Callable

from kiro_crew import platform_compat
from kiro_crew.env import augmented_path
from kiro_crew.github_runner import agent_writable_roots
from kiro_crew.name_grant_lookup import (  # noqa: F401
    _ASSIGN_NAME_RE,
    _COMPLEX_ENV_SHEBANG,
    _DIGEST_CHUNK,
    _EXEC_ENV_PREFIXES,
    _EXEC_ENV_SUFFIXES,
    _EXEC_ENV_VARS,
    _INTERPRETER_DEPTH,
    _content_digest,
    _decides_execution,
    _identity,
    _is_absolute,
    _is_regular_file,
    _is_trusted_system_file,
    _model_basename,
    _model_is_windows,
    _model_normcase,
    _model_sep_in,
    _program_key,
    _shebang_interpreter,
    _windows_case_insensitive_file,
    _windows_extension_refusal,
    _windows_extensions,
    _windows_which,
)
from kiro_crew.name_grant_refusals import (  # noqa: F401
    _DECLINE_NOTICE_LIMIT,
    _DECLINE_NOTICE_LOCK,
    _DECLINE_NOTICES,
    _PLATFORM_SCOPE_CODES,
    _REFUSAL_LOG_TEXT,
    AGENT_TREE,
    AMBIGUOUS_ENV,
    AMBIGUOUS_PATH,
    BUILTIN_SHADOWS,
    DISPATCHER,
    EXPANDED,
    FILE_ASSOCIATION,
    IDENTITY_CHANGED,
    RELATIVE_PATH,
    SHADOWED,
    UNENUMERABLE,
    UNINSPECTABLE,
    UNKNOWN_COMMAND,
    UNTOKENIZABLE,
    UNWITNESSED,
    WINDOWS_UNMODELLED,
    Refusal,
    _notice_digest,
    platform_scope_notice,
    should_log_decline,
    windows_environment_refusal,
)
from kiro_crew.name_grant_tables import (  # noqa: F401
    _BASH_FUNC_KEY_PREFIX,
    _BASH_FUNC_VALUE_PREFIX,
    _COMSPEC_SCRIPT_EXTENSIONS,
    _DEFAULT_PATHEXT,
    _DISPATCHERS,
    _ENV_PRELOAD_VARS,
    _EXPANDING_CHARS,
    _INERT_BUILTINS,
    _POWERSHELL_CORE_COMMANDS,
    _POWERSHELL_DEFAULT_ALIASES,
    _WINDOWS_DISPATCHERS,
    _WINDOWS_EXPANDING_CHARS,
    _WINDOWS_INERT_BUILTINS,
    _WINDOWS_RUNNABLE_EXTENSIONS,
    MODELLED_WINDOWS_SHELL,
    _inherited_preload,
)
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
#: silently skipped. Without it, ``head x;(payload)`` yields only ``head``.
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

#: PowerShell's BLOCK-COMMENT delimiters, which the walk cannot model. ``shlex``
#: has no notion of ``<# ... #>``, and turning ``commenters`` off does not help:
#: it hands ``<`` and ``>`` back as REDIRECT operators, so each one consumes the
#: token after it as a redirect target. ``echo ok; <# echo #> evil`` therefore
#: reports ``['echo', 'echo']`` and ``<#c#>evil`` reports no names at all, while
#: PowerShell discards the comment and runs ``evil`` -- a program the walk never
#: saw. Windows only: in a POSIX shell ``<#`` opens stdin from a file named
#: ``#`` and ``#> evil`` writes stdout into ``evil``, so nothing runs there that
#: the walk missed, and the two characters keep their redirect meaning.
_WINDOWS_COMMENT_DELIMITERS = ("<#", "#>")

#: ``(program name, directory) -> identity`` for the first file each name
#: resolved to. See :func:`_pin_refusal`; bounded so a long-lived gateway cannot
#: accumulate an entry per name it has ever seen.
_PINS: "OrderedDict[tuple[str, str], tuple]" = OrderedDict()
_PIN_LIMIT = 512

#: Guards :data:`_PINS`. Every check runs on a worker thread (the tiers call
#: through ``asyncio.to_thread``), so read-compare-touch has to be one critical
#: section: otherwise a concurrent insert can evict the key a check is holding.
_PIN_LOCK = threading.Lock()


def environment_refusal() -> Refusal | None:
    """Why NO program name can be vouched for in this environment, or ``None``.

    The refusals that are a property of the ENVIRONMENT rather than of any one
    name: something outside the resolved file decides what a name runs, so
    resolving it describes a file that is not necessarily the one that executes.

    Two callers need exactly this set, which is why it is one helper rather than
    an inline sequence:

    * :func:`name_grant_refusal` returns it, so no grant is honoured while it
      holds.
    * :func:`pin_human_approval` DECLINES TO PIN while it holds. A pin records
      that a human saw a command and said yes to the file behind each of its
      names -- but while a profile function or ``BASH_ENV`` can define that name
      ahead of the file, the thing they approved may not be the file at all.
      Pinning there would bank an identity the approval never established, and
      the pin outlives the environment state: the user removes the profile and a
      name grant then auto-approves an executable no human ever approved.

    ``kirocrew doctor`` reports it too, so the row cannot claim grants are
    satisfiable on a host where every one of them is refused.
    """

    if platform_compat.IS_WINDOWS:
        # PowerShell's lookup is modelled (see the module docstring), but only
        # once the session that will run the command is known not to redefine
        # names first: a per-user profile script runs ahead of the command and
        # can define a function over any program name. Until Windows can say
        # where that script would live, and while one exists, no name can be
        # vouched for.
        windows_refusal = windows_environment_refusal()
        if windows_refusal is not None:
            return windows_refusal
    if _path_is_ambiguous():
        return Refusal(
            AMBIGUOUS_PATH,
            "the agent's search path contains an empty or relative entry, so which "
            "file a program name resolves to depends on a working directory this "
            "check cannot see",
        )
    if not platform_compat.IS_WINDOWS:
        preload = _inherited_preload()
        if preload is not None:
            # The environment this process passes to a child shell can redefine
            # any program name as a shell function, so resolving the name says
            # nothing about what will run. Refusing every name grant while that
            # is set is the honest answer; it costs auto-approve for a session
            # whose environment carries one of these, which is rare and already
            # unusual. (Bash reads these; PowerShell does not, and its
            # equivalent -- the profile -- is the Windows check above.)
            return Refusal(
                AMBIGUOUS_ENV,
                f"{preload} is set in the inherited environment, so a shell function "
                "can replace any program this check resolves",
            )
    return None


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

#: PowerShell KEYWORDS, the Windows counterpart of :data:`_RESERVED_WORDS`. Each
#: either hides the real program behind a syntax word (``foreach ($x in $y) {
#: evil }``, ``try { evil } catch {}``) or changes what runs (``function head {
#: evil }``, ``param``, ``using``). PowerShell names are case-insensitive, so the
#: Windows walk compares lower-cased. Only ever consulted together with
#: :data:`_RESERVED_WORDS`, never instead of it.
_POWERSHELL_KEYWORDS = frozenset(
    {
        "begin",
        "break",
        "catch",
        "class",
        "configuration",
        "continue",
        "data",
        "define",
        "do",
        "dynamicparam",
        "else",
        "elseif",
        "end",
        "enum",
        "exit",
        "filter",
        "finally",
        "for",
        "foreach",
        "from",
        "function",
        "hidden",
        "if",
        "in",
        "inlinescript",
        "param",
        "parallel",
        "process",
        "return",
        "sequence",
        "static",
        "switch",
        "throw",
        "trap",
        "try",
        "until",
        "using",
        "var",
        "while",
        "workflow",
    }
)


#: First characters that mark a PowerShell command position as EXPRESSION-shaped
#: rather than a literal command name. What runs is not a token this walk can
#: read: ``(...)`` and ``$var``/``$env:x`` produce a value the ``&`` operator
#: invokes, and ``{...}`` is a scriptblock whose body runs. A grant naming any
#: fixed name says nothing about the file/value the expression resolves to, so
#: refuse the line rather than vouch for the token that HAPPENED to sit in the
#: position. Consulted only on Windows; a POSIX ``(`` is a subshell whose first
#: word genuinely IS the program that runs, and stays a command starter there.
_POWERSHELL_EXPRESSION_HEADS = ("(", "{", "$")


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

    try:
        return _walk_program_names(command)
    except _Unmodelled:
        return None


class _Unmodelled(Exception):
    """The walk met a part it does not model; ``str()`` names that part."""


#: The two messages ``shlex`` raises, as the person at the approval card reads them.
#: Each names this walk's limit, not a shell rule: the shell lets both span lines.
_SHLEX_REASONS = {
    "No closing quotation": "an unclosed quote; this check tokenizes one line at a time",
    "No escaped character": "a trailing backslash; this check does not follow a line continuation",
}

_SHOWN_LIMIT = 40


def _shown(part: str) -> str:
    """*part* quoted for the approval card, cut so a long token cannot bury the notice.

    Nothing after an ``=`` is shown: an assignment's value can be a secret.
    """

    name, equals, _ = part.partition("=")
    part = name + equals
    return repr(part if len(part) <= _SHOWN_LIMIT else part[: _SHOWN_LIMIT - 3] + "...")


def _walk_program_names(command: str) -> list[str]:
    """:func:`program_names`, raising :class:`_Unmodelled` with the part it gave up on."""

    lines = command.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    numbered = sum(1 for line in lines if line.strip()) > 1
    names: list[str] = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            names.extend(_program_names_line(line))
        except _Unmodelled as exc:
            if not numbered:
                raise
            raise _Unmodelled(f"line {number}: {exc}") from None
    return names


def _program_names_line(command: str) -> list[str]:
    """:func:`program_names` for a single line; raises :class:`_Unmodelled`."""

    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    windows = platform_compat.IS_WINDOWS
    if windows and any(delim in command for delim in _WINDOWS_COMMENT_DELIMITERS):
        # A block comment has to be caught BEFORE tokenizing: `shlex` destroys
        # the delimiters, handing back `<` and `>` as redirect operators that
        # then swallow the real program as a redirect target. Refusing the line
        # is what keeps the walk's output describing the command PowerShell
        # runs, and it closes the pin path too -- `pin_human_approval` reads
        # this same walk, so a name a comment merely mentions is never recorded
        # as the file a human approved.
        raise _Unmodelled("a PowerShell <# ... #> block comment")
    if windows:
        # PowerShell's escape character is the backtick, which `_UNENUMERABLE`
        # refuses wholesale; a backslash is a PATH SEPARATOR. In POSIX mode the
        # lexer would read `C:\workspace\tool.exe` as `C:workspacetool.exe` -- no
        # longer a path, so the path-form branch never runs and a planted file
        # is judged as a bare name. Turning the escape off is what keeps the
        # token the shell will actually resolve. Quotes behave as PowerShell's
        # do: both kinds delimit, and neither is escapable, so a `\"` inside a
        # double-quoted operand ends the string exactly as PowerShell reads it.
        lexer.escape = ""
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
    except ValueError as exc:
        reason = _SHLEX_REASONS.get(str(exc), "text the tokenizer could not split")
        raise _Unmodelled(reason) from None
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
            # PowerShell's call operator (`&`) reads the VALUE of the token that
            # follows -- not its name -- as the program to run: `& (Get-Content
            # .\prog.txt)` runs whatever the file names, `& {calc}` runs the
            # scriptblock's body, and `& $env:COMSPEC` runs the variable's
            # value. What actually runs is not any token this walk inspects, so
            # refuse as grammar this tier does not model. A LITERAL name after
            # `&` (`& git status`) is ordinary and stays fine, and on POSIX
            # `( head file )` opens a subshell whose first word IS the program.
            if (
                windows
                and token == "&"
                and index < len(tokens)
                and tokens[index]
                and tokens[index][0] in _POWERSHELL_EXPRESSION_HEADS
            ):
                raise _Unmodelled(
                    f"the call operator '&' before the expression {_shown(tokens[index])}"
                )
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
            raise _Unmodelled(f"the operator {_shown(token)}")
        if not expect_program:
            continue
        # On Windows, PowerShell's dot-source operator invokes an expression's
        # VALUE in the current scope, just as `&` invokes its value in a new
        # scope: `. (Get-Content .\prog.txt)` reads the file and runs whatever
        # it names. Its expression-shaped target is unmodelled for the same
        # reason. A literal name after `.` (`. myscript.ps1`) is not vouched
        # for here either -- it falls out as an unresolved command word below
        # -- so this branch only closes the expression shapes.
        if (
            windows
            and token == "."
            and index < len(tokens)
            and tokens[index]
            and tokens[index][0] in _POWERSHELL_EXPRESSION_HEADS
        ):
            raise _Unmodelled(
                f"the dot-source operator '.' before the expression {_shown(tokens[index])}"
            )
        # A bare scriptblock (`{...}`) or a variable reference (`$var`,
        # `$env:x`) sitting in a command position dispatches through a VALUE,
        # not a literal name -- PowerShell invokes the scriptblock body or the
        # variable's contents. The single-character `{` also enters through
        # `_RESERVED_WORDS` (which lists `{` and `}` themselves), so this
        # closes the multi-character shapes the reserved-word branch misses --
        # `{calc}`, `$env:COMSPEC`, `$prog` -- rather than being caught only
        # incidentally when the token happens to name nothing that resolves.
        if windows and token[:1] in ("{", "$"):
            raise _Unmodelled(f"the expression {_shown(token)} in a command position")
        if token in _RESERVED_WORDS or (windows and token.lower() in _POWERSHELL_KEYWORDS):
            # Grammar this walk does not model. The program is elsewhere in a
            # shape it cannot follow, so report "unknown" rather than the subset
            # it managed to see.
            raise _Unmodelled(f"the shell keyword {_shown(token)}")
        # `VAR=value cmd` assigns into the environment; the program follows it.
        # Only a STRICT `NAME=` prefix is skipped, and only for a variable that
        # does not decide what runs. Anything else carrying `=` in a command
        # position -- `PATH+=:.`, `A[0]=x`, a quoted oddity -- is a state change
        # this walk cannot evaluate, and skipping it would leave the program that
        # follows unchecked, so the line is refused.
        #
        # PowerShell has no such prefix: `FOO=bar head x` is a command NAMED
        # `FOO=bar`, which no grant identifies, so on Windows every `=` in a
        # command position is refused rather than skipped.
        if "=" in token:
            if windows:
                raise _Unmodelled(f"{_shown(token)}, which PowerShell reads as a command")
            head = token.split("=", 1)[0]
            if _decides_execution(head):
                raise _Unmodelled(f"the assignment {_shown(token)}, which changes what runs")
            if not _ASSIGN_NAME_RE.fullmatch(head):
                raise _Unmodelled(f"the assignment-like word {_shown(token)}")
            continue
        names.append(token)
        expect_program = False
    return names


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
    # On Windows `git`, `Git` and `git.exe` all run the same file, so they must
    # share a pin: keying on the resolved file's own basename folds the optional
    # extension and the case together. On POSIX the name IS the file's name.
    pinned_name = _model_basename(found) if _model_is_windows() else name
    key = (_model_normcase(pinned_name), _model_normcase(os.path.dirname(found)))
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

    windows = platform_compat.IS_WINDOWS
    expanding = _WINDOWS_EXPANDING_CHARS if windows else _EXPANDING_CHARS
    if any(ch in name for ch in expanding):
        # `$CMD arg`, `./*.sh`: the shell decides what this names after this
        # check has read it, so no grant can identify the program.
        return Refusal(EXPANDED, f"{name} is expanded by the shell rather than naming a program")
    if not as_interpreter and _program_key(name) in _DISPATCHERS:
        return Refusal(
            DISPATCHER,
            f"{name} runs a program named in its own arguments, which this check "
            "cannot identify from the command line",
        )
    if not as_interpreter and windows and _program_key(name) in _WINDOWS_DISPATCHERS:
        return Refusal(
            DISPATCHER,
            f"{name} runs a program named in its own arguments, which this check "
            "cannot identify from the command line",
        )
    path_form = (
        _model_sep_in(name)
        # `C:tool.exe` has no separator and is still a path -- relative to the
        # current directory of drive C:, which PowerShell will not resolve as a
        # command and this check must not resolve as a bare name.
        or (windows and ":" in name)
    )
    if path_form:
        if not _is_absolute(name):
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
        if windows:
            association = _windows_extension_refusal(name, real)
            if association is not None:
                return association
        if _is_trusted_system_file(_model_basename(name), real):
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

    if windows:
        # PowerShell resolves aliases and session functions BEFORE `PATH`, so a
        # default alias runs its cmdlet whatever file shares the name. Judge the
        # built-in, not the file: inert ones need no witness (nothing on disk
        # decides what they do), and any other built-in is refused rather than
        # answered for by a file the shell will not run. Case-insensitive, as
        # PowerShell's own lookup is; an explicit extension (`sort.exe`) is not
        # an alias in PowerShell and is not one here.
        alias = name.lower()
        if alias in _WINDOWS_INERT_BUILTINS:
            return None
        if alias in _POWERSHELL_DEFAULT_ALIASES:
            return Refusal(
                BUILTIN_SHADOWS,
                f"{name} is a PowerShell built-in that runs ahead of any program on "
                "the search path, and it is not one this check treats as inert",
            )
        if alias in _POWERSHELL_CORE_COMMANDS:
            # A FULL cmdlet name from the modules that ship as PowerShell itself.
            # Whether it or a same-named file on `PATH` runs depends on what an
            # earlier command in the same line already auto-loaded -- `Core` is
            # loaded always, `Management`/`Utility` from first use -- so it is not
            # decidable from this name alone. Resolving it to a file could vouch
            # for a file the shell will not run, which is the one answer this
            # check must never give.
            return Refusal(
                BUILTIN_SHADOWS,
                f"{name} is a PowerShell cmdlet that can run ahead of any program on "
                "the search path, depending on what the same command line has already "
                "loaded, and it is not one this check treats as inert",
            )
        found = _windows_which(name, _agent_search_path())
    else:
        found = shutil.which(name, path=_agent_search_path())
    if not found:
        # NOTHING ON THE SEARCH PATH ANSWERS TO THIS NAME, SO IT IS A SHELL
        # BUILTIN (or a typo), AND IT IS REFUSED UNLESS PROVABLY INERT.
        #
        # Allowing every unresolved name -- on the reasoning that there is no
        # shadowed program and so nothing to vouch for -- is wrong, and it admits
        # `exec`, `export`, `set`, `printf -v` and `trap 'payload' DEBUG` one at a
        # time. A builtin does not need to SHADOW a program to decide
        # what runs -- it IS the mechanism, and `shutil.which` cannot see it at
        # all. Bash has around seventy builtins, so enumerating the dangerous
        # ones does not converge; the ALLOWLIST below is the whole
        # inversion, and it is short because very few builtins can neither run a
        # program nor change how a later name resolves.
        #
        # The cost is that an unknown command word prompts instead of being
        # waved through: a shell function or alias from the user's rc file, and a
        # typo (which would have failed anyway). That is the correct direction for
        # a check whose entire job is to say which file will run. (On Windows the
        # inert table was consulted above, before the search path, because there
        # the built-in wins even when a file of that name exists.)
        if not windows and _model_basename(name) in _INERT_BUILTINS:
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
    if windows:
        association = _windows_extension_refusal(name, found)
        if association is not None:
            return association
    system = platform_compat.trusted_system_bin(name)
    if system is not None:
        if _is_trusted_system_file(name, real):
            # The system program itself, identified by realpath equality with the
            # trusted-directory copy. The name IS the file by construction, so no
            # witness is needed -- this keeps coreutils and the read-only allowlist
            # working with no approval history at all.
            return _interpreter_refusal(name, real, witness, depth)
        # The name resolves to a file that is NOT the trusted-directory copy, so it
        # shadows the system program. That stays a hard refusal even when the file
        # lives in `/nix/store` and the account cannot write it: on a multi-user Nix
        # install with the default `allowed-users = *`, any user can place bytes in the
        # store (`nix-store --add`), and the entry is then owned by the build user and
        # read-only to this account. Unwritability proves the bytes cannot change in
        # PLACE; it does not prove WHO produced them, and the account still controls the
        # name->store mapping through the profile / PATH symlink the resolved path never
        # walks. So a store copy is never silently vouched for and never offered for a
        # pin -- the refusal names the resolved store path so the person reading the
        # approval card sees exactly which file is shadowing the system program.
        return Refusal(
            SHADOWED,
            f"{name} resolves to {real}, which shadows the system program at {system}",
        )
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
    key = _program_key(real)
    if key in _DISPATCHERS or (_model_is_windows() and key in _WINDOWS_DISPATCHERS):
        return Refusal(
            DISPATCHER,
            f"{name} resolves to a program that runs whatever its arguments name, "
            "which this check cannot identify from the command line",
        )
    return None


def _interpreter_refusal(name: str, real: str, witness: bool, depth: int) -> Refusal | None:
    """Apply the same questions to the interpreter *real* hands itself to.

    Measured on a stock host, the read-only allowlist's own programs are binaries
    except ``egrep`` and ``fgrep``, which are scripts naming ``sh`` by ABSOLUTE
    path -- a trusted system file, so their chain ends immediately and needs no
    witness.
    """

    if platform_compat.IS_WINDOWS:
        # Windows picks the interpreter from the EXTENSION, never from the
        # file's first line: the loader maps an `.exe`, `COMSPEC` runs a `.cmd`
        # (checked in `_windows_extension_refusal`), and PowerShell runs a `.ps1`
        # itself. A `#!` there is a comment -- `npm.ps1` opens with
        # `#!/usr/bin/env pwsh` and PowerShell never reads it -- so following it
        # would judge a program that does not run and refuse one that does.
        return None
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

    Records NOTHING while :func:`environment_refusal` holds. What the human
    approved is then not established to be the file behind the name -- a profile
    function or ``BASH_ENV`` can define that name ahead of it -- and a pin taken
    there outlives the state that made it wrong, so removing the profile would
    turn it into an auto-approve for an executable nobody approved. Costs one
    prompt per program once the environment clears, which is the same price the
    first approval always paid.
    """

    try:
        if environment_refusal() is not None:
            return
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
    environment = environment_refusal()
    if environment is not None:
        return environment
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
    try:
        names = _walk_program_names(command)
    except _Unmodelled as exc:
        return Refusal(
            UNTOKENIZABLE,
            f"the command line could not be reduced to a known set of program names ({exc})",
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

    Windows takes the same thread as every other platform: it resolves names
    and digests files exactly as POSIX does, so an on-loop answer would put that
    I/O where it must never be. There is deliberately no on-loop shortcut for
    any platform.

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
