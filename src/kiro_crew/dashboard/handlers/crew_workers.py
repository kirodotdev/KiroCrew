"""A Crew Mode root's workers: list them, and type into one as the person.

``GET /api/chat/slots/{slot}/crew-workers`` lists every session the root dispatched,
directly or through another worker (``crew_mode.stamp_rows`` decides who is whose).
``POST /api/chat/slots/{slot}/crew-workers/{worker}/steer`` delivers the person's text
into one of them: it cuts into the worker's running turn when it can, and otherwise
queues for its next one, through the same delivery helpers the composer's own Steer
uses, so the worker's transcript shows it as the person's own message.

Both are the side panel's, so both take the gate the root session's own dashboard
read takes: app tokens are answered 404 and the caller must be the dashboard owner.
Neither can reach a session outside the named root's own tree: the worker must be
stamped with that root, and the root must be a live Crew Mode root.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any, NamedTuple

from aiohttp import web

from kiro_crew import crew_mode
from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request
from kiro_crew.dashboard.handlers.members import _deny_app_caller

logger = logging.getLogger(__name__)

#: Worker statuses, in the vocabulary the side panel shows.
STATUS_WORKING = "working"
STATUS_IDLE = "idle"
STATUS_NEEDS_APPROVAL = "needs_approval"
STATUS_CLOSED = "closed"


def _bad(code: str, message: str, status: int = 400) -> web.Response:
    return web.json_response({"error": message, "code": code}, status=status)


async def _gate(request: web.Request, operation: str) -> web.Response | None:
    denied = await _deny_app_caller(request, operation)
    if denied is not None:
        return denied
    return await require_owner_dashboard_request(request, operation)


def _stamped_rows(state: Any) -> list[dict[str, Any]]:
    """Every live slot's row, stamped with its Crew Mode root and worker fields."""
    return list(state.serialize_slots(dashboard_user=True))


def _root_row(rows: list[dict[str, Any]], root: str) -> dict[str, Any] | None:
    for row in rows:
        if row.get("key") == root and row.get(crew_mode.ROOT_FIELD):
            return row
    return None


def _status(row: Mapping[str, Any]) -> str:
    if row.get("pending_approval") or row.get("needs_input"):
        return STATUS_NEEDS_APPROVAL
    if row.get("running"):
        return STATUS_WORKING
    return STATUS_IDLE


#: How the side panel groups workers: what needs the person, what is moving, what
#: has not started, what is finished. The dashboard applies the same rule to live
#: slot rows (``lib/crewWorkers.ts``) for the session list's summary.
GROUP_NEEDS_YOU = "needs_you"
GROUP_WORKING = "working"
GROUP_WAITING = "waiting"
GROUP_DONE = "done"


class LedgerFacts(NamedTuple):
    """What a worker's ledger item on its root says: id, the card's line, and the
    item's own progress (``done``, ``needs_you`` or ``""`` when it says neither)."""

    item: str = ""
    line: str = ""
    progress: str = ""


#: Ledger facts for a worker with no item on its root's ledger.
NO_LEDGER = LedgerFacts()


def _group(status: str, progress: str = "") -> str:
    """The side panel group from real signals only.

    A tool approval or an open question card is ``needs_you``; a running turn is
    ``working``; a closed tab, a closed ledger item, or a worker that reported
    ``done`` is ``done``; a worker that reported it waits on a person (blocked or a
    question with ``reason`` approval or needs_human) is ``needs_you``. Anything
    else is idle, ``waiting``: having replied is not having finished (a question
    asked in plain text after its card timed out leaves a worker idle, not done).
    """
    if status == STATUS_NEEDS_APPROVAL:
        return GROUP_NEEDS_YOU
    if status == STATUS_WORKING:
        return GROUP_WORKING
    if status == STATUS_CLOSED or progress == GROUP_DONE:
        return GROUP_DONE
    if progress == GROUP_NEEDS_YOU:
        return GROUP_NEEDS_YOU
    return GROUP_WAITING


