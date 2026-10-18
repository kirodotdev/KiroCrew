"""The crew kind: the dispatch and report entries written to a crew's own log."""

from __future__ import annotations

import logging
import traceback
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from kiro_crew.crew_log.emit_runtime import _report
from kiro_crew.crew_log.emit_state import _KIND, _SRC_GATEWAY, _crew_log, enabled, logger

if TYPE_CHECKING:  # pragma: no cover -- typing only; the runtime imports stay gated
    # Type-only, so the boot-path import gate is untouched: this name exists for the
    # checker and for test_crew_log_exc_info_sites.py, which reads annotations to decide
    # whether a frame can hold a handle. A handle passed in as ``Any`` is invisible to it.
    from kiro_crew.crew_log.store import CrewLog


# --------------------------------------------------------------------------- #
# The crew kind
# --------------------------------------------------------------------------- #
#
# The first writer for ``crew-log/crews/<store>/``. Deliberately NOT routed
# through the write-behind queue in ``emit_runtime``: every structure that queue owns is keyed
# by an ACP SESSION id and ``_handle`` opens its unit with ``_KIND``, so handing
# it a crew's store name would make it look for a SESSION unit under that name
# and, failing to find one, drop the entry as a policy no-op. Threading a kind
# through the batch machinery is a change to the session path, which these two
# entries do not need: a dispatch is written once per work item and a report once
# per milestone, both already off the event loop in the route's worker thread.
#
# No handle is cached either. A handle holds the unit's write lease until it is
# dropped, and a cached crew handle would hold one for the process's life --
# refusing ``remove_unit`` for a unit nothing is writing. Opening per entry costs
# a bounded tail read, which is what the lease's own refcount makes safe to
# repeat.

_KIND_CREW = "crew"

CREW_DISPATCH = "crew/dispatch"

CREW_REPORT = "crew/report"


def crew_src(store: str) -> str:
    """The ``src`` a crew signs its own dispatches with.

    A crew writing into its OWN log is the guest form ``crew:<name>``, and the
    name is the unit's own id -- so this is derived rather than passed, and no
    caller can sign a dispatch as a crew it is not.
    """
    return f"crew:{store}"


def _crew_unit(store: str) -> Any:
    """An open crew log for *store*, created when it has none. ``None`` if inert.

    Unlike :func:`_handle`, this one CREATES. A session's crew log is created by
    the turn path, which knows whether the session is real; a crew's has no such
    moment -- the crew exists in the members store, and the first fact worth
    recording about its work is the first dispatch. So the first append opens the
    file, and a crew that dispatches nothing never gets one.

    ``None`` means the flag is off or the store is unnamed, which is a policy
    no-op. Every other failure is the caller's to treat as "not recorded".
    """
    if not store or not enabled():
        return None
    subsystem = _crew_log()
    if subsystem.CrewLog.exists(_KIND_CREW, store):
        return subsystem.CrewLog.open(_KIND_CREW, store)
    try:
        return subsystem.CrewLog.create(_KIND_CREW, store)
    except subsystem.CrewLogError as exc:
        # Two threads can pass the ``exists`` check together and both create. The
        # loser is told ``already_exists``, which is the file it wanted, so it
        # opens instead of reporting a failure.
        if exc.code != subsystem.CODE_ALREADY_EXISTS:
            raise
        return subsystem.CrewLog.open(_KIND_CREW, store)


def _dispatch_target_ok(data: "Mapping[str, Any]") -> bool:
    """Whether ``target`` names exactly one party, which the registry cannot ask.

    ``target.kind`` decides which of ``slot`` or ``name`` carries the party, and
    the two forms are EXCLUSIVE -- a target names a session slot or a crew, never
    both. A declaration has no spelling for a conditional requirement, so the
    obligation lands here, on the writer, where the entry is built.
    """
    target = data.get("target")
    if not isinstance(target, Mapping):
        return False
    kind = target.get("kind")
    carried = {"session": "slot", "crew": "name"}.get(kind if isinstance(kind, str) else "")
    if carried is None:
        return False
    absent = "name" if carried == "slot" else "slot"
    return bool(target.get(carried)) and absent not in target


def _newest_dispatch_for(log: CrewLog, item: str, start: int) -> "int | None":
    """The seq of the newest ``crew/dispatch`` naming *item* at or after *start*."""
    found: int | None = None
    for entry in log.iter_from(start):
        if entry.type != CREW_DISPATCH:
            continue
        if isinstance(entry.data, Mapping) and entry.data.get("item") == item:
            found = entry.seq
    return found


