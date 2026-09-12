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
  place to act on an inference. So ``--include-orphans`` widens the WORK half
  only: it admits a conductor that holds no items at all, which is the one
  finished-looking shape whose emptiness is not by itself evidence of anything.
* A work ledger qualifies when EVERY item is in a terminal state plus age. One
  open item disqualifies the whole conductor, and so does one unreadable item
  file — a torn record reads as absent to :func:`work_ledger.list_work_items`,
  so "no open items" would otherwise be provable by damaging one.

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
* An orphan — a ledger whose key names no session this machine still has — pays
  the age threshold too, and never relaxes any other rule. A live session's
  first transcript write lags its first ledger write, so a zero-age orphan is
  that race rather than garbage.
* Every string this module prints comes off disk, so every one of them is
  sanitised first. A ledger key carries whatever a channel put in a session key
  and a phase carries whatever a model wrote, so an unescaped line lets stored
  bytes move the cursor, repaint the summary, or retitle the terminal.

THE ELIGIBILITY DECISION IS RE-TAKEN UNDER THE LOCK, by the store that owns it.
This module selects; ``session_ledger.purge_matching(guard=...)`` and
``work_ledger.purge_conductor`` re-establish that the record is still finished
inside its own hold and refuse otherwise. A selection made outside the lock is a
snapshot, and the gateway keeps running while an operator reads it.

THE ORPHAN CHECK LIVES HERE, not in either store. ``session_ledger`` states that
it never imports dashboard state, and ``work_ledger`` reaches the product through
one routes module; a store that resolved session existence for itself would break
both. This module is the CLI layer (``cli_doctor`` is its only importer), so it
is where knowledge of transcripts and the session map is allowed to meet ledger
identity. The check is DISK-ONLY and therefore conservative: it asks
``history.transcript_stems`` and ``session_map.json`` — the real key-to-file
mapping rather than a guess at it — and it cannot see a live in-memory slot that
has not persisted anything yet, which is why it is opt-in and why an orphan still
has to be old.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from kiro_crew import session_ledger as sl
from kiro_crew import work_ledger as wl
from kiro_crew.config.paths import config_dir

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

#: Reported for a store whose breadcrumb key resolves to a DIFFERENT directory.
#: Listed so an operator can see it and remove it by hand, never purged: the
#: delete primitives are aimed by key, so aiming one here would remove the
#: canonical ledger this scan never listed.
_MISMATCH_REASON = "store path does not match its own slot_key; remove it by hand after checking it"


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
    include_orphans: bool

    @property
    def unreadable(self) -> tuple[Candidate, ...]:
        return tuple(c for c in self.candidates if c.unreadable)

    @property
    def removable(self) -> tuple[Candidate, ...]:
        """Candidates a plain ``--purge`` may remove: readable and addressable."""
        return tuple(c for c in self.candidates if c.purgeable and not c.unreadable)


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
# Session existence (the orphan question)
# --------------------------------------------------------------------------- #


def _session_map_keys() -> set[str]:
    """Every key in ``session_map.json``, read by path rather than by object.

    ``SessionMap()`` migrates and prunes as it loads; a read-only sweep must not
    rewrite the map it is only consulting.
    """
    path = config_dir() / "session_map.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return set()
    if not isinstance(raw, dict):
        return set()
    return {key for key in raw if isinstance(key, str)}


def _key_spellings(ledger_key: str) -> tuple[str, ...]:
    """*ledger_key* plus the dashboard spellings ``session_ledger`` strips.

    ``session_ledger.ledger_key`` removes ``dashboard:`` / ``dashboard_`` before
    storing, so one stored key legitimately corresponds to several live ones and a
    check against the stored spelling alone would call every dashboard session an
    orphan.
    """
    return (ledger_key, f"dashboard_{ledger_key}", f"dashboard:{ledger_key}")


def _has_transcript(ledger_key: str) -> bool:
    """Whether any spelling of *ledger_key* has a transcript on disk."""
    from kiro_crew.history import SESSIONS_DIR_NAME, transcript_stems

    sessions = config_dir() / SESSIONS_DIR_NAME
    for spelling in _key_spellings(ledger_key):
        for stem in transcript_stems(spelling):
            if (sessions / f"{stem}.jsonl").exists():
                return True
    return False


