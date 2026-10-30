"""The context part of the ``usage`` fold: one fixed-size state, ``apply(state, entry)``.

Built the way DeepSeek Harness builds its token-meter projections: the state is a fixed
handful of numbers that never grows with the session, an entry that does not concern it
returns the SAME state object, every change returns a NEW one (nothing in it is edited
in place, so a snapshot can share it), and nothing is ever read back from the log.

What it holds, and nothing else:

1. ``ours`` -- Kiro Crew's own share of the agent loop's context, as five running sums
   (message, memory, rules, skills, other) since that context last restarted. Each
   ``context/composed`` adds to them; a new unit or an engine compaction restarts them
   (a compaction restarts them from the compacting turn's own prompt, because that
   prompt is still in the context the engine rebuilt). So does a ``compaction/applied``
   the gateway ran in place: it keeps only what was composed after the compaction
   (see :func:`_compaction`).
2. ``engine`` -- the newest ``context/measured`` breakdown and time to first output,
   replaced whole each time (last-wins), with the turn it belongs to.
3. ``used`` / ``used_window`` -- the newest occupancy reading ``turn/completed``
   carried, last-wins, which sizes the agent loop's part when the engine measured
   nothing (an estimate, see :data:`CHARS_PER_TOKEN`).
4. ``trend`` -- a fixed ring of the newest :data:`TREND_TURNS` per-turn rows. Each
   row is the whole turn, so the panel can show any of them as it shows the newest:
   :data:`ROW_FIELDS` integers (the five sums at that turn, the agent loop's part,
   the turn's own ordinal, the unit and turn number it was composed under so a turn
   sent again finds its own row, the turn's own occupancy reading, Crew's unscaled
   total, how many blocks did not fit, and the row's version), then the turn's Kiro Crew blocks by name
   (at most :data:`BLOCKS_PER_TURN`, a name under ``TEXT_LIMIT``), then a copy of
   its engine measurement (``None`` when it sent none). One row per turn, oldest dropped; the ring is full-size from the
   start, so the state's shape is the same on turn 1 and on turn 10,000. ``start`` is
   the newest session-start turn's row, kept apart because the panel lists it above
   the chart.

``turns`` is the one turn counter; each row carries the ordinal it was closed at, so
the panel labels every row and counts the turns before the ring exactly, however many
session starts fell between them. Characters throughout; ``used`` alone is in tokens.
"""

from __future__ import annotations

from typing import Any, Final

from kiro_crew.context_blocks import PHASE_PER_TURN, PHASE_SESSION_START
from kiro_crew.crew_log import emit_turn as _emit_turn
from kiro_crew.crew_log.projection_values import _as_int, _as_str, _keyable
from kiro_crew.crew_log.schema import Entry
from kiro_crew.engine_context import (
    BREAKDOWN_SCHEMA,
    HISTORY_PARTS,
    KNOWN_CATEGORIES,
    LIST_CATEGORIES,
    MAX_COUNT,
    MEDIA_PARTS,
    SUB_PARTS,
    count,
)

#: Characters per token for the agent loop's part when only a token reading exists.
#: The SAME ratio the composer uses to turn a block's characters into its token count
#: (``emit_turn._EST_CHARS_PER_TOKEN``), so the two estimates agree.
CHARS_PER_TOKEN: Final[float] = _emit_turn._EST_CHARS_PER_TOKEN
# Not kept as a name of this module: the projection facade serves every name here.
del _emit_turn

#: The Context panel's own categories of a composed block, mirroring ``CATEGORY_OF``
#: in ``website/src/pages/ContextBreakdownPanel.tsx``. A label absent here is ``other``.
CREW_CATEGORIES: Final[tuple[str, ...]] = ("message", "memory", "rules", "skills", "other")
CREW_CATEGORY_OF: Final[dict[str, str]] = {
    "your_message": "message",
    "memory": "memory",
    "semantic_memory": "memory",
    "episodic_memory": "memory",
    "task_facts": "memory",
    "lessons": "rules",
    "critical_rules": "rules",
    "agent_instructions": "rules",
    "response_preferences": "rules",
    "skill_index": "skills",
    "skill_hint": "skills",
    "loaded_skill": "skills",
}
_CAT_INDEX: Final[dict[str, int]] = {name: i for i, name in enumerate(CREW_CATEGORIES)}

#: Rows the trend ring keeps; equal to the panel's ``MAX_CHART_TURNS``.
TREND_TURNS: Final[int] = 30
#: Integers at the head of one trend row: the five sums, the agent loop's part, the
#: turn ordinal, the unit and turn number the turn was composed under, the turn's own
#: occupancy reading and window, Crew's unscaled total, the blocks left out, and the
#: row's version (the write counter when it was written).
ROW_FIELDS: Final[int] = len(CREW_CATEGORIES) + 9
_LOOP: Final[int] = len(CREW_CATEGORIES)
_ORD: Final[int] = _LOOP + 1
_UNIT: Final[int] = _ORD + 1
_TURN: Final[int] = _UNIT + 1
_USED: Final[int] = _TURN + 1
_WINDOW: Final[int] = _USED + 1
_RAW: Final[int] = _WINDOW + 1
_OMITTED: Final[int] = _RAW + 1
_VERSION: Final[int] = _OMITTED + 1
#: After the integers: the turn's blocks as ``[name, chars]`` pairs, then its engine
#: measurement by ``_SLOTS`` (or ``None``).
_BLOCKS: Final[int] = ROW_FIELDS
_ROW_ENGINE: Final[int] = ROW_FIELDS + 1

