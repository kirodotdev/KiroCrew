"""The ``usage`` session fold: what a session spent in tokens and credits, the context
injected per turn, and its compactions -- with the caps it keeps and the credit table
(``CREDIT_SOURCES``) every billing fold charges by.
"""

from __future__ import annotations

import math
from typing import Any, Final

from kiro_crew.context_blocks import PHASE_SESSION_START
from kiro_crew.crew_log.projection_usage_measured import (
    decode_measured,
    measured_start,
    measured_step,
    note_closed,
    note_composed,
    note_unit_start,
)
from kiro_crew.crew_log.projection_values import _as_int, _as_str, _keyable, _note_omitted
from kiro_crew.crew_log.schema import Entry

#: Newest per-turn context rows a ``usage`` projection keeps, the same posture and
#: the same value as :data:`TIMELINE_LIMIT`: this is the second window the session
#: folds hold, and a projection is pushed over a socket on every growth, so its size is
#: bounded by construction rather than by how long the session ran. No count of what
#: fell off the front rides beside it: every retained row carries its own ``ordinal``,
#: assigned before any truncation, so a reader reads the first row's true position and
#: knows exactly how much precedes it. A whole-session drop total could not answer
#: that for a reader bounded to a narrower window, which is what made it a defect.
#:
#: Measured at the WORST CASE rather than reasoned about, since this is the fold's
#: largest new state: 200 rows each holding :data:`CONTEXT_SOURCES_PER_TURN_LIMIT`
#: sources whose labels sit at :data:`TEXT_LIMIT`, and the engine's measurement at its
#: cap (one string, see ``projection_usage_measured``), is 864,361 bytes of Python
#: objects; the measurement is 55,449 of them.
#: ``test_a_full_window_stays_under_the_slot_fold_cell_budget`` re-measures
#: that same worst case, so the figure fails rather than rots -- and it measures the
#: CAP, not a realistic row, because a realistic 40-source row is 511,192 and would
#: leave the real ceiling untested.
#:
#: It matters because this fold is read per SLOT, so its state lands in the slot-fold
#: cache, whose own ceiling is stated against its largest member -- ``radar`` at
#: 995,342 bytes. A full window plus a full ordinal memo (``context_turn_keys``, 120,042
#: bytes at its widest) fits under that with 10,939 bytes to spare, which is why a row's
#: measurement is a string and not a dict: as a dict of its numbers it would not fit.
#: The row shape is what buys that: the entry's own list of three-key dicts measures
#: 3,319,357 at the same worst case and would breach the ceiling by 2.3 MB. See
#: ``row_sources`` in ``_usage_step``.
CONTEXT_TURNS_LIMIT: Final[int] = 200

#: Sources one retained context row details. The label vocabulary
#: (``context_blocks.split_blocks``) is fixed and far under this, so the cap is what
#: keeps a row bounded against a NEWER writer's longer vocabulary rather than a limit
#: today's writer reaches. Past it the row's own ``chars`` total stays as recorded, so
#: a truncated source list is never mistaken for a smaller prompt, and the session-wide
#: ``by_source`` breakdown counts every source regardless -- it is keyed by label and so
#: is not subject to this per-row cap. Each source a row leaves out is counted in the
#: rendered ``context.sources_omitted``.
CONTEXT_SOURCES_PER_TURN_LIMIT: Final[int] = 64

#: Distinct models a ``usage`` projection details per model. Past it the whole-
#: session totals stay exact and ``models_omitted`` counts the models left out,
#: the same posture as ``TOOL_NAME_LIMIT``. A turn carries a model string, so an
#: unbounded ``by_model`` would grow the checkpoint over a long session.
MODEL_LIMIT: Final[int] = 100

#: ``(unit, turn)`` keys ``_count_turn_ordinal`` remembers, so a rewound or edit-resent
#: turn reuses its ordinal rather than taking a new one. Sized to the REWINDABLE range:
#: ``chat_rewind`` can re-run any user message still in the slot's live list, and that
#: list holds up to ``dashboard.state._MAX_SLOT_MESSAGES`` (10,000) rows, so up to that
#: many distinct turns can recur. A smaller bound evicts a turn a user can still rewind
#: to, and the rerun then takes a NEW ordinal that nothing refolds. Pinned equal to that
#: cap by ``test_usage_the_ordinal_memo_covers_the_rewindable_range``. Bounded at all
#: because it is checkpointed fold state; the keys are held as ONE string (see
#: ``context_turn_keys``) so the bound costs ~100 KB rather than a dict of 10,000 entries.
#: Oldest-inserted keys evict first.
CONTEXT_ORDINAL_MEMO_LIMIT: Final[int] = 10_000

#: The token dimensions ``turn/completed`` bills, in the order it declares them.
TOKEN_DIMENSIONS: Final[tuple[str, ...]] = ("input", "output", "cache_read", "cache_write")


