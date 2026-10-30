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
stamped with that root. The list and the timeline also answer a CLOSED known root
(``crew_mode.known_roots``) from saved data, so a reload under a closed conductor
still reads; steer and the approval card need a live Crew Mode root.

A queued steer is recorded when the drain delivers it, on either route
(``chat_send.mark_crew_steer``); a delivered one at once.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Mapping
from typing import Any, NamedTuple

from aiohttp import web

from kiro_crew import crew_mode
from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request
from kiro_crew.dashboard.handlers.members import _deny_app_caller
from kiro_crew.dashboard.slot_projection import summary_rows

logger = logging.getLogger(__name__)

#: Worker statuses, in the vocabulary the side panel shows.
STATUS_WORKING = "working"
STATUS_IDLE = "idle"
STATUS_NEEDS_APPROVAL = "needs_approval"
#: A worker whose question card is open (one from before it was adopted); it
#: waits on its conductor like a question report.
STATUS_ASKING = "asking"
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
    if row.get("pending_approval"):
        return STATUS_NEEDS_APPROVAL
    if row.get("needs_input"):
        return STATUS_ASKING
    if row.get("running"):
        return STATUS_WORKING
    return STATUS_IDLE


#: How the side panel groups workers. A worker never asks the person itself: its
#: questions and blockers go to its conductor (``with_conductor``), which answers or
#: asks the person in its own chat. The one thing a conductor cannot answer is a
#: tool approval (``approval``), which the person resolves on the conductor's
#: approval card. Then what is moving, what has not started, what is finished. The
#: dashboard applies the same rule to live slot rows (``lib/crewWorkers.ts``).
GROUP_APPROVAL = "approval"
GROUP_WORKING = "working"
GROUP_WITH_CONDUCTOR = "with_conductor"
GROUP_WAITING = "waiting"
GROUP_DONE = "done"


class LedgerFacts(NamedTuple):
    """What a worker's ledger item on its root says: id, the card's line, and the
    item's own progress (``done``, ``with_conductor`` or ``""`` when it says neither)."""

    item: str = ""
    line: str = ""
    progress: str = ""


#: Ledger facts for a worker with no item on its root's ledger.
NO_LEDGER = LedgerFacts()


def _group(status: str, progress: str = "") -> str:
    """The side panel group from real signals only.

    A tool approval is ``approval``; a running turn is ``working``; a closed tab, a
    closed ledger item, or a worker that reported ``done`` is ``done``; a worker
    that reported ``blocked`` or ``question`` (whatever its ``reason``), or one with
    an open question card, waits on its conductor: ``with_conductor``. Anything else
    is idle, ``waiting``: having replied is not having finished.
    """
    if status == STATUS_NEEDS_APPROVAL:
        return GROUP_APPROVAL
    if status == STATUS_WORKING:
        return GROUP_WORKING
    if status == STATUS_CLOSED or progress == GROUP_DONE:
        return GROUP_DONE
    if status == STATUS_ASKING or progress == GROUP_WITH_CONDUCTOR:
        return GROUP_WITH_CONDUCTOR
    return GROUP_WAITING


#: Report statuses that mean the worker waits on its conductor.
_WAITING_REPORTS = frozenset({"blocked", "question"})


def _progress_of(item: Any) -> str:
    """``done`` / ``with_conductor`` / ``""`` from a ledger item's own fields.

    A ``blocked`` or ``question`` report is the conductor's to answer whatever its
    ``reason`` says: a ``needs_human`` reason tells the conductor to ask the person
    in its own chat, never the side panel to show the worker as needing them.
    """
    if getattr(item, "is_terminal", False) or getattr(item, "status", None) == "done":
        return GROUP_DONE
    if getattr(item, "status", None) in _WAITING_REPORTS:
        return GROUP_WITH_CONDUCTOR
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


#: Rows read from a closed worker's saved transcript for its lane: the newest ones,
#: bounded like a live window, so a long-closed worker costs one bounded tail read.
CLOSED_LANE_ROWS = 2000


