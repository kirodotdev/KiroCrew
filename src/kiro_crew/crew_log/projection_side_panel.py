"""The side panel's four small session folds: ``status``, ``timeline``, ``tools`` and
``approvals``, each beside the caps it keeps.
"""

from __future__ import annotations

from typing import Any, Final

from kiro_crew.crew_log.errors import CODE_BAD_DATA, CrewLogError
from kiro_crew.crew_log.projection_values import (
    _as_id,
    _as_int,
    _as_str,
    _as_text_or_none,
    _keyable,
    _note_omitted,
)
from kiro_crew.crew_log.schema import Entry

#: Newest moments a ``timeline`` keeps. A projection is pushed over a socket on
#: every growth, so its value is bounded by construction rather than by how long
#: the session ran; the count of moments dropped off the front is kept, so a
#: reader is told the list is a window rather than the whole history.
TIMELINE_LIMIT: Final[int] = 200

#: Distinct tool names a ``tools`` projection details. Past it the totals stay
#: exact and ``names_omitted`` counts the names left out.
TOOL_NAME_LIMIT: Final[int] = 100

#: Open tool calls and pending approvals listed individually.
OPEN_LIST_LIMIT: Final[int] = 50

#: Open tool calls and pending approvals RETAINED in the fold state. The render
#: lists ``OPEN_LIST_LIMIT`` of them, but the state kept every id it had not yet
#: matched, so a session that leaked unmatched calls or approvals grew the
#: checkpoint without bound -- the one thing these folds say they never do. Past
#: this cap a further distinct id is COUNTED as omitted and not retained, so the
#: state stays bounded and a later completion for a dropped id reads as unmatched
#: rather than reopening unbounded growth. Set above the render cap so the listed
#: window is always drawn from retained entries.
OPEN_RETAIN_LIMIT: Final[int] = 512

#: Distinct MCP servers a single tool row records. A tool called through many
#: servers would otherwise append every distinct name to its row without bound.
SERVERS_PER_TOOL_LIMIT: Final[int] = 32

#: Types a ``timeline`` records. Turn, lifecycle and cost boundaries -- the
#: moments a person scanning a session looks for. Message, step and tool entries
#: are deliberately absent: they are the bulk of a log, they are what the page
#: route and the ``tools`` projection already serve, and a timeline that included
#: them would be a second copy of the file rather than a summary of it.
TIMELINE_TYPES: Final[frozenset[str]] = frozenset(
    {
        "session/opened",
        "session/seeded",
        "session/closed",
        "turn/started",
        "turn/completed",
        "turn/refused",
        "compaction/applied",
        "model/selected",
        "write/dropped",
        "approval/requested",
        "approval/decided",
        "subagent/spawned",
        "subagent/completed",
        "subagent/failed",
    }
)


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #


def _status_start() -> dict[str, Any]:
    return {
        "opened_at": None,
        "closed_at": None,
        "close_reason": None,
        "resumed": False,
        "previous": None,
        "seeded": False,
        "agent": "",
        "owner": "",
        "slot": "",
        "cwd": "",
        "model": "",
        "provider": "",
        "open_turn": None,
        "turns_completed": 0,
        "turns_refused": 0,
        "last_stop_reason": None,
        "last_error": None,
        "last_time": None,
        "entries": 0,
        "dropped_count": 0,
        "dropped_bytes": 0,
    }