#: Report statuses that mean the worker waits on someone, and the reasons that
#: make that someone the person.
_WAITING_REPORTS = frozenset({"blocked", "question"})
_PERSON_REASONS = frozenset({"approval", "needs_human"})


def _progress_of(item: Any) -> str:
    """``done`` / ``needs_you`` / ``""`` from a ledger item's own fields."""
    if getattr(item, "is_terminal", False) or getattr(item, "status", None) == "done":
        return GROUP_DONE
    if (
        getattr(item, "status", None) in _WAITING_REPORTS
        and getattr(item, "reason", None) in _PERSON_REASONS
    ):
        return GROUP_NEEDS_YOU
    return ""


def _last_activity(row: Mapping[str, Any]) -> str:
    for field in ("last_turn_ts", "last_ts", "last_activity_ts", "created"):
        value = row.get(field)
        if isinstance(value, str) and value:
            return value
    return ""


def _closed_workers(root: str, live: set[str]) -> list[str]:
    """Slot keys the crew log says *root* dispatched whose tabs are gone.

    Read from the session tree's in-memory fold, which never touches disk, and only
    once it is seeded for the store in use: an unseeded tree folds nothing and would
    report no closed worker, which is what the list then shows.
    """
    try:
        from kiro_crew.crew_log.session_tree_projection import projection

        tree = projection()
        if not tree.seeded_for_current_store:
            return []
        nodes = tree.nodes()
    except Exception:
        logger.debug("crew workers: no session tree for closed workers", exc_info=True)
        return []
    closed: list[str] = []
    for slot, node in nodes.items():
        if slot in live or slot == root:
            continue
        seen = {slot}
        current: Any = node
        while current is not None and not current.cycle:
            parent = current.parent_slot
            if not parent or parent in seen:
                break
            if parent == root:
                closed.append(slot)
                break
            seen.add(parent)
            current = nodes.get(parent)
    return sorted(closed)


def _closed_titles(state: Any, keys: list[str]) -> dict[str, tuple[str, str]]:
    """Closed worker key -> (title, agent) from its saved session metadata. BLOCKING.

    A closed tab has no slot row, so its card would otherwise read "Untitled": the
    title and agent come from the conversation log's metadata for
    ``dashboard:<key>``, redacted like a live title. A key with no readable
    metadata falls back to the key's short form (``chat-3``) and no agent.
    """
    from kiro_crew.security import redact_credentials, redact_exfiltration_urls

    log = getattr(state, "conversation_log", None)
    out: dict[str, tuple[str, str]] = {}
    for key in keys:
        meta: Any = None
        if log is not None:
            try:
                meta = log.get_metadata(f"dashboard:{key}")
            except Exception:
                logger.debug("crew workers: no metadata for closed %s", key, exc_info=True)
        meta = meta if isinstance(meta, Mapping) else {}
        title = meta.get("title") if isinstance(meta.get("title"), str) else ""
        agent = meta.get("agent") if isinstance(meta.get("agent"), str) else ""
        if title:
            title, _ = redact_exfiltration_urls(title)
            title, _ = redact_credentials(title)
        out[key] = (title or _short_key(key), agent)
    return out


def _short_key(key: str) -> str:
    """``chat-3-1792540567`` -> ``chat-3``: the slot's number without its timestamp."""
    head, _, tail = key.rpartition("-")
    return head if head and tail.isdigit() and len(tail) >= 9 else key


def _ledger_items(root: str) -> dict[str, LedgerFacts]:
    """Worker slot key -> :class:`LedgerFacts` on *root*'s own ledger. BLOCKING.

    The line is the worker's latest report summary, else the item's title, through
    the board's egress redactor, as the board page shows them.

    Read through the board's seam (``work_ledger_board.task_records``), the one the
    store's identity boundary admits for gateway-owned readers: the root comes from
    server state, never from a request. Only the root's own ledger is read, so a
    worker bound by another worker's ledger shows no item. A root that owns no
    ledger has none to show.
    """
    from kiro_crew import session_ledger
    from kiro_crew.dashboard.handlers import work_ledger_board

    try:
        _record, items = work_ledger_board.task_records(session_ledger.ledger_key(root))
    except Exception:
        logger.debug("crew workers: no ledger for this root", exc_info=True)
        return {}
    out: dict[str, LedgerFacts] = {}
    for item in items:
        worker = session_ledger.ledger_key(item.worker_session_key or "")
        if worker and item.item_id:
            line = work_ledger_board._redact_deep({"line": item.summary or item.title}).get("line")
            out[worker] = LedgerFacts(
                item.item_id, _one_line(line if isinstance(line, str) else ""), _progress_of(item)
            )
    return out