def _closed_rows(state: Any, key: str) -> list[dict[str, Any]] | None:
    """A closed worker's newest saved transcript rows, or ``None``. BLOCKING.

    ``None`` means no record: no conversation log, a read that failed, or a
    transcript with no row left (deleted or never written).
    """
    log = getattr(state, "conversation_log", None)
    if log is None:
        return None
    try:
        rows = log.read_messages_chained_tail(f"dashboard:{key}", CLOSED_LANE_ROWS)
    except Exception:
        logger.debug("crew timeline: no transcript for closed %s", key, exc_info=True)
        return None
    rows = [row for row in rows if isinstance(row, dict)]
    return rows or None


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
    from kiro_crew.work_ledger import TERMINAL_ITEM_STATES

    out: dict[str, LedgerFacts] = {}
    open_for: set[str] = set()
    for item in items:
        worker = session_ledger.ledger_key(item.worker_session_key or "")
        if worker and item.item_id:
            # A worker with an old closed item and a new open one is the open one's.
            is_open = getattr(item, "state", "") not in TERMINAL_ITEM_STATES
            if worker in open_for and not is_open:
                continue
            if is_open:
                open_for.add(worker)
            line = work_ledger_board._redact_deep({"line": item.summary or item.title}).get("line")
            out[worker] = LedgerFacts(
                item.item_id, _one_line(line if isinstance(line, str) else ""), _progress_of(item)
            )
    return out


def _crew_ledger_items(root: str, members: Iterable[str]) -> dict[str, LedgerFacts]:
    """:func:`_ledger_items` for *root*'s whole crew. BLOCKING.

    *root*'s own ledger first, then each crew member's (a sub-conductor binds the
    sessions it dispatched on its own ledger), so a grandchild's report and item
    show where its conductor recorded them. Only ledgers owned by *root* or by a
    session in its crew (*members*, from the server's own stamps and lineage) are
    read, and an entry on *root*'s ledger wins over one further down.
    """
    out = dict(_ledger_items(root))
    for key in dict.fromkeys(members):
        if key and key != root:
            for worker, facts in _ledger_items(key).items():
                out.setdefault(worker, facts)
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


#: How a message another session delivered opens (``session_messaging``): the
#: relay's provenance line, or the server's defanged copy of a forged one.
_SEED_PREFIXES = ("[sent by session ", "[quoted: sent by session ")


def _is_seed(text: str) -> bool:
    return text.lstrip().startswith(_SEED_PREFIXES)


def _last_reply(slot: Any) -> str:
    """The newest assistant line in *slot*'s transcript, ``""`` when it has none.

    The live window holds raw content, so the line goes through the same display
    redaction as the sidebar preview: its first line, markdown stripped, then
    redacted.
    """
    # circular import: chat_utils_display imports dashboard.state.
    from kiro_crew.dashboard.chat_utils_display import _redacted_preview

    for message in reversed(summary_rows(slot) if slot is not None else []):
        if isinstance(message, Mapping) and message.get("role") == "assistant":
            text = message.get("content")
            if isinstance(text, str) and text.strip():
                # The whole first line: _redacted_preview strips markdown,
                # redacts, and only then cuts, so a cut never splits a secret.
                first = next(line for line in text.splitlines() if line.strip())
                return _redacted_preview(first.strip(), WORKER_LINE_MAX)
    return ""


def _worker_line(row: Mapping[str, Any], ledger_line: str, slot: Any = None) -> str:
    """What a worker card says under its title: its task line, else its last reply.

    A message another session delivered (its seed or a steer) is not the worker's
    own line, so its newest assistant line stands in for it.
    """
    if ledger_line:
        return ledger_line
    last = row.get("last_message")
    if not isinstance(last, str):
        return ""
    if _is_seed(last):
        return _last_reply(slot)
    return _one_line(last)


async def api_crew_workers(request: web.Request) -> web.Response:
    """GET /api/chat/slots/{slot}/crew-workers -- one Crew Mode root's workers."""
    refused = await _gate(request, "crew_mode.workers")
    if refused is not None:
        return refused
    state = request.app["state"]
    root = request.match_info.get("slot", "")
    rows = _stamped_rows(state)
    root_closed = False
    if _root_row(rows, root) is None:
        # A closed root (its tab gone, its saved session a known Crew Mode root)
        # still has workers: the list is read from saved data, and says so, so a
        # worker page reloaded under it keeps the root's name and the real state.
        if root not in crew_mode.known_roots() or root in (getattr(state, "_slots", None) or {}):
            return _bad("not_crew_mode_root", "this session is not a Crew Mode root", status=404)
        root_closed = True
    live = crew_mode.workers_of(rows, root)
    live_keys = {str(row.get("key")) for row in rows}
    closed = _closed_workers(root, live_keys)
    items = await asyncio.to_thread(
        _crew_ledger_items, root, [*(str(row.get("key")) for row in live), *closed]
    )
    workers: list[dict[str, Any]] = []
    get_slot = getattr(state, "get_slot", None)
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
                "summary": _worker_line(row, line, get_slot(key) if callable(get_slot) else None),
                "created_by": str(row.get("created_by") or ""),
                "dispatched_by": str(row.get(crew_mode.DISPATCHER_FIELD) or ""),
                "approval": _approval_of(row),
            }
        )
    closed_meta = await asyncio.to_thread(
        _closed_titles, state, [*closed, root] if root_closed else closed
    )
    parents = crew_mode.persisted_parents() if closed else {}
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
                "dispatched_by": parents.get(key, "") or "",
            }
        )
    out: dict[str, Any] = {"root": root, "workers": workers}
    if root_closed:
        out["root_closed"] = True
        out["root_title"] = closed_meta.get(root, ("", ""))[0]
    return web.json_response(out)


