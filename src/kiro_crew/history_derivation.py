"""The transcript's privacy contract, and the seam every deriving reader uses.

What a header's ``memory_mode`` means, and the one place a reader that learns from a
transcript obtains its rows: the line is validated and the rows are read under one lock
hold, and a restricted or unreadable line is a refusal.

Each function here whose first parameter is ``self`` is a ``ConversationLog`` method:
the class binds it by name, so it reads and writes the log's own state on the instance,
and a patch of the method on the class or on an instance reaches every caller.

Instance fields written: none.

Instance fields read: none.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterable, Iterator, Sequence
from collections.abc import Set as AbstractSet
from typing import TYPE_CHECKING

from kiro_crew.execution_context import MEMORY_MODES
from kiro_crew.history_files import transcript_lock_stems
from kiro_crew.history_locks import HistoryLockTimeout

if TYPE_CHECKING:
    from kiro_crew.history import ConversationLog


# Canonical set of memory_mode values that mark a session private — never
# searchable/listable/summarizable. Single source of truth shared by the MCP
# history tools (mcp_core) and the dashboard session handlers so the exclusion
# can't silently diverge between surfaces.
INCOGNITO_MEMORY_MODES = frozenset({"incognito", "temporary"})

#: The most of a transcript's first line any reader parses for its mode. The
#: metadata line is a small JSON object; a longer first line is unknown to every
#: reader alike, so the write gate and the export cannot disagree about it.
TRANSCRIPT_HEADER_MAX_BYTES = 64 * 1024


def memory_mode_from_header_line(first_line: bytes) -> str | None:
    """The ``memory_mode`` a transcript's first line records, or ``None`` when unknown.

    The one parse of a transcript header's mode, shared by the restricted-session
    write gate and the whole-install export and import, so the readers that must
    fail closed cannot drift apart. Pure: the caller reads (and bounds) the line.

    * a JSON object with ``_type: metadata`` and no ``memory_mode`` (or ``null``)
      is a legacy persistent session -> ``"persistent"``;
    * a string mode is ``strip().lower()``-ed and must then be in
      :data:`MEMORY_MODES`;
    * anything else -- not JSON, not an object, not a metadata header, a
      non-string or unrecognised mode -> ``None``. Callers deny on ``None``.
    """
    try:
        data = json.loads(first_line.decode("utf-8", "replace"))
    except (ValueError, RecursionError):
        # ValueError covers JSONDecodeError and an integer past the interpreter's
        # digit limit; deep nesting raises RecursionError.
        return None
    if not isinstance(data, dict) or data.get("_type") != "metadata":
        return None
    mode = data.get("memory_mode")
    if mode is None:
        return "persistent"
    if not isinstance(mode, str):
        return None
    # Allowlist, not normalize-and-hope: `"incognito "` must not read as an
    # unrestricted mode just because it misses INCOGNITO_MEMORY_MODES.
    normalized = mode.strip().lower()
    return normalized if normalized in MEMORY_MODES else None


def is_incognito_transcript(memory_mode: object) -> bool:
    """True when *memory_mode* marks a transcript private (incognito/temporary).

    The single shared predicate for :data:`INCOGNITO_MEMORY_MODES` membership,
    so the normalization cannot drift between the surfaces that must agree on
    what "private" means (history scans, MCP history tools, dashboard session
    handlers, Discord resume, summary/folder/channel-slot derivations).

    Normalization is ``str()`` + ``lower()`` — exactly what the call sites
    apply: ``None``/absent reads as persistent (not private), and comparison is
    case-insensitive because the set holds lowercase members while a
    hand-edited transcript header is not bound by the API's validation.
    Whitespace is deliberately NOT stripped and unrecognized values read as
    not-private: callers that must fail closed on an unreadable or junk header
    (e.g. the restricted-session write gate) resolve the mode through an
    allowlist first and deny on ``None`` before this membership test applies.
    """
    return str(memory_mode or "").lower() in INCOGNITO_MEMORY_MODES


def transcript_privacy_mode(memory_mode: object) -> str:
    """The private mode a transcript header records, normalized, or ``""``.

    The companion of :func:`is_incognito_transcript` for a caller that needs
    the MODE rather than the yes/no: the same ``str()`` + ``lower()``
    normalization and the same membership test, so the two cannot disagree
    about which headers are private; ``""`` for a header that records no mode
    or a value the set does not hold (whitespace is not stripped here either --
    a header no reader recognizes is one the stamps below may REPAIR with a
    recognized mode, which stripping would prevent). Every reader that compares
    or reports a header's mode goes through this rather than the raw string: a
    header is not bound by the API's validation (a hand edit, a foreign writer),
    and a raw ``Temporary`` handed to a strictness compare reads as UNKNOWN --
    weaker than any recognized mode -- so the tighten-only stamps would rewrite
    it as ``incognito``, a tightening lost, and the consolidator would report
    the mode as spelled rather than as the mode it is.
    """
    normalized = str(memory_mode or "").lower()
    return normalized if normalized in INCOGNITO_MEMORY_MODES else ""


class TranscriptWithheld(RuntimeError):
    """A derivation read met a restricted (or unreadable) line under the transcript lock.

    Raised by the ONE seam every reader that DERIVES from a transcript goes
    through -- :meth:`ConversationLog.derive_messages`,
    :meth:`ConversationLog.derive_messages_chained`,
    :meth:`ConversationLog.derive_recent`,
    :meth:`ConversationLog.publication_hold`, and
    :meth:`ConversationLog.snapshot_for_consolidation` with
    ``withhold_restricted=True`` -- when the metadata line says the session is
    incognito or temporary, or cannot be read at all. The line and the rows are
    read under the same lock hold, so what the caller gets can never be rows a
    restricted line already governed. Nothing wrong has happened to the session:
    it is simply one nothing may be learned from, so callers treat this as a
    refusal (skip, 400, ``_CONSOLIDATION_REFUSED``), never as a failure.
    """


class TranscriptBusy(TranscriptWithheld):
    """The derivation seam could not take the transcript lock in time.

    The seam validates the line and reads the rows under ``_locked``, which is a
    patient, cross-process acquire with a ceiling (:class:`HistoryLockTimeout`).
    A reader that cannot obtain the lock cannot vouch for the contract, so it gets
    no rows -- the same fail-closed answer as a restricted line, and a subclass so
    every skip/continue that handles :class:`TranscriptWithheld` already handles
    it. Kept distinct so a caller that answers a person can say "busy, retry"
    (the export's retryable 503) instead of "private".
    """


def transcript_withholds_derivation(log: "ConversationLog", key: str) -> bool:
    """True when *key*'s ON-DISK line forbids deriving anything from the transcript.

    The metadata line's ``memory_mode`` is the file's privacy contract: every
    reader that learns from the file gates on it, and any writer -- this process,
    another gateway on the same data home, a subagent or cron appending to the
    session -- may only ever tighten it. A reader that reads the ROWS from disk
    but gates on a LIVE slot's mode (the session summary, the export) can
    therefore lag the file: the line says restricted, the slot it kept in memory
    still says persistent, and the private rows go to a model or a file. Such a
    reader asks this predicate about the file it is about to read, and again
    about the file it just read, so a tightening that lands between the two is
    caught as well.

    Fails CLOSED: a line that cannot be read answers ``True``, because a reader
    that cannot see the contract has no business acting on the rows. An absent
    file (no line yet) is not a refusal -- there is nothing on disk to protect.
    """
    metadata, readable = log.get_metadata_status(key)
    if not readable:
        return True
    return is_incognito_transcript(metadata.get("memory_mode"))


# ------------------------------------------------------------------ #
# The derivation seam: the ONE place a reader that LEARNS from a transcript
# (a summary, an export or transfer bundle, a suggestions prompt, an MCP
# history tool, the consolidator, skill detection -- anything that hands rows
# to a model, a peer, a file or memory) obtains its rows.
#
# The metadata line's ``memory_mode`` is the file's privacy contract and a
# ratchet any writer may tighten; a reader that checked the line and then
# read the rows in a separate step, or that trusted a live slot's mode, could
# be handed rows a restricted line already governs (a same-key hand-over
# landing a closed restricted tab's rows; a second gateway on the same data
# home tightening the line). Here the line is validated and the rows are read
# under one ``_locked`` hold, so the two cannot disagree, and a restricted or
# unreadable line raises :class:`TranscriptWithheld` instead of yielding rows.
#
# The plain reads (``read_messages``, ``read_messages_chained``, ``recent``)
# stay for transcript PLUMBING -- resuming a tab, saving, rendering the
# History browser, migrating a file -- which must see a restricted transcript
# (that is the point of keeping it). ``test_transcript_derivation_seam.py``
# enumerates every plain-read call site in the tree, so a new consumer cannot
# be written against the raw reads without naming itself there as plumbing.
# Callers hold no loop: ``_locked`` is a blocking, cross-process acquire.
# ------------------------------------------------------------------ #


@contextlib.contextmanager
def derivation_hold(self: ConversationLog, stems: Iterable[str]) -> Iterator[None]:
    """Hold *stems* for a derivation read; a lock timeout is a refusal.

    ``locked_stems`` raises :class:`HistoryLockTimeout` when a holder (this
    session's own save, a cron append, a second gateway) outlasts the acquire
    ceiling. For a deriving reader that is not an error to surface as a
    500 or a failed tool: it is "cannot vouch for the contract right now",
    answered as :class:`TranscriptBusy` so the caller's best-effort skip
    holds and a person-facing caller can say retry.
    """
    try:
        with self.locked_stems(stems):
            yield
    except HistoryLockTimeout as exc:
        raise TranscriptBusy(f"transcript lock not acquired in time: {exc}") from exc


@contextlib.contextmanager
def publication_hold(
    self: ConversationLog, key: str, *, expected_keys: Sequence[str] | None = None
) -> Iterator[None]:
    """Hold and re-validate *key* for one transcript-derived publication.

    The chained transcript locks stay held through exactly one durable write
    or synchronous response commit. When *expected_keys* is supplied, it is
    the chain whose rows produced the pending publication. Any membership
    change is retryable because the pending output does not describe the
    current chain. A restricted line raises :class:`TranscriptWithheld`; a
    lock timeout, unreadable line, or changed chain raises
    :class:`TranscriptBusy`. Callers must enter this off the event loop and
    must never keep the hold across a model call or an ``await``.
    """
    keys = list(expected_keys) if expected_keys is not None else self.chained_keys(key)
    keys = keys or [key]
    stems = {stem for chained in keys for stem in transcript_lock_stems(chained)}
    with self.derivation_hold(stems):
        settled_chain = self.chained_keys(key)
        settled = settled_chain or [key]
        if set(settled) != set(keys):
            raise TranscriptBusy(f"transcript chain for {key!r} changed while it was being locked")
        for chained in settled:
            self._withhold_if_restricted(chained)
        yield


def _withhold_if_restricted(self: ConversationLog, key: str) -> None:
    """Raise :class:`TranscriptWithheld` unless *key*'s line permits derivation.

    Called INSIDE ``_locked(key)`` so the verdict describes the same file
    state the caller's row read sees. An absent file (no line yet) is not a
    refusal. A line that cannot be READ is refused too, but as
    :class:`TranscriptBusy`: nothing about the session's privacy was measured,
    so a handler that maps the two must not tell the user the chat is
    incognito -- it is a transient failure to retry, like a lock timeout, and
    the subclass keeps every best-effort ``except TranscriptWithheld`` skip
    failing closed exactly as before.
    """
    metadata, readable = self.get_metadata_status(key)
    if not readable:
        raise TranscriptBusy(f"transcript {key!r} could not be read; nothing is derived from it")
    if is_incognito_transcript(metadata.get("memory_mode")):
        raise TranscriptWithheld(f"transcript {key!r} is restricted; nothing is derived from it")


def derive_messages(self: ConversationLog, key: str) -> list[dict]:
    """Rows for a reader that derives from them; refused for a restricted line.

    The guarded twin of :meth:`read_messages` (same rows, same shared cache
    object -- callers must not mutate), read under the transcript lock after
    the line has been validated in the same hold.
    """
    with self.derivation_hold(transcript_lock_stems(key)):
        self._withhold_if_restricted(key)
        return self.read_messages(key)


def derive_messages_chained(self: ConversationLog, key: str) -> list[dict]:
    """Guarded twin of :meth:`read_messages_chained` (see :meth:`derive_messages`)."""
    rows, _keys = self.derive_messages_chained_with_keys(key)
    return rows


def derive_messages_chained_with_keys(
    self: ConversationLog, key: str
) -> tuple[list[dict], tuple[str, ...]]:
    """Return guarded chained rows and the exact chain validated with them.

    A chained read concatenates EVERY transcript sharing the tab id -- a legacy
    tab's earlier files as well as the requested key -- so the contract that
    governs the result is the strictest line among them, not the requested
    key's alone: a sibling tightened to ``temporary`` would otherwise ride out
    under a persistent sibling's line. Every chained transcript is locked (one
    deterministic lock set, no partial holds) and validated before a single
    row is read. The chain is resolved once more inside the hold, and only that
    validated settled set is read; a member joining afterwards is never pulled
    in by a third resolution outside the lock set. The returned keys let an
    egress publication compare its pending bundle with this same settled set.
    A member that joined between the resolve and the hold is unlocked and
    unvalidated, so the read is refused -- as :class:`TranscriptBusy`, the
    same answer :meth:`publication_hold` gives a changed chain: nothing about
    the session's privacy was measured, so a person-facing caller says retry
    rather than private.
    """
    keys = self.chained_keys(key) or [key]
    stems = {stem for chained in keys for stem in transcript_lock_stems(chained)}
    with self.derivation_hold(stems):
        settled_chain = self.chained_keys(key)
        settled = settled_chain or [key]
        if set(settled) - set(keys):
            # The chain grew between the resolve and the hold: its new member
            # is not locked, so refuse rather than read it unvalidated. A
            # membership change is "cannot vouch right now", not a privacy
            # verdict, so it is the retryable refusal.
            raise TranscriptBusy(f"transcript chain for {key!r} changed while it was being locked")
        for chained in settled:
            self._withhold_if_restricted(chained)
        validated_keys = tuple(settled)
        if not settled_chain:
            # Preserve read_messages_chained's shared-cache identity when the
            # index knows no chain for this key.
            return self._read_messages(key), validated_keys
        rows: list[dict] = []
        for chained in settled:
            rows.extend(self._read_messages(chained))
        return rows or self._read_messages(key), validated_keys


def derive_recent(
    self: ConversationLog,
    key: str,
    max_messages: int = 20,
    roles: AbstractSet[str] | None = None,
) -> list[dict]:
    """Guarded twin of :meth:`recent` (see :meth:`derive_messages`)."""
    with self.derivation_hold(transcript_lock_stems(key)):
        self._withhold_if_restricted(key)
        return self.recent(key, max_messages, roles)