def _status_step(state: dict[str, Any], entry: Entry) -> None:
    data = entry.data
    state["entries"] += 1
    state["last_time"] = entry.time
    kind = entry.type
    if kind == "session/opened":
        # A resume writes this type too, so the echo is refreshed rather than
        # kept from the first one: the agent or model a session re-attaches under
        # is the one it is serving on now. The opening TIME is the exception --
        # it is when this session began, which a re-attach does not change.
        if state["opened_at"] is None:
            state["opened_at"] = entry.time
        state["resumed"] = bool(data.get("resumed")) or state["resumed"]
        # The edge to the crew log this slot was writing BEFORE this one. Kept from
        # whichever entry carried it rather than refreshed from the newest, because
        # only the creating entry carries one: a re-attach echo has no ``previous``,
        # and letting it overwrite would drop the edge a chain walker needs.
        if state["previous"] is None:
            previous = data.get("previous")
            if isinstance(previous, dict):
                sid = previous.get("sid")
                if isinstance(sid, str) and sid:
                    state["previous"] = _as_str(sid)
        for key in ("agent", "owner", "slot", "cwd"):
            value = data.get(key)
            if isinstance(value, str):
                state[key] = _as_str(value)
        model = _as_str(data.get("model"))
        if model:
            state["model"] = model
        # A reopened session is serving again, so the close a reader would have
        # seen before it describes a life this entry has ended.
        state["closed_at"] = None
        state["close_reason"] = None
    elif kind == "session/seeded":
        state["seeded"] = True
    elif kind == "session/closed":
        state["closed_at"] = entry.time
        state["close_reason"] = _as_text_or_none(data.get("reason"))
        # The open turn is left ALONE. A close is not a turn ending: a session cut
        # off mid-turn has no ``turn/completed``, so clearing here would assert
        # that the turn finished when nothing recorded it doing so, and the one
        # fact a reader wants -- this session died with work in flight -- is
        # exactly what would be erased. A reader sees both ``closed_at`` and the
        # open turn and can tell what happened. Only ``turn/completed`` closes a
        # turn, which is the rule the render states.
    elif kind == "turn/started":
        attempt = data.get("attempt")
        state["open_turn"] = {
            "turn": _as_int(data.get("turn")),
            "attempt": attempt if isinstance(attempt, int) and not isinstance(attempt, bool) else 1,
            "actor": _as_str(data.get("actor")),
            "started_at": entry.time,
            "seq": entry.seq,
        }
    elif kind == "turn/completed":
        state["turns_completed"] += 1
        state["last_stop_reason"] = _as_text_or_none(data.get("stop_reason"))
        state["last_error"] = _as_text_or_none(data.get("error"))
        for key in ("model", "provider"):
            value = _as_str(data.get(key))
            if value:
                state[key] = value
        state["open_turn"] = None
    elif kind == "turn/refused":
        state["turns_refused"] += 1
    elif kind == "model/selected":
        model = _as_str(data.get("model"))
        if model:
            state["model"] = model
    elif kind == "request/configured":
        for key in ("model", "provider"):
            value = _as_str(data.get(key))
            if value:
                state[key] = value
    elif kind == "write/dropped":
        state["dropped_count"] += _as_int(data.get("dropped_count"))
        state["dropped_bytes"] += _as_int(data.get("dropped_bytes"))


def _status_render(state: dict[str, Any]) -> dict[str, Any]:
    if state["closed_at"] is not None:
        lifecycle = "closed"
    elif state["opened_at"] is not None:
        lifecycle = "open"
    else:
        # Reachable: retention can remove the segment that carried
        # ``session/opened``, and a fold over what survives has no opener to read.
        lifecycle = "unknown"
    return {
        "lifecycle": lifecycle,
        "opened_at": state["opened_at"],
        "closed_at": state["closed_at"],
        "close_reason": state["close_reason"],
        "resumed": state["resumed"],
        # The previous crew log of this SLOT, or null when this is its first one (or
        # when retention removed the segment that carried the edge). A reader
        # joining a slot's whole history follows this, one store at a time.
        "previous": state["previous"],
        "seeded": state["seeded"],
        "agent": state["agent"],
        "owner": state["owner"],
        "slot": state["slot"],
        "cwd": state["cwd"],
        "model": state["model"],
        "provider": state["provider"],
        # An open turn is REPORTED, never closed. The store's repair appends real
        # closers under write ownership; a reader that closed it here would make
        # two readers of one file disagree about the same turn.
        "turn": dict(state["open_turn"]) if state["open_turn"] else None,
        "turn_open": state["open_turn"] is not None,
        "turns_completed": state["turns_completed"],
        "turns_refused": state["turns_refused"],
        "last_stop_reason": state["last_stop_reason"],
        "last_error": state["last_error"],
        "last_time": state["last_time"],
        "entries": state["entries"],
        "dropped": {"count": state["dropped_count"], "bytes": state["dropped_bytes"]},
    }


# --------------------------------------------------------------------------- #
# timeline
# --------------------------------------------------------------------------- #