#: The longest tool input an approval card shows: enough to read a command whole,
#: short enough that one worker's argument blob cannot flood the conductor's pane.
APPROVAL_INPUT_MAX_CHARS = 2000
#: The longest tool name and purpose line an approval card shows.
APPROVAL_LINE_MAX_CHARS = 200


def _approval_of(row: Mapping[str, Any]) -> dict[str, str] | None:
    """The worker's pending tool approval for the conductor's card, or ``None``.

    From the row's ``pending_approval_info`` (``slot_projection``), which names the
    request the worker's own permission card shows. Every text field is redacted
    again and then cut, so a cut can never leave a credential's prefix behind.
    """
    from kiro_crew.security import redact

    info = row.get("pending_approval_info")
    if not row.get("pending_approval") or not isinstance(info, Mapping):
        return None
    request_id = info.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        return None

    def text(field: str, cap: int) -> str:
        value = info.get(field)
        return redact(value)[:cap] if isinstance(value, str) else ""

    out = {
        "origin": "native" if info.get("origin") == "native" else "coordinator",
        "request_id": request_id[:APPROVAL_LINE_MAX_CHARS],
        "request_mid": text("request_mid", APPROVAL_LINE_MAX_CHARS),
        "tool": text("tool", APPROVAL_LINE_MAX_CHARS),
        "tool_input": text("tool_input", APPROVAL_INPUT_MAX_CHARS),
        "tool_purpose": text("tool_purpose", APPROVAL_LINE_MAX_CHARS),
        # A coordinator record's own instance (a uuid hex, not user text), so the
        # card's decision is pinned to that record like a native one's mid.
        "request_instance": _instance_of(info.get("request_instance")),
    }
    if info.get("trust_grantable") == "1":
        out["trust_grantable"] = "1"
    raw_input = info.get("tool_input")
    if isinstance(raw_input, str) and len(redact(raw_input)) > APPROVAL_INPUT_MAX_CHARS:
        out["tool_input_cut"] = "1"
    return out


def _instance_of(value: Any) -> str:
    """*value* when it is a coordinator record instance (hex, <= 64), else ``""``."""
    if isinstance(value, str) and 0 < len(value) <= 64:
        if all(ch in "0123456789abcdef" for ch in value):
            return value
    return ""


#: The three decisions the conductor's approval card offers, as the per-slot
#: approve route spells them. Nothing wider (no command or base grants, no YOLO).
_CARD_ACTIONS = {"approve": "approved", "reject": "rejected_once", "trust": "trust"}


