"""The KAS backend's per-turn profile: what Kiro Crew asks for and what it keeps.

KAS can measure two things about a turn and put them on frames it already sends,
when ``session/prompt`` asks for them in ``_meta.kiro`` (:data:`PROMPT_META`):

- ``contextBreakdown`` on the ``turn_completion`` ``session_info_update``: the
  turn's LAST model request split into fourteen categories, in characters. Text
  counts its length, tool definitions and tool-call arguments their JSON length;
  images and documents are outside the total.
- ``ttft`` on the turn's first displayed ``agent_message_chunk`` or
  ``agent_thought_chunk``: four epoch-millisecond timestamps from the start of
  the turn's execution to the first chunk leaving the engine.

Only numbers are kept. ``summary`` mode is requested, not ``detailed``: the
detailed items name steering files, skills and MCP servers by path or URI, about
3 KB a turn against 1 KB, and no reader shows them. The category totals are the
same in both modes.

Every count read off the wire goes through :func:`count`, which refuses a bool, a
negative, a non-finite float and anything that is not a number, and clamps the rest
to :data:`MAX_COUNT`. A malformed frame therefore loses its numbers, never the turn.

The parsed profile rides :attr:`kiro_crew.acp.types.AcpPromptStats.kas_profile`,
which ``carry_over()`` drops at the next prompt, so a profile always belongs to the
prompt that was running when its frame arrived.
"""

from __future__ import annotations

import math
from typing import Any, Final

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

__all__ = [
    "BREAKDOWN_SCHEMA",
    "HISTORY_PARTS",
    "KNOWN_CATEGORIES",
    "MAX_COUNT",
    "PROMPT_META",
    "count",
    "parse_breakdown",
    "parse_ttft",
    "record_breakdown",
    "record_ttft",
]

#: ``_meta`` sent on every KAS ``session/prompt``. KAS reads both keys off the
#: prompt and strips them before parsing the rest of the bag, so they never echo
#: back on a chunk. ``perf`` is not asked for: it stamps every streamed chunk.
PROMPT_META: Final[dict[str, Any]] = {"kiro": {"contextBreakdown": "summary", "ttft": True}}

#: Categories one breakdown keeps. Schema 1 has fourteen; the rest of the room is for
#: a category a newer engine adds, which is kept by name rather than dropped.
MAX_CATEGORIES: Final[int] = 32

#: Longest category name kept. A longer one is dropped.
MAX_CATEGORY_NAME: Final[int] = 64

#: Longest execution or turn id kept; the engine caps both at 128.
ID_LIMIT: Final[int] = 128


def _stamp(value: Any) -> int | None:
    """An epoch-millisecond timestamp, unclamped, else ``None``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return int(value) if value > 0 else None


def _scaled(value: Any, factor: int) -> float | None:
    """*value* times *factor*, rounded, when it is a usable number, else ``None``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return float(round(value * factor))


def parse_breakdown(raw: Any) -> dict[str, Any] | None:
    """The numbers of one ``contextBreakdown`` object, or ``None`` when unusable.

    ``categories`` keeps every category the frame carries, known or not, up to
    :data:`MAX_CATEGORIES`, as ``name -> chars``. A category with no usable
    ``chars`` is left out and counted with the ones past the cap.
    """
    if not isinstance(raw, dict):
        return None
    schema = raw.get("schema")
    categories = raw.get("categories")
    if isinstance(schema, bool) or not isinstance(schema, int) or not isinstance(categories, dict):
        return None
    kept: dict[str, int] = {}
    for name, body in categories.items():
        chars = count(body.get("chars")) if isinstance(body, dict) else None
        if (
            chars is None
            or not isinstance(name, str)
            or not 0 < len(name) <= MAX_CATEGORY_NAME
            or len(kept) >= MAX_CATEGORIES
        ):
            continue
        kept[name] = chars
    out: dict[str, Any] = {"schema": schema, "categories": kept}
    for wire, field in (("modelCalls", "model_calls"), ("totalChars", "total_chars")):
        value = count(raw.get(wire))
        if value is not None:
            out[field] = value
    if raw.get("compacted") is True:
        out["compacted"] = True
    # The engine's own share of each category, one decimal, kept in tenths of a
    # percent: the bar is drawn from these, so it matches what the engine reports.
    percent = {
        name: tenths
        for name, body in categories.items()
        if name in kept
        and isinstance(body, dict)
        and (tenths := count(_scaled(body.get("percent"), 10))) is not None
    }
    if percent:
        out["percent"] = percent
    counts = {
        name: n
        for name in LIST_CATEGORIES
        if isinstance(body := categories.get(name), dict)
        and (n := count(body.get("count"))) is not None
    }
    if counts:
        out["counts"] = counts
    parts = {
        f"{category}.{field}": n
        for category, wire, field in SUB_PARTS
        if isinstance(body := categories.get(category), dict)
        and (n := count(body.get(wire))) is not None
    }
    if parts:
        out["parts"] = parts
    window = raw.get("window")
    if isinstance(window, dict):
        size = count(window.get("sizeTokens"))
        # Thousandths of a percent: 5.8616% is kept as 5862.
        usage = count(_scaled(window.get("usagePercentage"), 1000))
        win = {
            key: value
            for key, value in (("size_tokens", size), ("usage_milli", usage))
            if value is not None
        }
        if win:
            out["window"] = win
    media = raw.get("media")
    if isinstance(media, dict):
        kept_media = {
            field: n for wire, field in MEDIA_PARTS if (n := count(media.get(wire))) is not None
        }
        if kept_media:
            out["media"] = kept_media
    history = categories.get("history")
    if isinstance(history, dict):
        parts = {
            field: value
            for wire, field in HISTORY_PARTS
            if (value := count(history.get(wire))) is not None
        }
        if parts:
            out["history"] = parts
    return out