#: The keys a timeline moment copies from its entry when the entry carries one.
TIMELINE_DETAIL_KEYS: tuple[str, ...] = (
    "turn",
    "attempt",
    "actor",
    "stop_reason",
    "reason",
    "model",
    "source",
    "duration_ms",
    # Both spellings of a measured duration: ``duration_ms`` is the turn entry's,
    # ``ms`` is what the subagent closers and steps call theirs. Copying only one
    # of the two keeps that half of the log's durations out of the timeline.
    "ms",
    "credits",
    "freed_pct",
    "dropped_count",
    "agent_id",
    "agent",
    "resumed",
    "count",
    "approval_id",
    "decision",
    "by",
    "tool",
)
#: Every key a ``timeline.moments`` row can carry: ``type`` is the crew-log entry type,
#: which is what names the moment, and ``time`` its epoch-millisecond stamp.
TIMELINE_MOMENT_KEYS: tuple[str, ...] = ("seq", "time", "type", *TIMELINE_DETAIL_KEYS)


def _timeline_start() -> dict[str, Any]:
    # ``last_seq`` is the seq of the newest moment folded so far, or 0 before the
    # first. A crew log's seq is strictly increasing within one succession unit and
    # restarts at 1 in the next, so a moment at or below it is the mark that a SECOND
    # unit has begun -- which this fold refuses below rather than render as one
    # scrambled timeline.
    return {"moments": [], "dropped": 0, "last_seq": 0}


def _timeline_step(state: dict[str, Any], entry: Entry) -> None:
    if entry.type not in TIMELINE_TYPES:
        return
    if entry.seq <= state["last_seq"]:
        # A slot-wide read folds the units a slot ran under back to back, and each
        # unit's seq restarts at 1. One session holds exactly one unit, so the only
        # reader today never gets here; the day a slot-keyed reader folds this over a
        # slot, two units interleaved by wall clock carry a clock-rollback inversion.
        # Fail loudly at that boundary so the timeline is ordered by succession before
        # anyone orders it by bare timestamp.
        raise CrewLogError(
            f"timeline moment at seq {entry.seq} is at or below the fold's seq "
            f"{state['last_seq']}; the timeline fold spans one succession unit, not a "
            f"slot of them -- order the units by succession before folding",
            code=CODE_BAD_DATA,
            field="seq",
        )
    state["last_seq"] = entry.seq
    moment: dict[str, Any] = {"seq": entry.seq, "time": entry.time, "type": entry.type}
    data = entry.data
    for key in TIMELINE_DETAIL_KEYS:
        value = data.get(key)
        if isinstance(value, str):
            # Retained in a bounded window, so its SIZE is part of that bound.
            value = _as_str(value)
        if isinstance(value, (str, int, float, bool)) and value != "":
            moment[key] = value
    moments: list[dict[str, Any]] = state["moments"]
    moments.append(moment)
    if len(moments) > TIMELINE_LIMIT:
        # A window, and it says so: the count of moments cut off the front rides
        # in the value, so a reader is never shown a partial list that looks whole.
        state["dropped"] += len(moments) - TIMELINE_LIMIT
        del moments[: len(moments) - TIMELINE_LIMIT]


def _timeline_render(state: dict[str, Any]) -> dict[str, Any]:
    moments: list[dict[str, Any]] = state["moments"]
    return {
        "moments": [dict(moment) for moment in moments],
        "dropped": state["dropped"],
        "limit": TIMELINE_LIMIT,
        "first_seq": moments[0]["seq"] if moments else None,
        "last_seq": moments[-1]["seq"] if moments else None,
    }


# --------------------------------------------------------------------------- #
# tools
# --------------------------------------------------------------------------- #


def _tools_start() -> dict[str, Any]:
    return {
        "calls": 0,
        "completed": 0,
        "errors": 0,
        "unidentified_calls": 0,
        "unmatched_completions": 0,
        "elapsed_ms": 0,
        "by_name": {},
        "open": {},
        "open_omitted": 0,
        "names_omitted": 0,
        "names_omitted_saturated": False,
        "omitted_names": [],
    }