#: Turn numbers of the current unit the ordinal memo remembers, so a rewound,
#: regenerated or edit-resent turn keeps its ordinal however far back it is. Sized to
#: the REWINDABLE range: ``chat_rewind`` can re-run any user message still in the
#: slot's live list, which holds up to ``dashboard.state._MAX_SLOT_MESSAGES`` (10,000)
#: rows; ``test_usage_the_ordinal_memo_covers_the_rewindable_range`` pins the two equal.
#: Held as ONE string of turn numbers (``turn_memo``), oldest first, so the bound costs
#: about 90 KB at its worst; the oldest number evicts first.
CONTEXT_ORDINAL_MEMO_LIMIT: Final[int] = 10_000

#: Kiro Crew blocks one row keeps by name: the composer's own most per turn. A name
#: past it, or one at ``TEXT_LIMIT`` (it cannot be told from a cut one), is counted in
#: the row's omitted total, and its characters stay in the five sums.
BLOCKS_PER_TURN: Final[int] = 64

#: Occupancy readings kept for the fullest-turn peak, newest last, each
#: ``[time_ms, used, window]``: the same reach the per-turn rows had, 200 turns, so a
#: reader bounded to a span of days takes its peak from the readings inside it.
PEAK_READINGS: Final[int] = 200

_ZERO5: Final[tuple[int, ...]] = (0,) * len(CREW_CATEGORIES)


def _zero_row() -> list[Any]:
    """An empty row: its ordinal 0 means none."""
    return [*(0,) * ROW_FIELDS, [], None]


_HISTORY_KEYS: Final[tuple[str, ...]] = tuple(field for _wire, field in HISTORY_PARTS)
_TTFT_KEYS: Final[tuple[str, ...]] = ("total_ms", "setup_ms", "model_ms")

_PART_KEYS: Final[tuple[str, ...]] = tuple(f"{cat}.{field}" for cat, _wire, field in SUB_PARTS)
_MEDIA_KEYS: Final[tuple[str, ...]] = tuple(field for _wire, field in MEDIA_PARTS)
_WINDOW_KEYS: Final[tuple[str, ...]] = ("size_tokens", "usage_milli")

#: The positions of one stored engine measurement. ``None`` is "not sent", never 0.
#: A fixed list whatever the engine sends, so the state never grows.
_SLOTS: Final[tuple[str, ...]] = (
    "model_calls",
    "total_chars",
    *(f"cat:{name}" for name in KNOWN_CATEGORIES),
    # The engine's share of each category, in tenths of a percent.
    *(f"pct:{name}" for name in KNOWN_CATEGORIES),
    *(f"cnt:{name}" for name in LIST_CATEGORIES),
    *(f"part:{name}" for name in _PART_KEYS),
    *(f"hist:{name}" for name in _HISTORY_KEYS),
    *(f"win:{name}" for name in _WINDOW_KEYS),
    *(f"media:{name}" for name in _MEDIA_KEYS),
    "unknown_chars",
    *(f"ttft:{name}" for name in _TTFT_KEYS),
    "flags",
)
MEASURED_FIELDS: Final[int] = len(_SLOTS)
_INDEX: Final[dict[str, int]] = {name: i for i, name in enumerate(_SLOTS)}

FLAG_RESET: Final[int] = 1
FLAG_SCHEMA_UNKNOWN: Final[int] = 2
FLAG_COMPACTED: Final[int] = 4
FLAG_BREAKDOWN: Final[int] = 8
FLAG_RERUN: Final[int] = 16

#: Stop reasons of a turn that ran to its end, so the engine kept its prompt.
_NORMAL_STOPS: Final[frozenset[str]] = frozenset(
    {"end_turn", "max_tokens", "max_turn_requests", "refusal"}
)

#: Every entry type the context part reads.
CONTEXT_TYPES: Final[frozenset[str]] = frozenset(
    {
        "session/opened",
        "context/composed",
        "context/measured",
        "turn/completed",
        "compaction/applied",
        "turn/refused",
        "request/configured",
    }
)