async def api_crew_worker_approve(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/crew-workers/{worker}/approve -- the approval card.

    Body: ``{action: approve | reject | trust, request_id, request_mid?}``. The
    person decides one of *worker*'s pending tool approvals from its conductor's
    pane. Owner only, app tokens answered 404 (the side panel's gate); the worker
    must be one of this live root's workers. The decision itself is the existing
    per-slot approve route's, resolved on the WORKER slot: Approve and Reject are
    one-time and pinned to the request the card showed; Trust this worker is that
    route's ``trust`` for the worker's own slot (in memory, gone on a restart).
    """
    refused = await _gate(request, "crew_mode.approve")
    if refused is not None:
        return refused
    state = request.app["state"]
    root = request.match_info.get("slot", "")
    worker = request.match_info.get("worker", "")
    try:
        body = await request.json()
    except Exception:
        return _bad("invalid_json", "the body must be a JSON object")
    if not isinstance(body, dict):
        return _bad("invalid_json", "the body must be a JSON object")
    action = _CARD_ACTIONS.get(str(body.get("action") or ""))
    request_id = body.get("request_id")
    request_mid = body.get("request_mid")
    if action is None:
        return _bad("invalid_action", "action must be approve, reject or trust")
    if not isinstance(request_id, str) or not request_id:
        return _bad("missing_request_id", "request_id is required")
    rows = _stamped_rows(state)
    if _root_row(rows, root) is None:
        return _bad("not_crew_mode_root", "this session is not a Crew Mode root", status=404)
    if not any(row.get("key") == worker for row in crew_mode.workers_of(rows, root)):
        return _bad(
            "not_a_worker", "that session is not one of this conductor's crew members", status=404
        )
    slot = state.get_slot(worker)
    if slot is None:
        return _bad("worker_closed", "that crew member's tab is closed", status=409)
    # The slot approve route reads the worker's pending cards from its transcript.
    refused = await _load_worker_window(state, root, worker, slot)
    if refused is not None:
        return refused
    coordinator = state._pending_approvals.get(request_id)
    if isinstance(coordinator, dict) and coordinator.get("slot") == worker:
        # A coordinator approval (e.g. a spawn) has no transcript row and no mid:
        # pin it to the record instance the card showed, the same target the
        # normal approvals route requires, so a stale card cannot decide a
        # same-id record that replaced it. Trust is not offered for these.
        if action == "trust":
            return _bad("trust_not_grantable", "this request cannot be trusted from the card")
        instance = _instance_of(body.get("request_instance"))
        if not instance:
            return _bad("approval_target_required", "request_instance is required for this request")
        if coordinator.get("instance") != instance or not state.resolve_state_approval(
            request_id, action == "approved"
        ):
            return _bad("approval_not_pending", "that request is no longer pending", status=404)
        _audit_card(root, worker, action, ok=True)
        return web.json_response({"ok": True})
    if action != "trust" and not (isinstance(request_mid, str) and request_mid):
        # Never a bare-id decide from the card: it names the request it means.
        return _bad("approval_target_required", "request_mid is required for this request")
    decided: dict[str, Any] = {"action": action, "request_id": request_id}
    if action != "trust" and isinstance(request_mid, str) and request_mid:
        # Pinned to the exact request the card showed: a stale card cannot pick a
        # same-id request that replaced it.
        decided.update(origin="native", request_mid=request_mid)
    from kiro_crew.dashboard import chat_handlers

    request["kirocrew_crew_worker_approve"] = (worker, decided)
    resp = await chat_handlers.api_chat_slot_approve(request)
    _audit_card(root, worker, action, ok=resp.status < 300)
    return resp


def _audit_card(root: str, worker: str, action: str, *, ok: bool) -> None:
    """One SEL line per approval-card decision."""
    try:
        from kiro_crew.sel import sel

        sel().log_api_access(
            caller=f"dashboard:{root}",
            operation="crew_mode.approve",
            outcome="ok" if ok else "denied",
            source="dashboard",
            resources=f"worker={worker} action={action}",
        )
    except Exception:
        logger.debug("crew approve: audit failed", exc_info=True)


async def _load_window(slot: Any) -> None:
    """Load *slot*'s transcript window off the event loop (the state facade's loader)."""
    from kiro_crew.dashboard.state import load_window_off_loop

    await load_window_off_loop(slot)


async def _load_worker_window(state: Any, root: str, worker: str, slot: Any) -> web.Response | None:
    """Load *worker*'s transcript window off the loop before an action reads it.

    The route middleware loads only the conductor the path names; a worker restored
    after a restart still has its window pending, and reading it on the loop would
    stall every chat. The load awaits, so the tab and the crew are checked again
    after it: ``None`` when *slot* is still *root*'s live worker, else the refusal.
    """
    await _load_window(slot)
    if state.get_slot(worker) is not slot:
        return _bad("worker_closed", "that crew member's tab is closed", status=409)
    if not any(
        row.get("key") == worker for row in crew_mode.workers_of(_stamped_rows(state), root)
    ):
        return _bad(
            "not_a_worker", "that session is not one of this conductor's crew members", status=404
        )
    return None


async def _window_rows(slot: Any) -> list[dict[str, Any]]:
    """*slot*'s loaded transcript window, loaded off the loop first.

    A list snapshot (one C-level copy of the references), not a copy of each row:
    ``derive_lane`` only reads the rows, and copying 10,000 dicts on the event loop
    per poll is the cost this avoids.
    """
    await _load_window(slot)
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
        "waiting": status in (STATUS_NEEDS_APPROVAL, STATUS_ASKING),
    }


async def api_crew_timeline(request: web.Request) -> web.Response:
    """GET /api/chat/slots/{slot}/crew-timeline -- the root's and workers' lanes.

    One lane per session (the root first, then its live workers in list order,
    then closed ones), each drawn from that session's own transcript window by
    :func:`kiro_crew.crew_timeline.derive_lane`. A closed worker carries the saved
    title the worker list shows and is drawn from the newest rows of its saved
    transcript; ``record`` is false when none can be read. A CLOSED known root is
    answered the same way, its own lane drawn from its saved transcript, so the
    Timeline shows what the List shows under a closed conductor. ``gaps`` names
    what the transcript does not record, so the view can say it instead of
    drawing it.
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
    root_closed = False
    if root_row is None:
        # A closed root answers like the worker list does (``api_crew_workers``):
        # its own lane is drawn from its saved transcript, so the Timeline under a
        # closed conductor shows the same sessions the List does.
        if root not in crew_mode.known_roots() or root in (getattr(state, "_slots", None) or {}):
            return _bad("not_crew_mode_root", "this session is not a Crew Mode root", status=404)
        root_closed = True
    heads = [] if root_row is None else [_lane_head(root_row, "conductor")]
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
    # Each lane's side panel group, by the Workers list's own rule, so the header
    # says "Finished" only when no worker is working or waiting.
    live_keys = {str(row.get("key")) for row in rows}
    closed = _closed_workers(root, live_keys)
    items = await asyncio.to_thread(
        _crew_ledger_items, root, [*(str(lane["key"]) for lane in lanes), *closed]
    )
    for lane in lanes:
        lane["group"] = _group(lane["status"], items.get(lane["key"], NO_LEDGER).progress)

    def closed_lanes(keys: list[str], role: str) -> list[dict[str, Any]]:
        from kiro_crew.dashboard.chat_utils_display import _redact_for_display

        names = _closed_titles(state, keys)
        out: list[dict[str, Any]] = []
        for key in keys:
            title, agent = names.get(key, (_short_key(key), ""))
            saved = _closed_rows(state, key)
            # Drawn from its saved transcript like a live lane; with none left the
            # lane says so instead of drawing an empty track.
            drawn = crew_timeline.derive_lane(
                saved or [], now=now, running=False, waiting_now=False
            )
            out.append(
                crew_timeline.redact_labels(
                    {
                        "key": key,
                        "title": title,
                        "agent": agent,
                        "model": "",
                        "model_source": MODEL_DEFAULT,
                        "role": role,
                        "status": STATUS_CLOSED,
                        "group": GROUP_DONE,
                        "running": False,
                        "waiting": False,
                        "record": saved is not None,
                        **drawn,
                    },
                    _redact_for_display,
                )
            )
        return out

    if root_closed:
        # The closed conductor's lane comes first, as a live one does.
        lanes[:0] = await asyncio.to_thread(closed_lanes, [root], "conductor")
    lanes.extend(await asyncio.to_thread(closed_lanes, closed, "worker"))
    no_record = any(lane.get("record") is False for lane in lanes)
    live = any(lane["running"] or lane["waiting"] for lane in lanes)
    # Off the event loop like the lane derivation: pairing scans every seed
    # against its sender's sends, which grows with a long-running crew.
    handoffs = await asyncio.to_thread(crew_timeline.handoffs, lanes)
    for lane in lanes:
        # Raw tool arguments and envelope fields, read only to pair the hand-offs
        # above: they are model output, so they never leave in the reply.
        lane.pop("sends", None)
        lane.pop("seeds", None)
    return web.json_response(
        {
            "root": root,
            "now": now.isoformat(),
            "live": live,
            **crew_timeline.summarize(lanes, now=now, live=live),
            "lanes": lanes,
            "handoffs": handoffs,
            # The served-model gap is named only when a lane actually runs on it,
            # and the closed-worker gap only when a closed lane has no record left.
            "gaps": [
                gap
                for gap in crew_timeline.GAPS
                if (
                    gap != crew_timeline.GAP_SERVED_MODEL
                    or any(lane.get("model_source") == MODEL_DEFAULT for lane in lanes)
                )
                and (gap != crew_timeline.GAP_CLOSED_NO_RECORD or no_record)
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
        return _bad(
            "not_a_worker", "that session is not one of this conductor's crew members", status=404
        )
    slot = state.get_slot(worker)
    if slot is None:
        return _bad("worker_closed", "that crew member's tab is closed", status=409)
    if getattr(slot, "_app", ""):
        return _bad("app_session", "an app's session is not steered from here", status=409)
    from kiro_crew.dashboard.relay_archive import (
        RELAY_ARCHIVE_CODE,
        RELAY_ARCHIVE_ERROR,
        is_relay_archive,
    )

    if is_relay_archive(slot):
        return _bad(RELAY_ARCHIVE_CODE, RELAY_ARCHIVE_ERROR, status=409)
    # Delivery can start the worker's next turn, which reads its transcript.
    refused = await _load_worker_window(state, root, worker, slot)
    if refused is not None:
        return refused
    rows = _stamped_rows(state)
    delivered = await _deliver_as_person(state, slot, message, root=root, worker=worker)
    queue_id = delivered.pop("queue_id", "")
    if delivered.pop("requeued", False):
        # Requeued by the turn's teardown: its hold is on the entry, recorded at delivery.
        delivered.update(ledger_event=None, ledger_error=None, ledger_pending=True)
        return web.json_response(delivered)
    if delivered.get("steered"):
        # In the running turn, not read yet: recorded when the turn reads it.
        delivered.update(ledger_event=None, ledger_error=None, ledger_pending=True)
        return web.json_response(delivered)
    if queue_id and not delivered.get("started"):
        # Queued, not delivered: like the worker's own composer (``chat_send.api_chat``)
        # the steer is recorded when the drain hands the entry to the worker, with
        # its text as edited then, and never when the person cancels it first.
        from kiro_crew.dashboard.chat_handlers import mark_crew_steer

        entry = next(
            (e for e in (getattr(slot, "_queue", None) or []) if e.get("id") == queue_id),
            None,
        )
        if entry is not None:
            mark_crew_steer(entry, root, worker)
            delivered.update(ledger_event=None, ledger_error=None, ledger_pending=True)
            return web.json_response(delivered)
    # The steer is also the person's word on the worker's item: one ``steer`` event
    # on the ledger that holds it, which the conductor reads before it decides and
    # never overrules. The same transaction the worker's own composer runs.
    delivered.update(await person_steer(state, root, worker, message, rows=rows))
    return web.json_response(delivered)


def _steer_text(message: str) -> str:
    """*message* redacted; the store cuts it to its event cap after redaction."""
    from kiro_crew.security import redact

    return redact(message)


#: ``ledger_error`` when the worker holds no open item on any ledger under its root.
STEER_NO_ITEM = "no_item"


def _worker_item(root: str, worker: str, tree: frozenset[str]) -> tuple[str, str]:
    """``(board key, item id)`` of *worker*'s item, or ``("", "")``. BLOCKING.

    The worker's own binding first (the one ``work_report`` reads), so a worker a
    sub-conductor dispatched is steered on that sub-conductor's ledger; the binding
    counts only when its board is the root or a session in the root's tree
    (*tree*), so a forged or stale binding cannot point a steer at another crew.
    Failing that, the item the root's own ledger lists for the worker.
    """
    from kiro_crew import session_ledger, work_ledger

    folded_root = session_ledger.ledger_key(root)
    try:
        binding = work_ledger.read_binding(session_ledger.ledger_key(worker))
    except Exception:
        binding = None
    if binding is not None:
        board, item_id = binding
        board = session_ledger.ledger_key(board)
        if board == folded_root or board in tree or _persisted_under(board, folded_root):
            return board, item_id
    item_id = _ledger_items(root).get(session_ledger.ledger_key(worker), NO_LEDGER).item
    return (folded_root, item_id) if item_id else ("", "")


def _persisted_under(board: str, folded_root: str) -> bool:
    """Whether *board* descends from *folded_root* in the persisted lineage. BLOCKING.

    A sub-conductor whose tab closed is not in the live tree, but its crew members'
    bindings still name its ledger; the session tree still records it under the
    root. The walk stops on a repeat, so a cycle in the records ends it.
    """
    try:
        from kiro_crew import session_ledger

        parents = {
            session_ledger.ledger_key(k): session_ledger.ledger_key(v)
            for k, v in crew_mode.persisted_parents().items()
            if k and v
        }
    except Exception:
        return False
    seen: set[str] = set()
    key = parents.get(board, "")
    while key and key not in seen:
        if key == folded_root:
            return True
        seen.add(key)
        key = parents.get(key, "")
    return False


async def person_steer(
    state: Any,
    root: str,
    worker: str,
    message: str,
    *,
    rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The one person-steer transaction: one ``steer`` event for a delivered message.

    Run after *message* reached *worker* (a session of Crew Mode root *root*) as
    the person's own text, by the side panel's steer route and by the worker's
    own composer (``POST /api/chat`` on a worker). The event lands on the ledger
    that holds the worker's item (:func:`_worker_item`), redacted
    (:func:`_steer_text`), into a crew-log unit that exists even when the root has
    no live session (``record_person_steer_result``). Returns
    ``{"ledger_event": item id or None, "ledger_error": why or None}``, so a
    caller never reports a recorded steer that was only delivered.
    """
    from kiro_crew.dashboard.handlers import work_ledger as wl
    from kiro_crew.metrics import crew_mode as crew_mode_metrics

    if rows is None:
        try:
            rows = _stamped_rows(state)
        except Exception:
            rows = []
    stamped = rows
    tree = frozenset(
        str(row.get("key")) for row in crew_mode.workers_of(stamped, root) if row.get("key")
    )
    board, item_id = await asyncio.to_thread(_worker_item, root, worker, tree)
    landed, why = "", STEER_NO_ITEM
    if item_id:
        try:
            landed, why = await wl.record_person_steer_result(
                state, board, item_id, _steer_text(message), unit_keys=(worker,)
            )
        except Exception:
            logger.warning("crew steer: the ledger event was not written", exc_info=True)
            landed, why = "", "not_recorded"
    crew_mode_metrics.record_steer(on_item=bool(landed))
    return {"ledger_event": landed or None, "ledger_error": None if landed else why}


async def _deliver_as_person(
    state: Any, slot: Any, message: str, *, root: str = "", worker: str = ""
) -> dict[str, Any]:
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
        from kiro_crew.dashboard.chat_handlers import (
            crew_steer_discarded,
            hold_crew_steer,
        )

        # Held from before the write, as the composer holds it: recorded when the
        # turn reads it, never if the person takes it back first.
        owns_hold = bool(root) and hold_crew_steer(slot.key, message, root, worker or slot.key)
        outcome = await steer_into_running_turn(
            state,
            slot,
            message,
            user_origin=True,
            admission=containment_meta(state, slot),
        )
        if owns_hold and outcome not in (STEER_STEERED, STEER_REQUEUED):
            # Refused outright. A requeued steer keeps its hold for the teardown's
            # requeue, the turn's read or a Stop, as the composer's does.
            crew_steer_discarded(slot, message)
        if outcome == STEER_STEERED:
            return {"ok": True, "steered": True, "queued": False, "started": False}
        if outcome == STEER_REQUEUED:
            # The teardown moved the text into the queue, and the hold set before
            # the write moved onto that entry with it (``crew_steer_requeued``), so
            # the drain records it on delivery. Nothing to name here: looking the
            # entry up by text could pick an older one, or one already delivered.
            return {
                "ok": True,
                "steered": False,
                "queued": True,
                "started": False,
                "queue_id": "",
                "requeued": True,
            }
    queue_id = queue_for_next_turn(
        state, slot, message, directive_user_origin=True, steer_intent=True
    )
    started = False
    if not slot.running:
        # The composer's own default: with the worker's sub-agents still running the
        # drain holds the text until they finish (``queue_drain``), and the reply
        # says it is queued rather than started.
        await _start_next_queued_turn(state, slot)
        # Started means THIS entry left the queue: an earlier entry the drain took
        # first leaves this one queued, to be recorded when it is delivered.
        started = bool(queue_id) and not any(
            e.get("id") == queue_id for e in (getattr(slot, "_queue", None) or [])
        )
    try:
        state.push_slots_update()
    except Exception:  # pragma: no cover - sidebar refresh is best-effort
        logger.debug("crew steer: push_slots_update failed", exc_info=True)
    return {
        "ok": True,
        "steered": False,
        "queued": not started,
        "started": started,
        # Internal: the route marks this entry so the drain records the steer.
        "queue_id": str(queue_id or ""),
    }
