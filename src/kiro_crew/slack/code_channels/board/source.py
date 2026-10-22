"""Where the work board's data comes from: the channel conductor's OWN ledger.

ONE READ PATH, ONE SCOPE CHECK. :func:`read_board` is the only reader. It asks
:func:`scope_admits` first -- the single call site the conductor-dispatch
exemption's scope predicate plugs into -- then reads the ledger of the session
the channel is bound to (its stored :class:`~..store.Binding`, never a caller's
argument) through ``handlers/work_ledger_board.own_worker_records``, the
permitted store seam, which keeps a worker key only when that worker's own
binding names this conductor and this item. Nothing here takes a session key
from anywhere else, so the board can show this conductor's own workers and no
other session.

LIVENESS IS JOINED HERE and reduced to a word (:mod:`.model`'s ``LIVE_*``): the
dashboard slot of each worker is read for a pending tool approval, a turn in
flight, a queue, or absence (``ledger_wake.worker_closed``, the same resolver the
conductor's wake gate uses). The board publishes the derived state, never the
slot itself.

PROJECTION TO THE PUBLISHED SET happens here too: each item becomes a
:class:`~.model.WorkerRow`, whose fields are exactly what the exemption admits
into the channel (see :mod:`.model`). PR and CI are filled in later by the
workspace refresh, which owns the forge reads.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Awaitable, Callable

from kiro_crew.slack.code_channels.board import model
from kiro_crew.slack.code_channels.store import Binding
from kiro_crew.validation import is_valid_followup_branch

logger = logging.getLogger(__name__)

#: ``(conductor_record, items)`` for a ledger key; the store seam's shape.
RecordsReader = Callable[[str], Awaitable["tuple[Any, list[Any]]"]]
#: Whether the channel's binding may have its conductor's ledger read and published.
ScopePredicate = Callable[[Any, Binding], bool]


async def _default_records(slot_key: str) -> tuple[Any, list[Any]]:
    from kiro_crew.dashboard.handlers.work_ledger_board import own_worker_records

    return await own_worker_records(slot_key)


def _bound_slot_is_live(state: Any, binding: Binding) -> bool:
    """The binding's session is the channel's live bound slot (the core's own test)."""
    from kiro_crew.slack.code_channels.binding import bound_slot

    slot = bound_slot(state, binding)
    if slot is None or getattr(slot, "key", "") != binding.session_key:
        return False
    linked = getattr(slot, "_slack_channel", None)
    return not (isinstance(linked, str) and linked and linked != binding.channel_id)


def _publish_refusal(state: Any, binding: Binding) -> str:
    """Why the channel may not be shown what its conductor's ledger holds; ``""`` if it may.

    The dispatch exemption's own publish clauses (``code_channel_exemption``): its
    switch, the channel's state (archived, stopped, workspace, shared) and ONE read
    of the slot's outbound binding naming exactly this channel. The board does
    not judge who DROVE a turn -- it is the gateway's publisher, not a turn -- so
    the exemption's sender clause, and its per-judgement audit row, stay with the
    gates that admit dispatch.
    """
    from kiro_crew.dashboard.session_control_parts.code_channel_exemption import (
        _publish_clauses,
    )
    from kiro_crew.slack.code_channels.binding import bound_slot

    why, _admission = _publish_clauses(state, bound_slot(state, binding), binding)
    return why


def _default_scope(state: Any, binding: Binding) -> bool:
    """THE board's scope predicate (one call site, :func:`scope_admits`).

    The channel's stored binding still names its live bound slot and is not
    archived -- the conductor the channel was opened for, reading its own
    ledger -- AND the dispatch exemption's publish clauses admit the channel as
    that ledger's audience. The worker half (each worker binds BACK to this
    conductor and item) is the store seam's, ``own_worker_records``.
    """
    return (
        bool(binding.session_key)
        and not binding.archived_at
        and _bound_slot_is_live(state, binding)
        and not _publish_refusal(state, binding)
    )


_scope: ScopePredicate = _default_scope
_records: RecordsReader = _default_records


def set_scope_predicate(predicate: ScopePredicate | None) -> None:
    """Install the scope predicate (the exemption's, or a test's); ``None`` restores."""
    global _scope
    _scope = predicate or _default_scope


def set_records_reader(reader: RecordsReader | None) -> None:
    """Install the ledger reader (tests); ``None`` restores the store seam."""
    global _records
    _records = reader or _default_records


def scope_admits(state: Any, binding: Binding) -> bool:
    """THE scope check: may this channel's conductor ledger reach the channel?"""
    try:
        return bool(_scope(state, binding))
    except Exception:
        logger.debug("code channel %s: board scope check failed", binding.channel_id)
        return False


def find_slot(state: Any, key: str) -> Any | None:
    if state is None or not key:
        return None
    for candidate in (key, f"dashboard_{key}"):
        try:
            slot = state.get_slot(candidate)
        except Exception:
            continue
        if slot is not None:
            return slot
    return None


def _approval(state: Any, slot: Any) -> tuple[bool, str]:
    """``(pending, kind)``: whether a tool approval waits on *slot*, and its ACP kind.

    Pending is the session-status verb's own answer (``_pending_approval_fields``).
    The kind is the slot projection's ``pending_approval_info["tool_kind"]`` --
    the ACP tool-call kind (``execute``, ``edit``, ``fetch`` ...) or ``spawn`` for
    a coordinator spawn approval -- which :func:`~.model.approval_words` maps to a
    FIXED phrase. The approval's title (``pending_approval_tool``) is not used:
    it is the transcript's permission row, a shell call's command line.
    """
    from kiro_crew.dashboard.session_control_parts.session_status import (
        _pending_approval_fields,
    )

    try:
        if not _pending_approval_fields(state, slot).get("pending_approval"):
            return False, ""
        info = slot.to_dict().get("pending_approval_info") or {}
    except Exception:
        return False, ""
    return True, str(info.get("tool_kind") or "") if isinstance(info, dict) else ""


def approval_activity(state: Any, worker_key: str) -> model.Activity:
    """The call a worker's pending tool approval asks for, as raw :class:`~.model.Activity`.

    The title is the session-status verb's ``pending_approval_tool`` (the
    transcript's permission row, e.g. ``Running: <command>``) and the kind the
    projection's ``pending_approval_info["tool_kind"]``. Never published as is:
    :func:`~.model.step_text` redacts it, takes host paths out and cuts it.
    """
    slot = find_slot(state, worker_key)
    if slot is None:
        return model.Activity()
    from kiro_crew.dashboard.session_control_parts.session_status import (
        _pending_approval_fields,
    )

    try:
        fields = _pending_approval_fields(state, slot)
        if not fields.get("pending_approval"):
            return model.Activity()
        info = slot.to_dict().get("pending_approval_info") or {}
    except Exception:
        return model.Activity()
    kind = str(info.get("tool_kind") or "") if isinstance(info, dict) else ""
    return model.Activity(
        project=str(getattr(slot, "project", "") or ""),
        tool_title=str(fields.get("pending_approval_tool") or ""),
        tool_kind=kind,
    )


def worker_live(state: Any, worker_key: str) -> tuple[str, str]:
    """``(word, approval kind)`` for a worker's session; the kind only while one is pending.

    The word is :mod:`.model`'s ``LIVE_*``. Running and gone are the conductor
    wake gate's own resolvers (``ledger_wake.worker_running`` /
    ``worker_closed``), so the board and the gate cannot disagree about a worker;
    a pending approval is the session-status verb's answer.
    """
    if not worker_key:
        return model.LIVE_UNBOUND, ""
    from kiro_crew import ledger_wake

    slot = find_slot(state, worker_key)
    if slot is None:
        # Absent AND not under construction: the same answer the wake gate reads.
        gone = ledger_wake.worker_closed(state, worker_key)
        return (model.LIVE_GONE if gone else model.LIVE_IDLE), ""
    pending, tool = _approval(state, slot)
    if pending:
        return model.LIVE_APPROVAL, tool
    if ledger_wake.worker_running(state, slot.key):
        return model.LIVE_RUNNING, ""
    if len(getattr(slot, "_queue", ()) or ()) > 0:
        return model.LIVE_QUEUED, ""
    return model.LIVE_IDLE, ""


def liveness(state: Any, worker_key: str) -> str:
    """One word for a worker's session (:func:`worker_live` without the tool)."""
    return worker_live(state, worker_key)[0]


#: Transcript roles that open a turn (the slot projection's prompt roles, plus a
#: monitor's wake).
_PROMPT_ROLES = frozenset({"user", "inject", "nudge"})


def worker_activity(state: Any, worker_key: str) -> model.Activity:
    """What a worker's RUNNING turn is doing now, from its slot's own transcript rows.

    The rows are the ones the dashboard's sidebar line and pill render: the turn's
    opening prompt row (its time is when the turn started) and the newest
    ``tool`` row when nothing came after it (the call still in flight). Read as
    raw facts; :func:`~.model.step_text` makes them publishable. An idle or
    unknown session answers an empty :class:`~.model.Activity`.
    """
    slot = find_slot(state, worker_key)
    if slot is None or not getattr(slot, "turn_running", False):
        return model.Activity()
    try:
        from kiro_crew.dashboard.slot_projection import summary_rows

        rows = list(summary_rows(slot) or ())
    except Exception:
        return model.Activity()
    started = ""
    tool: dict[str, Any] | None = None
    for index in range(len(rows) - 1, -1, -1):
        row = rows[index] if isinstance(rows[index], dict) else {}
        role = row.get("role")
        if role in _PROMPT_ROLES:
            started = str(row.get("ts") or "")
            break
        if role == "tool" and index == len(rows) - 1:
            tool = row
    if tool is None:
        return model.Activity(
            turn_started_at=started, project=str(getattr(slot, "project", "") or "")
        )
    raw_meta = tool.get("meta")
    meta: dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
    return model.Activity(
        turn_started_at=started,
        project=str(getattr(slot, "project", "") or ""),
        tool_title=str(tool.get("content") or ""),
        tool_kind=str(meta.get("kind") or ""),
        tool_input=str(meta.get("input") or ""),
        tool_name=str(meta.get("tool_name") or ""),
    )


def _stamp(raw: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(raw or "").replace("Z", "+00:00")) if raw else None
    except ValueError:
        return None


def _handed_back(item: Any) -> bool:
    """A ``decide`` at or after a ``blocked`` / ``question`` report answered it.

    The ledger's own rule (``work_ledger._decided_after_report``): only an asking
    report is handed back, and a decide in the report's own second counts. A
    decide after a ``done`` report hands nothing back.
    """
    if item.status not in {"blocked", "question"}:
        return False
    decided, reported = _stamp(getattr(item, "decided_at", None)), _stamp(item.last_report_at)
    try:
        return bool(decided and reported and decided >= reported)
    except TypeError:  # one naive, one aware: cannot be ordered, so not handed back
        return False


def _fresh_progress(item: Any, turn_started_at: str) -> str:
    """The worker's ``progress`` summary when it was reported during this turn, else ``""``."""
    if item.status != "progress" or not item.summary:
        return ""
    reported, started = _stamp(item.last_report_at), _stamp(turn_started_at)
    try:
        if reported is None or (started is not None and reported < started):
            return ""
    except TypeError:
        return ""
    return str(item.summary)


def project(
    record: Any,
    items: list[Any],
    live_of: Callable[[str], str],
    tool_of: Callable[[str], str] | None = None,
    activity_of: Callable[[str], model.Activity] | None = None,
    approval_of: Callable[[str], model.Activity] | None = None,
) -> model.BoardSnapshot:
    """The published snapshot: EVERY ledger item reduced to a :class:`~.model.WorkerRow`.

    *tool_of* gives the ACP kind of a worker's pending approval (``""`` when it
    is not known), read only for a row that waits on that approval and published
    only as :func:`~.model.approval_words`' fixed phrase. *activity_of* gives a
    working row's running turn, published only as :func:`~.model.step_text`.
    """
    rows: list[model.WorkerRow] = []
    ordered = sorted(items, key=lambda i: (str(getattr(i, "created_at", "") or ""), i.item_id))
    for item in ordered:
        worker = str(item.worker_session_key or "")
        live = live_of(worker)
        item_state = str(item.state or "open")
        state = model.row_state(
            item_state=item_state,
            status=item.status,
            reason=item.reason,
            live=live,
            verdict=getattr(item, "verdict", None),
            handed_back=_handed_back(item),
        )
        artifacts = item.artifacts if isinstance(item.artifacts, dict) else {}
        branch = str(artifacts.get("branch") or "").strip()
        waiting_on = ""
        approval_tool = ""
        if state == model.WAITING_ON_YOU:
            waiting_on = "approval" if live == model.LIVE_APPROVAL else "report"
        if waiting_on == "approval" and tool_of is not None:
            approval_tool = model.approval_words(tool_of(worker))
        step, turn_started_at = "", ""
        if state == model.WORKING and activity_of is not None:
            activity = activity_of(worker)
            turn_started_at = activity.turn_started_at
            step = model.step_text(activity, report=_fresh_progress(item, turn_started_at))
        elif waiting_on == "approval" and approval_of is not None:
            asked = approval_of(worker)
            step = model.step_text(asked) if model.tool_step(asked) else ""
        elif state == model.IDLE and item.status == "progress" and item.summary:
            step = model.safe_line(str(item.summary), first_line=True)
        rows.append(
            model.WorkerRow(
                item_id=str(item.item_id),
                title=model.safe_line(
                    str(item.title or ""), limit=model.TITLE_CHARS, first_line=True
                )
                or "Untitled",
                state=state,
                summary=model.safe_line(
                    str(item.summary or ""), limit=model.SUMMARY_CHARS, first_line=True
                ),
                branch=branch if branch and is_valid_followup_branch(branch) else "",
                pr_number=int(item.pr) if isinstance(item.pr, int) and item.pr > 0 else 0,
                session_key=worker,
                report_stamp=str(item.last_report_at or ""),
                round=int(getattr(item, "round", 0) or 0),
                waiting_on=waiting_on,
                approval_tool=approval_tool,
                closed_as=item_state if state == model.CLOSED else "",
                step=step,
                turn_started_at=turn_started_at,
            )
        )
    goal = model.safe_line(
        str(getattr(record, "goal", "") or ""), limit=model.GOAL_CHARS, first_line=True
    )
    return model.BoardSnapshot(
        goal=goal, round=int(getattr(record, "round", 0) or 0), rows=tuple(rows)
    )


async def read_board(state: Any, binding: Binding) -> model.BoardSnapshot | None:
    """The channel's board, or ``None`` when its conductor keeps no ledger (or out of scope)."""
    if not scope_admits(state, binding):
        return None
    from kiro_crew import session_ledger

    key = session_ledger.ledger_key(binding.session_key)
    try:
        record, items = await _records(key)
    except Exception:
        logger.debug("code channel %s: ledger unreadable for the board", binding.channel_id)
        return None
    if record is None:
        return None
    # The read crossed a thread hop: the binding may have been retargeted meanwhile.
    if not scope_admits(state, binding):
        return None
    # One live read per worker: the word and, while pending, the approval's kind.
    seen: dict[str, tuple[str, str]] = {}

    def live(key: str) -> tuple[str, str]:
        if key not in seen:
            seen[key] = worker_live(state, key)
        return seen[key]

    return project(
        record,
        list(items),
        lambda key: live(key)[0],
        tool_of=lambda key: live(key)[1],
        activity_of=lambda key: worker_activity(state, key),
        approval_of=lambda key: approval_activity(state, key),
    )