def context_start() -> dict[str, Any]:
    """The context state before any entry: every field present, the ring full of zeros."""
    return {
        # Crew-log units seen (a resume is the same unit and does not count).
        "unit": 0,
        "ours": list(_ZERO5),
        # ``ours`` as it stood when the newest turn that carried an occupancy reading
        # closed: where an in-place compaction's boundary is (see ``_compaction``).
        "ours_read": list(_ZERO5),
        # The newest composed turn: its unit, number, phase and its own five sums
        # across every composition of it (what a compaction restarts ``ours`` from).
        "turn_unit": 0,
        "turn_no": 0,
        "turn_phase": "",
        "turn_ours": list(_ZERO5),
        # The newest composed turn's blocks by name, and how many did not fit.
        "turn_blocks": [],
        "turn_omitted": 0,
        # The newest closed turn, and whether a turn was sent again since ``ours`` last
        # restarted (so the split may count an attempt the engine dropped).
        "closed_unit": 0,
        "closed_no": 0,
        "rerun": False,
        # Distinct composed turns: the one turn counter.
        "turns": 0,
        # The newest composed turn's ordinal: ``turns`` for a new turn, and the
        # ordinal it already has when it is sent again (0 when that is not known).
        "turn_ord": 0,
        # The current unit's turn numbers in ordinal order, ``,7,9,11,``: the last one
        # holds ordinal ``turns``, each earlier one a step less (see ``_memo_ordinal``).
        "turn_memo": ",",
        # The engine's ``compaction_summary`` as last measured, -1 before any.
        "summary": -1,
        # The newest measurement: its values by ``_SLOTS``, the turn it belongs to, and
        # the engine's own turn id (a second footer of one engine turn is ignored).
        "engine": [None] * MEASURED_FIELDS,
        "engine_unit": 0,
        "engine_no": 0,
        "engine_id": "",
        # The newest occupancy reading, in TOKENS; -1 before any.
        "used": -1,
        "used_window": 0,
        # The readings the peak is taken from (see ``PEAK_READINGS``), across units:
        # only a reading with a stated window, the ones that have a fullness ratio.
        "peaks": [],
        # The newest non-zero window ``request/configured`` stated in this unit: the
        # size a reader is told before any turn has reported a reading.
        "conf_window": 0,
        # The trend ring, oldest first, always TREND_TURNS rows; ``trend_rows`` says how
        # many are real. Each row ends with its own turn ordinal.
        "trend": [_zero_row() for _ in range(TREND_TURNS)],
        "trend_rows": 0,
        # Rows written so far: each new or replaced row takes the next number as its
        # version, so a turn sent again reads as a new version whatever it holds.
        "row_writes": 0,
        "trend_unit": 0,
        "trend_no": 0,
        # The newest session-start turn's row; its ordinal 0 means none.
        "start": _zero_row(),
    }


def crew_split(sources: Any) -> list[int]:
    """One composition's characters per Crew category, from its source list."""
    out = [0] * len(CREW_CATEGORIES)
    if isinstance(sources, list):
        for source in sources:
            if not isinstance(source, dict):
                continue
            kind = source.get("kind")
            cat = CREW_CATEGORY_OF.get(kind, "other") if isinstance(kind, str) else None
            if cat is not None:
                out[_CAT_INDEX[cat]] += max(0, _as_int(source.get("chars")))
    return [min(v, MAX_COUNT) for v in out]


def crew_blocks(sources: Any, blocks: Any, omitted: int) -> tuple[list[list[Any]], int]:
    """*blocks* (``[name, chars]`` pairs) with one composition's sources added by name.

    A name already held grows; a new one is kept while the row holds fewer than
    :data:`BLOCKS_PER_TURN`; anything else is counted in *omitted*. Returns new lists.
    """
    out = [[name, int(chars)] for name, chars in blocks]
    index = {name: i for i, (name, _chars) in enumerate(out)}
    if isinstance(sources, list):
        for source in sources:
            if not isinstance(source, dict) or not isinstance(source.get("kind"), str):
                continue
            name = _as_str(source.get("kind"))
            chars = max(0, _as_int(source.get("chars")))
            if name and _keyable(name) and name in index:
                out[index[name]][1] = min(out[index[name]][1] + chars, MAX_COUNT)
            elif name and _keyable(name) and len(out) < BLOCKS_PER_TURN:
                index[name] = len(out)
                out.append([name, min(chars, MAX_COUNT)])
            else:
                omitted = min(omitted + 1, MAX_COUNT)
    return out, omitted


def _add(a: Any, b: Any) -> list[int]:
    return [min(int(x) + int(y), MAX_COUNT) for x, y in zip(a, b)]


def _engine_value(engine: Any, name: str) -> int | None:
    if not engine:
        return None
    value = engine[_INDEX[name]]
    return int(value) if value is not None else None


