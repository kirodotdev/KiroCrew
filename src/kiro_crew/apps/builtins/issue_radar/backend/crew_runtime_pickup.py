"""Pickup due: the sweep's wake for a crew that has room and more to choose from.

The scope-label and new-issue signals say that work ARRIVED, once, on first sight. A
crew that is full at that moment cannot take it, and when a slot frees later nothing
arrives, so nothing would wake it until its idle clock. This signal closes that: per
live crew, on the open-issues page the sweep has already read, it reads

- the free slots, ``max_open`` minus ``crew_store.open_slot_count``,
- the claimable numbers: open issues in the crew's label scope (an empty scope is
  every issue), not carrying a ``crew:`` label, the claim label or the needs-human
  label, not in the shared skip index and not one of the crew's open items -- the
  brief's "Selecting an issue" filters, minus the random pick,

and compares them with the last state it OBSERVED for that crew. It wakes when the
crew has something new to decide: a claimable number the stored state does not hold,
or a different number of free slots. A freed slot is room for a candidate it already
saw; a slot fewer is the crew's own claim, and a turn takes one issue, so with room
left the next pick is due. Candidates only leaving (slid off the page, claimed by
someone else) is never worth a turn, so that state is written straight away without a
wake. That keeps the comparison against the latest view: a candidate that leaves and
comes back is new again. A turn that looks and takes nothing commits the state it
saw, so the crew is not woken again for the same choice.

A grown state is committed only once the wake started a turn; a crew that was mid-turn
keeps the old state and is woken on the next sweep. A crew with no room or no
candidate has its state cleared, so the first sweep it has both again wakes it. A crew
with an item in an acting phase is not read at all: its turns advance that item before
they pick anything up, and its 300 s clock already brings them, so its state waits
until the item parks.

FIRST observation (no stored state) is growth, unlike the other new-work signals: with
the idle clock at a day, this wake is what gives a freshly launched crew, or one
upgraded from a build without this signal, its first selection turn -- one turn per
crew. A gateway restart replays nothing: the state lives in ``crew-wake-checks.json``
and survives it.

The page is the newest-updated first page, so an issue older than it is invisible
here; the idle backstop covers that.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

SIG_PICKUP_DUE = "pickup-due"

#: The ``crew-wake-checks.json`` section: crew id ->
#: ``{"numbers": [...], "free": n, "held": [...]}``.
PICKUP_SECTION = "pickup"

#: The most numbers one crew's stored state keeps. Larger than any open-issues page a
#: forge serves, so a real observation is never cut; it bounds only a state file that
#: something else wrote.
_MAX_PICKUP_NUMBERS = 500

#: Labels with this prefix are the crews' own (claimed, needs-human, ...): the brief's
#: "drop anything already carrying a ``crew:`` label".
_CREW_LABEL_PREFIX = "crew:"


def claimable_numbers(free: Iterable[tuple[int, set[str]]], crew_scope: list[str]) -> list[int]:
    """The numbers in *free* the crew could select, sorted.

    *free* is already past the claim, needs-human, skip-index and own-item filters;
    *crew_scope* is casefolded, and empty means no label filter at all.
    """
    wanted = set(crew_scope)
    return sorted(
        number
        for number, labels in free
        if (not wanted or labels & wanted)
        and not any(label.startswith(_CREW_LABEL_PREFIX) for label in labels)
    )


def _stored_state(entry: Any) -> tuple[set[int], int, set[int]] | None:
    """``(numbers, free, held)`` from a stored entry, or ``None`` when there is none usable.

    Read back whole: the comparison is against what was last observed, and cutting
    it to anything smaller would make the numbers it dropped look new. An entry of
    another shape reads as absent, which wakes: a missed turn costs a day, a spare
    one costs a turn.
    """
    if not isinstance(entry, Mapping):
        return None
    numbers, free, held = entry.get("numbers"), entry.get("free"), entry.get("held")
    if not isinstance(numbers, list) or not isinstance(held, list):
        return None
    if not isinstance(free, int) or isinstance(free, bool):
        return None

    def ints(values: list[Any]) -> set[int]:
        return {n for n in values if isinstance(n, int) and not isinstance(n, bool)}

    return ints(numbers), free, ints(held)


def stage_pickup(
    checks: dict[str, Any],
    cid: str,
    claimable: list[int],
    held: Iterable[int],
    max_open: int,
    staged: dict[str, dict[str, Any]],
    due: dict[str, str],
) -> None:
    """Decide the pickup wake for one crew, in place.

    Growth goes into *due* (crew id -> reason) and the observed state into *staged*,
    committed only by :func:`settle_pickup` once a turn started. A shrunk state is
    written to *checks* at once; no room or no candidate clears the crew's entry.
    *held* is the issue numbers of the crew's open items: one more is a claim, and
    a turn takes one issue, so with room left the next pick is due even when an item
    that closed in the same stretch left the free count where it was.
    """
    section = checks.setdefault(PICKUP_SECTION, {})
    mine = sorted(set(held))[:_MAX_PICKUP_NUMBERS]
    free_slots = max_open - len(mine)
    if free_slots <= 0 or not claimable:
        section.pop(cid, None)
        return
    numbers = sorted(set(claimable))[:_MAX_PICKUP_NUMBERS]
    current = {"numbers": numbers, "free": free_slots, "held": mine}
    stored = _stored_state(section.get(cid))
    if (
        stored is None
        or set(numbers) - stored[0]
        or free_slots != stored[1]
        or set(mine) != stored[2]
    ):
        staged.setdefault(cid, {})["pickup"] = current
        due[cid] = (
            f"{SIG_PICKUP_DUE} ({len(numbers)} claimable, {free_slots} of {max_open} slots free)"
        )
    elif section.get(cid) != current:
        section[cid] = current


def fold_pickup_reasons(
    due: Mapping[str, str],
    reasons: dict[str, list[str]],
    woken: dict[str, Any],
    staged: Mapping[str, dict[str, Any]],
    arrivals: Iterable[str],
) -> None:
    """Add each due pickup wake to *reasons* and *woken*, unless something else already
    wakes that crew this sweep.

    A turn woken by an arrival signal (*arrivals*: work came into the crew's view) runs
    selection, so the pickup state commits with it. Any other turn advances the item
    that signalled before it picks anything up (the brief's per-turn order) and may run
    no selection, so its pickup state is left unstaged and the growth is found again on
    the next sweep."""
    arrived = set(arrivals)
    for cid, reason in due.items():
        if reasons.get(cid):
            if not any(r.split(" ")[1:2] and r.split(" ")[1] in arrived for r in reasons[cid]):
                (staged.get(cid) or {}).pop("pickup", None)
            continue
        reasons[cid] = [reason]
        woken.setdefault(cid, []).append(SIG_PICKUP_DUE)


def settle_pickup(checks: dict[str, Any], cid: str, staged: Mapping[str, Any]) -> bool:
    """Record the staged state for a crew whose wake started a turn. Returns whether
    *checks* changed."""
    state = staged.get("pickup")
    if not isinstance(state, Mapping):
        return False
    checks.setdefault(PICKUP_SECTION, {})[cid] = dict(state)
    return True