def _tool_row(state: dict[str, Any], name: str) -> dict[str, Any] | None:
    """The per-name row for *name*, or ``None`` when it gets no detail row.

    A name gets no row for either of two reasons -- the name budget is spent, or
    the name is too long to key safely -- and both take the same path: COUNTED in
    the totals and left out of the detail, so the aggregate a caller sums stays
    exact while the value stays bounded.
    """
    by_name: dict[str, Any] = state["by_name"]
    if _keyable(name):
        row = by_name.get(name)
        if row is not None:
            return row
        if len(by_name) < TOOL_NAME_LIMIT:
            row = {
                "calls": 0,
                "completed": 0,
                "errors": 0,
                "elapsed_ms": 0,
                "last_status": None,
                "last_time": None,
                "servers": [],
                "servers_over": [],
                "servers_omitted": 0,
                "servers_saturated": False,
            }
            by_name[name] = row
            return row
    _note_omitted(
        state,
        name,
        seen_key="omitted_names",
        count_key="names_omitted",
        saturated_key="names_omitted_saturated",
        budget=TOOL_NAME_LIMIT,
    )
    return None


def _tools_step(state: dict[str, Any], entry: Entry) -> None:
    data = entry.data
    if entry.type == "tool/called":
        state["calls"] += 1
        name = _as_str(data.get("name"))
        row = _tool_row(state, name)
        if row is not None:
            row["calls"] += 1
            row["last_time"] = entry.time
            server = _as_str(data.get("server"))
            if server:
                if server in row["servers"]:
                    pass  # already detailed for this tool
                elif _keyable(server) and len(row["servers"]) < SERVERS_PER_TOOL_LIMIT:
                    row["servers"].append(server)
                else:
                    # Count DISTINCT omitted servers, not repeated calls through
                    # one of them, and say so when the dedup list that makes the
                    # count exact is spent. A server at the cut length comes here
                    # too: it cannot be told apart from a cut one, so listing it
                    # would let it stand for a server it is not.
                    _note_omitted(
                        row,
                        server,
                        seen_key="servers_over",
                        count_key="servers_omitted",
                        saturated_key="servers_saturated",
                        budget=SERVERS_PER_TOOL_LIMIT,
                    )
        call_id = _as_id(data.get("call_id"))
        # An empty call_id is what the declaration allows when the frame carried
        # none, and it identifies NOTHING: keying the open-call map by it would
        # make every such call the same call, so one completion would close a
        # different call's frame. An id past ``ID_LIMIT`` is treated the same way,
        # because it is retained here and its size is part of the bound. Those are
        # counted and left unpaired.
        if call_id:
            if call_id in state["open"] or len(state["open"]) < OPEN_RETAIN_LIMIT:
                state["open"][call_id] = {
                    "call_id": call_id,
                    "name": name,
                    "turn": _as_int(data.get("turn")),
                    "time": entry.time,
                    "seq": entry.seq,
                }
            else:
                # The retained open-call map is full of never-matched ids. A
                # further distinct one is counted and dropped rather than kept,
                # so the checkpoint stays bounded; its later completion reads as
                # unmatched, which it effectively is.
                state["open_omitted"] += 1
        else:
            state["unidentified_calls"] += 1
    elif entry.type == "tool/completed":
        state["completed"] += 1
        name = _as_str(data.get("name"))
        status = _as_str(data.get("status"))
        # Two independent signals, and either one is an error: ``status`` is the
        # frame's own outcome, while ``is_error`` is tri-state and absent when the
        # caller asserted nothing -- so an absent one is not a claim that the call
        # worked.
        failed = status in {"refused", "error", "failed"} or data.get("is_error") is True
        if failed:
            state["errors"] += 1
        elapsed = _as_int(data.get("elapsed_ms"))
        state["elapsed_ms"] += elapsed
        row = _tool_row(state, name)
        if row is not None:
            row["completed"] += 1
            row["elapsed_ms"] += elapsed
            row["last_status"] = status
            row["last_time"] = entry.time
            if failed:
                row["errors"] += 1
        # The SAME coercion as the open side, so the two agree on what an identity
        # is. If a completion paired on a raw id while the call retained a coerced
        # one, an over-long id would look unmatched here and unbounded there.
        call_id = _as_id(data.get("call_id"))
        if call_id:
            if state["open"].pop(call_id, None) is None:
                state["unmatched_completions"] += 1