def crew_share(ctx: dict[str, Any]) -> tuple[int, ...]:
    """``ours``, scaled down to the engine's own user text when it holds less.

    Only on the turn the engine measured: when Crew counts more than the engine still
    has (a rewind, a summary that absorbed a turn), every category shrinks by the same
    factor and the rounding remainder goes to the largest, so they add up to it.
    """
    ours = tuple(int(v) for v in ctx["ours"])
    user = _engine_value(ctx["engine"], "hist:user") if measured_now(ctx) else None
    total = sum(ours)
    if user is None or total <= user:
        return ours
    scaled = [v * user // total if total else 0 for v in ours]
    scaled[max(range(len(scaled)), key=lambda i: ours[i])] += user - sum(scaled)
    return tuple(scaled)


def measured_now(ctx: dict[str, Any]) -> bool:
    """Whether the stored breakdown belongs to the newest composed turn."""
    engine = ctx["engine"]
    return bool(
        engine
        and int(engine[_INDEX["flags"]] or 0) & FLAG_BREAKDOWN
        and ctx["engine_unit"] == ctx["turn_unit"]
        and ctx["engine_no"] == ctx["turn_no"]
    )


def loop_chars(ctx: dict[str, Any]) -> tuple[int, bool]:
    """The agent loop's part of the newest turn, and whether it is an estimate.

    Measured: the engine's total less Crew's share of it. Otherwise the newest
    occupancy reading at :data:`CHARS_PER_TOKEN`, less Crew's sums; 0 with no reading.
    """
    ours = sum(crew_share(ctx))
    if measured_now(ctx):
        total = _engine_value(ctx["engine"], "total_chars") or 0
        return max(0, total - ours), False
    used = int(ctx["used"])
    if used <= 0:
        return 0, True
    return max(0, int(used * CHARS_PER_TOKEN) - ours), True


def _trend_row(ctx: dict[str, Any], used: int, window: int) -> list[Any]:
    """The closing turn as one row: its numbers, its blocks, its own measurement."""
    # The rest of the context only where it was measured: the chart shows no
    # estimate, so an unmeasured turn's band holds Crew's blocks alone.
    loop, estimated = loop_chars(ctx)
    engine = ctx["engine"]
    own = (
        engine
        if ctx["engine_unit"] == ctx["turn_unit"]
        and ctx["engine_no"] == ctx["turn_no"]
        and any(v is not None for v in engine)
        else None
    )
    return [
        *crew_share(ctx),
        0 if estimated else min(loop, MAX_COUNT),
        int(ctx["turn_ord"]),
        int(ctx["turn_unit"]),
        int(ctx["turn_no"]),
        used,
        window,
        min(sum(int(v) for v in ctx["ours"]), MAX_COUNT),
        int(ctx["turn_omitted"]),
        int(ctx["row_writes"]) + 1,
        [list(pair) for pair in ctx["turn_blocks"]],
        list(own) if own is not None else None,
    ]


def _memo_ordinal(ctx: dict[str, Any], turn: int) -> int:
    """The ordinal turn number *turn* of the current unit already has; 0 if none.

    A turn number is the conversation's durable row count, which rises by two or more
    per turn, so an ordinal is never computed from turn numbers. It is read from the
    memo: the numbers are listed in ordinal order and the newest holds ``turns``, so
    a number's ordinal is ``turns`` less the numbers listed after it.
    """
    memo, needle = ctx["turn_memo"], f",{turn},"
    found = memo.find(needle)
    if found < 0:
        return 0
    return max(0, int(ctx["turns"]) - memo.count(",", found + len(needle)))


def _memo_added(memo: str, turn: int) -> str:
    """*memo* with *turn* as its newest number, its oldest dropped past the bound."""
    memo = f"{memo}{turn},"
    if memo.count(",") - 1 > CONTEXT_ORDINAL_MEMO_LIMIT:
        memo = "," + memo[memo.index(",", 1) + 1 :]
    return memo


def _memo_cut(memo: str, turn: int) -> str:
    """*memo* up to and including *turn*: a rewind drops the turns after it."""
    needle = f",{turn},"
    found = memo.find(needle)
    return memo if found < 0 else memo[: found + len(needle)]


def _truncated(ctx: dict[str, Any], ordinal: int) -> dict[str, Any]:
    """*ctx* with every row from *ordinal* on removed: a rewind drops the turns after it."""
    rows = int(ctx["trend_rows"])
    kept = [row for row in list(ctx["trend"])[TREND_TURNS - rows :] if int(row[_ORD]) < ordinal]
    out = dict(ctx)
    out["trend"] = [_zero_row() for _ in range(TREND_TURNS - len(kept))] + kept
    out["trend_rows"] = len(kept)
    if int(ctx["start"][_ORD]) >= ordinal:
        out["start"] = _zero_row()
    return out


def _composed(ctx: dict[str, Any], entry: Entry) -> dict[str, Any]:
    data = entry.data
    unit, turn = ctx["unit"], _as_int(data.get("turn"))
    split = crew_split(data.get("sources"))
    same = unit == ctx["turn_unit"] and turn == ctx["turn_no"]
    closed = unit == ctx["closed_unit"] and turn == ctx["closed_no"]
    out = dict(ctx)
    out["ours"] = _add(ctx["ours"], split)
    if same and not closed:
        # Another composition of the turn still open: one turn, its sums grow.
        out["turn_ours"] = _add(ctx["turn_ours"], split)
        out["turn_blocks"], out["turn_omitted"] = crew_blocks(
            data.get("sources"), ctx["turn_blocks"], int(ctx["turn_omitted"])
        )
    else:
        out["turn_ours"] = split
        out["turn_blocks"], out["turn_omitted"] = crew_blocks(data.get("sources"), [], 0)
        # The turn number already has an ordinal in this unit (a retry, a regenerate,
        # an edit-resend, a rewind however far back the memo reaches): the
        # conversation was cut back to it, so it keeps its ordinal, every row from
        # that ordinal on goes, and ``turns`` is then the real count.
        rewound = _memo_ordinal(ctx, turn) if unit == ctx["turn_unit"] else 0
        if rewound:
            out = _truncated(out, rewound)
            out["turn_memo"] = _memo_cut(ctx["turn_memo"], turn)
            out["turns"] = rewound
            out["turn_ord"] = rewound
            out["rerun"] = True
            out["closed_unit"], out["closed_no"] = 0, 0
            # A new attempt has not been measured yet: the previous attempt's
            # breakdown, time to first output and engine turn id describe a request
            # that is gone.
            out["engine"] = [None] * MEASURED_FIELDS
            out["engine_unit"], out["engine_no"], out["engine_id"] = 0, 0, ""
        else:
            out["turns"] = ctx["turns"] + 1
            out["turn_ord"] = out["turns"]
            out["turn_memo"] = _memo_added(ctx["turn_memo"], turn)
    out["turn_unit"], out["turn_no"] = unit, turn
    out["turn_phase"] = _as_str(data.get("phase"))
    return out


def _measured(ctx: dict[str, Any], entry: Entry) -> dict[str, Any]:
    data = entry.data
    unit, turn = ctx["unit"], _as_int(data.get("turn"))
    if (
        ctx["turns"] == 0
        or unit != ctx["turn_unit"]
        or turn != ctx["turn_no"]
        or (unit == ctx["closed_unit"] and turn == ctx["closed_no"])
    ):
        # Only the newest open turn is measured; anything else attaches nowhere.
        return ctx
    engine_id = _as_str(data.get("turn_id"))
    same_turn = ctx["engine_unit"] == unit and ctx["engine_no"] == turn and ctx["engine"]
    if engine_id and same_turn and engine_id == ctx["engine_id"]:
        return ctx
    previous = list(ctx["engine"]) if same_turn else None  # a copy: never edit ctx
    values: list[int | None] = [None] * MEASURED_FIELDS
    flags = 0
    out = dict(ctx)
    breakdown = data.get("breakdown")
    if isinstance(breakdown, dict):
        flags |= FLAG_BREAKDOWN
        if breakdown.get("schema") != BREAKDOWN_SCHEMA:
            flags |= FLAG_SCHEMA_UNKNOWN
        for field in ("model_calls", "total_chars"):
            values[_INDEX[field]] = count(breakdown.get(field))
        unknown = 0
        categories = breakdown.get("categories")
        if isinstance(categories, dict):
            for name, chars in categories.items():
                size = count(chars)
                if size is None or not isinstance(name, str):
                    continue
                slot = _INDEX.get(f"cat:{name}")
                if slot is not None:
                    values[slot] = size
                else:
                    # A category this build does not know: counted, not named, so the
                    # state stays the same size whatever a newer engine sends.
                    unknown = min(unknown + size, MAX_COUNT)
        values[_INDEX["unknown_chars"]] = unknown
        for prefix, field, keys in (
            ("pct", "percent", KNOWN_CATEGORIES),
            ("cnt", "counts", LIST_CATEGORIES),
            ("part", "parts", _PART_KEYS),
            ("win", "window", _WINDOW_KEYS),
            ("media", "media", _MEDIA_KEYS),
        ):
            # Only the names this build lists: a newer engine's extra keys are not
            # kept, so the stored measurement stays the same size.
            source = breakdown.get(field)
            if isinstance(source, dict):
                for name in keys:
                    values[_INDEX[f"{prefix}:{name}"]] = count(source.get(name))
        history = breakdown.get("history")
        summary: int | None = None
        if isinstance(history, dict):
            for name in _HISTORY_KEYS:
                values[_INDEX[f"hist:{name}"]] = count(history.get(name))
            summary = values[_INDEX["hist:compaction_summary"]]
        compacted = breakdown.get("compacted") is True
        if compacted:
            flags |= FLAG_COMPACTED
        seen = ctx["summary"]
        if compacted or (summary is not None and summary != seen and (seen >= 0 or summary > 0)):
            # The engine's context restarted from a summary: the user text it still
            # holds begins at this turn's prompt.
            out["ours"] = list(ctx["turn_ours"])
            out["ours_read"] = list(_ZERO5)
            out["rerun"] = False
            flags |= FLAG_RESET
        if summary is not None:
            out["summary"] = summary
    elif previous is not None and int(previous[_INDEX["flags"]] or 0) & FLAG_BREAKDOWN:
        # A ttft-only footer of the turn already measured keeps its breakdown.
        values = previous
        flags = int(previous[_INDEX["flags"]] or 0)
    ttft = data.get("ttft")
    if isinstance(ttft, dict) and count(ttft.get("total_ms")) is not None:
        for name in _TTFT_KEYS:
            values[_INDEX[f"ttft:{name}"]] = count(ttft.get(name))
    elif previous is not None:
        for name in _TTFT_KEYS:
            values[_INDEX[f"ttft:{name}"]] = previous[_INDEX[f"ttft:{name}"]]
    if out["rerun"]:
        flags |= FLAG_RERUN
    values[_INDEX["flags"]] = flags
    out["engine"] = values
    out["engine_unit"], out["engine_no"], out["engine_id"] = unit, turn, engine_id
    return out


def _completed(ctx: dict[str, Any], entry: Entry) -> dict[str, Any]:
    data = entry.data
    unit, turn = ctx["unit"], _as_int(data.get("turn"))
    out = dict(ctx)
    occupancy = data.get("context")
    # The turn's OWN reading, for its row: a reading another turn carried describes
    # no turn that ran, so a turn with none keeps 0.
    own_used = own_window = 0
    if isinstance(occupancy, dict) and occupancy.get("used") is not None:
        out["used"] = min(max(0, _as_int(occupancy.get("used"))), MAX_COUNT)
        out["used_window"] = min(max(0, _as_int(occupancy.get("window"))), MAX_COUNT)
        out["ours_read"] = list(ctx["ours"])
        own_used, own_window = out["used"], out["used_window"]
    out["closed_unit"], out["closed_no"] = unit, turn
    if _as_str(data.get("stop_reason")) not in _NORMAL_STOPS:
        # The prompt may have been rewound out of the engine before a retry; the sums
        # still count it, so the split is approximate until they next restart.
        out["rerun"] = True
    if ctx["turns"] == 0 or unit != ctx["turn_unit"] or turn != ctx["turn_no"]:
        if turn > 0 and not _memo_ordinal(ctx, turn):
            # A turn that ran without composing (a slash command) has no row, but it
            # holds its number in the conversation, as a refused turn does.
            out["turns"] = int(ctx["turns"]) + 1
            out["turn_memo"] = _memo_added(ctx["turn_memo"], turn)
        return out
    if own_window > 0:
        # A composed turn's own reading, dated, for the fullest-turn peak.
        out["peaks"] = [*ctx["peaks"], [int(entry.time), own_used, own_window]][-PEAK_READINGS:]
    row = _trend_row(out, own_used, own_window)
    out["row_writes"] = int(row[_VERSION])
    ordinal = int(row[_ORD])
    if ordinal == 0:
        # A turn sent again whose row has left the ring: no ordinal is known that
        # cannot collide with one shown, so it adds no row.
        return out
    start_phase = ctx["turn_phase"] == PHASE_SESSION_START
    # A turn sent again may close under the other phase (a regenerated session
    # start composes per turn, a resent ring turn may compose as a start), so its
    # old row is looked up in BOTH places: one ordinal, one row, whatever the phase.
    if int(ctx["start"][_ORD]) == ordinal and not start_phase:
        out["start"] = _zero_row()
    rows = int(ctx["trend_rows"])
    trend = list(ctx["trend"])
    found = next(
        (i for i in range(TREND_TURNS - rows, TREND_TURNS) if int(trend[i][_ORD]) == ordinal),
        None,
    )
    if start_phase:
        if found is not None:
            # The ring holds real rows at its tail: drop this one and pad the head.
            out["trend"] = [_zero_row(), *trend[:found], *trend[found + 1 :]]
            out["trend_rows"] = rows - 1
        out["start"] = row
        return out
    if found is not None:
        # The same turn closed again (a retry, a regenerate, an edit-resend): its
        # row is replaced in place, not added.
        trend[found] = row
        out["trend"] = trend
    else:
        out["trend"] = [*trend[1:], row]
        out["trend_rows"] = min(rows + 1, TREND_TURNS)
    out["trend_unit"], out["trend_no"] = unit, turn
    return out


def _compaction(ctx: dict[str, Any]) -> dict[str, Any]:
    """An in-place compaction the gateway ran: the sums keep only what came after it.

    The gateway compacts between turns, after a turn whose occupancy reading crossed
    its threshold, and writes ``compaction/applied`` once the effect is confirmed. That
    is either at once, before the next turn composes, or (a deferred verdict) at the
    next turn that carries a reading, while that turn is still open. Every turn closed
    in between carried no reading, which is why the verdict waited. So the boundary is
    the newest turn closed WITH a reading, and what was composed after it is what the
    compacted context still holds as Crew's.
    """
    kept = [max(0, int(a) - int(b)) for a, b in zip(ctx["ours"], ctx["ours_read"])]
    return {**ctx, "ours": kept, "ours_read": list(_ZERO5), "rerun": False}


def _refused(ctx: dict[str, Any], entry: Entry) -> dict[str, Any]:
    """A turn a dispatch gate refused: it never ran and has no row, but it holds its
    number in the conversation, so it takes an ordinal and later turns count past it."""
    turn = _as_int(entry.data.get("turn"))
    if turn <= 0 or _memo_ordinal(ctx, turn):
        return ctx
    return {
        **ctx,
        "turns": int(ctx["turns"]) + 1,
        "turn_memo": _memo_added(ctx["turn_memo"], turn),
    }


def context_apply(ctx: dict[str, Any], entry: Entry) -> dict[str, Any]:
    """Fold one entry. Returns ``ctx`` itself when the entry does not concern it."""
    if entry.type == "context/composed":
        return _composed(ctx, entry)
    if entry.type == "context/measured":
        return _measured(ctx, entry)
    if entry.type == "turn/completed":
        return _completed(ctx, entry)
    if entry.type == "compaction/applied":
        return _compaction(ctx)
    if entry.type == "turn/refused":
        return _refused(ctx, entry)
    if entry.type == "request/configured":
        # Newest NON-ZERO wins: a provider that reports no window writes 0, which
        # would erase a size the session was told earlier and still runs under.
        window = min(max(0, _as_int(entry.data.get("context_window"))), MAX_COUNT)
        return {**ctx, "conf_window": window} if window > 0 else ctx
    if entry.type == "session/opened" and entry.data.get("resumed") is True:
        # A re-attachment is told its window again; until then it states none.
        return {**ctx, "conf_window": 0} if ctx["conf_window"] else ctx
    if entry.type == "session/opened":
        # A new unit is a new engine session: its context starts empty.
        return {
            **ctx,
            "unit": ctx["unit"] + 1,
            "ours": list(_ZERO5),
            "turn_blocks": [],
            "turn_omitted": 0,
            "ours_read": list(_ZERO5),
            "turn_ours": list(_ZERO5),
            "rerun": False,
            "summary": -1,
            # The previous session's occupancy reading and measurement say nothing
            # about this one, and its turn numbers start again.
            "used": -1,
            "used_window": 0,
            "conf_window": 0,
            "engine": [None] * MEASURED_FIELDS,
            "engine_unit": 0,
            "engine_no": 0,
            "engine_id": "",
            "turn_memo": ",",
        }
    return ctx


def _parts_view(engine: Any) -> dict[str, dict[str, int]]:
    """The categories' own splits, as ``{category: {part: chars}}``."""
    out: dict[str, dict[str, int]] = {}
    for cat, _wire, field in SUB_PARTS:
        value = _engine_value(engine, f"part:{cat}.{field}")
        if value is not None:
            out.setdefault(cat, {})[field] = value
    return out


def _window_view(engine: Any) -> dict[str, Any] | None:
    size = _engine_value(engine, "win:size_tokens")
    usage = _engine_value(engine, "win:usage_milli")
    if size is None and usage is None:
        return None
    return {"size_tokens": size, "usage_percent": usage / 1000 if usage is not None else None}


def _media_view(engine: Any) -> dict[str, int] | None:
    media = {
        name: v for name in _MEDIA_KEYS if (v := _engine_value(engine, f"media:{name}")) is not None
    }
    return media or None


def _named(values: Any) -> dict[str, int]:
    return {name: int(v) for name, v in zip(CREW_CATEGORIES, values)}


def _engine_view(engine: Any, crew: int, share: Any) -> dict[str, Any] | None:
    """A stored measurement as the panel reads it, or ``None`` with no breakdown.

    *crew* is Crew's unscaled total and *share* its five sums as the engine holds
    them, so the same view serves the newest turn and any row of the ring.
    """
    if not engine or not int(engine[_INDEX["flags"]] or 0) & FLAG_BREAKDOWN:
        return None
    user = _engine_value(engine, "hist:user")
    flags = int(engine[_INDEX["flags"]] or 0)
    return {
        "total_chars": _engine_value(engine, "total_chars"),
        "model_calls": _engine_value(engine, "model_calls"),
        "categories": {
            name: v
            for name in KNOWN_CATEGORIES
            if (v := _engine_value(engine, f"cat:{name}")) is not None
        },
        "history": {
            name: v
            for name in _HISTORY_KEYS
            if (v := _engine_value(engine, f"hist:{name}")) is not None
        },
        # The engine's own share of each category, as a percent with one decimal.
        "percent": {
            name: v / 10
            for name in KNOWN_CATEGORIES
            if (v := _engine_value(engine, f"pct:{name}")) is not None
        },
        "counts": {
            name: v
            for name in LIST_CATEGORIES
            if (v := _engine_value(engine, f"cnt:{name}")) is not None
        },
        "parts": _parts_view(engine),
        "window": _window_view(engine),
        "media": _media_view(engine),
        "unknown_chars": _engine_value(engine, "unknown_chars") or 0,
        "compacted": bool(flags & FLAG_COMPACTED),
        "schema_known": not flags & FLAG_SCHEMA_UNKNOWN,
        "approximate": bool(flags & FLAG_RERUN),
        "crew_chars": sum(share),
        "crew_split": _named(share),
        "user_other_chars": max(0, user - crew) if user is not None else None,
        "mismatch_chars": max(0, crew - user) if user is not None else 0,
    }


def _ttft_view(engine: Any) -> dict[str, int] | None:
    if not engine:
        return None
    ttft = {
        name: v for name in _TTFT_KEYS if (v := _engine_value(engine, f"ttft:{name}")) is not None
    }
    return ttft if "total_ms" in ttft else None


def _row_render(row: Any) -> dict[str, Any]:
    """One stored row, compact: the chart's numbers plus what the turn's own panel
    needs, the measurement kept as its fixed list (see :func:`turn_detail`)."""
    return {
        "turn": int(row[_ORD]),
        "ours": _named(row[:_LOOP]),
        "loop": int(row[_LOOP]),
        "used": int(row[_USED]),
        "window": int(row[_WINDOW]),
        "crew_raw": int(row[_RAW]),
        "blocks_omitted": int(row[_OMITTED]),
        "blocks": [[str(name), int(chars)] for name, chars in row[_BLOCKS]],
        "engine": list(row[_ROW_ENGINE]) if row[_ROW_ENGINE] is not None else None,
    }


def turn_detail(item: dict[str, Any], phase: str) -> dict[str, Any]:
    """A rendered row (:func:`_row_render`) as the panel's whole-turn view.

    The same shape ``latest`` has, so a past turn shows exactly as the newest one:
    the engine's own parts when it measured the turn, otherwise Crew's blocks and the
    rest estimated from the turn's own occupancy reading.
    """
    raw_ours = item.get("ours")
    ours: dict[str, Any] = raw_ours if isinstance(raw_ours, dict) else {}
    share = tuple(int(ours.get(name, 0)) for name in CREW_CATEGORIES)
    engine = item.get("engine")
    if not (isinstance(engine, list) and len(engine) == MEASURED_FIELDS):
        engine = None
    view = _engine_view(engine, int(item.get("crew_raw") or sum(share)), share)
    used = max(0, int(item.get("used") or 0))
    if view is not None:
        loop, estimated = max(0, int(view["total_chars"] or 0) - sum(share)), False
    elif used > 0:
        loop, estimated = max(0, int(used * CHARS_PER_TOKEN) - sum(share)), True
    else:
        loop, estimated = 0, True
    raw_blocks = item.get("blocks")
    blocks: list[Any] = raw_blocks if isinstance(raw_blocks, list) else []
    return {
        "turn": int(item.get("turn") or 0),
        "phase": phase,
        "ours": _named(share),
        "loop_chars": loop,
        "loop_estimated": estimated,
        "engine": view,
        "ttft_ms": _ttft_view(engine),
        "context_used": used,
        "context_window": max(0, int(item.get("window") or 0)),
        "blocks": {str(name): int(chars) for name, chars in blocks},
        "blocks_omitted": int(item.get("blocks_omitted") or 0),
    }


def _chart_row(row: Any) -> dict[str, Any]:
    """One ring row as the pushed value carries it: the ordinal, the five sums, the
    rest and the turn's own occupancy reading in tokens (0 with none), the numbers the
    chart draws, and the row's version, which changes whenever the row is written again
    (the panel's cache key for the turn's read). The turn's whole panel is read on demand (:func:`context_turn`), so
    the pushed value stays a few KB however full the rows."""
    return {
        "turn": int(row[_ORD]),
        "ours": _named(row[:_LOOP]),
        "loop": int(row[_LOOP]),
        "used": int(row[_USED]),
        "version": int(row[_VERSION]),
    }


def _figure(row: Any) -> dict[str, Any] | None:
    """The size a turn's own panel heads with: the engine's characters when it measured
    the turn, else the turn's occupancy reading in tokens; None with neither."""
    detail = turn_detail(_row_render(row), "")
    engine = detail["engine"]
    if engine is not None:
        total = engine.get("total_chars")
        if total is None:
            total = sum(int(v) for v in (engine.get("categories") or {}).values()) + int(
                engine.get("unknown_chars") or 0
            )
        return {"n": int(total), "unit": "chars"}
    used = int(detail["context_used"])
    return {"n": used, "unit": "tokens"} if used > 0 else None


def context_turn(ctx: dict[str, Any], ordinal: int) -> dict[str, Any] | None:
    """The kept turn with *ordinal* as its whole panel, in ``latest``'s shape; None when
    no ring row and not the start row holds that ordinal (it left the ring)."""
    if ordinal <= 0:
        return None
    start = ctx["start"]
    if int(start[_ORD]) == ordinal:
        return turn_detail(_row_render(start), PHASE_SESSION_START)
    rows = int(ctx["trend_rows"])
    for row in list(ctx["trend"])[TREND_TURNS - rows :] if rows else []:
        if int(row[_ORD]) == ordinal:
            return turn_detail(_row_render(row), PHASE_PER_TURN)
    return None


def context_peak(ctx: dict[str, Any], since_ms: int) -> tuple[int, int]:
    """The fullest turn closed at or after *since_ms*, as ``(used, window)`` from that
    one reading; ``(0, conf_window)`` when no reading falls inside.

    Fullest is the highest ``used / window``, not the largest ``used``: a model switch
    moves the window, so fewer tokens of a small window can be fuller than more of a
    large one. Compared by cross-multiplication, integer-exact; ``>=`` keeps the newest
    of equally full turns, the one describing the model now running.
    """
    peak_used = peak_window = 0
    for when, used, window in ctx["peaks"]:
        if int(when) < since_ms:
            continue
        if not peak_window or int(used) * peak_window >= peak_used * int(window):
            peak_used, peak_window = int(used), int(window)
    return (peak_used, peak_window) if peak_window else (0, int(ctx["conf_window"]))


def context_turn_blocks(ctx: dict[str, Any]) -> list[tuple[int, str, dict[str, int]]]:
    """What each kept turn sent, by block name: ``(ordinal, phase, blocks)``, the start
    row first. Read by the Session Breakdown tree through the trace page, never pushed."""
    out: list[tuple[int, str, dict[str, int]]] = []
    start = ctx["start"]
    rows = int(ctx["trend_rows"])
    ring = list(ctx["trend"])[TREND_TURNS - rows :] if rows else []
    kept = ([(start, PHASE_SESSION_START)] if int(start[_ORD]) else []) + [
        (row, PHASE_PER_TURN) for row in ring
    ]
    for row, phase in kept:
        blocks = {str(name): int(chars) for name, chars in row[_BLOCKS] if int(chars) > 0}
        out.append((int(row[_ORD]), phase, blocks))
    return out


def context_render(ctx: dict[str, Any]) -> dict[str, Any]:
    """The context state as the panel reads it: the newest turn, the ring, the start row."""
    engine = ctx["engine"]
    share = crew_share(ctx)
    loop, estimated = loop_chars(ctx)
    engine_view = (
        _engine_view(engine, sum(int(v) for v in ctx["ours"]), share) if measured_now(ctx) else None
    )
    ttft = None
    if engine and ctx["engine_unit"] == ctx["turn_unit"] and ctx["engine_no"] == ctx["turn_no"]:
        ttft = _ttft_view(engine)
    rows = int(ctx["trend_rows"])
    ring = list(ctx["trend"])[TREND_TURNS - rows :] if rows else []
    trend = [_chart_row(row) for row in ring]
    first = int(ring[0][_ORD]) if ring else 0
    start = ctx["start"]
    start_turn = int(start[_ORD])
    return {
        "turns": int(ctx["turns"]),
        "chars_per_token": CHARS_PER_TOKEN,
        "latest": (
            {
                # The newest turn's own ordinal, so a turn sent again reads as itself.
                "turn": int(ctx["turn_ord"]) or int(ctx["turns"]),
                "phase": ctx["turn_phase"],
                "ours": _named(share),
                "loop_chars": loop,
                "loop_estimated": estimated,
                "engine": engine_view,
                "ttft_ms": ttft,
                "context_used": max(0, int(ctx["used"])),
                "context_window": int(ctx["used_window"]),
                "blocks": {str(name): int(chars) for name, chars in ctx["turn_blocks"]},
                "blocks_omitted": int(ctx["turn_omitted"]),
            }
            if ctx["turns"]
            else None
        ),
        "trend": trend,
        # Turns before the ring's first row, exactly; the start row, if one of them, is
        # listed above the chart.
        "earlier": max(0, first - 1) if first else 0,
        "start": {**_chart_row(start), "figure": _figure(start)} if start_turn else None,
    }
