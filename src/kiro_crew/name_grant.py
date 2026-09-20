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
import hashlib
import logging
import ntpath
import os
import posixpath
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
FILE_ASSOCIATION = "windows_file_association"
BUILTIN_SHADOWS = "powershell_builtin_precedes_program"
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
    WINDOWS_UNMODELLED: "the Windows shell's lookup inputs could not be established",
    FILE_ASSOCIATION: "a program resolves to a file Windows runs through a registered "
    "file association",
    BUILTIN_SHADOWS: "PowerShell resolves the name to a built-in command before the search path",
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

    ``dedupe_key`` is an optional fingerprint of the ENVIRONMENT STATE this
    refusal reads. When set, :func:`should_log_decline` collapses a repeated
    line for the same session AND the same fingerprint down to one warning: a
    profile that does not change between commands says the same thing about
    every one of them, and burying real per-invocation refusals under that
    repeated line is the noise :func:`should_log_decline` exists to prevent.
    The fingerprint is part of the key, not the code, so a mid-session change
    -- a profile is edited, or removed -- produces a fresh line rather than
    silence. Never used by :func:`log_decline`: the SEL audit row is written
    per invocation whatever the fingerprint says.
    """

    code: str
    detail: str
    dedupe_key: str | None = None

    @property
    def log_text(self) -> str:
        return _REFUSAL_LOG_TEXT.get(self.code, "a program name could not be vouched for")


#: Refusal codes that describe the PLATFORM rather than the command. Every other
#: code is a fact about the line that was run -- this name shadows a system
#: program, that file is not the one an approval identified -- so it is worth
#: saying every time it happens. A platform-scope code says the same thing about
#: every command a session will ever run, so repeating it per invocation buries
#: the per-command refusals it sits among and reads like a misconfiguration the
#: user could fix.
#:
#: Only ``WINDOWS_UNMODELLED`` qualifies today, and only in the one state that
#: still produces it: Windows could not say where the user's Documents folder
#: is, so whether a PowerShell profile runs before the command cannot be
#: established (:func:`windows_environment_refusal`). ``AMBIGUOUS_PATH`` and
#: ``AMBIGUOUS_ENV`` -- the latter also covering a PowerShell profile that
#: EXISTS -- are near-misses that are deliberately NOT here: both are
#: environment state a user can change mid-session, so a later invocation can
#: legitimately answer differently and each line is a fresh fact.
_PLATFORM_SCOPE_CODES = frozenset({WINDOWS_UNMODELLED})

#: ``(session bucket, code) -> None`` for platform-scope declines already logged.
#: Bounded like :data:`_PINS` so a long-lived gateway cannot accumulate an entry
#: per session it has ever served. An eviction costs one extra log line for a
#: session that comes back after 512 others, which is the right way to be wrong.
#: Every variable-length element of the key is held as its :func:`_notice_digest`
#: rather than verbatim, so the retained SIZE is bounded as well as the entry
#: count.
_DECLINE_NOTICES: "OrderedDict[tuple[str, str], None]" = OrderedDict()
_DECLINE_NOTICE_LIMIT = 512

#: Guards :data:`_DECLINE_NOTICES`. The tiers reach this from worker threads, so
#: the read and the insert have to be one critical section or two concurrent
#: invocations both read "not seen yet" and both log.
_DECLINE_NOTICE_LOCK = threading.Lock()


def _notice_digest(value: str) -> str:
    """A fixed-size stand-in for a variable-length key element.

    The ledger only tests membership -- every value is ``None`` and nothing reads
    a key back out -- so a digest dedupes exactly as the value itself did while
    making the retained bytes independent of how long that value is.

    That independence is the point, because both variable-length elements are
    chosen outside this module and neither has its length checked. The session
    key comes from the agent webhook, which takes ``sessionKey`` from the request
    body and validates its type and its PREFIX but caps no length, unlike the
    ``message`` field beside it. The dedupe fingerprint is a filesystem path plus
    an mtime, and a Documents folder redirected deep enough makes that path as
    long as the filesystem allows. Bounding the ledger by entry COUNT alone
    therefore bounds the wrong dimension: 512 entries of a caller's chosen size
    is not a bound. A blank value still digests to its own stable value, so a
    headless caller keeps the separate bucket it is documented to get.
    """

    digest = hashlib.blake2b(value.encode("utf-8", "surrogatepass"), digest_size=16)
    return digest.hexdigest()


def should_log_decline(session_key: str, refusal: Refusal) -> bool:
    """Answer whether this decline's log LINE is worth writing again.

    Governs the human-facing ``logger.warning`` only. It never governs
    :func:`log_decline`, which writes the SEL audit row: declining is a security
    decision and every one of them is audited, per invocation, whatever this
    returns. Nothing observable is lost by suppressing a repeat -- the text of a
    platform-scope refusal is a constant read out of :data:`_REFUSAL_LOG_TEXT`
    and carries nothing about the command that met it.

    True for every command-scope code by default: those differ per invocation.
    A command-scope refusal MAY opt in to session-deduplication by carrying a
    :attr:`Refusal.dedupe_key` -- the fingerprint of the environment state it
    reads -- and then the same session-plus-fingerprint pair collapses to one
    line, with a changed fingerprint (a profile edited or removed mid-session)
    producing a fresh one. True the first time a :data:`_PLATFORM_SCOPE_CODES`
    member is met in a session, then False for that same session and code.

    A blank *session_key* is treated as its own bucket rather than shared, so a
    surface that has no session (a headless caller) still gets its first notice.
    """

    if refusal.code in _PLATFORM_SCOPE_CODES:
        key: tuple = (_notice_digest(session_key), refusal.code)
    elif refusal.dedupe_key is not None:
        key = (_notice_digest(session_key), refusal.code, _notice_digest(refusal.dedupe_key))
    else:
        return True
    with _DECLINE_NOTICE_LOCK:
        if key in _DECLINE_NOTICES:
            return False
        _DECLINE_NOTICES[key] = None
        while len(_DECLINE_NOTICES) > _DECLINE_NOTICE_LIMIT:
            _DECLINE_NOTICES.popitem(last=False)
    return True


def platform_scope_notice() -> str | None:
    """Name the platform-scope limitation in force here, or None.

    One spelling for the surfaces that report it away from an invocation --
    ``kirocrew doctor`` today -- so the CLI cannot describe a posture this module
    does not actually hold. Derived from the same helper
    :func:`name_grant_refusal` consults, so the two cannot drift: the notice is
    the one Windows refusal that is a property of the host rather than of its
    current configuration.
    """

    refusal = windows_environment_refusal()
    if refusal is not None and refusal.code in _PLATFORM_SCOPE_CODES:
        return refusal.code
    return None


def windows_environment_refusal() -> Refusal | None:
    """Why NO name on this Windows host can be vouched for right now, else None.

    ``None`` on every other platform. Two states refuse:

    * Windows cannot say where the user's Documents folder is
      (``SHGetKnownFolderPath`` failed), so whether a profile script runs before
      the command cannot be established. Platform scope: a property of the host.
    * A per-user PowerShell profile EXISTS. kiro-cli starts the shell without
      ``-NoProfile``, so that script runs before every command, and a function
      it defines resolves ahead of any program on ``PATH`` -- measured, and the
      same threat ``BASH_ENV`` poses on POSIX. Whatever writes as the user
      writes Documents, so the profile is not a file this check can trust by
      location; nor is it pinned, because a pin records what a human approved,
      and no approval card ever shows the profile. Command scope: the user can
      remove the file and the next invocation answers differently.

    The all-users profiles under ``$PSHOME`` are deliberately not checked: they
    live beside the system binaries this module already trusts by location.

    Like every other answer this module gives, this one describes the filesystem
    as it is when the tier decides: a profile created after the check and before
    the shell starts is the same residual window as the resolved file's own
    contents changing there, and narrowing it is not something this check can do
    from inside -- the shell is spawned by kiro-cli, which offers no
    ``-NoProfile``. What closes it is that writing the file needs an approved
    command of its own.

    Public because ``kirocrew doctor`` prints the same answer, so a user reading
    "why does every hook still prompt" sees the file that is doing it.
    """

    if not platform_compat.IS_WINDOWS:
        return None
    profiles = platform_compat.windows_powershell_profile_paths()
    if profiles is None:
        return Refusal(
            WINDOWS_UNMODELLED,
            "Windows could not report the user's Documents folder, so whether a "
            "PowerShell profile runs before the command cannot be established",
        )
    for profile in profiles:
        if os.path.isfile(profile):
            # Fingerprint the profile's state -- path plus mtime -- so the
            # log-line ledger deduplicates ONE line per session per state and
            # writes a fresh one when the user edits or removes the file. The
            # audit row is unchanged; every invocation is still recorded. See
            # :attr:`Refusal.dedupe_key` and :func:`should_log_decline`.
            try:
                mtime = os.stat(profile).st_mtime_ns
            except OSError:
                mtime = 0
            return Refusal(
                AMBIGUOUS_ENV,
                f"a PowerShell profile at {profile} runs before every command and "
                "can define a function that replaces any program this check resolves",
                dedupe_key=f"{profile}|{mtime}",
            )
    return None


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
    windows = platform_compat.IS_WINDOWS
    if windows and any(delim in command for delim in _WINDOWS_COMMENT_DELIMITERS):
        # A block comment has to be caught BEFORE tokenizing: `shlex` destroys
        # the delimiters, handing back `<` and `>` as redirect operators that
        # then swallow the real program as a redirect target. Refusing the line
        # is what keeps the walk's output describing the command PowerShell
        # runs, and it closes the pin path too -- `pin_human_approval` reads
        # this same walk, so a name a comment merely mentions is never recorded
        # as the file a human approved.
        return None
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
                return None
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
            return None
        # A bare scriptblock (`{...}`) or a variable reference (`$var`,
        # `$env:x`) sitting in a command position dispatches through a VALUE,
        # not a literal name -- PowerShell invokes the scriptblock body or the
        # variable's contents. The single-character `{` also enters through
        # `_RESERVED_WORDS` (which lists `{` and `}` themselves), so this
        # closes the multi-character shapes the reserved-word branch misses --
        # `{calc}`, `$env:COMSPEC`, `$prog` -- rather than being caught only
        # incidentally when the token happens to name nothing that resolves.
        if windows and token[:1] in ("{", "$"):
            return None
        if token in _RESERVED_WORDS or (windows and token.lower() in _POWERSHELL_KEYWORDS):
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
        #
        # PowerShell has no such prefix: `FOO=bar head x` is a command NAMED
        # `FOO=bar`, which no grant identifies, so on Windows every `=` in a
        # command position is refused rather than skipped.
        if "=" in token:
            if windows:
                return None
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
        # vouching for `/bin/sh` says nothing about what executes: a grant naming
        # a shell is a grant to run anything, which is a decision for the approval
        # card, not for a name check. Interpreters that take CODE (`python3 -c`)
        # are deliberately NOT
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


#: Windows programs and PowerShell built-ins whose whole job is to run a program
#: named in their arguments -- the Windows shape of ``env`` and ``xargs``.
#: ``start`` and ``saps`` are Start-Process; ``iex``/``icm`` evaluate a string or
#: script block; ``ii`` opens a file with its association; the rest are system
#: executables that spawn what they are told to. Held separately from
#: :data:`_DISPATCHERS` and consulted only when :data:`platform_compat.IS_WINDOWS`
#: is true, because the names collide with ordinary programs on POSIX -- ``iex``
#: is Elixir's REPL, ``start`` is a program a user is free to install, and so on.
#: Matched lower-cased and without extension on Windows (``_program_key``),
#: because ``POWERSHELL.EXE`` runs the same program as ``powershell``.
_WINDOWS_DISPATCHERS = frozenset(
    {
        "start",
        "saps",
        "start-process",
        "iex",
        "invoke-expression",
        "icm",
        "invoke-command",
        "ii",
        "invoke-item",
        "call",
        "forfiles",
        "wmic",
        "rundll32",
        "mshta",
        "cscript",
        "wscript",
        "msiexec",
        "schtasks",
        "wsl",
        "runas",
        "explorer",
        "conhost",
        "powershell_ise",
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

# ── Windows: PowerShell's resolution order ──
#
# The shell kiro-cli spawns on Windows is PowerShell (`powershell -Command`), and
# PowerShell resolves a command word in a fixed order: alias, function, cmdlet,
# then the executables on `PATH`. The first three come from the session itself,
# so a name in one of those tables runs a BUILT-IN whatever `PATH` holds --
# `sort` is Sort-Object with `sort.exe` sitting right there in System32, and
# `curl` is Invoke-WebRequest. Resolving such a name from `PATH` would vouch for
# a file the shell never runs. The tables below are the default alias and
# function names of Windows PowerShell 5.1 (`Get-Alias`, plus the functions a
# `-NoProfile` session defines), so a name in them is judged as the built-in it
# is: allowed when the built-in is inert, refused otherwise. Module auto-loading
# does not enter into this: measured, an application found on `PATH` wins over
# an auto-loadable module function, and a name found nowhere is refused anyway.

#: The Windows shell whose built-in set the tables below mirror, as the argv of a
#: session equivalent to the one the command will run in. ONE spelling, because
#: two places have to mean the same shell: these tables, and the derivation check
#: that enumerates a live session to prove they are not stale.
#:
#: ``-NoProfile`` is here and NOT in what kiro-cli spawns, deliberately. The
#: tables record the shell's DEFAULT names, which is what a profile-free session
#: reports; a profile's own functions are not table material because a profile
#: existing refuses every grant outright (:func:`windows_environment_refusal`).
#:
#: This is also the module's single point of exposure to kiro-cli's choice of
#: shell (kirodotdev/Kiro#9537). If that ever becomes pwsh 7, changing it here
#: re-points the derivation check at the new shell, and the check then fails on
#: every name whose behaviour the tables get wrong -- which is the loud failure a
#: hand-written mirror of another program's state needs.
MODELLED_WINDOWS_SHELL: tuple[str, ...] = ("powershell", "-NoProfile", "-NonInteractive")

#: PowerShell default aliases and session functions that are INERT in this
#: module's sense -- they neither run a program named in their arguments nor
#: change how a later name resolves -- plus the cmdlets they stand for, so a
#: grant written in PowerShell's own idiom (`Get-ChildItem *`) works too. This
#: is the Windows counterpart of :data:`_INERT_BUILTINS`, held to the same
#: standard: `where`/`foreach`/`%` take script blocks and are absent; `ri`,
#: `rni`, `ni`, `si` reach the `alias:` and `function:` drives and are absent;
#: `more`, `help` and `man` pipe through an external pager and are absent.
#:
#: Taking a script block in ANY parameter is disqualifying, not just in the
#: pipeline position `where`/`foreach` use. A calculated property is a script
#: block PowerShell evaluates once per input object, so `sort`, `select`,
#: `group`, `compare` and the whole `format-*` family run whatever their
#: `-Property` or `-GroupBy` argument contains and are absent for the same
#: reason. Measured on 5.1 with object input (scalar input makes the format
#: engine ignore `-Property`, which hides this): the block runs 2-3 times per
#: two-object pipeline for each of them, while `measure-object` types
#: `-Property` as `String[]`, so it coerces the block to its source text and
#: never evaluates it -- it stays. A name is judged as a whole command, so
#: `select` is absent even though its `-ExpandProperty` is `String`-typed: the
#: same command's `-Property` evaluates, and the check sees the name, not which
#: parameter a given line happens to use.
_WINDOWS_INERT_BUILTINS = frozenset(
    {
        "cat",
        "gc",
        "type",
        "get-content",
        "ls",
        "dir",
        "gci",
        "get-childitem",
        "pwd",
        "gl",
        "get-location",
        "cd",
        "sl",
        "chdir",
        "cd..",
        "set-location",
        "pushd",
        "popd",
        "echo",
        "write",
        "write-output",
        "write-host",
        "measure",
        "measure-object",
        "gm",
        "get-member",
        "oh",
        "out-host",
        "out-string",
        "gi",
        "get-item",
        "gp",
        "get-itemproperty",
        "gpv",
        "get-itempropertyvalue",
        "sls",
        "select-string",
        "gps",
        "ps",
        "get-process",
        "gsv",
        "get-service",
        "gv",
        "get-variable",
        "gal",
        "get-alias",
        "gcm",
        "get-command",
        "ghy",
        "history",
        "h",
        "get-history",
        "gdr",
        "get-psdrive",
        "gu",
        "get-unique",
        "rvpa",
        "resolve-path",
        "cvpa",
        "convert-path",
        "test-path",
        "get-date",
        "join-path",
        "split-path",
        "cls",
        "clear",
        "clear-host",
        "sleep",
        "start-sleep",
    }
)

#: Every default alias and session function of Windows PowerShell 5.1, lower-cased
#: (`Get-Alias | % Name` plus `Get-ChildItem function: | % Name`, on 5.1.26100).
#: An ALIAS and a FUNCTION both resolve ahead of an application in PowerShell's
#: precedence, unconditionally and with no module to load first, which is why the
#: two sets share one table. A name here that is not in
#: :data:`_WINDOWS_INERT_BUILTINS` resolves to a built-in PowerShell runs INSTEAD
#: of any same-named file on `PATH`, so it is refused rather than resolved:
#: vouching for `sc.exe` when the shell runs Set-Content would be answering the
#: wrong question. PowerShell 7 drops a few of these (`curl`, `wget`, `sc`);
#: keeping them costs a prompt there, never a wrong answer. Spelling a program
#: WITH its extension (`sort.exe`) bypasses the alias in PowerShell, and
#: correspondingly bypasses this table.
#:
#: The set is not asserted, it is CHECKED: on a Windows host
#: ``test_the_builtin_tables_cover_every_name_this_shell_resolves`` enumerates the
#: live shell and fails if any name it resolves would reach the `PATH` walk
#: unrefused. That test is what caught `cfs` (-> ConvertFrom-String) and the
#: `get-verb` function, both of which an earlier hand-transcribed list had missed.
_POWERSHELL_DEFAULT_ALIASES = frozenset("""
    % ? ac asnp cat cd cfs chdir clc clear clhy cli clp cls clv cnsn compare copy
    cp cpi cpp curl cvpa dbp del diff dir dnsn ebp echo epal epcsv epsn erase
    etsn exsn fc fhx fl foreach ft fw gal gbp gc gci gcm gcs gdr get-verb ghy gi
    gjb gl gm gmo gp gps gpv group gsn gsnp gsv gu gv gwmi h history icm iex ihy
    ii ipal ipcsv ipmo ipsn irm ise iwmi iwr kill lp ls man md measure mi mount
    move mp mv nal ndr ni nmo npssc nsn nv ogv oh popd ps pushd pwd r rbp rcjb
    rcsn rd rdr ren ri rjb rm rmdir rmo rni rnp rp rsn rsnp rujb rv rvpa rwmi
    sajb sal saps sasv sbp sc select set shcm si sl sleep sls sort sp spjb spps
    spsv start sujb sv swmi tee trcm type wget where wjb write
    cd.. help mkdir more oss pause prompt tabexpansion2 importsystemmodules
    """.split())
# `cd\` and the 26 drive functions `a:`..`z:` are 5.1 session functions too, but a
# backslash or a colon makes each of them a PATH in this walk, and each is refused
# as a relative one before any table is consulted (measured, and pinned by the
# coverage test above, which accepts either a table hit or an earlier refusal).

#: Every command exported by the three modules that ship AS PowerShell itself --
#: `Microsoft.PowerShell.Core`, `.Management`, `.Utility` -- lower-cased
#: (`Get-Command -Module <those three> -CommandType Cmdlet,Function`, 258 names on
#: 5.1.26100). A name here that is not in :data:`_WINDOWS_INERT_BUILTINS` is
#: refused, for a reason the alias table does not cover: these are FULL cmdlet
#: names, not aliases, and whether one beats a same-named file on `PATH` depends
#: on what is already loaded in the session, which is not a property of the
#: command being judged.
#:
#: Measured on 5.1: a session `powershell -Command` starts with `Utility` loaded
#: and `Management` NOT, and there a `Set-Content.cmd` on `PATH` DOES win -- an
#: auto-loadable module loses to an application, as the module docstring says. But
#: `Core` is always loaded, so `Where-Object` or `Invoke-Command` beats any file
#: unconditionally; and one command from `Management` or `Utility` earlier in the
#: same line auto-loads that whole module, after which every later name in it
#: beats `PATH` too. Resolving such a name to a file would vouch for a file the
#: shell may not run, so the whole class is refused instead.
#:
#: Exactly these three modules and no other shipped module (`NetAdapter`,
#: `Defender`, `.Security`, `.Diagnostics` ...): a module is auto-loaded only by
#: one of its OWN commands, and the only names this walk lets past without a
#: refusal are the inert ones, which are all `Core`/`Management`/`Utility`. So no
#: permitted prefix can load anything else, and a lone `Get-NetAdapter` runs the
#: file on `PATH` (measured) and stays resolvable. Spelling the extension
#: (`set-content.exe`) is not a cmdlet name in PowerShell and is not one here.
_POWERSHELL_CORE_COMMANDS = frozenset("""
    add-computer add-content add-history add-member add-pssnapin add-type
    checkpoint-computer clear-content clear-eventlog clear-history clear-item
    clear-itemproperty clear-recyclebin clear-variable compare-object
    complete-transaction connect-pssession convertfrom-csv convertfrom-json
    convertfrom-sddlstring convertfrom-string convertfrom-stringdata convert-path
    convert-string convertto-csv convertto-html convertto-json convertto-xml
    copy-item copy-itemproperty debug-job debug-process debug-runspace
    disable-computerrestore disable-psbreakpoint disable-psremoting
    disable-pssessionconfiguration disable-runspacedebug disconnect-pssession
    enable-computerrestore enable-psbreakpoint enable-psremoting
    enable-pssessionconfiguration enable-runspacedebug enter-pshostprocess
    enter-pssession exit-pshostprocess exit-pssession export-alias export-clixml
    export-console export-csv export-formatdata export-modulemember
    export-pssession foreach-object format-custom format-hex format-list
    format-table format-wide get-alias get-childitem get-clipboard get-command
    get-computerinfo get-computerrestorepoint get-content get-controlpanelitem
    get-culture get-date get-event get-eventlog get-eventsubscriber get-filehash
    get-formatdata get-help get-history get-host get-hotfix get-item
    get-itemproperty get-itempropertyvalue get-job get-location get-member
    get-module get-process get-psbreakpoint get-pscallstack get-psdrive
    get-pshostprocessinfo get-psprovider get-pssession get-pssessioncapability
    get-pssessionconfiguration get-pssnapin get-random get-runspace
    get-runspacedebug get-service get-timezone get-tracesource get-transaction
    get-typedata get-uiculture get-unique get-variable get-wmiobject group-object
    import-alias import-clixml import-csv import-localizeddata import-module
    import-powershelldatafile import-pssession invoke-command invoke-expression
    invoke-history invoke-item invoke-restmethod invoke-webrequest
    invoke-wmimethod join-path limit-eventlog measure-command measure-object
    move-item move-itemproperty new-alias new-event new-eventlog new-guid
    new-item new-itemproperty new-module new-modulemanifest new-object new-psdrive
    new-psrolecapabilityfile new-pssession new-pssessionconfigurationfile
    new-pssessionoption new-pstransportoption new-service new-temporaryfile
    new-timespan new-variable new-webserviceproxy out-default out-file
    out-gridview out-host out-null out-printer out-string pop-location
    push-location read-host receive-job receive-pssession
    register-argumentcompleter register-engineevent register-objectevent
    register-pssessionconfiguration register-wmievent remove-computer remove-event
    remove-eventlog remove-item remove-itemproperty remove-job remove-module
    remove-psbreakpoint remove-psdrive remove-pssession remove-pssnapin
    remove-typedata remove-variable remove-wmiobject rename-computer rename-item
    rename-itemproperty reset-computermachinepassword resolve-path restart-computer
    restart-service restore-computer resume-job resume-service save-help
    select-object select-string select-xml send-mailmessage set-alias set-clipboard
    set-content set-date set-item set-itemproperty set-location set-psbreakpoint
    set-psdebug set-pssessionconfiguration set-service set-strictmode set-timezone
    set-tracesource set-variable set-wmiinstance show-command
    show-controlpanelitem show-eventlog sort-object split-path start-job
    start-process start-service start-sleep start-transaction stop-computer
    stop-job stop-process stop-service suspend-job suspend-service tee-object
    test-computersecurechannel test-connection test-modulemanifest test-path
    test-pssessionconfigurationfile trace-command unblock-file undo-transaction
    unregister-event unregister-pssessionconfiguration update-formatdata
    update-help update-list update-typedata use-transaction wait-debugger
    wait-event wait-job wait-process where-object write-debug write-error
    write-eventlog write-host write-information write-output write-progress
    write-verbose write-warning
    """.split())

#: Characters that make a PROGRAM token something other than a literal name on
#: Windows: :data:`_EXPANDING_CHARS` plus cmd.exe's `%VAR%` and `^` escape --
#: kept although PowerShell is the modelled shell, because a program named
#: through either cannot be a literal name in any shell -- and PowerShell's `@`
#: splatting / array prefix.
_WINDOWS_EXPANDING_CHARS = _EXPANDING_CHARS + ("%", "^", "@")

#: Extensions Windows runs DIRECTLY: an image the loader maps (`.exe`, `.com`),
#: a batch file `COMSPEC` interprets, or a script the running PowerShell reads
#: itself. Anything else that `PATHEXT` lets the shell resolve (`.py`, `.js`,
#: `.vbs` ...) is launched through the program the REGISTRY associates with the
#: extension, under `HKCU` as readily as `HKLM` -- so the interpreter is chosen
#: by a key the user (and so the agent) can write, not by the file this check
#: can pin. Such a hit is refused.
_WINDOWS_RUNNABLE_EXTENSIONS = frozenset({".exe", ".com", ".bat", ".cmd", ".ps1"})

#: The two of those that `COMSPEC` interprets, so the interpreter has to be the
#: system `cmd.exe` for the pin on the script to mean anything.
_COMSPEC_SCRIPT_EXTENSIONS = frozenset({".bat", ".cmd"})

#: `PATHEXT` when the environment does not supply one -- Windows' own default.
_DEFAULT_PATHEXT = ".COM;.EXE;.BAT;.CMD"


def _windows_extensions() -> tuple[str, ...]:
    """The extensions PowerShell appends to a bare name, in the order it tries them.

    `.ps1` FIRST, then `PATHEXT` in order: measured on 5.1, a directory holding
    `tool.ps1` and `tool.exe` runs the script. Read from the environment because
    a host can extend `PATHEXT` (this one carries `.PY`), and the shell uses
    what it was given.

    Deduplicated, keeping first position: a host whose `PATHEXT` lists `.PS1`
    itself would otherwise have `.ps1` appear twice, probing each directory for
    the same file twice. The shell tries a spelling once, so this does too.
    """

    raw = os.environ.get("PATHEXT") or _DEFAULT_PATHEXT
    ordered = (".ps1",) + tuple(ext.lower() for ext in raw.split(";") if ext.startswith("."))
    return tuple(dict.fromkeys(ordered))


def _windows_which(name: str, path: str) -> str | None:
    """PowerShell's resolution of a bare *name* over *path*, or ``None``.

    Directory-major: every candidate spelling is tried in the first directory
    before the second is looked at, which is what lets a later directory's
    `.exe` lose to an earlier one's `.ps1`. A name that already carries one of
    the extensions is tried as written first (`whoami.EXE` runs). Existence is
    the whole test -- Windows has no execute bit, and the shell runs any file
    with the right extension -- so the POSIX ``shutil.which`` is not used here:
    it neither knows `.ps1` nor puts it first.
    """

    extensions = _windows_extensions()
    lowered = name.lower()
    candidates = [name] if any(lowered.endswith(ext) for ext in extensions) else []
    candidates.extend(name + ext for ext in extensions)
    for directory in path.split(os.pathsep):
        if not directory:
            continue
        for candidate in candidates:
            hit = _windows_case_insensitive_file(directory, candidate)
            if hit is not None:
                return hit
    return None


def _windows_case_insensitive_file(directory: str, candidate: str) -> str | None:
    """A file named *candidate* in *directory*, matched the way Windows matches.

    Windows file names are case-insensitive, so `FIND.exe` runs `find.exe`. On a
    case-sensitive host (a POSIX CI runner with the Windows model patched on) a
    plain ``os.path.isfile`` would answer for the exact case only, so the Windows
    model would fail to resolve a mixed-case spelling the shell resolves. The
    fast path is the exact hit -- the only one that exists on a real Windows host
    -- and only a miss falls back to a case-folded directory scan, so production
    (where `os.path.isfile` already matches case-insensitively) never pays for
    the scan.
    """

    full = os.path.join(directory, candidate)
    if os.path.isfile(full):
        return full
    target = candidate.lower()
    try:
        entries = os.listdir(directory)
    except OSError:
        return None
    for entry in entries:
        if entry.lower() == target:
            hit = os.path.join(directory, entry)
            if os.path.isfile(hit):
                return hit
    return None


def _windows_extension_refusal(name: str, found: str) -> Refusal | None:
    """Refuse a hit Windows would run through something this check cannot pin."""

    ext = os.path.splitext(found)[1].lower()
    if ext not in _WINDOWS_RUNNABLE_EXTENSIONS:
        return Refusal(
            FILE_ASSOCIATION,
            f"{name} resolves to {found}, which Windows runs through the program "
            f"registered for {ext or 'files without an extension'} -- a registry "
            "choice this check cannot identify",
        )
    if ext in _COMSPEC_SCRIPT_EXTENSIONS:
        comspec = os.environ.get("COMSPEC") or ""
        try:
            real = os.path.realpath(comspec) if comspec else ""
        except (OSError, ValueError):
            real = ""
        if not real or not _is_trusted_system_file("cmd", real):
            return Refusal(
                AMBIGUOUS_ENV,
                f"{name} resolves to the batch file {found}, which runs under COMSPEC, "
                "and COMSPEC does not name the system cmd.exe",
            )
    return None


def _program_key(name: str) -> str:
    """The name a program is matched under in the dispatcher table.

    The basename as written on POSIX. On Windows names are case-insensitive and
    an extension is optional, so `POWERSHELL.EXE` and `powershell` must both meet
    the `powershell` entry: lower-cased, with a `PATHEXT`/`.ps1` extension
    removed. Alias tables are NOT consulted through this -- `sort.exe` written
    with its extension bypasses PowerShell's alias, and must bypass ours.
    """

    base = _model_basename(name)
    if not _model_is_windows():
        return base
    base = base.lower()
    root, ext = os.path.splitext(base)
    if ext in _windows_extensions():
        return root
    return base


def _model_is_windows() -> bool:
    """The path/name FLAVOUR the model reasons in, derived from the model flag.

    The Windows model is selected by :data:`platform_compat.IS_WINDOWS`, which
    the tests patch to exercise the Windows lexer, resolution order and refusal
    codes on a POSIX runner. The host's own ``os.path`` module and ``os.sep`` do
    NOT follow that flag -- on Linux they stay POSIX -- so a name-shape or
    case-folding decision made through the host module silently answers with the
    wrong flavour under the patched flag, passing on a real Windows host and
    failing only on the POSIX CI shard. Every model decision about a NAME (its
    separators, whether it is absolute, whether two names fold together) goes
    through the helpers below so the answer depends on the model, not the host.
    """

    return platform_compat.IS_WINDOWS


def _model_normcase(text: str) -> str:
    """Case-fold *text* the way the MODEL compares names, not the host.

    Windows file names are case-insensitive, so `Git` and `git` name one file;
    POSIX names are case-sensitive, so they name two. ``os.path.normcase`` is
    the host's rule (identity on POSIX, lower-case on Windows) and cannot be
    patched by the model flag, so the model uses this instead.
    """

    return text.lower() if _model_is_windows() else text


def _model_basename(path: str) -> str:
    """The final component of *path*, split the way the MODEL splits paths.

    ``ntpath.basename`` when the model is Windows -- so
    ``C:\\Windows\\System32\\cmd.exe`` yields ``cmd.exe`` -- and
    ``posixpath.basename`` otherwise. ``os.path.basename`` follows the HOST, not
    the model flag, so on a POSIX runner with the Windows model patched on it
    would return the whole backslash string and the name-shape decisions built on
    it (the dispatcher key, the pin key, the trusted-system and inert-builtin
    consults) would silently misfire. On a real Windows host ``os.path`` IS
    ``ntpath``, so production behaviour is byte-identical.
    """

    return (ntpath if _model_is_windows() else posixpath).basename(path)


def _model_sep_in(name: str) -> bool:
    """Whether *name* carries a separator the MODEL treats as making it a path.

    ``/`` is a separator in both flavours. A backslash is a separator ONLY in
    the Windows model -- on POSIX it is an escape character (``grep '\\d'``), so
    the model must not read a backslash as path-shaped there. Keyed on the model
    flag rather than ``os.sep`` (which is a backslash only on a Windows host).
    """

    if "/" in name:
        return True
    return _model_is_windows() and "\\" in name


def _is_absolute(name: str) -> bool:
    """Whether *name* pins a location without reference to a working directory.

    On Windows ``ntpath.isabs`` alone is not that: ``\\tool.exe`` is rooted on
    the CURRENT drive and ``C:tool.exe`` is relative to C:'s current directory,
    and both change meaning with a working directory the approval never saw.
    Only a path with a drive or UNC prefix is absolute here.

    The path flavour is chosen from the MODEL flag, not the host: ``os.path`` is
    POSIX on a Linux runner even when the Windows model is patched on, so an
    absolute Windows path would read as relative there. ``ntpath`` gives the
    Windows answer on any host, and ``posixpath`` the POSIX answer, so the model
    is deterministic per flag. On a real Windows host ``os.path`` IS ``ntpath``,
    so production behaviour is byte-identical.
    """

    pathmod = ntpath if _model_is_windows() else posixpath
    if not pathmod.isabs(name):
        return False
    if _model_is_windows() and not pathmod.splitdrive(name)[0]:
        return False
    return True


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
#: the chunk size. Capping the digest and hashing only a large file's head and
#: tail would leave a middle-only rewrite of a big binary undetected when it also
#: preserved the size and landed inside one ctime tick.
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
        # Folded the way the model compares names: a case-insensitive model
        # reaches one file through several spellings, and the resolver hands
        # back whichever spelling the caller asked for, so the raw string
        # would give one file as many identities as it has spellings.
        _model_normcase(real),
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


def _is_trusted_system_file(name: str, real: str) -> bool:
    """Whether *real* IS the trusted system program called *name*."""

    system = platform_compat.trusted_system_bin(name)
    if system is None:
        return False
    try:
        return _model_normcase(os.path.realpath(system)) == _model_normcase(real)
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
