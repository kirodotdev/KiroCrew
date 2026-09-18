"""Explicit cleanup sweep over the two on-disk ledgers — list, then purge.

Neither ledger is ever reclaimed by the product. Closing a dashboard tab
preserves a session ledger and so does permanently deleting its history, both
deliberately (see ``docs/system-specs/modules/session-work-ledger.md`` §2): a
transcript can be recreated by another process after any in-process owner check,
a stale ledger is reversible and deleting a successor's resumable state is not.
The conductor work ledger has no delete path at all. So the primitives that DO
delete — ``session_ledger.purge_matching`` and ``work_ledger.purge_conductor`` —
have no caller on any request path, and this module is the operator-run caller
that gives them one: an explicit maintenance command, never a hook.

REFUSE-SAFE IS THE WHOLE DESIGN. Every rule here answers "leave it alone" unless
the record itself proves it is finished:

* A session ledger qualifies on a phase in :data:`session_ledger.TERMINAL_PHASES`
  plus age. An in-flight phase is never a candidate, at any age, with NO
  exception -- not even when the session it belongs to is gone from this
  machine. The ledger is exactly what a resumed loop reads to recover its next
  step, "gone" is a disk inference, and an irreplaceable record is the wrong
  place to act on an inference. For the same reason this module does not try to
  tell whether a session still exists at all: a conductor that holds no items is
  simply kept, and an operator who wants to see such stores lists the root.
* A work ledger qualifies when EVERY item is in a terminal state plus age. One
  open item disqualifies the whole conductor, and so does one unreadable item
  file — a torn record reads as absent to :func:`work_ledger.list_work_items`,
  so "no open items" would otherwise be provable by damaging one. The
  classification is :func:`work_ledger.census_items`, the store's own, so the
  scanner here and the locked recheck in ``purge_conductor`` cannot disagree.

  That census answers the worker-binding question too, which is why there is no
  separate binding scan: a binding names ONE conductor and one of ITS items, so a
  binding into a ledger whose every item is terminal is by definition a binding
  onto a terminal item — the state ``work_ledger`` itself calls stale and lets the
  next bind replace. A binding whose item is still open is already covered,
  because that open item keeps the whole conductor. The sweep therefore leaves
  ``bindings/`` alone: a binding outliving its conductor is the half-state the
  store documents as replaceable, and deleting a worker's only report channel on
  a guess is the larger risk.
* A record this module cannot parse is REPORTED, not purged, unless the caller
  asks for that separately. Unreadable is a reason to look, not to delete. And
  damage is classified LAST, after the open-item and age gates: one torn item
  file must not be able to carry a live open item into the deletion that flag
  authorises, and a file being replaced right now can itself read as damaged.
* Every string this module prints comes off disk, so every one of them is
  sanitised first. A ledger key carries whatever a channel put in a session key
  and a phase carries whatever a model wrote, so an unescaped line lets stored
  bytes move the cursor, repaint the summary, or retitle the terminal.

THE ELIGIBILITY DECISION IS RE-TAKEN UNDER THE LOCK, by the store that owns it.
This module selects; ``session_ledger.purge_matching(guard=...)`` and
``work_ledger.purge_conductor`` re-establish that the record is still finished
inside its own hold and refuse otherwise. A selection made outside the lock is a
snapshot, and the gateway keeps running while an operator reads it -- so nothing
here re-scans to "confirm" the report; the store's refusal is the confirmation.

THIS MODULE KNOWS NOTHING ABOUT SESSIONS, deliberately. ``session_ledger``
states that it never imports dashboard state, and ``work_ledger`` reaches the
product through one routes module; the sweep keeps the same discipline. It reads
the two ledger stores and nothing else, so every verdict here is one the record
itself supports.
"""

from __future__ import annotations

import json
import logging
import math
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from kiro_crew import session_ledger as sl
from kiro_crew import work_ledger as wl

logger = logging.getLogger(__name__)

#: The two ledgers, named the way every printed line names them.
KIND_SESSION = "session"
KIND_WORK = "work"

#: Default idle window. A month is long enough that a workstream picked up again
#: after a holiday still has its state, and the flag exists for a caller who
#: wants a different answer.
DEFAULT_OLDER_THAN_DAYS = 30

#: Directory name under the work-ledger root that is NOT a conductor store.
_BINDINGS_DIR_NAME = "bindings"