#: The longest line a worker card shows; the card truncates it further to fit.
WORKER_LINE_MAX = 160


def _one_line(text: str) -> str:
    """*text*'s first non-empty line, markdown emphasis off, cut to the card's length."""
    for raw in text.splitlines():
        line = raw.strip().strip("*_`#> ").strip()
        if line:
            return line[:WORKER_LINE_MAX]
    return ""


def _worker_line(row: Mapping[str, Any], ledger_line: str) -> str:
    """What a worker card says under its title: its task line, else its last reply."""
    if ledger_line:
        return ledger_line
    last = row.get("last_message")
    return _one_line(last) if isinstance(last, str) else ""


async def api_crew_workers(request: web.Request) -> web.Response:
    """GET /api/chat/slots/{slot}/crew-workers -- one Crew Mode root's workers."""
    refused = await _gate(request, "crew_mode.workers")
    if refused is not None:
        return refused
    state = request.app["state"]
    root = request.match_info.get("slot", "")
    rows = _stamped_rows(state)
    if _root_row(rows, root) is None:
        return _bad("not_crew_mode_root", "this session is not a Crew Mode root", status=404)
    live = crew_mode.workers_of(rows, root)
    live_keys = {str(row.get("key")) for row in rows}
    closed = _closed_workers(root, live_keys)
    items = await asyncio.to_thread(_ledger_items, root)
    workers: list[dict[str, Any]] = []
    for row in live:
        key = str(row.get("key"))
        status = _status(row)
        facts = items.get(key, NO_LEDGER)
        item, line = facts.item, facts.line
        workers.append(
            {
                "key": key,
                "title": str(row.get("title") or ""),
                "agent": str(row.get("agent") or ""),
                "status": status,
                "group": _group(status, facts.progress),
                "last_activity": _last_activity(row),
                "ledger_item": item,
                "summary": _worker_line(row, line),
                "created_by": str(row.get("created_by") or ""),
            }
        )
    closed_meta = await asyncio.to_thread(_closed_titles, state, closed)
    for key in closed:
        item, line = items.get(key, NO_LEDGER)[:2]
        title, agent = closed_meta.get(key, ("", ""))
        workers.append(
            {
                "key": key,
                "title": title,
                "agent": agent,
                "status": STATUS_CLOSED,
                "group": GROUP_DONE,
                "last_activity": "",
                "ledger_item": item,
                "summary": line,
                "created_by": "",
            }
        )
    return web.json_response({"root": root, "workers": workers})


async def _window_rows(slot: Any) -> list[dict[str, Any]]:
    """*slot*'s loaded transcript window, loaded off the loop first.

    A list snapshot (one C-level copy of the references), not a copy of each row:
    ``derive_lane`` only reads the rows, and copying 10,000 dicts on the event loop
    per poll is the cost this avoids.
    """
    from kiro_crew.dashboard.state import load_window_off_loop

    await load_window_off_loop(slot)
    return list(slot.messages)


#: Where a lane's model name came from: what the backend says it served, what the
#: session picked, or the gateway's default for a session that picked none.
MODEL_SERVED = "served"
MODEL_CHOSEN = "chosen"
MODEL_DEFAULT = "default"