# --------------------------------------------------------------------------- #
# usage
# --------------------------------------------------------------------------- #


def _usage_start() -> dict[str, Any]:
    return {
        "turns_completed": 0,
        "credits": 0.0,
        # The same charges the total above sums, kept apart by who spent them. Every
        # bucket is present from the start, so an absent source reads as "spent
        # nothing" rather than leaving the reader to guess whether the split is
        # partial. ``reported`` counts the entries that carried a measurement, which
        # is what says how much of the bucket's total is covered -- an absent
        # ``credits`` is an unmetered provider, never a zero charge.
        "credits_by_source": {source: {"credits": 0.0, "reported": 0} for source in CREDIT_SOURCES},
        "tokens": {dimension: 0 for dimension in TOKEN_DIMENSIONS},
        "tokens_turns": 0,
        "duration_ms": 0,
        "duration_turns": 0,
        "by_model": {},
        "models_omitted": 0,
        "models_omitted_saturated": False,
        "omitted_models": [],
        "context_tokens": 0,
        "context_chars": 0,
        "context_blocks": 0,
        "context_estimated": 0,
        "context_by_source": {},
        # Sources a retained row could not detail (see ``CONTEXT_SOURCES_PER_TURN_LIMIT``).
        "context_sources_omitted": 0,
        # The per-turn window the Context panel reads, newest LAST, bounded by
        # ``CONTEXT_TURNS_LIMIT``. The cumulative ``context_by_source`` above answers
        # "what does this session inject in total"; a panel drawing one bar per turn
        # needs the turns themselves, and cannot recover them from a sum.
        "context_turns": [],
        # Which UNIT each row came from, counted off ``session/opened``. This works WITH
        # the ``_closed`` seal, and neither covers the other's case: the seal tells two
        # CLOSED runs of one ordinal apart (an attempt that already finished, in this
        # unit or an earlier one), while the unit tells apart a run that was never closed
        # at all -- a unit cut off mid-turn leaves its rows unsealed, and the next unit's
        # same-ordinal turn walks straight onto them. Measured both ways: seal alone
        # stamps the earlier unit's open row, unit alone stamps an earlier attempt whose
        # own closer reported no reading. Internal to the fold; stripped at render.
        "context_unit": 0,
        # Which crew-log UNIT the ordinal key belongs to, counted off the ``session/opened``
        # that STARTS a unit (``resumed`` not true) and nothing else. Distinct from
        # ``context_unit`` above, which also moves on every re-attachment: a re-attach is
        # the same conversation picked up again, and its turn numbers continue the same
        # unit's, so a regenerate after a gateway restart must find the key its first run
        # left. Only a new unit -- a new session or a compaction, each its own crew log --
        # starts a new key space, because that is where turn numbers restart.
        "context_key_unit": 0,
        # Whether a session-start composition has been folded yet, so exactly one row
        # -- the session's real start -- is stamped ``first_start``.
        "context_started": False,
        # A monotonic 1-based counter over the slot's TURN HISTORY, never reset by
        # truncation. Each composition row is stamped with its value as ``ordinal`` so a
        # retained row carries its TRUE position in the whole session history -- exact
        # regardless of how many older rows the window dropped or the day view excluded,
        # and a reader shows it directly instead of deriving a turn number from an array
        # index, which only counts the rows still present.
        #
        # It advances for turns WITHOUT a composition too, not just per composition row:
        # a turn can complete having composed nothing (a refusal, or a turn that reused
        # the prompt already in context), and if the counter only moved on compositions
        # a later row would carry an ordinal that under-counts the turns that actually
        # ran before it -- the panel would then show the wrong turn number and the wrong
        # "earlier turns not shown" count. So a composition advances it (below) AND a
        # ``turn/completed`` that closed a composition-less turn advances it once, so the
        # ordinal tracks the durable turn sequence, not the subset that composed. It is a
        # bare int that grows without bound over a session's life, the one running
        # counter a long session needs, and costs 8 bytes.
        "context_turns_seq": 0,
        # Every ``(unit, turn)`` the ordinal counter has already ACCOUNTED FOR, in the
        # order the counter gave them out. A turn is counted once -- the first event that
        # reaches it (a composition, the closer of a turn that composed nothing, or a
        # refusal) advances the counter and appends its key here; every later event naming
        # the same pair reads its ordinal back rather than advancing again, so a turn that
        # composes more than once, is retried, or is both composed and closed shares one
        # ordinal across its rows.
        #
        # Every seen pair rather than the single most-recent one because a turn number is
        # not monotonic within a unit: the panel recomputes it from the live row count
        # (``chat_runner``), and an edit-resend (``chat_regenerate``) or a rewind
        # (``chat_rewind``) truncates that list in place in the same crew-log session, so
        # a turn already counted can RECUR at a lower number later. A last-only check would
        # see the recurring turn as new and advance the counter again -- mislabelling the
        # rewound turn and inflating the dropped-turn count, with the wrong value persisted
        # in savepoint state and no refold that corrects it.
        #
        # ONE string, ``",{unit}:{turn},{unit}:{turn},"``, rather than a key -> ordinal
        # map: each new key takes the next ordinal, so a key's ordinal is its POSITION
        # (``context_turns_seq`` minus the keys after it) and needs no stored value. That
        # is what lets the memo cover the whole rewindable range
        # (``CONTEXT_ORDINAL_MEMO_LIMIT``) inside the slot-fold cell budget, and a str is
        # immutable, so a snapshot can share it without a copy. ``unit`` is the fold's own
        # integer counter and ``turn`` an int, so neither can contain a separator.
        "context_turn_keys": ",",
        # The newest NON-ZERO window ``request/configured`` stated. That entry is
        # written only when the configuration CHANGED, so the newest one still
        # describes every turn since, and a zero means the provider reported no
        # window rather than a window of nothing. This is what stamps a context ROW,
        # whose question is "what window was this prompt composed against".
        "context_window": 0,
        # No session-wide occupancy maximum is kept here. Occupancy is stamped onto the
        # ROWS instead (see the ``turn/completed`` branch in ``_usage_step``), because a
        # reader bounded to a time window has to be able to exclude a reading from
        # outside it -- and a scalar maximum cannot be narrowed to a window after the
        # fact. The reading and the window it was taken against stay together on the
        # row, so any window's peak is derivable from the rows inside it.
        # The model the newest ``request/configured`` named, stamped onto each context
        # row as it is appended. Truncated like every other retained label.
        "context_model": "",
        "compactions": 0,
        "freed_pct": 0.0,
        "steps": 0,
        "step_ms": 0,
        # The engine-measured half of the context rows (``context/measured``) and the
        # running sum it subtracts Crew's share with; see ``projection_usage_measured``.
        **measured_start(),
    }


