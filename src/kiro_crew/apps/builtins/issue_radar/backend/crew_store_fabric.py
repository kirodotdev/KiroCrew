"""The fabric fold: every crew's work items laid out on one lane.

The fabric payload's own schema version, the spine and off-spine phases, one work
item folded onto the lane, and the issue-title hints read from the list cache.
Callers reach every name here through
:mod:`kiro_crew.apps.builtins.issue_radar.backend.crew_store`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import store
from .crew_store_ledger import read_ledger
from .crew_store_records import PHASES, _finite_int, list_crews, logger

#: Bump on any incompatible change to :func:`fold_fabric`'s item shape. Its own
#: field, not :data:`CREW_SCHEMA`: the fabric payload is derived, not stored, so it
#: versions on its own cadence.
FABRIC_SCHEMA = 1

#: The phases that form the drawn SPINE, in topological order. It is
#: :data:`PHASES` minus the off-spine ones, and ``resolved`` is the ONLY terminal
#: left on it — the other terminals are alternate endings, not later stages, so
#: giving them a column would imply a skipped item got further than a claimed one
#: (PLAN design decision #2). ``awaiting-reply`` is off-spine for the same reason:
#: the crew is not the actor, it has handed the issue back to a human.
SPINE_PHASES = (
    "selected",
    "claimed",
    "investigating",
    "implementing",
    "awaiting-ci",
    "addressing-review",
    "awaiting-merge",
    "resolved",
)

#: Phases that render as a stub OFF the lane, not as a column. Every phase not on
#: the spine, computed from :data:`PHASES` so a phase added to one set can never be
#: silently absent from the other.
OFF_SPINE_PHASES = frozenset(PHASES) - frozenset(SPINE_PHASES)


def _fold_one_item(
    record: dict[str, Any],
    events: list[dict[str, Any]],
    title_hints: dict[int, str] | None = None,
) -> dict[str, Any]:
    """Fold ONE work item and its phase-bearing ledger lines into a fabric item.

    *events* are this item's lines in TIME order (oldest first). Each contributes a
    ``phase`` only if it carries one — a pre-feature line, or a write that carried
    no phase, simply has no key and is skipped here, which degrades the drawing to
    L0/L1 for that item rather than failing the fold.

    *title_hints* maps issue/PR ``number`` to the issue's REAL title, seeded from
    the issues and pulls list caches (:func:`_fabric_title_hints`). The crew ledger
    stores NO title — a work item carries ``number`` and ``phase``, not what the
    issue is called — so the title has to come from the same list caches Issue
    Radar already keeps, at zero extra API cost. A number with no cached title
    (never fetched, or aged out) folds to ``""`` and the lane shows its id only,
    rather than mislabelling the lane with the crew's resumable ``next`` intent.

    Three things a naive fold gets wrong, each pinned by a test:

    * **The live phase is the record's, authoritative — never the max timeline
      index.** A round-trip through review (``awaiting-ci -> addressing-review ->
      awaiting-ci``) ends LEFT of where it has been, so keying the head off the
      furthest column reached puts the item in a phase it already left. This
      function reads ``phase`` straight off the record and returns it as its own
      field; ``timeline`` is only the history.

    * **Off-spine phases are an ``exit``, not a timeline entry.** ``skipped`` /
      ``yielded`` / ``handed-back`` / ``preempted`` / ``awaiting-reply`` leave the
      spine's topology intact. ``exit`` is the LAST off-spine line seen — but it is
      cleared the moment a later on-spine line reopens the item, so it stands only
      when the item's live phase is itself off-spine. (The record's live phase is
      the tie-breaker: a torn ledger that ends off-spine while the record is on-spine
      still clears the exit.)

    * **Re-entering a phase after an exit is a reopen, and each restarts the dwell
      clock.** ``reopens`` counts an on-spine line landing on an already-visited
      phase while an exit was standing — the store clears ``outcome``/``finished_at``
      on exactly that transition, so it is a real second life, not churn.
    """
    timeline: list[dict[str, Any]] = []
    seen_spine: set[str] = set()
    exit_entry: dict[str, str] | None = None
    reopens = 0

    for ev in events:
        ph = ev.get("phase")
        if not isinstance(ph, str) or ph not in PHASES:
            continue
        at = str(ev.get("ts") or "")
        if ph in OFF_SPINE_PHASES:
            exit_entry = {"phase": ph, "at": at}
            continue
        # An on-spine line.
        if ph in seen_spine and exit_entry is not None:
            # Came back to a phase already visited, AND an exit was standing — this
            # is a genuine reopen (the store cleared the terminal fields to make it
            # one), not a round-trip through review within the spine.
            reopens += 1
        if exit_entry is not None:
            # Reopened: the exit does not hold, whatever it was.
            exit_entry = None
        seen_spine.add(ph)
        timeline.append({"phase": ph, "at": at})

    live_phase = record.get("phase")
    if not isinstance(live_phase, str) or live_phase not in PHASES:
        live_phase = "selected"
    # The record is authoritative. If it says the item is on-spine, no stale exit
    # from a torn tail may stand; if it says off-spine, that is the exit even when
    # the fold's own last line disagreed.
    if live_phase in OFF_SPINE_PHASES:
        if exit_entry is None or exit_entry.get("phase") != live_phase:
            exit_entry = {
                "phase": live_phase,
                "at": str(record.get("finished_at") or record.get("last_progress_at") or ""),
            }
    else:
        exit_entry = None

    number = record.get("number")
    hint_number = number if isinstance(number, int) and not isinstance(number, bool) else None
    title = ""
    if title_hints is not None and hint_number is not None:
        title = str(title_hints.get(hint_number) or "")
    # ``next`` is the crew's RESUMABLE INTENT ("add the Windows branch to
    # _safe_chmod"), not what the issue is called — it stays available under its
    # own name for a view that wants to show what the crew is about to do, but it
    # is NEVER the title. The title is the issue's real title from the list caches.
    return {
        "number": number,
        "crew_id": record.get("crew_id"),
        "title": title,
        "next": str(record.get("next") or ""),
        "pr_number": _finite_int(record.get("pr_number")),
        "phase": live_phase,
        # No `ci_state` here. It had no reader -- the app declared a type for it and
        # never touched the value -- and it was the one field in this payload carrying
        # an arbitrary nested dict straight from the record. `json.dumps` writes a
        # non-finite float as bare `NaN`, which is not JSON, so one hand-edited or
        # restored record could make `GET /crew/fabric` unparseable and the browser
        # would drop EVERY lane, not just that item. `pr_number` above already goes
        # through `_finite_int` for exactly this reason; an unread field does not earn
        # a sanitiser, it earns deletion.
        "timeline": timeline,
        "exit": exit_entry,
        "reopens": reopens,
    }


def _fabric_title_hints(owner: str, repo: str, root: Path | None = None) -> dict[int, str]:
    """``number -> issue/PR title``, seeded from the issues and pulls list caches.

    The crew ledger records no title — a work item is ``number`` + ``phase`` — so
    the lane's real title comes from the SAME list caches Issue Radar already
    keeps, at ZERO extra API cost: this reads whatever is cached and never fetches.
    A number that was never cached (or whose cache aged out) simply has no hint,
    and its lane degrades to showing the id alone rather than being mislabelled.

    Both open AND closed states are read, because a crew work item outlives the
    issue's open state — an item can be ``resolved``/``skipped`` while its issue is
    closed, so the open cache alone would drop exactly the finished lanes. Issues
    are read first and pulls layered on top: a work item that has become a PR is
    keyed by the ISSUE number it was claimed under, and the PR title is the more
    specific label for that lane once one exists, so it wins on a collision.

    Never raises: each cache read already tolerates an absent/torn/stale-schema
    file by returning ``None`` (treated as empty here), so a repo with no caches
    yields ``{}`` and every lane falls back to its id.
    """
    hints: dict[int, str] = {}

    def _absorb(rows: list[dict[str, Any]] | None) -> None:
        if not rows:
            return
        for row in rows:
            if not isinstance(row, dict):
                continue
            num = row.get("number")
            if not isinstance(num, int) or isinstance(num, bool):
                continue
            title = row.get("title")
            if isinstance(title, str) and title.strip():
                hints[num] = title.strip()

    # A cache read that fails must cost the TITLES, not the endpoint. These files are
    # Issue Radar's, written by its own refresh, so a refresh landing between the
    # reader's existence check and its read raises rather than returning empty -- and
    # a 500 for a failed title lookup contradicts the degradation this join already
    # promises everywhere else, where a number with no cached title simply renders as
    # its id. Broad on purpose: any unreadable cache means "no hints from that cache".
    for reader in (store.read_issues_cache, store.read_pulls_cache):
        for state in ("open", "closed"):
            try:
                _absorb(reader(owner, repo, root, state=state))
            except Exception:
                logger.debug(
                    "fabric title hints: %s cache unreadable for %s/%s (%s)",
                    reader.__name__,
                    owner,
                    repo,
                    state,
                    exc_info=True,
                )
    return hints


def fold_fabric(owner: str, repo: str, root: Path | None = None) -> list[dict[str, Any]]:
    """Every crew work item in this repo, folded into a fabric lane, newest first.

    ONE pass over the repo's crews, each read from its folded ledger: the item
    records and, per item, the lines that ENTERED a phase (the fold keeps those per
    item, so a lane parked in one phase for a long time keeps its entry line however
    much the other lanes chatter). A pre-projection line that carried no phase
    contributed nothing then and contributes nothing now, so an old history degrades
    the drawing rather than breaking the fold.

    Ordered newest-progress-first — the same order the crew page lists items in — so
    an operator scanning the pipeline sees the freshly-moved lanes at the top.
    """
    # Real issue/PR titles from the list caches Issue Radar already keeps — one
    # read per cache for the WHOLE fold, not per lane.
    title_hints = _fabric_title_hints(owner, repo, root)

    items: list[dict[str, Any]] = []
    for crew in list_crews(owner, repo, root, include_retired=True):
        cid = str(crew.get("id") or "")
        if not cid:
            continue
        ledger = read_ledger(owner, repo, cid, root)
        phase_lines = ledger.get("phase_lines") or {}
        for rec in ledger["items"]:
            num = rec.get("number")
            if not isinstance(num, int) or isinstance(num, bool):
                continue
            events = [
                {"phase": row.get("phase"), "ts": row.get("at")}
                for row in phase_lines.get(str(num), [])
            ]
            item = _fold_one_item(rec, events, title_hints)
            item["_sort"] = str(rec.get("last_progress_at") or "")
            items.append(item)

    items.sort(key=lambda it: it.pop("_sort"), reverse=True)
    return items