def _lane_model(row: Mapping[str, Any]) -> dict[str, str]:
    """The lane's model and its source. No I/O: the default is the live config's.

    ``served_model`` is the name the backend reported for the running session;
    ``model`` is the session's own pick. A session with neither runs the
    gateway's ``agent.model`` (``auto`` lets the backend choose, so the name the
    backend picked is then not recorded and the lane says it runs the default).
    """
    served = row.get("served_model")
    if isinstance(served, str) and served.strip():
        return {"model": served.strip(), "model_source": MODEL_SERVED}
    chosen = row.get("model")
    if isinstance(chosen, str) and chosen.strip():
        return {"model": chosen.strip(), "model_source": MODEL_CHOSEN}
    default = ""
    try:
        from kiro_crew.config import live

        cfg = live.snapshot()
        value = getattr(getattr(cfg, "agent", None), "model", "")
        default = value.strip() if isinstance(value, str) else ""
    except Exception:
        logger.debug("crew timeline: no live config for the default model", exc_info=True)
    return {"model": default, "model_source": MODEL_DEFAULT}


def _lane_head(row: Mapping[str, Any], role: str) -> dict[str, Any]:
    status = _status(row)
    return {
        "key": str(row.get("key")),
        "title": str(row.get("title") or ""),
        "agent": str(row.get("agent") or ""),
        **_lane_model(row),
        "role": role,
        "status": status,
        "running": bool(row.get("running")),
        "waiting": status == STATUS_NEEDS_APPROVAL,
    }


async def api_crew_timeline(request: web.Request) -> web.Response:
    """GET /api/chat/slots/{slot}/crew-timeline -- the root's and workers' lanes.

    One lane per session (the root first, then its live workers in list order,
    then closed ones with no bars), each drawn from that session's own transcript
    window by :func:`kiro_crew.crew_timeline.derive_lane`. ``gaps`` names what the
    transcript does not record, so the view can say it instead of drawing it.
    """
    refused = await _gate(request, "crew_mode.timeline")
    if refused is not None:
        return refused
    from datetime import datetime, timezone

    from kiro_crew import crew_timeline

    state = request.app["state"]
    root = request.match_info.get("slot", "")
    rows = _stamped_rows(state)
    root_row = _root_row(rows, root)
    if root_row is None:
        return _bad("not_crew_mode_root", "this session is not a Crew Mode root", status=404)
    heads = [_lane_head(root_row, "conductor")]
    heads.extend(_lane_head(row, "worker") for row in crew_mode.workers_of(rows, root))
    windows: list[list[dict[str, Any]]] = []
    for head in heads:
        slot = state.get_slot(head["key"])
        windows.append(await _window_rows(slot) if slot is not None else [])
    now = datetime.now(timezone.utc)

    def derive() -> list[dict[str, Any]]:
        from kiro_crew.dashboard.chat_utils_display import _redact_for_display

        return [
            crew_timeline.redact_labels(
                {
                    **head,
                    **crew_timeline.derive_lane(
                        window, now=now, running=head["running"], waiting_now=head["waiting"]
                    ),
                },
                _redact_for_display,
            )
            for head, window in zip(heads, windows)
        ]

    lanes = await asyncio.to_thread(derive)
    live_keys = {str(row.get("key")) for row in rows}
    lanes.extend(
        {
            "key": key,
            "title": "",
            "agent": "",
            "model": "",
            "model_source": MODEL_DEFAULT,
            "role": "worker",
            "status": STATUS_CLOSED,
            "running": False,
            "waiting": False,
            "segments": [],
            "marks": [],
            "seeds": [],
            "sends": [],
            "credits": 0.0,
            "first_ts": "",
            "last_ts": "",
            "dropped_segments": 0,
        }
        for key in _closed_workers(root, live_keys)
    )
    live = any(lane["running"] or lane["waiting"] for lane in lanes)
    return web.json_response(
        {
            "root": root,
            "now": now.isoformat(),
            "live": live,
            **crew_timeline.summarize(lanes, now=now, live=live),
            "lanes": lanes,
            "handoffs": crew_timeline.handoffs(lanes),
            # The served-model gap is named only when a lane actually runs on it.
            "gaps": [
                gap
                for gap in crew_timeline.GAPS
                if gap != crew_timeline.GAP_SERVED_MODEL
                or any(lane.get("model_source") == MODEL_DEFAULT for lane in lanes)
            ],
        }
    )