def _crew_thread(log: CrewLog, item: Any) -> "int | None":
    """The seq of the newest ``crew/dispatch`` for *item*, or ``None``.

    What makes a dispatch and its replies one conversation inside the crew's file.
    Read from the log rather than remembered, because the two writes are separate
    requests -- often in separate processes -- and an in-memory map would answer
    ``None`` for every report after a restart while the anchor sat on disk.

    ONE pass, from seq 1, because a narrower start would not be a cheaper read:
    :meth:`~kiro_crew.crew_log.store.CrewLog.iter_from` walks ``_iter_segments``
    from the first segment and decodes every entry, dropping the ones below its
    *seq* after parsing them. So a "recent entries" window costs the same full
    parse as the whole file, and a window MISS -- the ordinary case for an item
    whose dispatch has aged out -- would pay for that parse twice. A byte-tail
    reader like :func:`~kiro_crew.crew_log.store._anchor_exists`'s is what an
    actual bound would take, and it answers a different question (does this seq
    exist) than this one (which dispatch named this item).

    Reading the whole file is also what correctness wants, though not because a
    miss refuses the write: :func:`on_crew_report` records an unthreaded report
    rather than dropping it. What a miss costs is that the entry becomes
    indistinguishable from one volunteered with no dispatch behind it, which is a
    claim about where the work came from that nothing later can correct -- so every
    anchor the file actually holds is worth finding.

    An unreadable log answers ``None``, so a report still lands.
    """
    if not isinstance(item, str) or not item:
        return None
    try:
        return _newest_dispatch_for(log, item, 1)
    except Exception:  # noqa: BLE001 - an unthreaded report is better than none
        # Rendered text, never ``exc_info``: ``log`` is a live ``CrewLog`` in this frame,
        # so a record carrying the traceback carries this frame, and a handler that keeps
        # records (``caplog``, a ``MemoryHandler``) keeps the handle and its write lease
        # alive past the drop that should have released it. A string keeps no frames.
        # The render uses the ``traceback`` module imported above rather than the store's
        # ``log_exception_text``, because this module is the boot-path import gate (see
        # ``_crew_log``) and may not import the store at module level -- the same idiom
        # ``_record_session_tree_edge`` uses. Pinned by test_crew_log_exc_info_sites.py.
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "crew log: dispatch anchor lookup failed for %r:\n%s",
                item,
                traceback.format_exc().rstrip(),
            )
        return None


def _crew_append(store: str, entry_type: str, data: dict[str, Any], **envelope: Any) -> int:
    """Append one crew entry and return its seq, or ``0`` when nothing was written.

    BEST EFFORT, and that is a scope decision rather than laxity: the work board's
    own authority is the ``work/recorded`` entry in the acting session's log, which
    its route already refuses to proceed without. This entry is the crew-side
    record of the same fact, so a crew log that cannot be written must not fail the
    ledger write that succeeded -- a caller reads ``0`` as "not recorded" and
    carries on.
    """
    try:
        log = _crew_unit(store)
        if log is None:
            return 0
        return int(log.append(entry_type, data, src=envelope.pop("src"), **envelope).seq)
    except Exception as exc:  # noqa: BLE001 - see the best-effort note above
        _report(f"appending {entry_type} for crew {store!r}", exc, op="crew-append")
        return 0


def on_crew_dispatch(store: str, data: dict[str, Any]) -> int:
    """One work item handed to a target, recorded in the dispatching crew's log.

    The OPENER of the dispatch family: the reports for this item thread onto the
    seq returned here. *data* is the ``crew/dispatch`` payload -- ``item``,
    ``target``, and an optional ``brief`` -- and the registry checks the rest.

    Returns the appended seq, or ``0`` when nothing was written: the flag is off,
    the crew is unnamed, or ``target`` does not name exactly one party.
    """
    if not _dispatch_target_ok(data):
        logger.warning(
            "crew log: refusing a dispatch whose target names no single party (crew=%r)", store
        )
        return 0
    return _crew_append(store, CREW_DISPATCH, data, src=crew_src(store))


