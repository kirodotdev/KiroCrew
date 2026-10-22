"""The ``subagents`` session fold: which children a session dispatched and, for each,
what happened, how long it ran and what it cost.
"""

from __future__ import annotations

import math
from typing import Any, Final

from kiro_crew.crew_log.projection_side_panel import OPEN_RETAIN_LIMIT
from kiro_crew.crew_log.projection_usage import _credit_charge
from kiro_crew.crew_log.projection_values import _as_id, _as_str
from kiro_crew.crew_log.schema import Entry

# --------------------------------------------------------------------------- #
# subagents
# --------------------------------------------------------------------------- #
#
# WHAT THIS FOLD IS FOR. "Which children did this session dispatch, and for each one
# what happened, how long it ran and what it cost" -- answered from one savepoint read
# rather than from a 200-entry ``timeline`` window that happens to have swallowed the
# four ``subagent/*`` types along with everything else.
#
# The same number lives in five places besides this one -- the runtime accumulator, the
# terminal tombstone, the persistence record, the WS frame and its replay, and the
# browser store -- and keeping those five agreeing is where a reader's numbers go wrong.
# This fold is the one that is DERIVED from the log, so it cannot disagree with the
# record without the record itself being wrong.


#: Outcomes this fold counts under their own name. ``subagent/failed`` carries an OPEN
#: enum, deliberately: the value is the subagent runtime's own, and enforcing the set
#: would turn "the upstream vocabulary grew" into a lost record. So a closer whose
#: outcome is not one of these lands in ``unknown``, which is what the declaration
#: already means by it -- the row keeps the literal string, so the fact is not lost at
#: the level that can hold it.
_SUBAGENT_OUTCOMES: Final[frozenset[str]] = frozenset({"completed", "failed", "stopped", "unknown"})

#: Entry types ``subagents`` reads: one opener, two closers, and the dismissal. A
#: dismissal is neither an opener nor a closer -- it is the user clearing a card -- but
#: this fold is what the panel draws, so it is the fold that has to stop drawing the
#: row. ``subagent/steered`` is DECLARED in the session vocabulary and deliberately not
#: read here -- a steer is an event about a child rather than a state of one, and
#: nothing this fold answers for is a count of them. Leaving it out of ``affects`` is
#: safe in the direction that matters: the set may be wider than the truth but never
#: narrower, and a type the step ignores would cost a copy per entry for a value that
#: never changes.
SUBAGENT_TYPES: Final[frozenset[str]] = frozenset(
    {
        "subagent/spawned",
        "subagent/completed",
        "subagent/failed",
        "subagent/dismissed",
    }
)


def _subagents_start() -> dict[str, Any]:
    return {
        "by_id": {},
        # Deliberately NO ``open`` list. ``_subagents_render`` answers which children are
        # still going from the rows themselves (an ``outcome`` of ``None``) and from the
        # totals, so a second list here would be state nothing reads, kept in step by hand
        # on every closer.
        # Dispatches this fold counted but did NOT retain, for any of THREE reasons: an
        # id that identifies nothing (empty or over-long), an id a retained row already
        # holds, or a full ``by_id``. The three reasons differ; what they share is the
        # only thing this counter is for, which is keeping
        # ``spawned == len(by_id) + omitted`` true.
        "omitted": 0,
        "totals": {
            # EVERY dispatch, including the ones ``omitted`` counts. See
            # ``_subagents_render`` for why the two are allowed to disagree.
            "spawned": 0,
            "completed": 0,
            "failed": 0,
            "stopped": 0,
            "unknown": 0,
            # Closers that found no row. Without this, a session whose totals exceed
            # what its rows account for reads as an arithmetic bug.
            "closed_unmatched": 0,
            # DELIBERATELY no cost or duration aggregate here. ``usage`` folds the credits
            # one from these same closers -- ``CREDIT_SOURCES`` carries ``"subagent"``, and
            # ``credits_by_source["subagent"]`` is ``{"credits", "reported"}`` over exactly
            # this population -- so a second spelling would be two things to keep in step.
            # A summed duration had no such owner and no reader either, and the rule this
            # module holds is the one stated for a row: a field kept against a reader that
            # does not exist is state paid for on every copy and every savepoint write.
            # Both figures still ride per child on the row that closed, which answers the
            # question a surface actually asks -- whose, not how much altogether. What that
            # gives up is the duration of a child whose dispatch left no row: its outcome
            # and ``closed_unmatched`` still record that it ran and how it ended, but how
            # long it took is not kept anywhere.
        },
    }


