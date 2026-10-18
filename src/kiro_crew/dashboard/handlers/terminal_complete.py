"""The vetting and listing steps of terminal path completion: splitting the typed token,
resolving and vetting the directory it names, and filtering protected or unsafe entries.

``api_terminal_complete`` and the listing it ranks stay in the facade; this module holds
the steps they call. An owner of :mod:`kiro_crew.dashboard.handlers.terminal`: callers
and tests import and patch these names through that facade, which mirrors a patch into
every module of the family that holds the name.
"""

from __future__ import annotations

import os
import re

from kiro_crew.dashboard.handlers.terminal_session import _sel
from kiro_crew.hooks import validate_file_path
from kiro_crew.security import is_sensitive_path

_COMPLETE_MAX_ENTRIES = 200


_COMPLETE_TOKEN_MAX = 4096


# Hard ceiling on how many directory entries one completion may EXAMINE. The
# retention cap alone does not bound the work: a directory with a million
# entries would still be walked end to end while holding a pool thread at
# keystroke rate. Stopping early is safe because the user narrows by typing.
_COMPLETE_MAX_SCAN = 20000


# C0 controls (0x00-0x1F), DEL (0x7F), C1 controls (0x80-0x9F) and lone surrogate
# code points (U+D800-U+DFFF). A filename may legally contain any of these; the
# client TYPES the accepted completion into the PTY, so a name holding CR/LF
# would submit an executed command line and an ESC would inject a terminal
# escape sequence. Surrogates are how Python's surrogateescape decoding
# represents bytes that are not valid UTF-8: JSON carries them through, but the
# browser's TextEncoder replaces each with U+FFFD, so the client would type a
# path that does not exist on disk. Filter all of them at the source so such
# names never reach a client at all.
_UNSAFE_NAME_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\ud800-\udfff]")


def _split_path_token(token: str) -> tuple[str, str]:
    """Split a shell path token into its (directory part, name prefix).

    ``"../Kiro"`` → ``("../", "Kiro")``; ``"src/"`` → ``("src/", "")``;
    ``"Kiro"`` → ``("", "Kiro")``."""
    idx = token.rfind("/")
    if idx < 0:
        return "", token
    return token[: idx + 1], token[idx + 1 :]


def _resolve_completion_dir(cwd: str, dir_part: str) -> str:
    """Absolute directory that a token's directory part refers to.

    ``~`` is expanded (only as a leading segment, matching what the shell shows
    the user); a relative part resolves against the session's live cwd."""
    if not dir_part:
        return cwd
    expanded = os.path.expanduser(dir_part) if dir_part.startswith("~") else dir_part
    base = expanded if os.path.isabs(expanded) else os.path.join(cwd, expanded)
    return os.path.normpath(base)


def _vetted_completion_dir(directory: str) -> str | None:
    """Canonical form of *directory*, or ``None`` when it must not be enumerated.

    Delegates to ``hooks.validate_file_path`` — the named chokepoint the backend
    security rules require every file read to pass through — rather than
    reimplementing its ``realpath`` + ``is_sensitive_path`` pair. Canonicalizing
    before the denylist test is the load-bearing part: without it a benign-looking
    symlink (or a symlinked parent component) whose target lands inside the
    governance trust-root would pass a name-based check and then be enumerated
    through the link, leaking ``profiles/``, ``security_policy.json`` and
    credential-file names.

    The chokepoint stops at "which path is allowed"; it cannot bind the answer to
    the inode the scan will actually read, which is why ``_open_vetted_dir``
    follows. ``hooks.safe_read_file`` layers the same ``O_NOFOLLOW`` open over the
    same check for single-file reads, so the pairing here mirrors the established
    pattern rather than inventing one.

    A failure inside the chokepoint is treated as "do not enumerate": over-refusing
    a path we cannot canonicalize is the safe direction for a read gate."""
    try:
        return validate_file_path(directory)
    except (OSError, ValueError):
        return None


