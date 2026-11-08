"""A Crew Mode root's timeline: one lane per session, bars only from recorded rows.

The Workers panel's Timeline view draws what the root and each of its workers did
over time. Every bar here is taken from a transcript row that is already written
for the chat view; nothing is estimated from a model, a token count or a price.
Where a row does not record a moment, the bar ends at the next row that does and
says so (``end_bound``), and what the transcript does not record at all is listed
in :data:`GAPS` instead of being drawn.

Pure functions over row dicts (``role``, ``content``, ``ts``, ``meta``), so the
derivation is tested without a gateway; the route in
``dashboard/handlers/crew_workers.py`` feeds it the live slots' windows.
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timedelta
from typing import Any, Final

#: Bar kinds, in the legend's order.
KIND_TURN = "turn"
KIND_TOOL = "tool"
KIND_WAITING = "waiting"
#: Point marks.
MARK_FAILED = "failed"
MARK_REFUSED = "refused"

#: How a bar's end is known. ``recorded``: a row stamps it. ``next_row``: the row
#: that ends it is not written, so the bar stops at the next row the session wrote,
#: which is the latest it can have ended. ``now``: it is still open.
END_RECORDED = "recorded"
END_NEXT_ROW = "next_row"
END_NOW = "now"

#: What the transcript does not record, so the timeline does not draw it.
GAPS: tuple[str, ...] = (
    # A turn is one bar: thinking and writing are not stamped apart (only ttft_ms is).
    "thinking_vs_output",
    # A tool's finish has no timestamp of its own; the bar ends at the next row.
    "tool_end_time",
    # A tool that ran and failed is marked done like one that succeeded.
    "tool_failure_status",
    # Tokens are not recorded per turn; only credits are.
    "tokens",
    # A closed worker whose saved transcript cannot be read draws no bars;
    # named only when such a lane is shown (``GAP_CLOSED_NO_RECORD``).
    "closed_worker_rows",
    # Only the loaded window (the newest rows) is read, not older history.
    "rows_before_window",
    # On ``auto`` the backend picks the model and the name it picked is not reported.
    "served_model_unreported",
)

#: The gap a lane on the gateway default model adds (see ``GAPS``).
GAP_SERVED_MODEL: Final[str] = "served_model_unreported"
#: The gap a closed worker with no readable transcript adds (see ``GAPS``).
GAP_CLOSED_NO_RECORD: Final[str] = "closed_worker_rows"

#: Bars kept per lane, newest last. A lane over the cap keeps its newest bars and
#: says how many older ones it dropped, so one chatty session bounds the reply.
MAX_SEGMENTS_PER_LANE = 400

#: A bar or mark label's length as sent, and a failure mark's. Labels are kept at
#: up to :data:`_LABEL_RAW_MAX` until :func:`redact_labels` has run, then cut, so a
#: secret crossing the cut is still whole when the redactor sees it.
LABEL_MAX = 120
FAILED_LABEL_MAX = 160
_LABEL_RAW_MAX = 4000

#: The longest turn a recorded ``elapsed_ms`` may claim (a week); anything longer,
#: or not finite, is not a turn this gateway ran and draws no bar.
_ELAPSED_MS_MAX = 7 * 24 * 3600 * 1000

#: The envelope ``session_messaging._SEND_PROVENANCE`` prepends, and only that: the
#: ``[quoted: sent by session`` form is the server's defanged copy of a forged one.
_SEED_RE = re.compile(
    r"^\[sent by session (?P<sender>[\w.:\-]+) via session_(?P<via>send|create|broadcast)\]\n\n"
)
#: ``history.HUMAN_TURN_META_KEY``: the marker a row the person typed carries.
_HUMAN_TURN_KEY = "human"
_TOOL_NAME_RE = re.compile(r"^\S+\s+@[^/\s]+/(?P<name>[\w.\-]+)")
_HANDOFF_TOOLS = frozenset({"session_send"})
#: The dispatch that seeds a new session: its seed row says ``via session_create``.
_CREATE_TOOLS = frozenset({"session_create"})
#: The one-call dispatch: it opens the session and seeds it ``via session_send``.
_DISPATCH_TOOLS = frozenset({"work_dispatch"})
_QUESTION_TOOLS = frozenset({"ask_question"})


def parse_ts(value: Any) -> datetime | None:
    """A row's ISO timestamp, or None when it has none or it does not parse."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _iso(value: datetime) -> str:
    return value.isoformat()


def _meta(row: dict[str, Any]) -> dict[str, Any]:
    meta = row.get("meta")
    return meta if isinstance(meta, dict) else {}


def _content(row: dict[str, Any]) -> str:
    value = row.get("content")
    return value if isinstance(value, str) else ""