def _tools_render(state: dict[str, Any]) -> dict[str, Any]:
    open_calls = sorted(state["open"].values(), key=lambda call: call["seq"])
    return {
        "calls": state["calls"],
        "completed": state["completed"],
        "errors": state["errors"],
        # An unmatched call is reported OPEN, not completed with a guessed status.
        "open": len(open_calls),
        "open_calls": [dict(call) for call in open_calls[:OPEN_LIST_LIMIT]],
        "open_calls_omitted": max(0, len(open_calls) - OPEN_LIST_LIMIT),
        "open_dropped": state["open_omitted"],
        "unidentified_calls": state["unidentified_calls"],
        "unmatched_completions": state["unmatched_completions"],
        "elapsed_ms": state["elapsed_ms"],
        "by_name": {
            name: {
                "calls": row["calls"],
                "completed": row["completed"],
                "errors": row["errors"],
                "elapsed_ms": row["elapsed_ms"],
                "last_status": row["last_status"],
                "last_time": row["last_time"],
                "servers": list(row["servers"]),
                "servers_omitted": row["servers_omitted"],
                "servers_omitted_saturated": row["servers_saturated"],
            }
            for name, row in sorted(state["by_name"].items())
        },
        "names_omitted": state["names_omitted"],
        # True once the dedup budget is spent: ``names_omitted`` is then a floor,
        # not a total. Without this a reader cannot tell an exact count from a
        # stalled one.
        "names_omitted_saturated": state["names_omitted_saturated"],
    }


# --------------------------------------------------------------------------- #
# approvals
# --------------------------------------------------------------------------- #


def _approvals_start() -> dict[str, Any]:
    return {
        "requested": 0,
        "decided": 0,
        "unidentified_requests": 0,
        "unmatched_decisions": 0,
        "by_decision": {},
        "pending": {},
        "pending_omitted": 0,
        "last": None,
    }


def _approvals_step(state: dict[str, Any], entry: Entry) -> None:
    data = entry.data
    approval_id = _as_id(data.get("approval_id"))
    identified = bool(approval_id)
    if entry.type == "approval/requested":
        state["requested"] += 1
        if identified:
            if approval_id in state["pending"] or len(state["pending"]) < OPEN_RETAIN_LIMIT:
                state["pending"][approval_id] = {
                    "approval_id": approval_id,
                    "tool": _as_str(data.get("tool")),
                    "reason": _as_str(data.get("reason")),
                    "turn": _as_int(data.get("turn")),
                    "time": entry.time,
                    "seq": entry.seq,
                }
            else:
                # Retained pending map full of never-decided requests: count and
                # drop the further one so the checkpoint stays bounded. A later
                # decision for a dropped id reads as unmatched.
                state["pending_omitted"] += 1
        else:
            # Same rule as an empty tool call_id: an unidentified request cannot
            # be paired with a decision without pairing it with the wrong one.
            state["unidentified_requests"] += 1
    elif entry.type == "approval/decided":
        state["decided"] += 1
        decision = _as_str(data.get("decision"))
        state["by_decision"][decision] = state["by_decision"].get(decision, 0) + 1
        request = state["pending"].pop(approval_id, None) if identified else None
        if identified and request is None:
            state["unmatched_decisions"] += 1
        state["last"] = {
            "approval_id": approval_id if identified else "",
            "decision": decision,
            "by": _as_str(data.get("by")),
            "cause": _as_str(data.get("cause")),
            "tool": (request or {}).get("tool", ""),
            "turn": _as_int(data.get("turn")),
            "time": entry.time,
            "seq": entry.seq,
        }


def _approvals_render(state: dict[str, Any]) -> dict[str, Any]:
    pending = sorted(state["pending"].values(), key=lambda item: item["seq"])
    return {
        "requested": state["requested"],
        "decided": state["decided"],
        "pending": len(pending),
        "pending_requests": [dict(item) for item in pending[:OPEN_LIST_LIMIT]],
        "pending_omitted": max(0, len(pending) - OPEN_LIST_LIMIT),
        "pending_dropped": state["pending_omitted"],
        "unidentified_requests": state["unidentified_requests"],
        "unmatched_decisions": state["unmatched_decisions"],
        "by_decision": dict(sorted(state["by_decision"].items())),
        "last": dict(state["last"]) if state["last"] else None,
    }