async def api_crew_worker_steer(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/crew-workers/{worker}/steer -- type into a worker.

    The text is the PERSON's: it reaches the worker as user text (no peer envelope),
    exactly as if typed into the worker's own composer with Steer on. A running turn
    takes it at once when the backend can steer; otherwise it waits for the worker's
    next turn, and an idle worker starts that turn now. A refusal names why.
    """
    refused = await _gate(request, "crew_mode.steer")
    if refused is not None:
        return refused
    state = request.app["state"]
    root = request.match_info.get("slot", "")
    worker = request.match_info.get("worker", "")
    try:
        body = await request.json()
    except Exception:
        return _bad("invalid_json", "the body must be a JSON object")
    message = body.get("message") if isinstance(body, dict) else None
    if not isinstance(message, str) or not message.strip():
        return _bad("message_empty", "message is empty")
    from kiro_crew.dashboard.session_control_parts.session_messaging import MAX_SEND_MESSAGE_CHARS

    if len(message) > MAX_SEND_MESSAGE_CHARS:
        return _bad("message_too_long", f"message exceeds {MAX_SEND_MESSAGE_CHARS} characters")
    rows = _stamped_rows(state)
    if _root_row(rows, root) is None:
        return _bad("not_crew_mode_root", "this session is not a Crew Mode root", status=404)
    if not any(row.get("key") == worker for row in crew_mode.workers_of(rows, root)):
        return _bad("not_a_worker", "that session is not one of this root's workers", status=404)
    slot = state.get_slot(worker)
    if slot is None:
        return _bad("worker_closed", "that worker's tab is closed", status=409)
    if getattr(slot, "_app", ""):
        return _bad("app_session", "an app's session is not steered from here", status=409)
    from kiro_crew.dashboard.relay_archive import (
        RELAY_ARCHIVE_CODE,
        RELAY_ARCHIVE_ERROR,
        is_relay_archive,
    )

    if is_relay_archive(slot):
        return _bad(RELAY_ARCHIVE_CODE, RELAY_ARCHIVE_ERROR, status=409)
    return web.json_response(await _deliver_as_person(state, slot, message))


async def _deliver_as_person(state: Any, slot: Any, message: str) -> dict[str, Any]:
    """Steer *message* into *slot*'s running turn, else queue it; the composer's helpers.

    ``user_origin=True`` and ``directive_user_origin=True`` are what the composer
    passes for its own human: this text is the dashboard owner's, typed into the side
    panel's view of the worker. The containment stamp is taken before any await, as
    the composer takes it, so the queue drain re-checks the audience it was admitted
    under.
    """
    from kiro_crew.dashboard.chat_delivery import (
        STEER_REQUEUED,
        STEER_STEERED,
        queue_for_next_turn,
        steer_into_running_turn,
    )
    from kiro_crew.dashboard.chat_runner import _start_next_queued_turn
    from kiro_crew.dashboard.session_control import containment_meta

    if slot.turn_running:
        outcome = await steer_into_running_turn(
            state,
            slot,
            message,
            user_origin=True,
            admission=containment_meta(state, slot),
        )
        if outcome == STEER_STEERED:
            return {"ok": True, "steered": True, "queued": False, "started": False}
        if outcome == STEER_REQUEUED:
            return {"ok": True, "steered": False, "queued": True, "started": False}
    queue_for_next_turn(state, slot, message, directive_user_origin=True, steer_intent=True)
    started = False
    if not slot.running:
        # The composer's own default: with the worker's sub-agents still running the
        # drain holds the text until they finish (``queue_drain``), and the reply
        # says it is queued rather than started.
        started = bool(await _start_next_queued_turn(state, slot))
    try:
        state.push_slots_update()
    except Exception:  # pragma: no cover - sidebar refresh is best-effort
        logger.debug("crew steer: push_slots_update failed", exc_info=True)
    return {"ok": True, "steered": False, "queued": not started, "started": started}