def tool_name(row: dict[str, Any]) -> str:
    """The tool a tool row names: ``meta.tool_name``, else the ``@server/name`` title."""
    named = _meta(row).get("tool_name")
    if isinstance(named, str) and named:
        return named
    match = _TOOL_NAME_RE.match(_content(row))
    return match.group("name") if match else ""


def _tool_label(row: dict[str, Any]) -> str:
    text = _content(row)
    body = text[1:] if text[:1] in ("🔧", "✅", "🚫") else text
    return body.strip()[:_LABEL_RAW_MAX]


def _tool_arg(row: dict[str, Any], name: str) -> str:
    """A string argument the tool row records in its input, else ``""``."""
    raw = _meta(row).get("input")
    if not isinstance(raw, str) or not raw:
        return ""
    try:
        parsed = json.loads(raw)
    except ValueError:
        return ""
    if not isinstance(parsed, dict):
        return ""
    nested = parsed.get("arguments")
    args: dict[str, Any] = nested if isinstance(nested, dict) else parsed
    value = args.get(name)
    return value if isinstance(value, str) else ""


def send_target(row: dict[str, Any]) -> str:
    """The session a ``session_send`` row names in its recorded input, else ``""``."""
    return _tool_arg(row, "target")


def _is_failure(row: dict[str, Any]) -> bool:
    return row.get("role") == "error" or _content(row).startswith("❌")


def _credits(row: dict[str, Any]) -> float:
    stats = _meta(row).get("turn_stats")
    if not isinstance(stats, dict):
        return 0.0
    value = stats.get("credits")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value) if value > 0 else 0.0


def _elapsed_ms(row: dict[str, Any]) -> int | None:
    stats = _meta(row).get("turn_stats")
    if not isinstance(stats, dict):
        return None
    value = stats.get("elapsed_ms")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0 or value > _ELAPSED_MS_MAX:
        return None
    return int(value)


def _bar(kind: str, start: datetime, end: datetime, bound: str, label: str = "") -> dict[str, Any]:
    if end < start:
        end = start
    return {
        "kind": kind,
        "start": _iso(start),
        "end": _iso(end),
        "end_bound": bound,
        "label": label,
    }