def _subagent_credits(value: Any) -> float | None:
    """*value* as a credit charge on ONE row, or ``None`` when none was reported.

    Absent is NOT zero: a child that was never billed and a child that cost nothing
    are different facts, and only one of them is a measurement.

    The SHAPE screen is :func:`_credit_charge`, the module's single one. What this adds is
    the part that screen deliberately leaves to its caller -- sign and finiteness -- stated
    per VALUE rather than against a running total, because this fold keeps none: ``usage``
    owns the aggregate over these same closers, so the only credit figure published here is
    the one on a row, and a row has nothing to accumulate into.

    Both clauses are about what the row would PUBLISH. A non-finite value survives
    ``round``, the savepoint persists it, and a cold refold reads the same entry again, so
    one unusable charge would sit in the served projection for the life of the unit. A
    negative one is not a charge: the module's spend invariant is that a total never goes
    down, and a row is a term in someone else's.
    """
    charge = _credit_charge(value)
    if charge is None or not math.isfinite(charge) or charge < 0:
        return None
    return charge


#: The keys of one ``subagents.by_id`` row: what a page reads to draw a child -- its
#: ``task``, its ``outcome`` (``None`` while it runs) and how long and how much it took.
SUBAGENT_ROW_KEYS: tuple[str, ...] = (
    "agent_id",
    "seq_spawned",
    "started_ms",
    "agent",
    "model",
    "task",
    "outcome",
    "ms",
    "credits",
    "reason",
    "dismissed",
)