def _credit_charge(value: Any) -> float | None:
    """*value* as a credit charge this module can arithmetic on, else ``None``.

    THE module's one screen for a charge coming off the wire, shared by every fold that
    reads a ``credits`` field -- ``usage`` through :func:`_bill_credits`, and ``subagents``
    per row. Spelled once because it is one question, and two spellings of it drift: the
    second would be the one that forgets a case.

    Two shapes get through a naive check. ``bool`` is an ``int`` in Python, so ``True``
    would bill as one credit. And a JSON integer is UNBOUNDED while a ``float`` is not, so
    ``float(10 ** 400)`` raises ``OverflowError`` rather than returning a wrong number --
    and nothing between here and :func:`fold_session` catches it, so an escaping raise
    would cost the whole projection over one line that stays on disk for good. A line this
    module cannot interpret costs that line and nothing else.

    Deliberately does NOT screen the VALUE for sign, NaN or infinity. That is the caller's
    business and belongs on the RESULT: a charge has many unusable shapes and an
    accumulated total has one invariant (stay finite, never go down), so checking the total
    needs one clause where checking the charge needs a clause per shape.
    """
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (OverflowError, ValueError):
        return None


def _bill_credits(state: dict[str, Any], entry: Entry) -> float | None:
    """Add this entry's charge to the session total and to its own source bucket.

    One function for all three spenders, so the total and the split cannot drift
    apart: a bucket that is incremented somewhere the total is not would make the
    two disagree, and a reader has no way to tell which half is wrong.

    Returns the charge, so ``turn/completed`` can also attribute it per model, and
    ``None`` when the entry carried no ``credits`` at all. Absent credits are NOT
    zero: a provider that does not bill in them writes no key, and folding that in
    as 0.0 would state a measurement nobody made. ``reported`` beside each bucket is
    what tells a reader how many charges the bucket's total covers.
    """
    source = _CREDIT_SOURCE_OF.get(entry.type)
    if source is None:
        return None
    billed = _credit_charge(entry.data.get("credits"))
    if billed is None:
        return None
    bucket = state["credits_by_source"][source]
    total = state["credits"] + billed
    grown = bucket["credits"] + billed
    # ONE invariant, and it is about the RESULT rather than the charge: a spend
    # total stays finite, and it never goes down.
    #
    # Checking the charge instead needs a new clause per shape, and the shapes
    # outnumber the clauses -- NaN, each infinity, a negative, and a pair of finite
    # values that overflow on the way up are four different inputs with one
    # consequence. That consequence is what is worth stating, because it is also
    # what cannot be undone: ``round`` keeps a non-finite total non-finite, the
    # savepoint persists it, and a cold refold reads the same entry again, so the
    # fold serves a broken total for the life of the unit.
    #
    # Zero passes deliberately. It does not move the total, and a provider reporting
    # 0.0 measured zero -- a different fact from a closer that reported nothing,
    # which is what ``reported`` beside each bucket exists to tell apart.
    #
    # ``by_model`` needs no check of its own: every billed charge is non-negative, so
    # a model's row is a sub-sum of ``state["credits"]`` and cannot exceed a total
    # this guard has already proved finite.
    if not math.isfinite(total) or not math.isfinite(grown):
        return None
    if total < state["credits"] or grown < bucket["credits"]:
        return None
    state["credits"] = total
    bucket["credits"] = grown
    bucket["reported"] += 1
    return billed