def _max_ref_span() -> int:
    """The cited-span cap, read from the module that owns and enforces it.

    ``Ref`` validates a span against ``schema``, so ``schema`` holds the cap and is
    the single name a caller lowers to change it. The package re-exports the cap and
    caches the value on first access (:pep:`562`), which makes the re-export a
    second copy of one number: a reader that goes through the package can hold a
    value the owner does not have. Reading the owner keeps the clamp this function
    applies and the bound ``Ref`` enforces the same number.

    Imported per call, for the reason :func:`_crew_log` gives: this module stays
    free of import-time work. Every caller reaches here with the store already
    open, so the schema module is loaded by then and the lookup is a dict hit.
    """
    from kiro_crew.crew_log import schema

    return int(schema.MAX_REF_SPAN)


def on_crew_report(store: str, data: dict[str, Any], *, cite_unit: str) -> int:
    """One report on a work item, recorded in the DISPATCHING crew's log.

    ``src`` is ``gateway`` rather than a crew guest form: the reporting party here
    is a session, and the gateway is what writes a session's report into the crew's
    file.

    *cite_unit* is the reporting session's crew-log unit, and it is what makes the
    required ``ref`` the writer's obligation rather than the caller's: the span is
    built here, from that unit's own newest seq, so a report cannot be written
    without evidence. The span is clamped to the newest ``MAX_REF_SPAN`` lines,
    which is what the cap is for -- a long run is cited by its relevant span
    rather than in full. A unit with no readable log yields no citation and the
    report is not written, because a report with no ``ref`` is an unfalsifiable
    claim in a file nothing rewrites.

    A report whose dispatch anchor does not resolve is written UNTHREADED rather
    than dropped. The anchor can be missing for two reasons, and one of them does
    not heal: a read that failed transiently leaves the dispatch on disk, so the
    item's next report threads normally, but a dispatch whose own best-effort
    append failed leaves no dispatch entry at all -- and then refusing the reply
    refuses every later report for that item too, so the crew log reads for good
    as though the item was never dispatched. Silence about the work is the worse
    record: it is unbounded in time and invisible, while an unthreaded report
    states that the work happened and is merely missing its link.

    What that costs is worth naming, because it is not free. The spec reads a
    report with no ``thread`` as one volunteered with no dispatch behind it, so an
    unthreaded report here is indistinguishable from a volunteered one -- one
    field is ambiguous, rather than one item's whole history being absent. The
    anomaly is logged when it happens, which is where a reader looks to tell the
    two apart.

    The refusal that remains is the evidence one above: no citable unit means no
    write at all, because a report that cannot be checked is a claim, not a record.

    Returns the appended seq, or ``0`` when nothing was written.
    """
    subsystem = _crew_log()
    try:
        if not cite_unit or not subsystem.CrewLog.exists(_KIND, cite_unit):
            return 0
        last = int(subsystem.CrewLog.open(_KIND, cite_unit).last_seq)
    except Exception as exc:  # noqa: BLE001 - see _crew_append's best-effort note
        _report(f"citing {cite_unit!r} for a crew report", exc, op="crew-report-cite")
        return 0
    if last < 1:
        return 0
    span = _max_ref_span()
    evidence = subsystem.Ref(_KIND, cite_unit, max(1, last - span + 1), last)
    log = None
    try:
        log = _crew_unit(store)
    except Exception as exc:  # noqa: BLE001 - see _crew_append's best-effort note
        _report(f"opening the crew log for {store!r}", exc, op="crew-log-open")
    if log is None:
        return 0
    thread = _crew_thread(log, data.get("item"))
    if thread is None:
        # The OBSERVATION only. What happens next is not known yet: the append below
        # can fail, and its failure goes through ``_report``, which warns once per
        # process and is debug-only afterwards -- so a line claiming the report landed
        # would be the only default-level trace of a write that did not.
        logger.warning(
            "crew log: no dispatch to thread %r onto in crew %r",
            data.get("item"),
            store,
        )
    try:
        entry = log.append(CREW_REPORT, data, src=_SRC_GATEWAY, thread=thread, ref=evidence)
        if thread is None:
            logger.warning(
                "crew log: recorded the report for %r in crew %r unthreaded, so it reads "
                "as volunteered with no dispatch behind it",
                data.get("item"),
                store,
            )
        return int(entry.seq)
    except Exception as exc:  # noqa: BLE001 - see _crew_append's best-effort note
        _report(f"appending {CREW_REPORT} for crew {store!r}", exc, op="crew-report-append")
        return 0