def _subagents_step(state: dict[str, Any], entry: Entry) -> None:
    data = entry.data
    agent_id = _as_id(data.get("agent_id"))
    rows: dict[str, Any] = state["by_id"]
    totals: dict[str, Any] = state["totals"]

    if entry.type == "subagent/spawned":
        # Counted BEFORE the cap is consulted: this is how many children the session
        # dispatched, which is a fact about the session rather than about how many rows
        # this fold chose to keep.
        totals["spawned"] += 1
        # An empty or over-long id identifies nothing, so keying a row by it would make
        # every such child the same child and let one closer close another's row. It is
        # counted above and left unretained, the same path a dispatch past the cap takes.
        if not agent_id or agent_id in rows:
            # Both of these dispatches were counted in ``spawned`` and neither gets a row of
            # its own, so both belong in ``omitted``: that is what keeps
            # ``spawned == len(by_id) + omitted`` true, which the spec advertises as
            # checkable. A dispatch that vanished from both sides of the identity would read
            # to an auditing reader as arithmetic this fold got wrong.
            state["omitted"] += 1
            return
        if len(rows) >= OPEN_RETAIN_LIMIT:
            # The bound every fold holds: a session that dispatched more
            # children than the cap keeps its totals exact and stops growing the state.
            state["omitted"] += 1
            return
        rows[agent_id] = {
            "agent_id": agent_id,
            # Retained because ``render`` orders the rows by it. Deliberately the SEQ and
            # not the time: two children dispatched in one millisecond tie on a clock, and
            # a seq is what the log guarantees is ordered.
            "seq_spawned": entry.seq,
            # The envelope's own stamp, NOT a field the entry carries -- the log already
            # records when each line was written, so a ``started`` in ``data`` would be a
            # second clock a writer could disagree with. It is kept because a reader
            # assembling one list from SEVERAL sessions' folds has no common order
            # otherwise: ``seq_spawned`` orders rows inside one log and says nothing
            # across two. It also dates a row, which is what an age window needs.
            "started_ms": entry.time,
            "agent": _as_str(data.get("agent")),
            "model": _as_str(data.get("model")),
            # What the child was asked to do. ``""`` when the entry carried none, which
            # includes every log written before the field existed -- a surface draws no
            # task line for it rather than an empty one.
            "task": _as_str(data.get("task")),
            "outcome": None,
            # ``None``, not 0, for the same reason ``credits`` is: a closer writes ``ms``
            # only when it measured a duration above zero, and crash-repair's closer writes
            # none at all, so a 0 would present an absent measurement as a measured instant.
            "ms": None,
            "credits": None,
            "reason": "",
            # Whether the user has cleared this child's card. Latches true and never
            # back: a dismissal is an act, and the log does not un-record one.
            "dismissed": False,
        }
        return

    if entry.type == "subagent/dismissed":
        # The user clearing a card, which is neither an opener nor a closer: the row
        # keeps whatever outcome its own closer recorded, and a dismissal may arrive
        # before any closer for a child cleared while it was still running. Only the
        # ROW moves -- nothing in ``totals`` does, because what the session dispatched
        # and what it spent are not changed by the user hiding a card, and a spend
        # figure that fell when someone tidied the panel would be wrong.
        row = rows.get(agent_id) if agent_id else None
        if row is None:
            # A dismissal naming no retained row: the dispatch was omitted past the
            # cap, or its opener never reached the file. Nothing is drawn for that
            # child either way, so there is nothing to stop drawing and no counter
            # for it -- a count nothing renders is state paid for on every copy.
            return
        row["dismissed"] = True
        return

    # A closer, and the type is matched EXPLICITLY rather than reached by falling through
    # the two branches above. ``affects`` spares this fold the entries it does not read,
    # but only on the kernel's path (:class:`_SessionFold`): :func:`advance` and
    # :func:`fold` call ``step`` for every entry in the file, so a fall-through ``else``
    # here would bill every ``turn/completed`` in the log as a child that closed.
    if entry.type not in ("subagent/completed", "subagent/failed"):
        return
    if entry.type == "subagent/completed":
        outcome = "completed"
    else:
        outcome = _as_str(data.get("outcome")) or "unknown"
    counted = outcome if outcome in _SUBAGENT_OUTCOMES else "unknown"
    raw_ms = data.get("ms")
    ms = raw_ms if isinstance(raw_ms, int) and not isinstance(raw_ms, bool) else None
    # Screened as the value the ROW would publish. There is no total here to hang the
    # invariant on: ``usage`` owns the aggregate over these closers.
    credits = _subagent_credits(data.get("credits"))

    # The totals move for EVERY closer, row or no row. crash-repair closes a child
    # across the whole file, so a closer routinely names a dispatch this fold omitted --
    # and dropping it would under-report what the session actually ran and spent, which
    # is the one number a reader comes here for.
    totals[counted] += 1

    row = rows.get(agent_id) if agent_id else None
    if row is None:
        totals["closed_unmatched"] += 1
        return
    row["outcome"] = outcome
    row["ms"] = ms
    row["credits"] = credits
    row["reason"] = _as_str(data.get("reason"))


