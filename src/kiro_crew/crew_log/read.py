"""Reads over a crew log that more than one consumer needs: a page, and a listing.

Both functions here are BLOCKING and belong off the event loop. They are in the
storage package rather than in a route module because two consumers now want the
same answers -- the dashboard's own routes and the ``kirocrew-crew-log`` MCP
server's proxy leg -- and a second copy of a page reader is how two callers come
to disagree about what a page is.

Neither function makes an authorization claim. That is the same split
``crew_log.store`` states for itself: this layer has no caller identity to derive
a decision from, so the first caller with a permission model owns the decision,
and for both consumers here that caller is a dashboard route.
"""

from __future__ import annotations

import heapq
import json
import logging
import os
from collections import Counter
from pathlib import Path
from typing import Any, Final

from kiro_crew.crew_log import projection as projections
from kiro_crew.crew_log.errors import CrewLogError
from kiro_crew.crew_log.schema import KIND_SESSION
from kiro_crew.crew_log.store import LOG_FILE, crew_log_root
from kiro_crew.crew_log.store import now_ms as store_now_ms

logger = logging.getLogger(__name__)

#: Distinct refs a single page resolves. Each resolution opens the cited unit and
#: walks to the span, so the work is bounded per request rather than left to
#: however many citations a page happens to carry; identical refs on one page are
#: resolved once. Past the budget the entry keeps its ``ref`` and carries no
#: resolution, and the page says how many it left, so a reader is never shown a
#: silently unresolved citation.
MAX_PAGE_REFS: Final[int] = 25

#: Unit directories one listing is willing to OPEN. A listing walks the kind root
#: and folds each unit it accepts, so the cost is bounded here rather than by how
#: many sessions the host has ever run. The walk visits newest-mtime first and
#: reports ``scanned``/``truncated``, so a caller reading a truncated list knows
#: it is the newest window and not the whole tree.
MAX_LISTED_SCAN: Final[int] = 400

#: Unit directories one listing is willing to RETAIN while choosing which to open.
#: Separate from ``MAX_LISTED_SCAN`` and deliberately larger: the walk skips a
#: directory that is outside an ``active_within`` window or carries no readable
#: unit id WITHOUT counting it as scanned, so the candidate set has to be able to
#: absorb some skips before the scan cap is reached. It is a bound on MEMORY, not
#: on what a caller may see -- the newest this many are kept by mtime in a
#: fixed-size heap, so a host with a hundred thousand session logs holds this many
#: paths rather than all of them, and a set that had to be cut reports
#: ``truncated``.
MAX_LISTED_CANDIDATES: Final[int] = MAX_LISTED_SCAN * 2

#: Rows one listing returns, whatever a caller asks for.
MAX_LIST_LIMIT: Final[int] = 200


def read_page(session_id: str, start: int, end: int) -> dict[str, Any]:
    """One range of entries with their refs resolved. Blocking; runs off the loop."""
    handle = projections.open_session_log(session_id)
    if handle is None:
        return {
            "session_id": session_id,
            "exists": False,
            "from": start,
            "to": end,
            "last_seq": 0,
            "entries": [],
            "next_from": None,
            "refs_unresolved": 0,
        }
    # No vocabulary: a page renders history, so an unfamiliar line is shown
    # rather than made to refuse the lines around it.
    #
    # ``handle.last_seq`` is this instance's own cached figure -- the store's
    # docstring says it is authoritative only for its OWN appends -- and a reader
    # handle never appends, so a writer that grows the file after this handle
    # opened is invisible to it. The iteration below reads the file live and walks
    # the whole tail from ``start``, discarding what is past ``end`` rather than
    # never seeing it, so the true tail is observable here for free. Deriving
    # ``next_from`` from the cached figure instead would let a page return rows up
    # to ``end`` and still report that nothing follows, and a client that believes
    # it stops paging with entries left unread.
    observed_last = handle.last_seq
    entries: list[Any] = []
    for entry in handle.iter_from(start):
        if entry.seq > observed_last:
            observed_last = entry.seq
        if entry.seq <= end:
            entries.append(entry)
    resolutions: dict[tuple[Any, ...], dict[str, Any]] = {}
    unresolved = 0
    rows: list[dict[str, Any]] = []
    for entry in entries:
        row = entry.to_dict()
        if entry.ref is not None:
            key = (entry.ref.unit, entry.ref.id, entry.ref.from_seq, entry.ref.to_seq)
            found = resolutions.get(key)
            if found is None:
                if len(resolutions) >= MAX_PAGE_REFS:
                    unresolved += 1
                    rows.append(row)
                    continue
                outcome = handle.resolve(entry.ref)
                found = {
                    "status": outcome.status,
                    "entries": len(outcome.entries),
                    "first_seq": outcome.entries[0].seq if outcome.entries else None,
                    "last_seq": outcome.entries[-1].seq if outcome.entries else None,
                }
                resolutions[key] = found
            # The citation's VERDICT and span, not its bytes: the cited lines are
            # a page of their own unit, which this route already serves, and
            # inlining them would make one page carry up to MAX_REF_SPAN lines
            # per entry.
            row["ref_resolution"] = dict(found)
        rows.append(row)
    last_seq = observed_last
    return {
        "session_id": session_id,
        "exists": True,
        "from": start,
        "to": end,
        "last_seq": last_seq,
        "entries": rows,
        "next_from": end + 1 if end < last_seq else None,
        "refs_unresolved": unresolved,
    }