def _entry_is_sensitive(canonical_dir: str, entry: os.DirEntry) -> bool:
    """Whether one directory ENTRY must be withheld from a completion listing.

    Vetting only the DIRECTORY is not enough: ``~/.kiro/crew`` is not itself on
    the denylist while several of its children are (``security_policy.json``,
    ``profiles/``, ``token_signing.key``), so an entry-blind listing of an
    otherwise-allowed directory still discloses trust-root metadata names.

    ``is_sensitive_path`` is given the JOINED path rather than an explicitly
    resolved one. Two reasons:

    * it already builds resolved AND lexical candidate forms internally, so a
      symlinked child whose TARGET is protected is refused through the link —
      adding ``os.path.realpath(entry.path)`` here would only pay a second
      resolution syscall for the same verdict, at keystroke rate;
    * ``canonical_dir`` comes from ``_vetted_completion_dir``, so for an entry
      that is not itself a link the joined path is already canonical.

    ``validate_file_path`` (hooks.py) is the same check wrapped in exactly that
    redundant ``realpath`` plus an ``expanduser``, and belongs to the agent
    tool-call layer — so the underlying predicate is used directly.

    A classification failure counts as sensitive: over-refusing an entry we
    cannot classify is the safe direction for a read gate."""
    try:
        return is_sensitive_path(os.path.join(canonical_dir, entry.name))
    except (OSError, ValueError):
        return True


def _entry_sort_key(entry: dict) -> tuple:
    """Ranking used by BOTH the bounded-retention heap and the response order:
    earliest match offset first (so a true prefix beats a mid-name hit), dirs
    before files among equals, then case-insensitive name."""
    return (entry["at"], not entry["dir"], str(entry["name"]).lower())


def _open_vetted_dir(vetted: str) -> int | None:
    """A descriptor pinned to the directory ``vetted`` named when it was vetted.

    Vetting a PATH and then scanning that PATH are two resolutions of the same
    name, and anything may swap the name between them: replace the directory
    with a symlink to ``~/.ssh`` after the sensitive-path test has passed and
    the scan enumerates the target instead. Closing that window needs the scan
    to be pinned to an inode rather than re-resolving a name, which is what
    scanning a descriptor achieves — once this fd is open, no rename or symlink
    swap can redirect it.

    The open itself is still a name resolution, so it is verified afterwards:
    the fd's identity must equal the identity the vetted path resolves to. A
    swap in that remaining window changes one side of the comparison, so the
    mismatch refuses. ``O_NOFOLLOW`` additionally rejects a final component that
    has become a symlink, which ``realpath`` guaranteed it was not at vet time.

    Returns ``None`` when the directory cannot be opened or fails verification —
    for a keystroke-rate endpoint that is simply "no completions", not an error.
    The caller owns closing the descriptor."""
    try:
        fd = os.open(vetted, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None
    try:
        opened = os.fstat(fd)
        named = os.stat(vetted)
        if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
            os.close(fd)
            return None
    except OSError:
        os.close(fd)
        return None
    return fd


def _log_complete(caller: str, outcome: str, reason: str) -> None:
    """SEL API-access event for the completion route.

    Every outcome is audited (blocking rule in
    docs/system-specs/modules/learn-cron-dashboard.md: all terminal endpoints
    emit API-access events), but the payload is DELIBERATELY COARSE — a fixed
    reason word only. This route fires per keystroke, and the token, the prefix,
    the resolved directory and the entry names are all user filesystem contents;
    recording them would turn the audit log into a continuous transcript of what
    the user types and what their disk contains.

    The command tier obeys the same rule and is why it needs its own words rather
    than reusing ``listed``: a reason that named the command or the flag being
    completed would put the user's command line in the audit trail, which is
    exactly what this coarseness exists to prevent. ``cmd_unknown`` covers both
    "not allowlisted" and "not on PATH" for the same reason — distinguishing them
    would disclose which tools are installed."""
    _sel().log_api_access(
        caller=caller,
        operation="terminal.complete",
        outcome=outcome,
        source="dashboard",
        resources=reason,
    )