def _subagents_render(state: dict[str, Any]) -> dict[str, Any]:
    """The children this session dispatched, in dispatch order.

    ``totals["spawned"]`` and ``by_id`` are allowed to DISAGREE, and two counts
    reconcile them: ``spawned == len(by_id) + omitted + dismissed``. A reader that wants
    to know what the session did reads the total; one that wants per-child detail reads
    the rows and is told, by a non-zero ``omitted``, that it is holding a window rather
    than the whole list, and by a non-zero ``dismissed`` that the user cleared some of
    the cards. Reporting only the retained count would silently shrink a long session's
    history to the cap and look exact.

    A DISMISSED row is kept in the state and left out of ``by_id``. The row has to stay,
    because a dismissal is an act the log does not un-record and a later closer still
    lands on it; it has to leave ``by_id``, because this fold is what the panel draws and
    the user cleared that card. ``totals`` never moves for a dismissal: what the session
    dispatched and what it spent are facts about the session, so a spend figure that fell
    when someone tidied the panel would be wrong.

    ``credits`` on a row is ``None`` when that child reported no charge, never ``0``.
    A surface drawing this has three states to draw, not two: a number, "no charge was
    reported", and a child that has not closed yet.

    ``task`` is ``""`` when the dispatch recorded none, which is also what every log
    written before the field reads as. A surface draws no task line for it rather than
    an empty one: the log does not say what the child was asked, which is not the same
    claim as its having been asked for nothing.

    ``started_ms`` is the ``subagent/spawned`` entry's own envelope stamp. It is here
    for a reader assembling ONE list out of several sessions' folds, which
    ``seq_spawned`` cannot order -- seqs are per log -- and for dating a row against an
    age window.

    ``running`` is the count still open, and it is derived from TWO floors because neither
    alone is right.

    The totals give ``spawned - closed``, which retention never touches -- so it stays exact
    once the cap has dropped a dispatch, where counting the retained rows without a closer
    would report a session with 600 children in flight as having 512 or fewer.

    But that difference can fall BELOW what this fold can still point at. A closer whose
    ``subagent/spawned`` never reached the readable file -- an append dropped once its
    attempt budget was spent, or a damaged record ``_iter_segments`` skips -- bills into
    ``closed`` having never bumped ``spawned``. With one other child still in flight that
    arithmetic answers 0 while a retained row is drawn wearing a running pill, and a floor
    at 0 hides it rather than fixing it. So the retained open rows are the other floor: the
    result is at least the number of children this fold is still showing as open. In the
    retain-cap case the omitted dispatch WAS counted in ``spawned``, so the totals figure is
    the larger of the two and stays exact.
    """
    rows = sorted(state["by_id"].values(), key=lambda row: row["seq_spawned"])
    # The rows a surface draws: a dismissed card is one the user cleared, and the whole
    # point of recording the dismissal here is that this fold stops offering the row.
    # The dismissed ones stay in ``totals`` -- what the session dispatched and what it
    # spent are facts about the session, and a spend figure that fell when someone
    # tidied the panel would be wrong.
    drawn = [row for row in rows if not row["dismissed"]]
    dismissed = len(rows) - len(drawn)
    totals = dict(state["totals"])
    closed = sum(totals[outcome] for outcome in sorted(_SUBAGENT_OUTCOMES))
    # Counted, not emitted. An id list and the cap were both in this value with no reader:
    # the panel derives the open rows by filtering `by_id` for an absent outcome, which is
    # the same filter, so the list was a second spelling of something already there.
    #
    # Over the DRAWN rows, so a dismissed child still in flight puts no running pill on
    # a panel that does not show it. The totals floor below still counts it, which is
    # right: it is running whether or not anyone is looking.
    still_open = sum(1 for row in drawn if row["outcome"] is None)
    return {
        "by_id": {
            row["agent_id"]: {
                "agent_id": row["agent_id"],
                "seq_spawned": row["seq_spawned"],
                "started_ms": row["started_ms"],
                "agent": row["agent"],
                "model": row["model"],
                "task": row["task"],
                "outcome": row["outcome"],
                "ms": row["ms"],
                "credits": row["credits"],
                "reason": row["reason"],
            }
            for row in drawn
        },
        "running": max(still_open, totals["spawned"] - closed),
        # Whether that number is the answer or a FLOOR under it. It is a floor exactly when
        # both truncations are in play at once: a dispatch this fold omitted, and a closer
        # that matched no row. Each unmatched closer is subtracted from the count, but the
        # fold cannot tell whether it closed one of the omitted dispatches or a child whose
        # ``subagent/spawned`` never reached the file -- and only the first of those two
        # should reduce the running count. So a session at 513 dispatches with one orphan
        # closer reports 512 while 513 are in flight. A surface stating the count says "at
        # least" when this is false, because a number that can be short by an unknown amount
        # and is drawn as exact is the same class of lie as a truncated list drawn as whole.
        "running_exact": not (state["omitted"] > 0 and totals["closed_unmatched"] > 0),
        "omitted": state["omitted"],
        # Retained rows this render is NOT offering, because the user cleared them. It
        # is published rather than left implicit because it is the third term in the
        # identity above: without it a reader checking
        # ``spawned == len(by_id) + omitted`` sees it fail the moment anyone dismisses
        # a card, and cannot tell a tidied panel from arithmetic the fold got wrong.
        "dismissed": dismissed,
        "totals": totals,
    }