def _unit_id_in(directory: Path) -> str:
    """The raw unit id the log in *directory* declares, or ``""``.

    Read straight off the first line rather than derived from the directory
    NAME, because the name is a readable-plus-digest fold the store documents as
    not reversible. Nothing here has to trust the line either: the id is handed
    back to :func:`~kiro_crew.crew_log.store.CrewLog.open`, which re-derives the
    directory from it and refuses when it does not arrive at this one -- so a
    header naming another unit reads as an unopenable unit rather than as that
    other unit's log.
    """
    try:
        with open(directory / LOG_FILE, "r", encoding="utf-8", errors="replace") as handle:
            first = handle.readline()
    except OSError:
        return ""
    if not first.strip():
        return ""
    try:
        parsed = json.loads(first)
    except ValueError:
        return ""
    unit_id = parsed.get("id") if isinstance(parsed, dict) else None
    return unit_id if isinstance(unit_id, str) else ""


def _written_at(directory: Path | str) -> float:
    """When *directory*'s log was last APPENDED to, as an mtime.

    The log file, never the directory. A write to ``log.jsonl`` does not move the
    mtime of the directory holding it -- a directory's mtime moves when its entry
    SET changes, so for a unit created once and appended to forever it stays the
    creation time. Reading the directory instead would silently turn "newest
    first" into "most recently created first" and drop a long-lived active session
    out of a recency window, which is the opposite of what both callers want.

    A unit with no readable log sorts oldest rather than raising: it is rejected a
    moment later by :func:`_unit_id_in` anyway, and an unreadable member of the
    root must not be able to fail the whole listing.
    """
    try:
        return os.stat(os.path.join(str(directory), LOG_FILE)).st_mtime
    except OSError:
        return 0.0


def _candidate_dirs(kind: str) -> tuple[list[Path], bool]:
    """The newest unit directories under *kind*'s root by write time, and whether cut.

    An mtime rather than a fold of each file, because the ordering has to exist
    BEFORE anything is opened -- it is what makes a truncated listing the newest
    window rather than an arbitrary one. It is a proxy for the newest entry's
    time and is honest about being one: the rows carry ``last_ts`` read from the
    entries themselves, and a caller that needs exact recency ordering sorts on
    that.

    The bound is applied HERE, while iterating, not by the caller's scan cap
    downstream: ``scandir`` streams, and a fixed-size heap holds at most
    ``MAX_LISTED_CANDIDATES`` entries, so a host with any number of session logs
    costs this function a bounded amount of memory. Returning whether the set was
    cut lets the listing report ``truncated`` for a cut IT did not perform.
    """
    kept: list[tuple[float, str]] = []
    cut = False
    try:
        with os.scandir(crew_log_root(kind)) as entries:
            for entry in entries:
                try:
                    if entry.is_symlink() or not entry.is_dir():
                        continue
                except OSError:
                    continue
                stamp = _written_at(entry.path)
                if len(kept) < MAX_LISTED_CANDIDATES:
                    heapq.heappush(kept, (stamp, entry.path))
                    continue
                cut = True
                # The heap's root is the OLDEST retained candidate, so this keeps
                # the newest window and drops the loser immediately rather than
                # growing the list and sorting at the end.
                if stamp > kept[0][0]:
                    heapq.heapreplace(kept, (stamp, entry.path))
    except OSError:
        # Includes the ordinary case of a root that was never created, which is
        # every host with the flag off.
        return [], False
    kept.sort(key=lambda pair: pair[0], reverse=True)
    return [Path(path) for _, path in kept], cut


