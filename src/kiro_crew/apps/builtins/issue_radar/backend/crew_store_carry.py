"""Carrying a crew's pre-projection files into its crew log, once.

The markers the carry leaves, the legacy work-item and skip readers with the clamps
that make each row fit one entry, the preview a read folds in before the
carry has run, and the carry itself. Callers reach every name here through
:mod:`kiro_crew.apps.builtins.issue_radar.backend.crew_store`.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from kiro_crew import atomic_write as atomic_write_module
from kiro_crew.config.paths import data_home
from kiro_crew.crew_log.entry_types import RADAR_NUMBER_BOUNDS
from kiro_crew.crew_log.store import log_exception_text

from .crew_store_log import (
    _APPEND_FLUSH_SECONDS,
    _ENTRY_SRC,
    LEDGER_ENTRY_TYPE,
    CrewLedgerNotRecorded,
    _projection,
    _undrained,
    _unit_last_seq,
)
from .crew_store_records import (
    PHASES,
    _finite_int,
    _require_crew_id,
    coerce_skip_scope,
    crews_dir,
    logger,
    skips_path,
)

#: Marker files the one-time carry leaves behind, so the pre-projection files are
#: never read twice and a second crew does not re-carry the repository's passes.
_ITEMS_CARRIED_MARKER = ".carried"
_SKIPS_CARRIED_MARKER = "skipped.json.carried"

#: Written when a carry BEGINS. A carry that began and has no finished marker did not
#: fully land, and the next write runs it again -- the only case in which files that
#: appear beside a crew with a live record are read: a file that shows up later (a
#: restore from a backup) is otherwise left alone rather than merged over live state.
_ITEMS_CARRY_BEGUN_MARKER = ".carrying"
_SKIPS_CARRY_BEGUN_MARKER = "skipped.json.carrying"

#: Text ceiling the carry applies to what it re-states. It MUST equal the fold's
#: ``RADAR_TEXT_LIMIT``, which equals the cap the record tool accepts: the carry is
#: one-shot and marks itself finished, so a legacy value this cuts is gone from the
#: live ledger with nothing left to re-read it from.
_MAX_CARRIED_TEXT = 5000


# ── carrying the pre-projection files forward ───────────────────────────────


def _read_legacy_json(path: Path) -> Any:
    return atomic_write_module.read_json_or(
        path, None, logger=logger, what="issue-radar legacy record"
    )


def _legacy_text(value: Any, limit: int = _MAX_CARRIED_TEXT) -> str:
    return value[:limit] if isinstance(value, str) else ""


def _legacy_ci_state(value: dict[str, Any]) -> dict[str, Any]:
    """The members of a legacy ``ci_state`` the record tool would have accepted.

    The fold's own bounder (:func:`projection.radar_ci_state`): only the kept keys,
    each with the tool's type and ceiling, anything else dropped -- one oversized
    value is what could make a row unfit for an entry. Legacy files were written by
    ``json.dumps``, so a counter can only be an int or a float here; a float is not
    a counter and is dropped like any other wrong shape.
    """
    return _projection().radar_ci_state(value)


#: Free-text members of a carried entry, clamped together when the row must shrink.
_CARRIED_TEXT_FIELDS = ("decision", "why", "next", "worktree", "branch", "base_sha", "event")
#: The clamps tried in turn when a carried row does not fit one entry. The text
#: ceiling is in characters and the entry ceiling in bytes, and a character can
#: serialize to six bytes, so a row of long non-ASCII text can be over the ceiling
#: at the first clamp and under it at a later one.
_CARRY_SHRINK_STEPS = (_MAX_CARRIED_TEXT, 2000, 1000, 500, 256)


def _shrink_to_fit(data: dict[str, Any], fits: Any) -> dict[str, Any] | None:
    """*data* clamped until *fits* accepts it, or ``None`` when no clamp does.

    Clamping loses the tail of a long text; refusing the row loses the record. The
    fold clamps every text on the way in, so the first step costs nothing the fold
    would have kept, and a later step is taken only when the first does not fit.
    """
    for limit in _CARRY_SHRINK_STEPS:
        shrunk = dict(data)
        for key in _CARRIED_TEXT_FIELDS:
            if isinstance(shrunk.get(key), str):
                shrunk[key] = shrunk[key][:limit]
        for nested in ("tried", "skip"):
            inner = shrunk.get(nested)
            if isinstance(inner, dict):
                shrunk[nested] = {
                    k: (v[:limit] if isinstance(v, str) and k != "scope" else v)
                    for k, v in inner.items()
                }
        if fits(shrunk):
            return shrunk
    return None


def _legacy_number(value: Any, field: str = "number") -> int | None:
    """*value* as a number inside the record tool's range for *field*, or ``None``.

    The carry applies the magnitude bound the fold applies to the bytes it reads
    (:data:`RADAR_NUMBER_BOUNDS`), for the same reason the carry re-applies the CI
    bounds: a pre-projection file can hold any number, and one outside the range the
    tool would have accepted is dropped by the fold -- which on an item row would
    silently fold it as a crew-level line instead. Bounded here, such a row is unfit
    and named, so the carry does not finish and the write is refused.
    """
    number = _finite_int(value)
    if number is None:
        return None
    low, high = RADAR_NUMBER_BOUNDS[field]
    return number if low <= number <= high else None


def _legacy_item_entries(
    owner: str, repo: str, crew_id: str, legacy: dict[str, Any]
) -> list[dict[str, Any]]:
    """The carried entries for one pre-projection work item, or ``[]`` without a number.

    Carries the record a resume needs -- phase, next, decision, EVERY rejected
    approach, where the work lives and what is on the forge -- plus its own stamps,
    and an event that says where it came from. The phase rides with the event,
    keeping the rule that a phase never appears without a logged reason.

    One entry holds ONE ``tried``, so a `tried` history of *n* rows is *n* entries,
    each re-stating the whole record and differing only in the approach it carries.
    Order is the file's own, so the folded list ends with the same newest row the file
    ended with. Carrying the newest approach alone would delete the field's whole
    purpose for every pre-upgrade item the moment the marker is written, and the loss
    is not recoverable afterwards because the files are never read again. It needs
    nothing new in the fold: a carried ``tried`` is already deduplicated by content
    there and the list is already clamped, so a carry run twice is not a history listed
    twice, and re-stated scalars apply idempotently.

    The entries after the first carry no new ``phase`` VALUE, which is what keeps them
    quiet: the fold writes a phase line only for an entry that MOVED the item, so the
    item still enters its phase exactly once however long its history is.
    """
    number = _legacy_number(legacy.get("number"))
    if number is None:
        return []
    data: dict[str, Any] = {"crew_id": crew_id, "owner": owner, "repo": repo, "number": number}
    phase = legacy.get("phase")
    data["phase"] = phase if isinstance(phase, str) and phase in PHASES else "selected"
    for key in ("decision", "why", "next", "worktree", "branch", "base_sha"):
        if isinstance(legacy.get(key), str) and legacy[key]:
            data[key] = _legacy_text(legacy[key])
    if isinstance(legacy.get("outcome"), str) and legacy["outcome"]:
        data["outcome"] = _legacy_text(legacy["outcome"], 256)
    for key in ("pr_number", "claim_comment_id"):
        value = _legacy_number(legacy.get(key), key)
        if value is not None:
            data[key] = value
    if isinstance(legacy.get("ci_state"), dict) and legacy["ci_state"]:
        # Only the members the fold keeps, each bounded the way the record tool
        # bounds it on the way in: the file could hold any keys and any values, and
        # one oversized member is the one thing that could push an otherwise valid
        # row past the entry ceiling and leave it uncarried. Nothing the record tool
        # would have accepted is dropped.
        ci = _legacy_ci_state(legacy["ci_state"])
        if ci:
            data["ci_state"] = ci
    if isinstance(legacy.get("labels_applied"), list):
        # The record tool's own bounds (20 labels, short strings), for the same reason.
        data["labels_applied"] = [x[:256] for x in legacy["labels_applied"] if isinstance(x, str)][
            :20
        ]
    rows: list[dict[str, str]] = []
    dropped = 0
    tried = legacy.get("tried")
    if isinstance(tried, list):
        for row in tried:
            if (
                isinstance(row, dict)
                and isinstance(row.get("approach"), str)
                and row["approach"].strip()
            ):
                rows.append(
                    {
                        "approach": _legacy_text(row["approach"]),
                        "rejected_because": _legacy_text(row.get("rejected_because", "")),
                    }
                )
            else:
                # Not a row the fold would have kept either -- it drops a ``tried``
                # without a non-blank ``approach``. Counted so the event says so.
                dropped += 1
    tried_limit = _projection().RADAR_TRIED_LIMIT
    if len(rows) > tried_limit:
        # The fold keeps an item's newest ``tried_limit`` approaches, so rows past
        # that ceiling would be evicted by the very read that folded them. Left out
        # here instead, where the count can be SAID once rather than each row costing
        # an entry to be discarded.
        dropped += len(rows) - tried_limit
        del rows[: len(rows) - tried_limit]
    for stamp in ("claimed_at", "last_progress_at", "finished_at"):
        if isinstance(legacy.get(stamp), str) and legacy[stamp]:
            data[stamp] = _legacy_text(legacy[stamp], 64)
    data["carried"] = True
    note = "carried forward from the pre-projection crew ledger files"
    if dropped > 0:
        note += f"; not carried: {dropped} unreadable or over-ceiling rejected approach(es)"
    data["event"] = note
    data["event_kind"] = "claim"
    if rows:
        data["tried"] = rows[0]
    entries = [data]
    for position, row in enumerate(rows[1:], start=2):
        entries.append(
            {
                # The WHOLE record again, not just the identity and the row. A carried
                # entry re-states a record, and the fold applies the same scalars
                # idempotently, so repeating them costs a re-read and buys the one
                # thing an identity-only continuation cannot: if the writer takes this
                # entry and refuses the first, the item folds with its real phase and
                # next step instead of appearing as a bare ``selected`` placeholder
                # the crew would read as live work it never claimed.
                **data,
                "tried": row,
                "event": (
                    f"rejected approach {position} of {len(rows)} carried forward from "
                    "the pre-projection crew ledger files"
                ),
            }
        )
    return entries


def _legacy_skip_entry(
    owner: str, repo: str, crew_id: str, row: dict[str, Any]
) -> dict[str, Any] | None:
    """The carried entry for one pre-projection pass -- a skip row and no work item."""
    number = _legacy_number(row.get("number"))
    if number is None:
        return None
    skip: dict[str, Any] = {
        "reason": _legacy_text(row.get("reason")),
        "scope": coerce_skip_scope(row.get("scope")),
    }
    if isinstance(row.get("crew_id"), str) and row["crew_id"]:
        skip["crew_id"] = _legacy_text(row["crew_id"], 64)
    if isinstance(row.get("decided_at"), str) and row["decided_at"]:
        skip["decided_at"] = _legacy_text(row["decided_at"], 64)
    return {
        "crew_id": crew_id,
        "owner": owner,
        "repo": repo,
        "number": number,
        "skip": skip,
        "carried": True,
        "event": "pass carried forward from the pre-projection shared skip index",
        "event_kind": "skip",
    }


def _tried_key(row: Mapping[str, Any]) -> tuple[str, str]:
    """A rejected approach's identity, the way the fold deduplicates one.

    The fold strips the approach and keeps the reason as it is, so the same two
    members compared the same way are what make a row already present.
    """
    return (
        str(row.get("approach") or "").strip(),
        str(row.get("rejected_because") or ""),
    )


def _carried_history_complete(held: Mapping[str, Any], entries: list[dict[str, Any]]) -> bool:
    """Whether *held* already holds every rejected approach *entries* would carry.

    An item is several entries, so a carry can land some of them and have the rest
    refused -- and the run after it must be able to tell the two apart. A record that
    already has the whole history is left alone, because re-stating the file's fields
    over an item the crew has since worked would set it back to its pre-projection
    state. A record MISSING a row is carried again: the refused entries are the only
    copy of those approaches, the files are never read again once the carry is marked
    finished, and re-emitting costs nothing wrong -- the fold drops a carried approach
    it already has and re-applies the other fields to the same values.

    Wrong in either direction is safe, which is why it compares content rather than
    counting: a false "incomplete" re-emits rows the fold then deduplicates, and a
    false "complete" is no worse than not asking at all.
    """
    have = {_tried_key(row) for row in held.get("tried") or [] if isinstance(row, Mapping)}
    return all(
        _tried_key(data["tried"]) in have
        for data in entries
        if isinstance(data.get("tried"), Mapping)
    )


def _legacy_carry_rows(
    owner: str,
    repo: str,
    crew_id: str,
    root: Path | None,
    *,
    folded: Mapping[str, Any] | None,
    fits: Any,
) -> tuple[list[dict[str, Any]], list[str]]:
    """The payloads a carry WOULD append, in order, and the rows it could not build.

    Shared by :func:`_carry_legacy_forward` and its read-side preview
    (:func:`_legacy_preview`), so the two cannot answer differently about what the
    upgrade brings forward: the preview FOLDS exactly these payloads where the carry
    appends them, which makes the previewed record the post-carry record by
    construction rather than by two implementations agreeing. Pure -- it reads the
    pre-projection files and writes nothing, not even the begun markers, which belong
    to the carry alone and are what make it a write.

    A work item *folded* already holds with its WHOLE carried history is left out: it
    landed on an earlier run, or the crew has since worked it, and a carried entry
    re-states the file's fields as an update. One whose history the record is missing
    a row of is carried again, because those refused entries are the only copy of
    those approaches (:func:`_carried_history_complete`). A pass the record holds is
    left out outright -- a pass is one entry, so holding it is holding all of it. A row
    that cannot be built -- unreadable, not a record, without a recoverable number,
    too large at every clamp -- is NAMED in the second list rather than passed over,
    because a carry that finished with a row left behind would discard that row's
    state for good.
    """
    crew_dir = crews_dir(owner, repo, root) / _require_crew_id(crew_id)
    items_marker = crew_dir / _ITEMS_CARRIED_MARKER
    legacy_skips = skips_path(owner, repo, root)
    skips_marker = crews_dir(owner, repo, root) / _SKIPS_CARRIED_MARKER
    held_items: Mapping[str, Any] = folded["items"] if folded else {}
    held_skips: set[str] = set(folded["skips"]) if folded else set()
    rows: list[dict[str, Any]] = []
    unfit: list[str] = []
    if crew_dir.is_dir() and not items_marker.exists():
        for path in sorted(crew_dir.glob("*.json")):
            legacy = _read_legacy_json(path)
            if not isinstance(legacy, dict):
                # Unreadable, or not a record. Named, not skipped: a skipped row
                # would be left behind by a finished carry.
                unfit.append(path.name)
                continue
            if _legacy_number(legacy.get("number")) is None:
                # The pre-projection writer named each item file by its number, so
                # a row without one still has it in the file name.
                try:
                    legacy = {**legacy, "number": int(path.stem)}
                except ValueError:
                    unfit.append(path.name)
                    continue
            entries = _legacy_item_entries(owner, repo, crew_id, legacy)
            if not entries:
                unfit.append(path.name)
                continue
            standing = held_items.get(str(entries[0]["number"]))
            if standing is not None and _carried_history_complete(standing, entries):
                continue
            fitted: list[dict[str, Any]] = []
            for data in entries:
                shrunk = _shrink_to_fit(data, fits)
                if shrunk is None:
                    # One entry of an item that fits at no clamp makes the WHOLE row
                    # unfit: appending the rest and marking the carry finished would
                    # leave that part of the item's history nowhere, and the files are
                    # never read again once marked.
                    fitted = []
                    break
                fitted.append(shrunk)
            if not fitted:
                unfit.append(path.name)
                continue
            rows.extend(fitted)
    if legacy_skips.is_file() and not skips_marker.exists():
        stored = _read_legacy_json(legacy_skips)
        if not isinstance(stored, dict):
            unfit.append(legacy_skips.name)
        else:
            for key, row in stored.items():
                if not isinstance(row, dict):
                    unfit.append(f"{legacy_skips.name}#{key}")
                    continue
                if _legacy_number(row.get("number")) is None:
                    try:
                        row = {**row, "number": int(str(key))}
                    except (TypeError, ValueError):
                        unfit.append(f"{legacy_skips.name}#{key}")
                        continue
                skip_data = _legacy_skip_entry(owner, repo, crew_id, row)
                if skip_data is None:
                    unfit.append(f"{legacy_skips.name}#{key}")
                    continue
                if str(skip_data["number"]) in held_skips:
                    continue
                fitted_skip = _shrink_to_fit(skip_data, fits)
                if fitted_skip is None:
                    unfit.append(f"{legacy_skips.name}#{key}")
                    continue
                rows.append(fitted_skip)
    return rows, unfit


def _legacy_preview(
    owner: str, repo: str, crew_id: str, root: Path | None, base: Any
) -> dict[str, Any] | None:
    """The record *crew_id*'s pending carry WOULD fold to, or ``None`` for no carry.

    The read half of the upgrade carry. A carry appends, so it can only run from a
    write: it needs a crew log to append into, it takes the crew's write lock, and it
    refuses its caller when a row did not land. A READ has none of those -- it may be
    asked with no live session at all, and it must not raise -- so without this the
    first read after an upgrade answers the EMPTY record while the pre-projection
    files sit there waiting for a write to consume them. Those reads are not
    cosmetic: they are the pre-investigate nudge snapshot (``list_work_items``,
    ``open_slot_count``) and the repository's shared skip index (``read_skips``), so
    the first turn of an upgraded crew would be told it has no open items and that no
    issue has been passed on -- and act on it, with forge comments that cannot be
    taken back, before any carry runs.

    So the read FOLDS the pending entries in without appending them, over the same
    base, through the same projection, from the same payloads the carry builds
    (:func:`_legacy_carry_rows`). The answer is the post-carry record by construction
    rather than by agreement, and the carry stays the only thing that makes it
    PERMANENT -- which is why this is not a second authority: it appends nothing,
    marks nothing, and stops answering the moment the log holds the rows.

    Three things stop it resurrecting state the log has settled. It is reached only
    on the write path's OWN carry trigger -- no radar entry has ever folded into this
    crew's record, or a carry began and did not finish -- so the two agree on when a
    carry is owed as well as on what it carries, and a crew whose every write was an
    idle sweep is not previewed for good. A row whose number the folded base already
    holds is left out by the shared builder, so a half-finished carry previews only
    what is still missing rather than re-stating an item the crew has since worked.
    And it folds ONTO the base, so entries that carry events but no item keep them.

    Never raises: every failure answers ``None``, which is the record the caller
    already had.
    """
    try:
        if not (not base.state["crew_id"] or _carry_pending(owner, repo, crew_id, root)):
            return None
        from kiro_crew.crew_log import emit as crew_log_emit

        projection = _projection()
        pending, _unfit = _legacy_carry_rows(
            owner,
            repo,
            crew_id,
            root,
            folded=base.state,
            # The same size gate the carry applies, so a row the carry will shrink is
            # previewed shrunk. Asked through the emitter because that is the
            # serializer the append uses; it is a pure byte check that does not need
            # the log switched ON, which is what lets a crew whose recording is off
            # still READ its pre-projection state instead of the empty record.
            fits=crew_log_emit.radar_entry_fits,
        )
        if not pending:
            # Nothing to bring forward: an upgraded install with no pre-projection
            # files, or every row already carried. Not an error and not an empty
            # answer -- the caller keeps the record it folded.
            return None
        from kiro_crew.crew_log.schema import Entry

        entries = tuple(
            Entry(
                type=LEDGER_ENTRY_TYPE,
                # One past what the fold consumed, which is all ``advance`` asks.
                # These objects are never written: the carry's own append takes each
                # seq from the store under the store's lock.
                seq=base.last_seq + offset,
                time=int(time.time() * 1000),
                src=_ENTRY_SRC,
                data=data,
            )
            for offset, data in enumerate(pending, start=1)
        )
        return dict(projection.projection_of(projection.advance(base, entries)).value)
    except Exception:
        logger.warning(
            "crew ledger: could not preview crew %s's pending carry; reading the "
            "record as folded",
            crew_id,
            exc_info=True,
        )
        return None


def _carry_legacy_forward(
    owner: str,
    repo: str,
    crew_id: str,
    session_id: str,
    root: Path | None = None,
    *,
    folded: Mapping[str, Any] | None = None,
) -> bool:
    """Append the pre-projection files' state into *session_id*'s crew log, once.

    Runs on a crew's first write -- the one before any radar entry has folded into
    its record -- and again on a later write while an earlier carry is unfinished.
    Two things are carried: the crew's own work-item files, and -- by whichever crew
    of the repository writes first -- the repository's shared skip index, whose rows
    keep the crew that decided them and when. A pass is one entry and a work item is
    one entry per rejected approach it carries (at least one), so the work is bounded
    by the files and by each item's clamped `tried` history, and happens once per crew
    and once per repository.

    *folded* is the crew's record as folded before this carry. A row whose number
    the record already holds is NOT emitted again: it landed on an earlier run, or
    the crew has since written that item itself, and a carried entry re-states the
    file's fields as an update, so re-emitting it would set a live item back to its
    pre-projection state. Only rows the record lacks are appended.

    A marker beside the carried files records that the carry FINISHED; the files
    are left in place and never read again. The marker is written only when every
    row was carried and read back from the log -- a drained writer is not proof,
    since the writer can refuse an entry and drain quietly. A row that could not be
    carried -- unreadable, not a record, without a number that can be recovered,
    or too large for one entry at every clamp -- is named in the log and leaves the
    carry unfinished, so the files stay unmarked and the next write tries again:
    marking a carry finished with a row left behind would discard that row's state
    for good.

    Returns whether anything was appended, so the caller re-folds.
    """
    from kiro_crew.crew_log import emit as crew_log_emit

    crew_dir = crews_dir(owner, repo, root) / _require_crew_id(crew_id)
    items_marker = crew_dir / _ITEMS_CARRIED_MARKER
    legacy_skips = skips_path(owner, repo, root)
    skips_marker = crews_dir(owner, repo, root) / _SKIPS_CARRIED_MARKER
    seq_before = _unit_last_seq(session_id)
    # Marked BEGUN before a single row is read, so a crash mid-carry leaves the marker
    # pair ``_carry_pending`` reads as unfinished. Written HERE and not in the shared
    # row builder, which is what lets the read-side preview use the same builder and
    # still be a read.
    if crew_dir.is_dir() and not items_marker.exists():
        _mark_carry_begun(crew_dir / _ITEMS_CARRY_BEGUN_MARKER)
    if legacy_skips.is_file() and not skips_marker.exists():
        _mark_carry_begun(legacy_skips.parent / _SKIPS_CARRY_BEGUN_MARKER)
    carried, unfit = _legacy_carry_rows(
        owner, repo, crew_id, root, folded=folded, fits=crew_log_emit.radar_entry_fits
    )
    for data in carried:
        crew_log_emit.on_radar_recorded(session_id, data)
    if carried:
        if not crew_log_emit.flush(timeout=_APPEND_FLUSH_SECONDS):
            # The carried rows are still in the writer. The write that triggered the
            # carry would now refold a record MISSING them -- a carried editing item
            # among them, and the one-editor rule could admit a second editor into a
            # log that cannot take a line back. So the write is refused, the crew is
            # marked undrained (its next write drains before it folds), and the files
            # stay unmarked so the carry runs again if anything did not land.
            logger.warning("crew ledger: the carry-forward did not drain within the flush budget")
            _undrained[(str(data_home()), crew_id)] = (session_id, seq_before, carried[-1])
            raise CrewLedgerNotRecorded(
                "the carry of this crew's pre-projection files is still queued in the "
                "crew log writer; send this update again shortly"
            )
        if not _all_landed(session_id, seq_before, carried):
            # Drained, but a row the writer was handed is not in the file: it refused
            # it. The record this write would fold is short of that row, so the write
            # is refused too; the files stay unmarked and are carried again next time,
            # and the warning names the crew so a row refused every time is found.
            logger.warning(
                "crew ledger: the carry-forward for crew %s did not fully land; the "
                "pre-projection files stay unmarked and are carried again on the next write",
                crew_id,
            )
            raise CrewLedgerNotRecorded(
                "a row of this crew's pre-projection files did not land in the crew "
                "log; send this update again"
            )
    if unfit:
        # A row that could not be carried -- unreadable, not a record, without a
        # recoverable number in the tool's range, or too large for one entry at every
        # clamp -- REFUSES this write, the same answer as a row that did not land.
        # Marking the carry finished would discard that row's stored state for good,
        # since the files are never read again once marked; folding without it is
        # worse than refusing, because an omitted row in an editing phase leaves its
        # item out of the record the one-editor rule reads, so another item can enter
        # that phase while the row still holds it, and a later carry then brings a
        # second editor into the record. The files stay unmarked and the rows are
        # named, so a retry appends only what is still missing once the named files
        # are repaired or moved aside.
        logger.warning(
            "crew ledger: %d pre-projection record(s) could not be carried into the crew "
            "log; the files stay unmarked and are tried again on the next write: %s",
            len(unfit),
            ", ".join(unfit),
        )
        raise CrewLedgerNotRecorded(
            "a row of this crew's pre-projection files could not be carried into the "
            f"crew log ({', '.join(unfit[:5])}); repair or move the named file(s) "
            "aside, then send this update again"
        )
    try:
        if crew_dir.is_dir():
            items_marker.touch(exist_ok=True)
            (crew_dir / _ITEMS_CARRY_BEGUN_MARKER).unlink(missing_ok=True)
        if legacy_skips.is_file():
            skips_marker.touch(exist_ok=True)
            (legacy_skips.parent / _SKIPS_CARRY_BEGUN_MARKER).unlink(missing_ok=True)
    except OSError:
        logger.warning(
            "crew ledger: could not mark the pre-projection files as carried", exc_info=True
        )
    return bool(carried)


def _touch(path: Path) -> None:
    try:
        path.touch(exist_ok=True)
    except OSError:
        logger.warning("crew ledger: could not write the carry marker %s", path, exc_info=True)


def _mark_carry_begun(path: Path) -> None:
    """Write a carry's BEGUN marker, or refuse the write that would have carried.

    The begun marker is what makes a carry that stops half-way run again: without it
    a partial carry leaves a record that has folded a radar entry, and such a record
    never carries on its own.
    So a begun marker that cannot be written is not best-effort like the finished one
    (a lost finished marker only re-runs an idempotent carry) -- nothing is emitted, and
    the write is refused so the caller sends it again once the marker can be written.
    """
    try:
        path.touch(exist_ok=True)
    except OSError as exc:
        logger.warning("crew ledger: could not write the carry marker %s", path, exc_info=True)
        raise CrewLedgerNotRecorded(
            "the carry of this crew's pre-projection files could not be marked as begun; "
            "nothing was carried, send this update again"
        ) from exc


def _carry_pending(owner: str, repo: str, crew_id: str, root: Path | None = None) -> bool:
    """Whether an earlier carry for this crew or its repository began and did not finish."""
    crew_dir = crews_dir(owner, repo, root) / _require_crew_id(crew_id)
    repo_dir = crews_dir(owner, repo, root)
    items_unfinished = (crew_dir / _ITEMS_CARRY_BEGUN_MARKER).exists() and not (
        crew_dir / _ITEMS_CARRIED_MARKER
    ).exists()
    skips_unfinished = (repo_dir / _SKIPS_CARRY_BEGUN_MARKER).exists() and not (
        repo_dir / _SKIPS_CARRIED_MARKER
    ).exists()
    return items_unfinished or skips_unfinished


def _all_landed(session_id: str, seq_before: int, payloads: list[dict[str, Any]]) -> bool:
    """Whether every one of *payloads* is in *session_id*'s log past *seq_before*.

    Read from the file rather than inferred from the writer: a drained writer can
    have REFUSED an entry (too large, undeclared field) and be quiet about it.
    """
    projection = _projection()
    try:
        handle = projection.open_session_log(session_id)
        if handle is None:
            return False
        since = [
            e.data
            for e in handle.iter_from(seq_before + 1, known=projection.KNOWN_TYPES)
            if e.type == LEDGER_ENTRY_TYPE
        ]
    except Exception:
        log_exception_text(
            logger, logging.DEBUG, "crew ledger: could not read the carried entries back"
        )
        return False
    return all(any(landed == data for landed in since) for data in payloads)