def _count_turn_ordinal(state: dict[str, Any], unit_no: int, turn_no: int) -> int:
    """Advance the session-global ordinal counter ONCE per distinct ``(unit, turn)``.

    *unit_no* is ``context_key_unit``, the crew-log unit, so the key is stable across a
    retry, a rewind, a regenerate and a re-attachment within one unit; every caller
    passes that and nothing else.

    The ordinal is a context row's position in the session's turn history -- what the
    panel renders as the turn's number and what it counts the dropped-off turns from.
    Every durable turn boundary passes through here: a composition, the closer of a
    turn that composed nothing, and the refusal of a turn a dispatch gate never let run.
    Each is ONE turn, so each consumes AT MOST one ordinal.

    ``context_turn_keys`` lists every ``(unit, turn)`` already accounted for, in the
    order the counter numbered them. If this call names a listed pair, its ordinal is
    read back from its position and the counter is left where it is -- so a turn that
    composes more than once, is retried, or is both composed and closed takes a single
    ordinal shared across its rows. A pair not yet seen advances the counter and is
    appended, which is what makes position and ordinal agree.

    Every pair rather than the single most-recent one because a turn number is NOT monotonic
    within a unit: an edit-resend or a rewind truncates the live row list the panel
    numbers from, so a turn already counted can recur at a lower number later. Reusing
    its original ordinal keeps the rewound turn's label stable; a last-only check would
    read the recurrence as new and advance the counter again, mislabelling it and
    inflating the dropped-turn count with no refold that corrects it.

    The list is BOUNDED at ``CONTEXT_ORDINAL_MEMO_LIMIT`` keys, the rewindable range, and
    evicts its oldest key. The counter is a bare int that keeps advancing, so evicting a
    key never disturbs a later turn's ordinal, and position still reads true because the
    keys after a match are counted from the END.
    """
    keys = state["context_turn_keys"]
    needle = f",{unit_no}:{turn_no},"
    found = keys.find(needle)
    if found >= 0:
        # Each key after this one ends in exactly one comma, and the newest key holds
        # the counter's current value.
        return int(state["context_turns_seq"] - keys.count(",", found + len(needle)))
    state["context_turns_seq"] += 1
    keys = keys + needle[1:]
    if state["context_turns_seq"] > CONTEXT_ORDINAL_MEMO_LIMIT:
        # One key in, one key out: every advance appends exactly one key, so past the
        # bound the list is full and drops its oldest.
        keys = "," + keys[keys.index(",", 1) + 1 :]
    state["context_turn_keys"] = keys
    return int(state["context_turns_seq"])