def _listed_row(unit_id: str, *, with_type_counts: bool) -> dict[str, Any] | None:
    """One unit's row, folded in a single pass, or ``None`` when it cannot be read.

    ONE pass over the entries, feeding the ``status`` fold and counting types at
    the same time. Counting in a second walk would double the read of every file
    in the listing, and re-deriving lifecycle here instead of folding would put a
    second implementation of "is this session open" next to the one the panel
    already shows.

    No vocabulary is passed to ``iter_from``: a listing is a listing, and a unit
    written by a newer writer must appear in it rather than take the whole list
    down. The fold simply ignores a type it does not branch on.
    """
    try:
        handle = projections.open_session_log(unit_id)
    except (CrewLogError, OSError):
        return None
    if handle is None:
        return None
    counts: Counter[str] = Counter()
    seen: dict[str, int] = {"ts": 0, "seq": 0}

    def _tapped(entries: Any) -> Any:
        for entry in entries:
            counts[entry.type] += 1
            seen["ts"] = entry.time
            seen["seq"] = entry.seq
            yield entry

    try:
        checkpoint = projections.advance(
            projections.initial("status"), _tapped(handle.iter_from(1))
        )
    except (CrewLogError, OSError):
        return None
    status = projections.projection_of(checkpoint).value
    header = handle.header
    row: dict[str, Any] = {
        "unit": unit_id,
        "slot": getattr(header, "slot", None) or status.get("slot") or "",
        "agent": getattr(header, "agent", "") or status.get("agent") or "",
        "model": status.get("model") or "",
        "first_ts": getattr(header, "created_at", 0) or 0,
        "last_ts": seen["ts"] or status.get("last_time") or 0,
        "last_seq": seen["seq"],
        "open": status.get("lifecycle") == "open",
        "lifecycle": status.get("lifecycle") or "unknown",
        "entries": status.get("entries") or 0,
    }
    if with_type_counts:
        row["type_counts"] = dict(sorted(counts.items()))
    return row


def list_session_units(
    *,
    slot_contains: str = "",
    active_within_ms: int = 0,
    now_ms: int = 0,
    with_type_counts: bool = False,
    limit: int = 50,
) -> dict[str, Any]:
    """The session crew logs on this host, newest first, with one row each.

    ``active_within_ms`` prunes by the LOG's mtime BEFORE the file is opened, which
    is the whole point of filtering rather than folding everything and cutting:
    the cheap signal answers "not recently written" without a read, and a unit it
    keeps is then folded and reports its real ``last_ts``. The log's mtime, never
    the directory's -- see :func:`_written_at` for why the directory answers a
    different question than the one asked here.

    ``slot_contains`` is a substring match over the row's slot, applied after the
    header is read because the slot lives in the header rather than in the
    directory name.

    Reports ``scanned`` and ``truncated`` rather than only the rows: a caller that
    cannot tell a short list from a cut one would read "3 sessions" off a host
    running three hundred.
    """
    capped = max(1, min(int(limit), MAX_LIST_LIMIT))
    cutoff_ms = 0
    if active_within_ms > 0:
        cutoff_ms = (now_ms or store_now_ms()) - active_within_ms
    needle = slot_contains.casefold()
    rows: list[dict[str, Any]] = []
    scanned = 0
    global_counts: Counter[str] = Counter()
    candidates, truncated = _candidate_dirs(KIND_SESSION)
    for directory in candidates:
        if len(rows) >= capped or scanned >= MAX_LISTED_SCAN:
            truncated = True
            break
        if cutoff_ms:
            if _written_at(directory) * 1000 < cutoff_ms:
                continue
        unit_id = _unit_id_in(directory)
        if not unit_id:
            continue
        scanned += 1
        row = _listed_row(unit_id, with_type_counts=with_type_counts)
        if row is None:
            continue
        if needle and needle not in str(row["slot"]).casefold():
            continue
        if cutoff_ms and row["last_ts"] and row["last_ts"] < cutoff_ms:
            continue
        if with_type_counts:
            global_counts.update(row["type_counts"])
        rows.append(row)
    rows.sort(key=lambda item: (item["last_ts"], item["unit"]), reverse=True)
    out: dict[str, Any] = {
        "kind": KIND_SESSION,
        "units": rows,
        "scanned": scanned,
        "truncated": truncated,
        "limit": capped,
    }
    if with_type_counts:
        out["type_counts"] = dict(sorted(global_counts.items()))
    return out