def _session_exists(ledger_key: str, mapped_keys: set[str]) -> bool:
    """Whether this machine still has the session *ledger_key* belongs to.

    Two disk sources, both permissive: a transcript under the sessions directory
    (live or archived-in-place — the history row a user can still open), or an
    entry in the session map (a conversation bound to a kiro-cli session, which a
    resume reads). Either one answers yes.
    """
    if any(spelling in mapped_keys for spelling in _key_spellings(ledger_key)):
        return True
    return _has_transcript(ledger_key)


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

    Takes no orphan argument: a session ledger's candidacy is its TERMINAL PHASE
    plus age, and nothing widens that. See the module docstring.
    """
    candidates: list[Candidate] = []
    kept = 0
    root = sl._ledger_root()
    try:
        children = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return candidates, kept
    for directory in children:
        key = _read_breadcrumb(directory)
        state, damaged = _read_session_state(directory)
        if state is None:
            # The age gate runs FIRST for a damaged record too, and the fallback
            # is the DIRECTORY mtime: ``atomic_write`` renames into this
            # directory on every write, so a live ledger's directory is fresh.
            # A file being replaced right now can read as unreadable — a
            # Windows read of a file another handle holds open raises — so
            # without this gate a live write would be reported as damage and,
            # with ``--purge-unreadable``, deleted.
            age = _age_days(None, directory, now)
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
                )
            )
            continue
        phase = state.get("phase") if isinstance(state.get("phase"), str) else ""
        age = _age_days(state.get("finished_at"), directory / sl._STATE_FILE, now)
        detail = f"phase={phase or '(none)'}"
        if phase not in sl.TERMINAL_PHASES:
            kept += 1
            continue
        if age < older_than_days:
            kept += 1
            continue
        if not _path_matches_key(KIND_SESSION, key, directory):
            candidates.append(
                Candidate(
                    kind=KIND_SESSION,
                    store=directory.name,
                    key=key,
                    detail=detail,
                    age_days=age,
                    reason=_MISMATCH_REASON,
                    unreadable=True,
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


def _work_ledger_key(directory: Path) -> str:
    """The conductor's session key: breadcrumb first, stored record second."""
    key = _read_breadcrumb(directory)
    if key:
        return key
    try:
        raw = json.loads((directory / "conductor.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return ""
    stored = raw.get("slot_key") if isinstance(raw, dict) else None
    return stored if isinstance(stored, str) else ""


def _header_damage(header: Path) -> str:
    """Why *header* cannot be trusted, or ``""`` when it reads as a record.

    Presence is not readability. An absent, torn, oversized or non-object
    ``conductor.json`` all mean the same thing for a delete decision -- this
    store's own account of itself is missing -- and treating only absence as
    damage let a malformed header read as a finished ledger.
    """
    try:
        size = header.stat().st_size
    except FileNotFoundError:
        return "no conductor record"
    except OSError as exc:
        return f"conductor record unreadable ({exc.strerror or exc})"
    if size > wl.MAX_RECORD_BYTES:
        return "conductor record over the size ceiling"
    try:
        raw = json.loads(header.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return "conductor record is not readable JSON"
    if not isinstance(raw, dict):
        return "conductor record is not a JSON object"
    return ""


def _item_census(directory: Path) -> tuple[int, int, int, str, str]:
    """``(open, closed, unreadable, newest closed_at, damage)`` over one conductor.

    Reads each item through ``work_ledger._read_json_record`` — the store's own
    reader, which already applies the size ceiling and answers ``None`` for
    absent, torn, oversized and non-UTF-8 alike — rather than a fourth
    hand-rolled parse in this module. It deliberately does NOT go through
    :func:`work_ledger.list_work_items`, which SKIPS an unreadable item: right
    for a listing, wrong for a delete decision, because a torn record would then
    be invisible and the conductor would look finished.

    A failure to ENUMERATE the directory is returned as *damage* and never folded
    into "zero items". An items directory this process cannot read is the one case
    where the count and the truth are unrelated: read as empty it selects the
    ordinary purge, which removes ``conductor.json`` and leaves the item data it
    could not see behind. An ABSENT directory is not damage — a conductor that
    never created an item has none.
    """
    open_items = closed = unreadable = 0
    newest = ""
    items_dir = directory / "items"
    try:
        with os.scandir(items_dir) as scan:
            names = sorted(entry.name for entry in scan if entry.is_file())
    except FileNotFoundError:
        return 0, 0, 0, "", ""
    except OSError as exc:
        return 0, 0, 0, "", f"items directory could not be read ({exc.strerror or exc})"
    for name in names:
        if not name.endswith(".json"):
            continue
        raw = wl._read_json_record(items_dir / name)
        if not isinstance(raw, dict):
            unreadable += 1
            continue
        state = raw.get("state")
        if not isinstance(state, str) or state not in wl.ITEM_STATES:
            # An unrecognised disposition is not evidence of closure.
            unreadable += 1
            continue
        if state in wl.TERMINAL_ITEM_STATES:
            closed += 1
            closed_at = raw.get("closed_at")
            if isinstance(closed_at, str) and closed_at > newest:
                newest = closed_at
        else:
            open_items += 1
    return open_items, closed, unreadable, newest, ""


def _scan_work_ledgers(
    *, older_than_days: float, include_orphans: bool, now: datetime, mapped_keys: set[str]
) -> tuple[list[Candidate], int]:
    candidates: list[Candidate] = []
    kept = 0
    root = wl._work_ledger_root()
    try:
        children = sorted(p for p in root.iterdir() if p.is_dir() and p.name != _BINDINGS_DIR_NAME)
    except OSError:
        return candidates, kept
    for directory in children:
        key = _work_ledger_key(directory)
        open_items, closed, unreadable, newest_closed, item_damage = _item_census(directory)
        detail = f"items={open_items} open/{closed} closed"
        header = directory / "conductor.json"
        age = _age_days(newest_closed, header if header.exists() else directory, now)
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
        header_damage = _header_damage(header)
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
        if not _path_matches_key(KIND_WORK, key, directory):
            candidates.append(
                Candidate(
                    kind=KIND_WORK,
                    store=directory.name,
                    key=key,
                    detail=detail,
                    age_days=age,
                    reason=_MISMATCH_REASON,
                    unreadable=True,
                    path=directory,
                    addressable=False,
                )
            )
            continue
        if not closed:
            # A conductor that never created an item is finished-LOOKING, not
            # finished: the same shape is a ledger a conductor opened seconds
            # ago and a ledger whose creator died before dispatching. Emptiness
            # is only evidence when the session it belongs to is also gone from
            # this machine, so it takes the orphan flag.
            orphan = include_orphans and bool(key) and not _session_exists(key, mapped_keys)
            if not orphan:
                kept += 1
                continue
            reason = f"no items, and no session for this key + idle {age:.0f}d"
        else:
            reason = f"every item closed + idle {age:.0f}d"
        candidates.append(
            Candidate(
                kind=KIND_WORK,
                store=directory.name,
                key=key,
                detail=detail,
                age_days=age,
                reason=reason,
                unreadable=False,
                path=directory,
            )
        )
    return candidates, kept


# --------------------------------------------------------------------------- #
# Public surface
# --------------------------------------------------------------------------- #


def scan(
    *,
    older_than_days: float = DEFAULT_OLDER_THAN_DAYS,
    include_orphans: bool = False,
) -> SweepReport:
    """List every ledger the sweep considers finished. Changes nothing.

    Never raises for a store that is absent, unreadable, or holds a directory
    this module does not recognise: a maintenance report that crashes on the one
    damaged record is worse than one that names it.
    """
    moment = datetime.now().astimezone()
    threshold = max(float(older_than_days), 0.0)
    mapped = _session_map_keys() if include_orphans else set()
    session_candidates, session_kept = _scan_session_ledgers(older_than_days=threshold, now=moment)
    work_candidates, work_kept = _scan_work_ledgers(
        older_than_days=threshold,
        include_orphans=include_orphans,
        now=moment,
        mapped_keys=mapped,
    )
    return SweepReport(
        candidates=tuple(session_candidates + work_candidates),
        kept=session_kept + work_kept,
        older_than_days=threshold,
        include_orphans=include_orphans,
    )


def purge(report: SweepReport, *, include_unreadable: bool = False) -> PurgeResult:
    """Delete the candidates *report* named. Irreversible.

    Session ledgers go through ``session_ledger.purge_matching`` with an identity
    fold and an empty folded set, so the only thing that can match is a store
    whose breadcrumb holds exactly one of the keys this call names — a lossy fold
    would be the one way to remove a ledger the report never listed — and with a
    ``guard`` that re-reads the record INSIDE that ledger's own lock and stands
    down unless it is still terminal (or still damaged, for the unreadable
    class). Work ledgers go one at a time through
    :func:`work_ledger.purge_conductor`, which re-runs the item census inside the
    conductor lock and REFUSES on any open or unreadable item.

    So eligibility is decided twice, deliberately, and the second decision is the
    binding one: this module's re-scan below is a cheap filter that avoids
    entering the store at all, while the guard and the census are the checks that
    hold a lock the store's own writers take. ``_create_item`` holds the
    conductor lock across its whole transaction, so a conductor cannot mint an
    item between the census and the removal.

    An unreadable record is skipped unless *include_unreadable* is set, and a
    record with no readable key is always skipped: neither primitive can be aimed
    at a store whose identity is unknown.

    THE REPORT IS RE-DERIVED FIRST, and a candidate the fresh scan does not
    name the same way is dropped as ``stale``. A report is a snapshot: the
    gateway keeps running while an operator reads it, so a ledger can be
    reopened, an item can be created, or a damaged file can be repaired between
    the scan and this call — and every one of those makes the ledger live again.
    The re-derivation runs :func:`scan` rather than a second copy of the rules,
    with the SAME window and orphan setting the report was built with, and the
    match is on kind, store directory AND the unreadable flag, so a record that
    merely changed class is dropped too. It narrows the window to one scan; it
    does not close it, because no lock spans both stores.
    """
    removed: list[Candidate] = []
    failed: list[Candidate] = []
    skipped_unreadable: list[Candidate] = []
    skipped_unaddressable: list[Candidate] = []
    stale: list[Candidate] = []

    fresh = scan(older_than_days=report.older_than_days, include_orphans=report.include_orphans)
    still_named = {(c.kind, c.store, c.unreadable) for c in fresh.candidates}

    session_targets: list[Candidate] = []
    for candidate in report.candidates:
        if candidate.unreadable and not include_unreadable:
            skipped_unreadable.append(candidate)
            continue
        if not candidate.purgeable:
            skipped_unaddressable.append(candidate)
            continue
        if (candidate.kind, candidate.store, candidate.unreadable) not in still_named:
            stale.append(candidate)
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
            gone = wl.purge_conductor(candidate.key, allow_unreadable=include_unreadable)
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

        def _still_finished(dir_path: Path) -> bool:
            """Re-decide *dir_path* under its own ledger lock. See :func:`purge`."""
            candidate = wanted.get(dir_path)
            if candidate is None:
                return False
            state, damaged = _read_session_state(dir_path)
            if candidate.unreadable:
                # Only still purgeable while it is still damaged: a record that
                # now parses was being written when the scan read it.
                return state is None and bool(damaged)
            if state is None:
                return False
            phase = state.get("phase")
            return isinstance(phase, str) and phase in sl.TERMINAL_PHASES

        # ``purge_matching`` reports a count, not which keys went, so existence is
        # rechecked per candidate — the count cannot tell a caller whose store
        # survived, and this report names stores individually.
        sl.purge_matching(keys, set(), lambda key: key, guard=_still_finished)
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
        mark = "unreadable" if candidate.unreadable else "candidate "
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