def _usage_step(state: dict[str, Any], entry: Entry) -> None:
    data = entry.data
    billed = _bill_credits(state, entry)
    if entry.type == "session/opened":
        # A new unit begins, and the rows appended from here belong to it. The
        # occupancy stamp is scoped to this counter because ordinals restart per unit.
        #
        # An opener is also written per RE-ATTACHMENT, so this can advance inside one
        # unit. Deliberately not special-cased: it splits a unit into two scopes, and
        # the effect is conservative -- a completion after a re-attachment leaves rows
        # composed before it unstamped, so occupancy reads absent. Under-reporting a
        # reading is the safe direction; carrying an earlier run's reading is not.
        state["context_unit"] += 1
        if data.get("resumed") is not True:
            state["context_key_unit"] = int(state.get("context_key_unit", 0)) + 1
            # A new unit is a new engine session, whose context starts empty. A
            # re-attachment picks the same engine session up again and keeps it.
            note_unit_start(state)
        # The model/window stamps are scoped to the unit that was told them, so a new
        # unit must start without them. The successor restamps from its own
        # request/configured; until it does, a row reads absent rather than inheriting
        # the predecessor's values. request/configured only accepts a NON-ZERO window
        # and a NON-EMPTY model (an auto/backend-default session reports model="" and a
        # provider that has not reported a window writes 0), so without this reset such
        # a successor would silently keep the previous unit's stamp -- carrying one
        # unit's reading onto another's rows, with no self-correcting re-fold.
        state["context_window"] = 0
        state["context_model"] = ""
    elif entry.type == "turn/completed":
        state["turns_completed"] += 1
        model = _as_str(data.get("model"))
        by_model = state["by_model"]
        per_model = None
        if _keyable(model):
            per_model = by_model.get(model)
            if per_model is None and len(by_model) < MODEL_LIMIT:
                per_model = {"turns": 0, "credits": 0.0, "credits_turns": 0, "tokens": 0}
                by_model[model] = per_model
        if per_model is None:
            # Past the model budget, or too long to key safely, the whole-session
            # totals below still count this turn exactly; only the per-model
            # detail is dropped. ``models_omitted`` counts the DISTINCT models
            # left out -- once each, not once per turn they ran -- which keeps the
            # retained ``by_model`` bounded over a long session without inventing
            # models that do not exist.
            _note_omitted(
                state,
                model,
                seen_key="omitted_models",
                count_key="models_omitted",
                saturated_key="models_omitted_saturated",
                budget=MODEL_LIMIT,
            )
        if per_model is not None:
            per_model["turns"] += 1
        # The session total and the turn bucket were already billed above. Only the
        # per-model row is left, and it stays turn-scoped: a child's closer names no
        # model, so attributing its spend to the parent turn's model would charge one
        # model for work another did.
        if billed is not None and per_model is not None:
            per_model["credits"] += billed
            per_model["credits_turns"] += 1
        tokens = data.get("tokens")
        if isinstance(tokens, dict):
            # A block counts as a REPORT only when some dimension is above zero. Logs
            # on disk carry four-zero blocks on closers whose provider sent no
            # counts; a completed turn cannot have cost zero tokens, so a block of
            # zeros is an absence written as a measurement, and counting it would
            # tell a reader beside a real bill that the turn measured 0. The sums
            # below are unaffected either way -- adding zeros moves nothing.
            reported = False
            for dimension in TOKEN_DIMENSIONS:
                measured = _as_int(tokens.get(dimension))
                state["tokens"][dimension] += measured
                if per_model is not None:
                    per_model["tokens"] += measured
                if measured > 0:
                    reported = True
            if reported:
                state["tokens_turns"] += 1
        # The fullest this window got, as the PROVIDER measured it, taken as one pair
        # so the reading and its window always come from the same turn.
        #
        # Deliberately NOT derived from ``tokens.input`` above: that total is summed
        # over every model call the turn made, so on a tool-using turn it exceeds the
        # window it would be divided by, and the ratio a reader computes from it is
        # wrong in the direction that looks alarming. Billing and occupancy are two
        # quantities, and only the second answers "how full did this get".
        #
        # A closer SEALS the rows it closes, whether or not it carried a reading. The
        # window merges every UNIT of a slot and turn ordinals restart per unit, and a
        # regenerate or rewind reruns an ordinal as a fresh ATTEMPT -- so a matching
        # turn number is NOT on its own proof a row belongs to the turn now closing.
        # Two things could otherwise be conflated: a later unit's turn 1 walking back
        # into a previous unit's turn 1, and this turn's attempt 2 walking back into
        # its own attempt 1 (whose rows are the SAME ordinal). The seal is what tells
        # them apart: each attempt's closer marks its own tail run closed, and the next
        # closer stops at the first already-closed row. Sealing happens even for a
        # closer with no reading, because otherwise an attempt that reported no
        # occupancy would leave its rows unsealed and the next attempt's reading would
        # stamp them. ``_closed`` is private bookkeeping the panel reader never sees;
        # the reading itself is ``used`` / ``used_window``, present only when measured.
        occupancy = data.get("context")
        used: int | None = None
        window = 0
        if isinstance(occupancy, dict):
            used = _as_int(occupancy.get("used"))
            window = _as_int(occupancy.get("window"))
        turn_no = _as_int(data.get("turn"))
        unit_no = state["context_unit"]
        rows = state["context_turns"]
        for index in range(len(rows) - 1, -1, -1):
            # The UNIT, beside the ordinal: ordinals restart in each unit of a slot, so
            # an earlier unit's turn N carries this turn's number without being this
            # turn. The seal below cannot catch that when the earlier turn was never
            # CLOSED -- a unit cut off mid-turn by a gateway restart leaves its rows
            # unsealed, and nothing else here tells them from an unstamped attempt.
            if rows[index]["turn"] != turn_no or rows[index]["unit"] != unit_no:
                break
            # The boundary between this closer's rows and an earlier attempt's (or an
            # earlier unit's same-ordinal turn's): that run was sealed by its own
            # closer, so its reading -- or its deliberate absence of one -- must stand.
            if rows[index].get("_closed"):
                break
            # REPLACED, never edited in place. ``_usage_copy`` shares these row dicts
            # between a snapshot and the state that keeps growing -- that sharing is
            # what makes the copy O(window) instead of O(window x sources) -- so writing
            # into a row would reach a projection already handed to a reader. Assigning
            # an element touches only this state's own freshly-copied list.
            sealed = {**rows[index], "_closed": True}
            if used is not None:
                sealed["used"] = used
                # Travels WITH the reading, including as 0: a turn that reported a used
                # count but no window is a reading whose window is unknown, and
                # borrowing another turn's size would pair the number with something it
                # was never measured against.
                sealed["used_window"] = window
            rows[index] = sealed
        duration = data.get("duration_ms")
        if isinstance(duration, int) and not isinstance(duration, bool):
            state["duration_ms"] += duration
            state["duration_turns"] += 1
        # A turn that closed having composed nothing still ran, so it occupies a
        # position in the turn history the ordinal tracks: advance the counter once for
        # it so the NEXT composition's ordinal counts it among the turns before it,
        # rather than the counter only ever moving on the turns that happened to compose
        # (which would make the panel show the wrong turn number). This turn is not yet
        # counted when the mark names some OTHER turn -- an earlier one, or none at all.
        #
        # ONCE, however many attempts it takes: the mark is stamped here as well as at a
        # composition, so a retried composition-less turn -- closed once per attempt,
        # which is why the seal above is per-attempt -- consumes one ordinal rather than
        # one per attempt. Without the stamp the second attempt reads the same unstamped
        # state as the first and advances again, and every later row carries a turn
        # number too high.
        _count_turn_ordinal(state, state["context_key_unit"], turn_no)
        note_closed(state, state["context_key_unit"], turn_no, _as_str(data.get("stop_reason")))
    elif entry.type == "turn/refused":
        # A refused turn never reached the model, so it carries no cost and composes
        # nothing -- but it still OCCUPIED a position in the turn history (a dispatch
        # gate declining a turn is ordinary operation), so it consumes an ordinal just
        # as a composition-less close does, and through the same helper so the rule is
        # one rule. ``turn/refused`` is a turn's only durable entry when it fires: no
        # ``turn/completed`` follows, so without this the counter never accounts for the
        # turn and every later composition in the unit reads an ordinal one low.
        _count_turn_ordinal(state, state["context_key_unit"], _as_int(data.get("turn")))
    elif entry.type == "context/composed":
        state["context_tokens"] += _as_int(data.get("tokens"))
        state["context_chars"] += _as_int(data.get("chars"))
        if data.get("tokens_estimated") is True:
            state["context_estimated"] += 1
        # Label to CHARACTERS for this one turn, which is the shape its reader asks
        # for. Deliberately NOT the entry's own ``[{kind, chars, tokens}]`` list:
        # per-turn per-source tokens have no reader, and they are not an independent
        # measurement -- the writer derives every one of them from that source's
        # ``chars`` at one fixed ratio, and the cumulative ``context_by_source``
        # below keeps the summed version for a reader who wants them.
        #
        # Measured at the worst case rather than argued, and it is what decides the
        # shape: a full window is 808,912 bytes this way against 3,319,357 as the
        # entry's list of three-key dicts. This fold is read per SLOT, so the cell
        # lands in the slot-fold cache whose largest budgeted member is 995,342 --
        # so the list shape does not merely cost more, it does not fit.
        row_sources: dict[str, int] = {}
        sources = data.get("sources")
        if isinstance(sources, list):
            for source in sources:
                if not isinstance(source, dict):
                    continue
                label = source.get("kind")
                if not isinstance(label, str) or not label:
                    continue
                per_source = state["context_by_source"].setdefault(
                    label, {"blocks": 0, "tokens": 0, "chars": 0}
                )
                per_source["blocks"] += 1
                per_source["tokens"] += _as_int(source.get("tokens"))
                per_source["chars"] += _as_int(source.get("chars"))
                state["context_blocks"] += 1
                # The retained ROW is bounded where the cumulative tally above is
                # not: that one keys by label and so holds one entry per distinct
                # label whatever the turn count, while a row is kept per turn and
                # would multiply any per-row growth by the window. A label at
                # ``TEXT_LIMIT`` is not keyed, the same reason ``_keyable`` gives
                # everywhere else: it cannot be told apart from one that was cut, so
                # two sources would pool into one number.
                # A source left out is COUNTED (``sources_omitted`` on the rendered
                # ``context``), so a reader can tell a truncated breakdown from a
                # complete one. A label the row already holds still adds to it at the
                # cap: only a NEW label is refused, so a kept label is never cut short.
                keyed = _as_str(label)
                if _keyable(keyed) and (
                    keyed in row_sources or len(row_sources) < CONTEXT_SOURCES_PER_TURN_LIMIT
                ):
                    row_sources[keyed] = row_sources.get(keyed, 0) + _as_int(source.get("chars"))
                else:
                    state["context_sources_omitted"] += 1
        row: dict[str, Any] = {
            "turn": _as_int(data.get("turn")),
            # The entry's own writer-assigned stamp (epoch ms). The entry data
            # carries no time of its own, and this is the one the log recorded.
            "ts": entry.time,
            "chars": _as_int(data.get("chars")),
            # NO per-row ``tokens`` / ``tokens_estimated``: the only reader of these rows
            # shapes them to the ``ContextTrace`` interface, which asks for neither, and a
            # row's token count is not an independent measurement -- the writer derives it
            # from the same ``chars`` at a fixed ratio. The session-wide counts those two
            # feed (``tokens``, ``estimated_turns``) are accumulated above and unaffected.
            "sources": row_sources,
            # Which unit of the slot this composition came from. Internal to the fold
            # and stripped at render: it keeps one unit's reading off another unit's
            # same-ordinal rows, and no reader asks for it.
            "unit": state["context_unit"],
            # Which population this row belongs to, as the COMPOSER stated it, or ""
            # when it did not. Never derived here: a reader separating the one-off
            # session-start injection from the per-turn ones needs the composer's own
            # answer, and the guess available to a fold -- the first row of a unit --
            # is wrong for the rebuild a mid-session replay triggers.
            "phase": _as_str(data.get("phase")),
            # Stamped from the newest configuration rather than looked up later, so a
            # row records the model and window its prompt was actually measured
            # against even after either moves.
            "model": state["context_model"],
            "window": state["context_window"],
        }
        # The row's TRUE position in the whole session history, assigned before any
        # truncation and never reused. Dropped rows take their ordinal with them, so the
        # first retained row's ordinal is exactly how many rows came before it -- what a
        # reader needs to show a turn's real number without counting an array that holds
        # only the survivors.
        #
        # ONCE per distinct (unit, turn), through the shared helper: a single turn can
        # compose more than once (a rebuild re-emits the session-start injection, a
        # prompt recomposed within one turn), and every such row is the SAME turn, so it
        # carries that turn's one ordinal. The helper reuses the current counter value
        # when this turn is already the one it last accounted for, so a second
        # composition does not advance past the real turn sequence.
        row["ordinal"] = _count_turn_ordinal(state, state["context_key_unit"], row["turn"])
        # Whether this row is the slot's FIRST session-start composition: the session's
        # real start, as opposed to a later rebuild. Not derivable from the ordinal --
        # a refused or composition-less turn 1 consumes ordinal 1, so the real start
        # can carry 2 -- nor from the earliest retained row, which a trimmed window or
        # a day view can make a rebuild. The fold sees every entry, so it marks it once.
        row["first_start"] = row["phase"] == PHASE_SESSION_START and not state["context_started"]
        if row["first_start"]:
            state["context_started"] = True
        note_composed(state, state["context_key_unit"], row["turn"], row["chars"])
        turns_window = state["context_turns"]
        turns_window.append(row)
        if len(turns_window) > CONTEXT_TURNS_LIMIT:
            over = len(turns_window) - CONTEXT_TURNS_LIMIT
            # Drop the OLDEST per-turn rows first, and keep the session-start rows they
            # sit among. A session-start composition is the one-off injection a unit
            # opens with -- many times the size of a per-turn one and the anchor the
            # Context panel draws its history against. Trimming it like an ordinary row
            # once a session passes CONTEXT_TURNS_LIMIT turns would silently corrupt the
            # panel's totals (the reader sums the RETAINED rows) and lose the largest
            # bar. So the trim skips session-start rows: only per-turn rows count toward
            # ``over`` and are removed, oldest first.
            #
            # Session-start rows are one per unit start/rebuild, so the count preserved
            # is bounded by the units folded into a slot, not by how long the session
            # ran. The final clamp below is the backstop for the pathological case where
            # session-start rows alone would exceed the cap: it drops oldest-overall so
            # the byte budget this limit exists to hold is never breached.
            removed = 0
            index = 0
            while removed < over and index < len(turns_window):
                if turns_window[index].get("phase") == PHASE_SESSION_START:
                    index += 1
                    continue
                del turns_window[index]
                removed += 1
            if len(turns_window) > CONTEXT_TURNS_LIMIT:
                del turns_window[: len(turns_window) - CONTEXT_TURNS_LIMIT]
    elif entry.type == "context/measured":
        measured_step(state, entry)
    elif entry.type == "request/configured":
        # Newest NON-ZERO wins. This entry is written only when the configuration
        # changed, and a provider that reports no window writes 0 -- taking that as
        # the current window would erase a size the session was told earlier and is
        # still running under.
        window = _as_int(data.get("context_window"))
        if window > 0:
            state["context_window"] = window
        configured_model = _as_str(data.get("model"))
        if configured_model:
            state["context_model"] = configured_model
    elif entry.type == "compaction/applied":
        state["compactions"] += 1
        freed = data.get("freed_pct")
        if isinstance(freed, (int, float)) and not isinstance(freed, bool):
            state["freed_pct"] += float(freed)
    elif entry.type == "step/completed":
        state["steps"] += 1
        state["step_ms"] += _as_int(data.get("ms"))