def parse_ttft(raw: Any) -> dict[str, int] | None:
    """Two spans and the total, in ms, from one ``ttft`` object, else ``None``.

    ``total_ms`` runs from the start of the turn's execution to the first displayed
    chunk. ``setup_ms`` ends when the first model request left, and ``model_ms`` when
    the model's first visible content arrived. The engine omits the two middle stamps when it captured no model timing,
    and a span whose ends are missing or out of order is left out rather than guessed.
    """
    if not isinstance(raw, dict):
        return None
    start = _stamp(raw.get("promptReceivedAt"))
    shown = _stamp(raw.get("firstChunkEmittedAt"))
    if start is None or shown is None or shown < start:
        return None
    sent = _stamp(raw.get("modelRequestSentAt"))
    first = _stamp(raw.get("firstTokenReceivedAt"))
    out = {"total_ms": min(shown - start, MAX_COUNT)}
    if sent is not None and start <= sent <= shown:
        out["setup_ms"] = min(sent - start, MAX_COUNT)
        if first is not None and sent <= first <= shown:
            out["model_ms"] = min(first - sent, MAX_COUNT)
    return out


def _frame_id(kiro: dict[str, Any], key: str) -> str:
    value = kiro.get(key)
    return value if isinstance(value, str) and 0 < len(value) <= ID_LIMIT else ""


def _not_this_turns(kiro: dict[str, Any]) -> bool:
    """Whether a frame describes something other than the running prompt's own turn.

    A sub-agent's frame carries its parent turn's ids plus ``agentSubtaskId``. A frame
    ``session/load`` replays carries ``replay: true``: it repeats a turn the crew log
    recorded when it ran.
    """
    return kiro.get("agentSubtaskId") not in (None, "") or kiro.get("replay") is True


def record_breakdown(stats: Any, kiro: dict[str, Any]) -> None:
    """Put a ``turn_completion`` frame's breakdown on *stats*' per-turn profile.

    Assigned, so a second footer for the same prompt (a steering continuation runs
    as a turn of its own and closes with its own footer) replaces the first: its last
    request is the newer one. A frame without a usable breakdown changes nothing.
    """
    if _not_this_turns(kiro):
        return
    parsed = parse_breakdown(kiro.get("contextBreakdown"))
    if parsed is None:
        return
    profile = dict(getattr(stats, "kas_profile", None) or {})
    profile["breakdown"] = parsed
    turn_id = _frame_id(kiro, "turnId")
    if turn_id:
        profile["turn_id"] = turn_id
    stats.kas_profile = profile


def record_ttft(stats: Any, kiro: dict[str, Any]) -> None:
    """Put a chunk's ``ttft`` on *stats*' per-turn profile; the first one wins.

    The engine sends one per turn. Kept first-wins all the same, so a prompt whose
    execution runs more than one turn reports the wait before its FIRST output.
    """
    if _not_this_turns(kiro):
        return
    profile = getattr(stats, "kas_profile", None) or {}
    if "ttft" in profile:
        return
    parsed = parse_ttft(kiro.get("ttft"))
    if parsed is None:
        return
    stats.kas_profile = {**profile, "ttft": parsed}