#: Reason for a store directory that is a symbolic link or junction: it names
#: somewhere else, and a delete aimed through it would land there.
_LINK_REASON = "store directory is a link, not a directory; report only -- never followed"
#: Reported for a store whose breadcrumb key resolves to a DIFFERENT directory.
#: Listed so an operator can see it and remove it by hand, never purged: the
#: delete primitives are aimed by key, so aiming one here would remove the
#: canonical ledger this scan never listed.
_MISMATCH_REASON = "store path does not match its own slot_key; remove it by hand after checking it"

#: Reported for a store with no readable ``slot_key`` breadcrumb. Neither delete
#: primitive can be aimed at it, so it is listed for a human.
_NO_BREADCRUMB_REASON = "no slot_key breadcrumb, so no purge can name it"


@dataclass(frozen=True)
class Candidate:
    """One ledger the sweep would remove, or would report without removing.

    ``key`` is the ledger's own identity, read from its ``slot_key`` breadcrumb —
    the store directory name is a readable fold plus a digest and is deliberately
    not decodable, so the breadcrumb is the only way back to the key a purge must
    name. A candidate with no readable breadcrumb is reported with an empty key
    and is never purged: neither store's delete primitive can be aimed at it.
    """

    kind: str
    store: str
    key: str
    detail: str
    age_days: float
    reason: str
    unreadable: bool
    path: Path
    addressable: bool = True

    @property
    def purgeable(self) -> bool:
        """Whether a purge can actually name this record.

        Both delete primitives are aimed by KEY and resolve the directory
        themselves, so a store is purgeable only when its key is readable AND the
        directory this scan looked at is the one that key resolves to. See
        ``addressable`` and :func:`_canonical_dir`.
        """
        return bool(self.key) and self.addressable


@dataclass(frozen=True)
class SweepReport:
    """What one scan found: what would go, and how many were left alone.

    ``kept`` is a COUNT, not a list of reasons. The reasons had one consumer --
    a test asserting the string -- and the invariant a keep rule encodes is
    "this store is absent from ``candidates``", which is what the tests assert
    instead.
    """

    candidates: tuple[Candidate, ...]
    kept: int
    older_than_days: float

    @property
    def unreadable(self) -> tuple[Candidate, ...]:
        return tuple(c for c in self.candidates if c.unreadable)

    @property
    def removable(self) -> tuple[Candidate, ...]:
        """Candidates a plain ``--purge`` may remove: readable and addressable."""
        return tuple(c for c in self.candidates if c.purgeable and not c.unreadable)

    @property
    def report_only(self) -> tuple[Candidate, ...]:
        """Readable stores listed for a human and never purged by this command."""
        return tuple(c for c in self.candidates if not c.unreadable and not c.purgeable)


@dataclass(frozen=True)
class PurgeResult:
    """What a purge actually removed, and what it declined to.

    ``stale`` holds candidates the re-derived scan does not name — see
    :func:`purge`. They are a normal outcome, not an error: something changed
    the record between the report and the delete, and the delete stood down.
    """

    removed: tuple[Candidate, ...]
    failed: tuple[Candidate, ...]
    skipped_unreadable: tuple[Candidate, ...]
    skipped_unaddressable: tuple[Candidate, ...]
    stale: tuple[Candidate, ...] = ()


# --------------------------------------------------------------------------- #
# Terminal-safe rendering
# --------------------------------------------------------------------------- #

#: Everything this module prints is read off disk, and none of it is trusted.
#: A ledger key is a session key -- a channel puts arbitrary text in one -- a
#: phase and a goal are model-written, and a store directory name can be
#: hand-created. Printed raw, any of them can carry an ESC sequence that moves
#: the cursor, clears the line, repaints the summary count the operator is about
#: to act on, or retitles the window. ``_CONTROL`` matches every C0 and C1
#: control character (including ESC and DEL) so a sequence loses its
#: introducer and renders inert.
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")

#: Per-field print ceiling. A clamped-but-full ledger field is 2000 characters
#: and a store name can be 89; a report line is for reading, so a long value is
#: elided rather than allowed to push the reason off the screen.
_PRINT_MAX = 120


