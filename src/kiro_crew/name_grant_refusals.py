"""The refusal vocabulary of a name-based approval, and its decline-log dedupe.

The refusal codes, :class:`Refusal` with the constant log text each code reads, the
per-session ledger that logs a platform-scope decline once (:func:`should_log_decline`),
and the Windows host-environment refusal that ``kirocrew doctor`` prints as well
(:func:`platform_scope_notice`, :func:`windows_environment_refusal`).

An owner module of :mod:`kiro_crew.name_grant`, the one import path: callers, tests and
documents reach every name here as ``kiro_crew.name_grant.<name>``. It imports no other
owner, and nothing here reads a name the tests patch through the facade
(``test/test_name_grant_facade.py`` holds it to that). A new refusal code and its log text
go here.
"""

from __future__ import annotations

import hashlib
import os
import threading
from collections import OrderedDict
from dataclasses import dataclass

from kiro_crew import platform_compat

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
#: Bounded like ``name_grant._PINS`` so a long-lived gateway cannot accumulate an entry
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