def derive_lane(
    rows: list[dict[str, Any]],
    *,
    now: datetime,
    running: bool,
    waiting_now: bool,
) -> dict[str, Any]:
    """One session's bars, marks and seeds from its transcript *rows*.

    *running* and *waiting_now* are the slot's live flags; they decide whether the
    last open bar runs to *now* or stops at the last row. Rows without a parseable
    ``ts`` are skipped.
    """
    timed: list[tuple[datetime, dict[str, Any]]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        ts = parse_ts(row.get("ts"))
        if ts is not None and row.get("role"):
            timed.append((ts, row))
    timed.sort(key=lambda pair: pair[0])
    open_end = now if (running or waiting_now) else None

    # Row indexes by tool_call_id, built once, so finding a call's ✅ row is a
    # lookup instead of a scan to the end of the window for every tool row.
    tcids: list[str] = []
    by_tcid: dict[str, list[int]] = {}
    for position, (_ts, row) in enumerate(timed):
        raw = _meta(row).get("tool_call_id")
        key = raw if isinstance(raw, str) else ""
        tcids.append(key)
        if key:
            by_tcid.setdefault(key, []).append(position)

    def next_ts(index: int, *, skip_tcid: str = "") -> datetime | None:
        # Only the call's own rows are skipped, and those sit next to each other,
        # so this walks a handful of rows, never the window.
        for position in range(index + 1, len(timed)):
            if skip_tcid and tcids[position] == skip_tcid:
                continue
            return timed[position][0]
        return None

    def bounded(index: int, *, skip_tcid: str = "") -> tuple[datetime, str] | None:
        later = next_ts(index, skip_tcid=skip_tcid)
        if later is not None:
            return later, END_NEXT_ROW
        if open_end is not None:
            return open_end, END_NOW
        return None

    bars: list[dict[str, Any]] = []
    marks: list[dict[str, Any]] = []
    seeds: list[dict[str, Any]] = []
    sends: list[dict[str, str]] = []
    credits = 0.0
    turn_open_at: datetime | None = None
    seen_tcids: set[str] = set()
    # The lane's last recorded moment: its last row, or a later recorded turn end
    # (a Stop's turn ends when the turn does, after the Stop row was written).
    last_at: datetime | None = timed[-1][0] if timed else None

    for index, (ts, row) in enumerate(timed):
        role = row.get("role")
        meta = _meta(row)
        if role in ("user", "steer"):
            # Only the envelope the server prepends, on a row no person typed: a
            # person's own row carries the human-turn marker, so typing the envelope
            # by hand never draws a hand-off.
            seed = None if meta.get(_HUMAN_TURN_KEY) else _SEED_RE.match(_content(row))
            if seed:
                seeds.append(
                    {"ts": _iso(ts), "from": seed.group("sender"), "via": seed.group("via")}
                )
            if turn_open_at is None and role == "user":
                # A steer row lands inside a turn (and stays after a take-back), so
                # it never opens one: only the row that started a turn does.
                turn_open_at = ts
            continue
        if role == "assistant":
            elapsed = _elapsed_ms(row)
            if elapsed is not None:
                credits += _credits(row)
                # The stats sit on the turn's last reply row, which is not always
                # written at the turn's end: a Stop during a tool call puts the
                # whole turn's time on the narration before it. So the bar runs
                # for the recorded time from the row that opened the turn, and
                # never ends before the reply row itself.
                span = timedelta(milliseconds=elapsed)
                turn_end = ts if turn_open_at is None else max(ts, turn_open_at + span)
                bars.append(_bar(KIND_TURN, turn_end - span, turn_end, END_RECORDED))
                if last_at is None or turn_end > last_at:
                    last_at = turn_end
                turn_open_at = None
            continue
        if _is_failure(row):
            marks.append(
                {"kind": MARK_FAILED, "ts": _iso(ts), "label": _content(row)[:_LABEL_RAW_MAX]}
            )
            if role == "error":
                # An error row ends the turn it broke; the next user row opens a new one.
                turn_open_at = None
            continue
        if role == "permission":
            end = bounded(index)
            if end is not None:
                bars.append(_bar(KIND_WAITING, ts, end[0], end[1], _content(row)[:_LABEL_RAW_MAX]))
            continue
        if role != "tool":
            continue
        raw_tcid = meta.get("tool_call_id")
        tcid: str = raw_tcid if isinstance(raw_tcid, str) else ""
        text = _content(row)
        name = tool_name(row)
        if text.startswith("🚫"):
            marks.append({"kind": MARK_REFUSED, "ts": _iso(ts), "label": _tool_label(row)})
            continue
        if tcid and tcid in seen_tcids:
            # The ✅ row of a call already drawn from its 🔧 row.
            continue
        if tcid:
            seen_tcids.add(tcid)
        if name in _HANDOFF_TOOLS:
            target = send_target(row)
            if target:
                sends.append({"ts": _iso(ts), "to": target})
        elif name in _CREATE_TOOLS:
            # The new session's key is not in the call's input; its title is, when
            # the caller named one, and the seed lands on the session it created.
            sends.append({"ts": _iso(ts), "to": _tool_arg(row, "title"), "via": "create"})
        elif name in _DISPATCH_TOOLS:
            # Opens the session (its title is ``session_title``, else ``title``) and
            # seeds it, so the seed row reads ``via session_send``.
            title = _tool_arg(row, "session_title") or _tool_arg(row, "title")
            sends.append({"ts": _iso(ts), "to": title, "via": "dispatch"})
        approved: datetime | None = None
        end_index = index
        if tcid and text.startswith("🔧"):
            # The call's next row, if any: a ✅ there is the approval.
            later = next((j for j in by_tcid.get(tcid, ()) if j > index), None)
            if later is not None and _content(timed[later][1]).startswith("✅"):
                approved, end_index = timed[later][0], later
        start = ts
        if approved is not None and approved > ts:
            # The ✅ row is written when the person approves: 🔧 -> ✅ is the wait.
            bars.append(_bar(KIND_WAITING, ts, approved, END_RECORDED, _tool_label(row)))
            start = approved
        kind = KIND_WAITING if name in _QUESTION_TOOLS else KIND_TOOL
        end = bounded(end_index, skip_tcid=tcid)
        if end is None:
            # Finished with no row after it and nothing running: the end is unknown,
            # so the bar is drawn as a point rather than given a length.
            end = (start, END_NEXT_ROW)
        bars.append(_bar(kind, start, end[0], end[1], _tool_label(row)))

    if turn_open_at is not None and open_end is not None:
        bars.append(_bar(KIND_TURN, turn_open_at, open_end, END_NOW))

    bars.sort(key=lambda bar: bar["start"])
    dropped = max(0, len(bars) - MAX_SEGMENTS_PER_LANE)
    if dropped:
        bars = bars[dropped:]
    return {
        "segments": bars,
        "marks": marks[-MAX_SEGMENTS_PER_LANE:],
        "seeds": seeds,
        "sends": sends,
        "credits": round(credits, 4),
        "first_ts": _iso(timed[0][0]) if timed else "",
        "last_ts": _iso(last_at) if last_at else "",
        "dropped_segments": dropped,
    }


def handoffs(lanes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Conductor -> worker hand-offs, both ends recorded.

    A worker's seed row (``[sent by session <sender> via session_send]``) is where
    the hand-off lands; it leaves at the sender's last ``session_send`` call whose
    recorded ``target`` is that worker, at or before that row. A ``via
    session_create`` seed (the first dispatch) leaves at the sender's last
    ``session_create`` call at or before it, one whose recorded ``title``, when it
    names one, is the worker's. A ``via session_send`` seed also leaves at the
    sender's last ``work_dispatch`` call whose session title is the worker's. A
    seed whose sender is not a lane, or which has no such call, is not drawn.
    """
    by_key = {lane["key"]: lane for lane in lanes}
    titles: dict[str, int] = {}
    for lane in lanes:
        title = str(lane.get("title") or "").strip().casefold()
        if title:
            titles[title] = titles.get(title, 0) + 1

    def names(target: object, lane: dict[str, Any]) -> bool:
        """Whether a recorded ``session_send`` target names *lane*, in any form the
        delivery accepts: the slot key, its transcript stem, or a title that only
        this lane carries (an ambiguous title is refused by the delivery too)."""
        if not isinstance(target, str) or not target:
            return False
        key = str(lane["key"])
        if target in (key, f"dashboard_{key}"):
            return True
        wanted = target.strip().casefold()
        title = str(lane.get("title") or "").strip().casefold()
        return bool(wanted) and wanted == title and titles.get(title) == 1

    def created(send: dict[str, Any], lane: dict[str, Any]) -> bool:
        """Whether a recorded ``session_create`` call can be *lane*'s dispatch: an
        untitled create can be any lane's, a titled one only the lane it named."""
        wanted = str(send.get("to") or "").strip().casefold()
        return not wanted or wanted == str(lane.get("title") or "").strip().casefold()

    def dispatched(send: dict[str, Any], lane: dict[str, Any]) -> bool:
        """Whether a recorded ``work_dispatch`` call opened *lane*: its title names it,
        and only this lane carries that title. The call's input holds no session key,
        so two workers sharing a title cannot be told apart and neither is drawn."""
        wanted = str(send.get("to") or "").strip().casefold()
        title = str(lane.get("title") or "").strip().casefold()
        return bool(wanted) and wanted == title and titles.get(title) == 1

    def matches(send: dict[str, Any], lane: dict[str, Any], by_create: bool) -> bool:
        via = send.get("via")
        if by_create:
            return via != "dispatch" and created(send, lane)
        if via == "dispatch":
            return dispatched(send, lane)
        return via != "create" and names(send.get("to"), lane)

    out: list[dict[str, Any]] = []
    for lane in lanes:
        for seed in lane.get("seeds", []):
            sender = by_key.get(seed.get("from", ""))
            if sender is None:
                continue
            landed = parse_ts(seed.get("ts"))
            if landed is None:
                continue
            by_create = seed.get("via") == "create"
            left = [
                ts
                for ts in (
                    parse_ts(send.get("ts"))
                    for send in sender.get("sends", [])
                    if matches(send, lane, by_create)
                )
                if ts and ts <= landed
            ]
            if not left:
                continue
            out.append(
                {
                    "from": sender["key"],
                    "to": lane["key"],
                    "start": _iso(max(left)),
                    "end": _iso(landed),
                }
            )
    out.sort(key=lambda link: link["start"])
    return out


def redact_labels(lane: dict[str, Any], redact: Any) -> dict[str, Any]:
    """*lane* with every bar and mark ``label`` redacted, then cut. IN PLACE.

    Labels are slices of transcript rows (tool, permission, error), so they leave
    through the same display redaction every other transcript egress applies. The
    cut comes after: a secret crossing it is whole when *redact* sees it, so no
    prefix of one survives in the label.
    """
    for item in [*lane.get("segments", ()), *lane.get("marks", ())]:
        label = item.get("label")
        if isinstance(label, str) and label:
            limit = FAILED_LABEL_MAX if item.get("kind") == MARK_FAILED else LABEL_MAX
            item["label"] = str(redact(label))[:limit]
    return lane


def summarize(lanes: list[dict[str, Any]], *, now: datetime, live: bool) -> dict[str, Any]:
    """The header: real elapsed time and summed credits across every lane."""
    firsts = [ts for ts in (parse_ts(lane.get("first_ts")) for lane in lanes) if ts]
    lasts = [ts for ts in (parse_ts(lane.get("last_ts")) for lane in lanes) if ts]
    started = min(firsts) if firsts else None
    if live:
        ended = now
    elif lasts:
        ended = max(lasts)
    else:
        ended = None
    elapsed_ms = 0
    if started and ended and ended >= started:
        elapsed_ms = int((ended - started).total_seconds() * 1000)
    return {
        "started": _iso(started) if started else "",
        "ended": _iso(ended) if ended else "",
        "elapsed_ms": elapsed_ms,
        "credits": round(sum(float(lane.get("credits") or 0) for lane in lanes), 4),
    }