def _safe(value: object) -> str:
    """*value* as one printable line: controls stripped, length capped.

    Replaces rather than deletes, so a stripped sequence leaves a visible mark
    instead of silently closing up into a different-looking string.
    """
    text = value if isinstance(value, str) else str(value)
    cleaned = _CONTROL.sub("\ufffd", text)
    if len(cleaned) > _PRINT_MAX:
        cleaned = cleaned[: _PRINT_MAX - 1] + "\u2026"
    return cleaned


# --------------------------------------------------------------------------- #
# Age
# --------------------------------------------------------------------------- #


def _parse_iso(value: Any) -> datetime | None:
    """An aware ``datetime`` for a stored stamp, or ``None`` when it is not one.

    Both stores write ``datetime.now().astimezone().isoformat()``, so a stored
    stamp normally carries an offset; a naive one (hand-edited, or written by a
    build that did not) is read as local time rather than discarded.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.astimezone() if parsed.tzinfo is None else parsed


def _mtime(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).astimezone()
    except OSError:
        return None


def _link_candidate(kind: str, directory: Path, key: str, now: datetime) -> Candidate:
    """A store directory that is a link: listed so an operator sees it, never aimed at.

    ``addressable=False`` keeps it out of every purge path whatever flags are
    given -- the primitives refuse a linked store too, but a report should not
    offer what the store will refuse. Age is the link's own mtime.
    """
    return Candidate(
        kind=kind,
        store=directory.name,
        key=key,
        detail="link",
        age_days=_age_days(None, directory, now),
        reason=_LINK_REASON,
        unreadable=False,
        path=directory,
        addressable=False,
    )


def _age_of(moment: datetime | None, now: datetime) -> float:
    """Age in days of *moment*; ``inf`` when there is none, so a ``min`` ignores it."""
    if moment is None:
        return float("inf")
    return max((now - moment).total_seconds(), 0.0) / 86400.0


def _age_days(stamp: Any, fallback: Path, now: datetime) -> float:
    """Age in days from *stamp*, falling back to *fallback*'s mtime.

    The fallback is what makes a record written before its terminal stamp existed
    — or one whose stamp was cleared — measurable at all. An age of ``0.0`` when
    neither is available means "assume brand new", which fails toward keeping the
    ledger.
    """
    moment = _parse_iso(stamp) or _mtime(fallback)
    if moment is None:
        return 0.0
    return max((now - moment).total_seconds(), 0.0) / 86400.0


# --------------------------------------------------------------------------- #
# Session ledgers
# --------------------------------------------------------------------------- #


def _canonical_dir(kind: str, key: str) -> Path | None:
    """Where *key*'s ledger lives by the store's own naming, or ``None``.

    The check that keeps a scan's PATH and a purge's KEY talking about the same
    store. Both primitives are aimed by key and resolve the directory themselves,
    so a store whose ``slot_key`` breadcrumb names a key that resolves ELSEWHERE
    is not deletable through them: a copy of a ledger, or a hand-made directory
    carrying another ledger's breadcrumb, would send the delete at the canonical
    store instead — one this scan never listed and an operator never saw on the
    report they approved.
    """
    try:
        return sl.ledger_dir(key) if kind == KIND_SESSION else wl.conductor_dir(key)
    except (ValueError, wl.WorkLedgerError):
        return None


def _path_matches_key(kind: str, key: str, directory: Path) -> bool:
    """Whether *directory* is the store *key* resolves to. Never raises."""
    canonical = _canonical_dir(kind, key)
    if canonical is None:
        return False
    try:
        return canonical.resolve() == directory.resolve()
    except OSError:
        return canonical == directory


def _read_breadcrumb(directory: Path) -> str:
    try:
        return (directory / "slot_key").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""


def _damaged_session_age(directory: Path, now: datetime) -> float:
    """A damaged session ledger's idle age: the NEWER of its directory and state file.

    The record has no stamp to read. The directory's mtime sees every atomic
    replace; the state file's mtime sees an in-place write that damaged it. The
    smaller age wins, so a store touched either way inside the window is kept.
    """
    return min(
        _age_days(None, directory, now),
        _age_days(None, directory / sl._STATE_FILE, now),
    )


def _session_age_days(state: dict[str, Any], directory: Path, now: datetime) -> float:
    """A session ledger's idle age: the NEWER of ``finished_at`` and the last write.

    ``finished_at`` alone is not the ledger's age. ``session_ledger.record``
    re-stamps it only on a terminal PHASE write; a goal, artifact or event-only
    update to an already-terminal ledger leaves it untouched while replacing
    ``state.json``, so a record touched a minute ago could read as thirty days
    idle. The state file's mtime is that write, so the age is the smaller of the
    two readings. Used by the scanner and re-applied by the purge guard under the
    lock, so the two cannot disagree.
    """
    state_path = directory / sl._STATE_FILE
    return min(
        _age_days(state.get("finished_at"), state_path, now),
        _age_days(None, state_path, now),
    )


def _read_session_state(directory: Path) -> tuple[dict[str, Any] | None, str]:
    """(state, why it is unreadable). A readable record answers ``(state, "")``.

    Deliberately NOT ``session_ledger._read_state_unlocked``: that folds every
    failure into an empty record, which is the right answer for a turn that must
    keep going and the wrong one here, where "empty" and "damaged" lead to
    opposite decisions.
    """
    path = directory / sl._STATE_FILE
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return None, "no state file"
    except OSError as exc:
        return None, f"state file unreadable ({exc.strerror or exc})"
    if size > sl._MAX_STATE_BYTES:
        return None, "state file over the size ceiling"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None, "state file is not readable JSON"
    if not isinstance(raw, dict):
        return None, "state file is not a JSON object"
    return raw, ""


def _scan_session_ledgers(*, older_than_days: float, now: datetime) -> tuple[list[Candidate], int]:
    """Session-ledger candidates, and how many were left alone.

    A session ledger's candidacy is its TERMINAL PHASE plus age, and nothing
    widens that. See the module docstring.
    """
    candidates: list[Candidate] = []
    kept = 0
    root = sl._ledger_root()
    try:
        children = sorted(p for p in root.iterdir() if p.is_dir())
    except FileNotFoundError:
        # No store yet: a machine that never recorded a ledger has nothing to
        # sweep, and "nothing" is the truth.
        return candidates, kept
    # Any other failure to read the ROOT propagates. Folding it into "no
    # candidates" would print a clean empty report over a store the sweep could
    # not see, and the operator would believe there is nothing to clean.
    for directory in children:
        key = _read_breadcrumb(directory)
        if sl.is_link(directory):
            candidates.append(_link_candidate(KIND_SESSION, directory, key, now))
            continue
        state, damaged = _read_session_state(directory)
        if state is None:
            # The age gate runs FIRST for a damaged record too, and the reading
            # is the NEWER of the directory and the state file: ``atomic_write``
            # renames into this directory on every write, so a live ledger's
            # directory is fresh, and an in-place write to ``state.json`` --
            # a truncation, a hand edit -- freshens the file without the
            # directory. A file being replaced right now can read as unreadable
            # — a Windows read of a file another handle holds open raises — so
            # without this gate a live write would be reported as damage and,
            # with ``--purge-unreadable``, deleted. ``_still_finished`` applies
            # the same reading under the lock.
            age = _damaged_session_age(directory, now)
            if age < older_than_days:
                kept += 1
                continue
            reason = damaged
            if not key:
                reason = f"{damaged}; no slot_key breadcrumb, so no purge can name it"
            candidates.append(
                Candidate(
                    kind=KIND_SESSION,
                    store=directory.name,
                    key=key,
                    detail="phase=?",
                    age_days=age,
                    reason=reason,
                    unreadable=True,
                    path=directory,
                    addressable=bool(key) and _path_matches_key(KIND_SESSION, key, directory),
                )
            )
            continue
        phase = state.get("phase") if isinstance(state.get("phase"), str) else ""
        age = _session_age_days(state, directory, now)
        detail = f"phase={phase or '(none)'}"
        if phase not in sl.TERMINAL_PHASES:
            kept += 1
            continue
        if age < older_than_days:
            kept += 1
            continue
        if not key or not _path_matches_key(KIND_SESSION, key, directory):
            # Same verdict either way -- report for a human, never aim a
            # primitive -- but the reason must say which, because the remedy
            # differs: no breadcrumb is a store whose writer died before it
            # landed; a mismatch is a copy.
            candidates.append(
                Candidate(
                    kind=KIND_SESSION,
                    store=directory.name,
                    key=key,
                    detail=detail,
                    age_days=age,
                    reason=_MISMATCH_REASON if key else _NO_BREADCRUMB_REASON,
                    unreadable=False,
                    path=directory,
                    addressable=False,
                )
            )
            continue
        candidates.append(
            Candidate(
                kind=KIND_SESSION,
                store=directory.name,
                key=key,
                detail=detail,
                age_days=age,
                reason=f"phase {phase} + idle {age:.0f}d",
                unreadable=False,
                path=directory,
            )
        )
    return candidates, kept


# --------------------------------------------------------------------------- #
# Work ledgers
# --------------------------------------------------------------------------- #


def _scan_work_ledgers(*, older_than_days: float, now: datetime) -> tuple[list[Candidate], int]:
    candidates: list[Candidate] = []
    kept = 0
    root = wl._work_ledger_root()
    try:
        children = sorted(p for p in root.iterdir() if p.is_dir() and p.name != _BINDINGS_DIR_NAME)
    except FileNotFoundError:
        return candidates, kept
    # Same as the session scanner: an unreadable root is an error, not "empty".
    for directory in children:
        # The breadcrumb is the ONLY source of a store's key, for both kinds. The
        # header carries a ``slot_key`` field too, but recovering the key from it
        # would let a store whose breadcrumb write failed be purged -- and the
        # rule this module states everywhere else is that a store it cannot
        # name is reported for a human, never deleted.
        key = _read_breadcrumb(directory)
        if sl.is_link(directory):
            candidates.append(_link_candidate(KIND_WORK, directory, key, now))
            continue
        census = wl.census_items(directory)
        open_items, closed, unreadable = census.open_items, census.closed, census.unreadable
        item_damage = census.damage
        detail = f"items={open_items} open/{closed} closed"
        header = directory / "conductor.json"
        # Age is the NEWEST of three readings: the newest item close, the header's
        # own mtime, and the newest write under ``items/`` by mtime. The
        # conductor's ``goal`` action rewrites the header with no item involved,
        # so a conductor that just bumped its round on old closed items is live;
        # and an item the census could NOT read carries no stamp and is written
        # without touching the header, so a crash-torn write into an old
        # conductor would otherwise be invisible to this window and deletable
        # under ``--purge-unreadable`` the moment it landed. ``purge_conductor``
        # re-applies the same three readings under its lock (``_newest_activity``).
        age = min(
            _age_days(census.newest_closed_at, header if header.exists() else directory, now),
            _age_days(None, header if header.exists() else directory, now),
            _age_of(census.newest_write_at, now),
        )
        # ONE OPEN ITEM OUTRANKS EVERY OTHER READING, damage included. A torn
        # item file makes the whole conductor unreadable, and an unreadable
        # record is deletable with ``--purge-unreadable`` — so classifying
        # damage first would let one torn file carry a live open item, and the
        # work its worker is still reporting against, into that deletion.
        if open_items:
            kept += 1
            continue
        if age < older_than_days:
            kept += 1
            continue
        if not closed and not unreadable and not item_damage and header.exists():
            # NO ITEMS AT ALL, and a header, and this runs BEFORE the header is
            # judged for readability. A
            # conductor that never created an item is finished-LOOKING, not
            # finished: the same shape is a ledger a conductor opened seconds
            # ago and a ledger whose creator died before dispatching, and no
            # lock can hold "its session is gone" still through a delete. So it
            # is kept, always -- a torn header on it included, because
            # ``--purge-unreadable`` authorises deleting a record that cannot be
            # READ, not one that was never shown to be finished, and an empty
            # conductor with a torn header is both. ``purge_conductor`` refuses
            # the same shape under its lock. An operator who wants to see such
            # stores lists the work-ledger root; this command reports only what
            # it can act on. An items directory that cannot be enumerated is NOT
            # this case: that is damage with an unknown item count, classified
            # below. Nor is a directory with NEITHER header nor items: that is
            # the residue a purge leaves when a lock file could not be unlinked
            # (on Windows a queued writer holds its handle through the
            # post-release unlink), it holds nothing a conductor wrote, and it
            # is classified below as damage -- listed, and removable with
            # ``--purge-unreadable``.
            kept += 1
            continue
        header_damage = wl.header_damage(directory)
        if item_damage or unreadable or header_damage:
            damaged = item_damage or (
                f"{unreadable} unreadable item record(s)" if unreadable else header_damage
            )
            if not key:
                damaged = f"{damaged}; no slot_key breadcrumb, so no purge can name it"
            candidates.append(
                Candidate(
                    kind=KIND_WORK,
                    store=directory.name,
                    key=key,
                    detail=detail,
                    age_days=age,
                    reason=damaged,
                    unreadable=True,
                    path=directory,
                    addressable=_path_matches_key(KIND_WORK, key, directory),
                )
            )
            continue
        if not key or not _path_matches_key(KIND_WORK, key, directory):
            # Either nothing names this store, or its name resolves elsewhere.
            # Both are the same verdict -- report for a human, never aim a
            # primitive -- but the reason has to say which, because the remedy
            # differs: a missing breadcrumb is a store whose writer died before
            # the breadcrumb landed; a mismatch is a copy.
            # The record READS; what is wrong is its name. ``unreadable`` is a
            # statement about the record, so it stays False and the store is
            # rendered ``report`` and counted report-only rather than as
            # corruption -- ``--purge-unreadable`` is about records that will
            # not parse, and this one parses. A damaged store whose name is also
            # wrong took the branch above and is both.
            candidates.append(
                Candidate(
                    kind=KIND_WORK,
                    store=directory.name,
                    key=key,
                    detail=detail,
                    age_days=age,
                    reason=(
                        _MISMATCH_REASON
                        if key
                        else "no slot_key breadcrumb, so no purge can name it"
                    ),
                    unreadable=False,
                    path=directory,
                    addressable=False,
                )
            )
            continue
        candidates.append(
            Candidate(
                kind=KIND_WORK,
                store=directory.name,
                key=key,
                detail=detail,
                age_days=age,
                reason=f"every item closed + idle {age:.0f}d",
                unreadable=False,
                path=directory,
            )
        )
    return candidates, kept


# --------------------------------------------------------------------------- #
# Public surface
# --------------------------------------------------------------------------- #


def scan(*, older_than_days: float = DEFAULT_OLDER_THAN_DAYS) -> SweepReport:
    """List every ledger the sweep considers finished. Changes nothing.

    Never raises for a store that is ABSENT, or for any single record or
    directory inside a store that is damaged or unrecognised: a maintenance
    report that crashes on the one damaged record is worse than one that names
    it. It DOES raise ``OSError`` when a ledger ROOT exists but cannot be read --
    that is not a damaged record to name, it is the whole store the sweep cannot
    see, and a clean empty report over it would be a false "nothing to clean".
    The CLI turns that into a non-zero exit with the error text.
    """
    moment = datetime.now().astimezone()
    threshold = max(float(older_than_days), 0.0)
    session_candidates, session_kept = _scan_session_ledgers(older_than_days=threshold, now=moment)
    work_candidates, work_kept = _scan_work_ledgers(older_than_days=threshold, now=moment)
    return SweepReport(
        candidates=tuple(session_candidates + work_candidates),
        kept=session_kept + work_kept,
        older_than_days=threshold,
    )


def purge(report: SweepReport, *, include_unreadable: bool = False) -> PurgeResult:
    """Delete the candidates *report* named. Irreversible.

    Session ledgers go through ``session_ledger.purge_matching`` by EXACT key, so
    the only thing that can match is a store whose breadcrumb holds exactly one
    of the keys this call names — a lossy fold would be the one way to remove a
    ledger the report never listed, which is why the primitive takes none — and
    with a ``guard`` that re-reads the record INSIDE that ledger's own lock and stands
    down unless it is still terminal (or still damaged, for the unreadable
    class). Work ledgers go one at a time through
    :func:`work_ledger.purge_conductor`, which re-runs the item census inside the
    conductor lock and REFUSES on any open or unreadable item.

    The guard and the census are the checks that hold a lock the store's own
    writers take, so they are the binding decision. ``_create_item`` holds the
    conductor lock across its whole transaction, so a conductor cannot mint an
    item between the census and the removal.

    An unreadable record is skipped unless *include_unreadable* is set, and a
    record with no readable key is always skipped: neither primitive can be aimed
    at a store whose identity is unknown.

    A report is a snapshot: the gateway keeps running while an operator reads
    it, so a ledger can be reopened, an item created, or a header touched between
    the scan and this call. That is why this function does NOT trust the report:
    every delete is re-decided by the store, under its own lock, with the same
    window the report was built with, and a refusal there is recorded as
    ``stale`` — the normal outcome for a ledger that came back to life.
    """
    removed: list[Candidate] = []
    failed: list[Candidate] = []
    skipped_unreadable: list[Candidate] = []
    skipped_unaddressable: list[Candidate] = []
    stale: list[Candidate] = []

    session_targets: list[Candidate] = []
    for candidate in report.candidates:
        if candidate.unreadable and not include_unreadable:
            skipped_unreadable.append(candidate)
            continue
        if not candidate.purgeable:
            skipped_unaddressable.append(candidate)
            continue
        # Re-assert the identity the primitives are aimed by, immediately before
        # aiming one. The scanners already refuse a mismatched store; this is the
        # same check at the last moment a caller-supplied report could disagree
        # with the store's own naming, and it fails toward not deleting.
        if not _path_matches_key(candidate.kind, candidate.key, candidate.path):
            skipped_unaddressable.append(candidate)
            continue
        if candidate.kind == KIND_SESSION:
            session_targets.append(candidate)
            continue
        try:
            gone = wl.purge_conductor(
                candidate.key,
                allow_unreadable=include_unreadable,
                idle_for=timedelta(days=report.older_than_days),
            )
        except wl.WorkLedgerError as exc:
            if exc.code == wl.CODE_LEDGER_NOT_FINISHED:
                # The store refused under its own lock: the ledger came back to
                # life after the scan. That is the guard working, not a failure.
                stale.append(candidate)
                continue
            logger.debug("ledger sweep: work ledger purge refused", exc_info=True)
            failed.append(candidate)
            continue
        except OSError:
            logger.debug("ledger sweep: work ledger purge failed", exc_info=True)
            failed.append(candidate)
            continue
        (removed if gone else failed).append(candidate)

    if session_targets:
        keys = {candidate.key for candidate in session_targets}
        wanted = {c.path: c for c in session_targets}
        moment = datetime.now().astimezone()

        def _still_finished(dir_path: Path) -> bool:
            """Re-decide *dir_path* under its own ledger lock. See :func:`purge`.

            Applies the WHOLE rule the scanner applied -- terminal phase AND age --
            not just the phase. ``session_ledger.record`` re-stamps ``finished_at``
            on every terminal phase write, so a session that resumed and finished
            again between the re-scan above and this hold is terminal but young,
            and young means it was just in use.
            """
            candidate = wanted.get(dir_path)
            if candidate is None:
                return False
            state, damaged = _read_session_state(dir_path)
            if candidate.unreadable:
                # Still purgeable only while it is still damaged AND still old:
                # a record that now parses was being written when the scan read
                # it, and a damaged store whose DIRECTORY is fresh is being
                # written to now -- ``atomic_write`` renames into it -- so the
                # scanner's age gate for a damaged record is re-applied here
                # with the same reading, or a live write that reads as damage
                # mid-replace would be deleted on a stale report.
                if not (state is None and damaged):
                    return False
                return _damaged_session_age(dir_path, moment) >= report.older_than_days
            if state is None:
                return False
            phase = state.get("phase")
            if not (isinstance(phase, str) and phase in sl.TERMINAL_PHASES):
                return False
            return _session_age_days(state, dir_path, moment) >= report.older_than_days

        # ``purge_matching`` reports a count, not which keys went, so existence is
        # rechecked per candidate — the count cannot tell a caller whose store
        # survived, and this report names stores individually.
        sl.purge_matching(keys, guard=_still_finished)
        for candidate in session_targets:
            if not candidate.path.exists():
                removed.append(candidate)
            elif _still_finished(candidate.path):
                failed.append(candidate)
            else:
                stale.append(candidate)

    return PurgeResult(
        removed=tuple(removed),
        failed=tuple(failed),
        skipped_unreadable=tuple(skipped_unreadable),
        skipped_unaddressable=tuple(skipped_unaddressable),
        stale=tuple(stale),
    )


def render(report: SweepReport, *, purged: PurgeResult | None = None) -> list[str]:
    """The printed lines, built as data so a test can assert on them.

    One line per candidate — kind, store directory, key, phase or item counts,
    age, and why it qualified — then one summary line. Nothing here decides
    anything; the decision was :func:`scan`'s.
    """
    lines: list[str] = []
    for candidate in sorted(report.candidates, key=lambda c: (c.kind, c.store)):
        if candidate.unreadable:
            mark = "unreadable"
        elif not candidate.purgeable:
            mark = "report    "
        else:
            mark = "candidate "
        # Every field below came off disk: see ``_safe``. The kind and the age are
        # this module's own, so they are the only two printed as they are.
        key = _safe(candidate.key) if candidate.key else "(no breadcrumb)"
        lines.append(
            f"  {mark} {candidate.kind:<7} {_safe(candidate.store)}  key={key}  "
            f"{_safe(candidate.detail)}  age={candidate.age_days:.0f}d  "
            f"— {_safe(candidate.reason)}"
        )
    if not report.candidates:
        lines.append("  no ledger is older than the threshold and finished")
    counts = [
        f"{len(report.removable)} candidate(s)",
        f"{len(report.unreadable)} unreadable",
        f"{len(report.report_only)} report-only",
        f"{report.kept} left alone",
    ]
    lines.append("  " + " · ".join(counts))
    if purged is not None:
        lines.append(
            f"  purged {len(purged.removed)}"
            + (f", failed {len(purged.failed)}" if purged.failed else "")
            + (
                f", skipped {len(purged.skipped_unreadable)} unreadable"
                if purged.skipped_unreadable
                else ""
            )
            + (
                f", skipped {len(purged.skipped_unaddressable)} without a key"
                if purged.skipped_unaddressable
                else ""
            )
            + (
                f", stood down on {len(purged.stale)} that changed since the scan"
                if purged.stale
                else ""
            )
        )
    return lines


# --------------------------------------------------------------------------- #
# The command
# --------------------------------------------------------------------------- #


_purge = purge  # the keyword ``purge`` below is the flag; this is the function


def run_command(*, purge: bool, older_than_days: float | None, purge_unreadable: bool) -> None:
    """``kirocrew ledger-sweep``: print (and with *purge*, delete) the finished ledgers.

    Its OWN top-level command, not a ``doctor`` mode. ``doctor`` is read-only by
    its own contract -- a diagnostic run because something broke must not unlink
    files -- and ``--purge`` is irreversible, so the two do not belong under one
    name. A separate command also removes a whole class of hazard: inside
    ``doctor``'s flat flag namespace a modifier could be parsed without its mode,
    so bare ``doctor --purge`` ran the health pass and exited 0, reading exactly
    like a purge that found nothing; here every flag belongs to this command and
    argparse refuses it anywhere else. Dry run is the default and changes
    nothing -- the purge is a second, explicit invocation, because the deletion is
    irreversible and the report is what makes it reviewable.

    Deliberately NOT an MCP tool and never automatic. Neither ledger is reclaimed
    on tab close, on permanent history deletion, or at gateway start, all by
    design (``docs/system-specs/modules/session-work-ledger.md`` §2); this stays
    an operator command so no model-reachable surface can delete a session's
    resumable state.
    """
    # ``None`` means "the module's default", so the window has ONE owner. A
    # literal repeated in argparse would be a second default that drifts silently.
    window = DEFAULT_OLDER_THAN_DAYS if older_than_days is None else float(older_than_days)
    # ``math.isfinite`` FIRST, and not a bare ``window < 0``: every comparison
    # against NaN is false, so a NaN window passes the sign check AND then makes
    # every ``age < window`` false, which admits every ledger in both stores at
    # once. Infinity is refused alongside it -- it is not a window, and taking it
    # as "collect nothing" would make a typo look like a working command.
    if not math.isfinite(window) or window < 0:
        print("  ❌ --older-than-days must be a finite number of days, and not negative")
        sys.exit(2)
    header = "Ledger Sweep" + ("" if purge else " (dry run — nothing is deleted)")
    print(f"{header}\n")
    try:
        report = scan(older_than_days=window)
    except OSError as exc:
        print(f"  ❌ could not read the ledger stores: {exc}")
        sys.exit(1)
    result = _purge(report, include_unreadable=purge_unreadable) if purge else None
    for line in render(report, purged=result):
        print(line)
    if not purge and report.removable:
        # The hint carries the window THIS report was built with. A bare
        # ``--purge`` would re-scan at the default, so an operator who previewed
        # with a larger, more conservative window and then followed the printed
        # command would irreversibly delete the ledgers between the two windows
        # -- ones the report they read never listed.
        print(f"\n  Purge them with: kirocrew ledger-sweep --purge --older-than-days {window:g}")
    if not purge and report.unreadable:
        print("  Unreadable records need --purge-unreadable, and are worth reading first.")