def _usage_render(state: dict[str, Any]) -> dict[str, Any]:
    tokens = dict(state["tokens"])
    return {
        "turns": {
            "completed": state["turns_completed"],
            # Turn-scoped on purpose, and it stays that way now the total covers
            # three sources: this number answers how many of the session's TURNS
            # reported a cost, which a whole-session count could not.
            "credits_reported": state["credits_by_source"]["turn"]["reported"],
            "tokens_reported": state["tokens_turns"],
            "duration_reported": state["duration_turns"],
        },
        "credits": round(state["credits"], 6),
        # The total above, split by who spent it. Rounded per bucket so the parts
        # are each readable; a reader comparing them against the total is comparing
        # two roundings of the same sum, not two different sums.
        "credits_by_source": {
            source: {
                "credits": round(row["credits"], 6),
                "reported": row["reported"],
            }
            for source, row in state["credits_by_source"].items()
        },
        "tokens": {**tokens, "total": sum(tokens.values())},
        "duration_ms": state["duration_ms"],
        "by_model": {
            name: {
                "turns": row["turns"],
                "credits": round(row["credits"], 6),
                "credits_reported": row["credits_turns"],
                "tokens": row["tokens"],
            }
            for name, row in sorted(state["by_model"].items())
        },
        "models_omitted": state["models_omitted"],
        # True once the dedup budget is spent: ``models_omitted`` is then a floor,
        # not a total, the same posture ``names_omitted`` takes.
        "models_omitted_saturated": state["models_omitted_saturated"],
        "context": {
            "tokens": state["context_tokens"],
            "chars": state["context_chars"],
            "blocks": state["context_blocks"],
            "estimated_turns": state["context_estimated"],
            "by_source": {
                name: dict(row) for name, row in sorted(state["context_by_source"].items())
            },
            # How many sources the retained ``turns`` rows leave out, across the session.
            # 0 means every row's ``sources`` is its whole breakdown.
            "sources_omitted": state["context_sources_omitted"],
            # The per-turn window, OLDEST FIRST. No count of what fell off the front
            # rides beside it: each row carries its own ``ordinal``, assigned before
            # any truncation, so the first row's number is what tells a reader this is
            # a window rather than the whole history -- and it says how much precedes
            # it, which a whole-session total could not for a narrower window.
            # ``_closed`` is the fold's private seal (which closer already stamped this
            # row) and ``unit`` (which run of the slot appended it) are both dropped
            # here: together they keep one run's reading off another's same-ordinal
            # rows, and no reader asks for either.
            "turns": [
                {
                    **{k: v for k, v in row.items() if k not in ("_closed", "unit", "measured")},
                    "sources": dict(row["sources"]),
                    # The engine's own measurement of the turn, decoded from the row's
                    # compact string; ``None`` when the turn has none.
                    "measured": decode_measured(row.get("measured")),
                }
                for row in state["context_turns"]
            ],
            # Engine categories this build does not know, by name, so a category a
            # newer engine adds is shown rather than lost.
            "measured_unknown": {
                name: dict(row) for name, row in sorted(state["context_measured_unknown"].items())
            },
            # NO session-wide occupancy pair here. The reading and its window ride on
            # each ROW (``used`` / ``used_window``), because the reader is bounded to a
            # TIME WINDOW and a maximum computed here could not be narrowed to it: the
            # fullest turn of a long session is frequently older than every row the
            # caller asked for. The reader takes the peak over the rows it keeps and
            # gets its window from the same row.
            #
            # ``window`` remains: it is the newest size ``request/configured`` stated,
            # which is what a reader with NO occupancy reading in its window can still
            # be told the session runs under. A reader with a reading uses that row's
            # own ``used_window`` instead.
            "window": state["context_window"],
        },
        "compactions": {
            "count": state["compactions"],
            "freed_pct": round(state["freed_pct"], 4),
        },
        "steps": {"completed": state["steps"], "ms": state["step_ms"]},
    }


#: Where a credit charge came from, which is the split ``usage`` keeps beside its
#: total. Fixed rather than discovered: these are the three writers that carry a
#: ``credits`` field, so the buckets are a closed set and a reader is never shown a
#: partial split. A fourth spender would add a bucket here and move ``usage``'s own
#: :attr:`_Fold.state_version`, which is the version its savepoints carry.
CREDIT_SOURCES: Final[tuple[str, ...]] = ("turn", "subagent", "background")

#: Which bucket each billing entry type lands in.
_CREDIT_SOURCE_OF: Final[dict[str, str]] = {
    "turn/completed": "turn",
    "subagent/completed": "subagent",
    "subagent/failed": "subagent",
    "background/completed": "background",
}
