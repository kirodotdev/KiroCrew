"""The Crew page's read of a conductor's work ledger (RFC Phase 4, the surfaces).

`handlers/work_ledger.py` serves the CONDUCTOR: it is MCP-only, it resolves which
ledger to read from the caller's own `X-Session-Key`, and its item rows carry
`worker_session_key`. None of those three suit a browser. A page has a cookie
rather than a session key, so it cannot address a ledger at all through that
route, and Phase 4 forbids `worker_session_key` in any Crew page payload.

So this module adds a second read over the SAME store, for the other caller:

* **Masked.** A row is `WorkItem.to_dict()` minus `worker_session_key`, plus the
  derived flags the conductor's read already adds and the joined `alive` state.
  The mask is applied by naming the field to drop, not by listing the fields to
  keep, so a field added to the item later appears on the page instead of
  silently going missing — and the one field that must never appear is the one
  spelled out.
* **Cookie-authenticated.** Deliberately NOT in
  ``server._STRICT_INTERNAL_API_PATHS``: that list is what forces the
  internal-secret path, and a browser caller would fail it. The principal here is
  the dashboard owner, who already reads every session on this gateway, so the
  ledger to show comes from the query string. That is not the thing
  ``handlers/work_ledger.py``'s contract forbids: what it refuses is one AGENT
  session naming another's ledger, which is a privilege claim. A human operator
  naming their own conductor is not.
* **Liveness joined here.** ``alive`` is derived server-side from the worker's
  session key and the key itself is then dropped, which is the only way to answer
  "is this worker running" on a page that may not receive the key.

The conductor's route is not touched, so nothing reading it today can break.

The action route also carries ``open``, the one action that RETURNS a worker's session
key: to the dashboard owner's own host, so it can navigate there. See
:func:`api_work_ledger_board_action` for the exact contract.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from aiohttp import web

from kiro_crew import session_ledger, work_ledger
from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request
from kiro_crew.dashboard.handlers.work_ledger import (
    _MAX_EVENT_TAIL,
    _audit,
    _board_lock,
    _find_slot,
    _own_ledger,
    _refuse_if_dirty,
    _slot_closed,
    _slot_looping,
    _slot_open,
    _slot_running,
    _tail_events,
)
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.platform.context import redact_via_context

logger = logging.getLogger(__name__)

#: The one field Phase 4 forbids on a Crew page payload. Named as a mask rather
#: than an allow-list so a later item field reaches the page by default.
_MASKED_ITEM_FIELDS = ("worker_session_key",)

#: Event kinds whose ``text`` IS a worker session key and must therefore be
#: emptied, not just the item field.
#:
#: A ``bind`` line records which session was dispatched, and the key is the whole
#: of its text — so masking only ``WorkItem.worker_session_key`` leaves the key on
#: the page inside the event log, which a per-row check happily passes. The line
#: itself is kept: its ``kind`` and ``ts`` are when dispatch happened, which the
#: timeline needs and which the key is not required to express.
_KEY_BEARING_EVENT_KINDS = frozenset({"bind"})

#: The worker status that asks the conductor a question. The work ledger has no
#: ``request`` event kind (``EVENT_KINDS`` is create / bind / report / decision /
#: verdict / close), so the RFC's ``request`` half of the band waits for Phase 5;
#: this is the half that exists today.
_ASKING_STATUS = "question"

#: The two affordances Phase 4 names for an orphaned item.
_BOARD_ACTIONS = ("stop", "take_over")

# A conductor key arrives in a query string or a body, so it is caller-supplied text
# and not every string can name a ledger directory: `work_ledger.conductor_dir`
# refuses a path separator or a null byte, since either would let the key escape its
# own directory. Both routes below turn that refusal into this 400 rather than
# letting it surface as a 500.
#
# By CATCHING the store's own error rather than re-testing the shape here: the rule
# belongs to `work_ledger`, a copy of it in a handler is a copy that can drift, and a
# rule the store adds later is covered by this without a second edit.
_INVALID_CONDUCTOR = (
    "invalid_conductor",
    "that conductor session key cannot name a work ledger",
)

#: Whether this gateway can perform a take-over, and why not when it cannot.
#:
#: ``False`` because no server-side primitive exists to delegate to, and Phase 4's
#: own instruction is not to invent a second one. Two searches establish it:
#: ``dashboard/session_control.py`` exposes ``create_session``, ``stop_target``,
#: ``close_target``, ``send_to_target`` and ``read_messages`` and nothing that
#: re-owns a session, and ``work_ledger.CONDUCTOR_ACTIONS`` is
#: ``{create, bind, decide, verdict, close, goal}`` with no action that transfers an
#: item to another conductor. So a take-over has neither a session-level nor a
#: store-level operation behind it, and the page renders no take-over control at
#: all while this reads ``False``, rather than a control wired to something
#: invented here.
_TAKE_OVER_AVAILABLE = False
_TAKE_OVER_UNAVAILABLE_CODE = "no_server_primitive"

#: The third action. It is not in :data:`_BOARD_ACTIONS` because it is addressed by the
#: dashboard the page is drawn on, not by a conductor key, and it is not limited to
#: orphaned items.
_OPEN_ACTION = "open"

#: Source label on the SEL line this surface's stop writes.
_STOP_SOURCE = "crew_board"


def _mask_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Blank the text of any event whose text is a session key."""
    masked: list[dict[str, Any]] = []
    for event in events:
        if event.get("kind") in _KEY_BEARING_EVENT_KINDS:
            event = {**event, "text": ""}
        masked.append(event)
    return masked


def _parse_ts(raw: Any) -> datetime | None:
    """One stored ISO timestamp, or ``None`` when it is not one.

    Parsed rather than compared as text: two timestamps written in different
    offsets order wrongly as strings, and ordering is exactly what decides whether
    a decision answered a question.
    """
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        logger.debug("crew board: unparseable timestamp %r", text)
        return None


#: Event kinds that answer a worker's question: the conductor's decision, and the
#: person's own steer into that worker.
_ANSWER_KINDS = frozenset({"decision", "steer"})


def _is_outstanding(item: work_ledger.WorkItem, events: list[dict[str, Any]]) -> bool:
    """Whether this item is a question the conductor has not yet answered.

    A bare "has a decision" test is wrong: an item can be decided, sent back, and
    then ask something new, and the old decision must not silence the new question.
    So the newest ``decision`` must be NEWER than the report that asked. A person's
    ``steer`` newer than the ask answers it too: the person outranks the conductor,
    and the worker already has the answer.

    Falls back to ``last_report_at`` when the asking report has aged off the event
    tail, and treats an unparseable or missing ask as outstanding — a question the
    page failed to date is one a human should still look at.
    """
    if item.status != _ASKING_STATUS:
        return False
    asked: datetime | None = None
    answered: datetime | None = None
    for event in events:
        stamp = _parse_ts(event.get("ts"))
        if stamp is None:
            continue
        kind = event.get("kind")
        if kind == "report" and event.get("status") == _ASKING_STATUS:
            if asked is None or stamp > asked:
                asked = stamp
        elif kind in _ANSWER_KINDS:
            if answered is None or stamp > answered:
                answered = stamp
    if asked is None:
        asked = _parse_ts(item.last_report_at)
    if answered is None:
        return True
    if asked is None:
        return True
    return answered <= asked


def _project_item(
    row: dict[str, Any], *, alive: str, outstanding: bool, terminal: bool
) -> dict[str, Any]:
    """One masked row: the conductor's row minus the masked field, plus the joins."""
    projected = {k: v for k, v in row.items() if k not in _MASKED_ITEM_FIELDS}
    projected["alive"] = alive
    projected["outstanding"] = outstanding
    projected["terminal"] = terminal
    return projected


def _redact_deep(value: Any) -> Any:
    """Run every STRING in the payload through the egress credential redactor.

    ``title``, ``summary``, ``decision`` and every artifact value are written by an
    AGENT, and nothing between that write and this read inspects them. A worker
    that pasted a token into its own status line would otherwise have it rendered
    verbatim in a browser -- the masking above removes the one field we know is a
    secret, and says nothing about prose that happens to contain one.

    A mapping's KEYS are agent-authored on the same terms as its values: an
    artifact name is a free string the worker chose, capped only in length, and it
    reaches the browser verbatim. So keys are redacted too, and a key that
    redacts onto one already present is suffixed rather than dropped.

    Recursive over the whole payload rather than applied to a named list of prose
    fields, and for the same reason the item mask is a deny-list: a field added to
    ``WorkItem`` later is agent-authored too, and a redactor that must be extended
    per field is one that will be forgotten.

    ``redact_via_context`` rather than ``security.redact``: it is the canonical
    egress shim, so a host with a loaded companion applies that companion's extra
    patterns. It is deliberately fail-closed on a composition error, which surfaces
    as a 500 here -- a host that cannot compose its redaction must not answer with
    un-redacted content instead.
    """
    if isinstance(value, str):
        return redact_via_context(value)
    if isinstance(value, dict):
        redacted: dict[Any, Any] = {}
        for key, item in value.items():
            safe_key = redact_via_context(key) if isinstance(key, str) else key
            if safe_key in redacted:
                # Two distinct keys can redact to the same placeholder. Keep both
                # rows rather than let the later one overwrite the earlier.
                suffix = 2
                while f"{safe_key} ({suffix})" in redacted:
                    suffix += 1
                safe_key = f"{safe_key} ({suffix})"
            redacted[safe_key] = _redact_deep(item)
        return redacted
    if isinstance(value, list):
        return [_redact_deep(item) for item in value]
    return value


def task_records(
    slot_key: str,
) -> tuple[work_ledger.ConductorRecord | None, list[work_ledger.WorkItem]]:
    """Return this slot's conductor and work items for gateway-owned readers.

    The slot comes from server state, never caller input. Keeping this read on the
    existing dashboard seam preserves the store's identity boundary.
    """
    return work_ledger.read_conductor(slot_key), work_ledger.list_work_items(slot_key)


def work_item_is_terminal(state: object) -> bool:
    """Whether a stored work-item state is terminal."""
    return state in work_ledger.TERMINAL_ITEM_STATES


def closed_items(slot_key: str) -> list[dict[str, Any]]:
    """The closed items on thread *slot_key*'s own ledger, as this board reads them.

    The gateway-side reader behind a crewmate's weekly and monthly goal reports:
    the slot comes from the crewmate's own goal binding, never from a request, so
    no caller names a ledger here. Each row is ``{closed_at, title, state,
    summary}`` with ``closed_at`` parsed (rows without one are dropped) and every
    string through the same egress redactor as the board payload. A thread that
    owns no ledger, or whose key does not fold, has closed nothing. Blocking file IO.
    """
    try:
        items = work_ledger.list_work_items(session_ledger.ledger_key(slot_key))
    except (work_ledger.WorkLedgerError, OSError):
        return []
    rows: list[dict[str, Any]] = []
    for item in items:
        if not item.is_terminal:
            continue
        closed = work_ledger.parse_stamp(item.closed_at or "")
        if closed is None:
            continue
        rows.append(
            {
                "closed_at": closed,
                **_redact_deep({"title": item.title, "state": item.state, "summary": item.summary}),
            }
        )
    return rows


def _alive_of(state: DashboardState, worker_key: str) -> str:
    """``running`` / ``idle`` / ``closed`` for a worker session key.

    Computed here and the key discarded, because the page may not have the key.
    An item never bound to a session is ``closed``: nothing is running for it.
    """
    if not worker_key:
        return "closed"
    if _slot_running(state, worker_key):
        return "running"
    if _slot_open(state, worker_key) or _find_slot(state, worker_key) is not None:
        return "idle"
    return "closed"


async def api_work_ledger_board(request: web.Request) -> web.Response:
    """GET /api/crew-board?conductor=KEY — the Crew page's masked read.

    Not spelled under ``/api/work-ledger``: that prefix is in
    ``server._STRICT_INTERNAL_API_PATHS`` and the list matches by prefix, so a
    sub-path would inherit MCP-only auth and refuse every browser call.

    Answers ``no_ledger`` for a session that owns no work ledger, which today
    includes any ad-hoc conductor: only a session dispatched through the
    conductor tooling opens one. That is a real gap, not an error, and the page
    renders it as an empty board rather than a failure.
    """
    # Owner gate FIRST, before the caller's conductor key is even read. A ledger
    # is owned by one human's gateway, and ``_own_ledger`` only establishes that
    # the requested key HAS a ledger -- not that this caller may read it. Without
    # this, a non-owner dashboard token (a member, an app) could name any
    # conductor key and read its goal, items and worker reports.
    denied = await require_owner_dashboard_request(request, "work_ledger_board")
    if denied is not None:
        return denied

    # The authenticated dashboard owner is the CALLER of record for this route,
    # not the conductor whose ledger is read: the owner gate above admitted a
    # human's gateway token, and a ledger key is a URL parameter that same human
    # chose. Auditing the conductor key as caller would attribute the read to the
    # subject being read rather than to the operator who made the request.
    caller = str(request.get("user") or "unknown")

    requested = str(request.query.get("conductor") or "").strip()
    if not requested:
        return web.json_response(
            {
                "error": "a conductor session key is required (?conductor=KEY)",
                "code": "missing_conductor",
            },
            status=400,
        )
    key = session_ledger.ledger_key(requested)

    # A cache commit and its record append are two steps, and a read landing
    # between them serves a mutation the record may yet roll back -- or, once an
    # undo has failed, one the record never holds at all. The ledger header, the
    # items and their event tails are read under a single hold, so the payload is
    # one snapshot rather than a stitch of several. The projection below sits
    # outside the hold because it reads only what the hold already captured.
    try:
        async with _board_lock(key):
            record, refusal = await _own_ledger(key, "work_ledger_board", caller)
            if refusal is not None:
                return refusal
            dirty = await _refuse_if_dirty(key, caller, "work_ledger_board")
            if dirty is not None:
                return dirty
            items = await asyncio.to_thread(work_ledger.list_work_items, key)
            event_tails: dict[str, list[work_ledger.WorkEvent]] = {}
            for item in items:
                event_tails[item.item_id] = await asyncio.to_thread(
                    _tail_events, key, item.item_id, _MAX_EVENT_TAIL
                )
    except work_ledger.WorkLedgerError as exc:
        if exc.code != work_ledger.CODE_INVALID_VALUE:
            raise
        return web.json_response(
            {"error": _INVALID_CONDUCTOR[1], "code": _INVALID_CONDUCTOR[0]},
            status=400,
        )
    assert record is not None

    state: DashboardState = request.app["state"]
    conductor_alive = _slot_open(state, key)
    # The worker's transport, not the ledger: a report the harness never delivered.
    # Read outside the board hold because it is not part of the ledger snapshot.
    undelivered = await asyncio.to_thread(
        lambda: {item.item_id: work_ledger.read_undelivered(key, item) for item in items}
    )

    rows: list[dict[str, Any]] = []
    for item in items:
        row = item.to_dict()
        row["orphaned"] = work_ledger.is_orphaned(item, conductor_slot_exists=conductor_alive)
        row["stale"] = work_ledger.is_stale(
            item,
            worker_running=_slot_running(state, item.worker_session_key or ""),
            worker_closed=_slot_closed(state, item.worker_session_key or ""),
            worker_looping=_slot_looping(item.worker_session_key or ""),
        )
        row["acceptance_concrete"] = work_ledger.is_acceptance_concrete(item.acceptance)
        row["report_undelivered"] = undelivered.get(item.item_id)
        row["events"] = _mask_events([event.to_dict() for event in event_tails[item.item_id]])
        rows.append(
            _project_item(
                row,
                alive=_alive_of(state, item.worker_session_key or ""),
                outstanding=_is_outstanding(item, row["events"]),
                terminal=item.is_terminal,
            )
        )

    payload = {
        "conductor": record.to_dict(),
        "conductor_alive": "idle" if conductor_alive else "closed",
        "items": rows,
        # The ONE capability the page actually reads, because it changes what the
        # page renders: no take-over button is rendered while this is false.
        #
        # Deliberately not accompanied by `stop_available`, `channels_available` or
        # a reason code. Those were sent so Phase 5 and a future take-over could
        # turn features on server-side, but nothing reads them today, and a field
        # with no reader is not forward compatibility -- it is a claim the payload
        # makes and the page ignores. Each belongs in the change that adds its
        # reader.
        "take_over_available": _TAKE_OVER_AVAILABLE,
    }
    # Record the ALLOWED read, not only denials: an owner gate that logs a grant
    # nowhere leaves an operator unable to prove which conductor ledgers were read
    # and by whom. Safe GET methods skip the dashboard chain's audit middleware, so
    # the grant is logged here or not at all. Caller is the operator; the conductor
    # whose ledger was read is a resource, never the caller.
    _audit(caller, "crew_board_read", "ok", resources=f"conductor={key} items={len(rows)}")
    # Off the loop: the redaction stack is a wide regex sweep over every string in
    # up to 32 items and their event tails, which is real CPU and would otherwise
    # block the gateway's loop for the duration.
    return web.json_response(await asyncio.to_thread(_redact_deep, payload))


def _refuse(code: str, message: str, status: int) -> web.Response:
    """One refusal shape for this route, carrying no session key."""
    return web.json_response({"ok": False, "error": message, "code": code}, status=status)


def _refuse_open(
    caller: str, label: str, item_id: str, code: str, message: str, status: int
) -> web.Response:
    """A refused ``open``: audited as a denial, answered without any session key."""
    _audit(
        caller,
        "crew_board_action",
        "denied",
        resources=f"dashboard={label} item={item_id}:{code}",
    )
    return _refuse(code, message, status)


class _OpenRefused(Exception):
    """An ``open`` the route answers with this code, message and status."""

    def __init__(self, code: str, message: str, status: int) -> None:
        super().__init__(code)
        self.code = code
        self.message = message
        self.status = status


async def _dashboard_slot_of(state: DashboardState, dashboard: dict[str, Any]) -> tuple[str, str]:
    """``(slot, audit label)`` for the dashboard an ``open`` names; raises :class:`_OpenRefused`.

    The same resolution the dashboard's own read does, so ``open`` searches the fold the
    page was drawn from: a session dashboard is the slot itself and must be a ROOT
    session (:func:`~kiro_crew.dashboard.card_lifecycle.is_root_session`); a member
    dashboard is the crewmate's DM slot (``member_dashboard.dashboard_slot``).
    """
    kind = dashboard.get("kind")
    if kind == "session":
        slot_key = dashboard.get("slot")
        if not isinstance(slot_key, str) or not slot_key.strip():
            raise _OpenRefused("invalid_dashboard", "a session dashboard needs its slot", 400)
        slot_key = slot_key.strip()
        from kiro_crew.dashboard.card_lifecycle import root_session_refusal

        refusal = root_session_refusal(state.get_slot(slot_key))
        if refusal is not None:
            raise _OpenRefused(refusal[0], refusal[1], 404)
        return slot_key, f"session:{slot_key}"
    if kind == "member":
        slug = dashboard.get("slug")
        member = dashboard.get("member")
        if not isinstance(slug, str) or not isinstance(member, str) or not slug or not member:
            raise _OpenRefused(
                "invalid_dashboard", "a member dashboard needs its slug and member", 400
            )
        # Deferred: the member handler imports half the dashboard, and this module is
        # imported by the gateway's route table before it.
        from kiro_crew.dashboard.handlers import member_dashboard

        # The dashboard read's own resolver, so a name it accepts is a name `open` accepts.
        resolved = await member_dashboard.resolve_names(slug, member)
        if isinstance(resolved, member_dashboard.NameRefusal):
            raise _OpenRefused(resolved.code, resolved.message, resolved.status)
        slug, member = resolved
        member_slot = await asyncio.to_thread(member_dashboard.dashboard_slot, member, slug)
        return member_slot, f"member:{slug}"
    raise _OpenRefused("invalid_dashboard", "dashboard.kind must be 'session' or 'member'", 400)


async def _open_worker_session(
    request: web.Request, body: dict[str, Any], caller: str
) -> web.Response:
    """``open``: the slot key of the worker session an org-chart card stands for.

    Body: ``{"action": "open", "item_id": "it_...", "dashboard": {"kind": "session",
    "slot": KEY} | {"kind": "member", "slug": SLUG, "member": NAME}}``. Answers
    ``{"ok": true, "action": "open", "item_id": ..., "slot": WORKER_SLOT}``.

    Addressed by the DASHBOARD, not by a conductor, because the page cannot know a
    conductor key: a card holds a per-render board alias and an item id, and the alias
    table is never stored. The server instead reads the ``workstreams`` fold of the
    dashboard's own slot, the state the page was drawn from, and finds the item there
    (:func:`~kiro_crew.crew_log.projection_readers.workstream_item_workers`). An id on more
    than one board is refused 409 ``ambiguous_item``: the alias cannot break the tie.

    The caller is the dashboard owner (the route's gate ran before this), and the owner's
    browser already holds every session key on the gateway through ``/api/chat/slots``, so
    returning this one adds no information surface. It goes to the host's JavaScript only.
    """
    item_id = str(body.get("item_id") or "").strip()
    dashboard = body.get("dashboard")
    label = "unresolved"
    if "conductor" in body and dashboard is not None:
        return _refuse_open(
            caller,
            label,
            item_id[:32],
            "invalid_body",
            "name the dashboard or the conductor, not both",
            400,
        )
    if not work_ledger._ITEM_ID_RE.fullmatch(item_id):
        return _refuse_open(
            caller, label, item_id[:32], "invalid_item_id", "item_id is not an item id", 400
        )
    if not isinstance(dashboard, dict):
        return _refuse_open(
            caller, label, item_id, "invalid_dashboard", "a dashboard object is required", 400
        )

    state: DashboardState = request.app["state"]
    try:
        slot_key, label = await _dashboard_slot_of(state, dashboard)
    except _OpenRefused as refusal:
        return _refuse_open(caller, label, item_id, refusal.code, refusal.message, refusal.status)

    # Deferred like the other store calls in the dashboard handlers: the crew log is an
    # optional subsystem and the boot path must not load it.
    from kiro_crew.crew_log import projection

    try:
        workers = await asyncio.to_thread(projection.workstream_item_workers, slot_key, item_id)
    except Exception:
        logger.warning("crew board: workstreams read failed for open", exc_info=True)
        return _refuse_open(
            caller, label, item_id, "workstreams_unavailable", "the workstreams read failed", 503
        )
    if not workers:
        return _refuse_open(
            caller, label, item_id, "item_not_found", "no such item on this dashboard", 404
        )
    if len(workers) > 1:
        return _refuse_open(
            caller,
            label,
            item_id,
            "ambiguous_item",
            "this item id is on more than one board; open the session from the sidebar",
            409,
        )
    worker_key = workers[0]
    if not worker_key:
        return _refuse_open(
            caller, label, item_id, "no_worker_bound", "this item has no bound worker to open", 409
        )
    worker = _find_slot(state, worker_key)
    if worker is None:
        return _refuse_open(
            caller,
            label,
            item_id,
            "worker_closed",
            "this item's worker session is no longer open",
            409,
        )
    _audit(caller, "crew_board_action", "ok", resources=f"dashboard={label} item={item_id}:open")
    return web.json_response(
        {"ok": True, "action": _OPEN_ACTION, "item_id": item_id, "slot": str(worker.key)}
    )


async def api_work_ledger_board_action(request: web.Request) -> web.Response:
    """POST /api/crew-board/action — act on one item.

    Body: ``{"conductor": KEY, "item_id": "it_...", "action": "stop"|"take_over"}``,
    or, for ``open``, ``{"action": "open", "item_id": "it_...", "dashboard": {...}}``
    (see :func:`_open_worker_session`).

    The worker session key is resolved HERE, from the store, and is never accepted from
    the body. ``stop`` and ``take_over`` never return it, which is what lets the page
    drive an affordance whose target it is not allowed to see. The masking on the
    read is what makes this route necessary at all — a browser that cannot learn
    the key cannot reach that session itself. ``open`` returns it to its caller, whom
    the owner gate below has already established as the dashboard owner, so the host can
    navigate to that session. The host must never pass it into a dashboard frame.

    ``stop`` and ``take_over`` are scoped to ORPHANED items, per Phase 4: that is the
    state the affordances are named for, and it is the state in which nothing is left
    reading the worker's reports. Anything else is ``409`` rather than a quiet no-op, so
    a page working from a stale poll is told its view has moved on. ``open`` works on any
    item whose worker session is still open.

    Delegates the stop to ``chat_handlers.stop_slot_turn``, the single primitive the
    Stop button and ``session_control.stop_target`` both already use — not to
    ``stop_target`` itself, which cannot serve this case: it authorizes through
    ``authorize_target`` → ``caller_slot_key``, and that resolves a caller only by
    walking the LIVE slot table. An orphaned item is by definition one whose
    conductor slot is gone, so a delegation naming the conductor as caller would be
    refused ``caller_unidentified`` on every request this route accepts. The
    authorization it would have performed is done here instead: the owner gate
    below establishes that the caller is the operator, and the ledger lookup
    establishes that the named conductor is one of that operator's own.
    """
    # Owner gate FIRST. This route cancels the turn a worker session is running,
    # so a non-owner dashboard token (a member, an app) naming someone else's
    # conductor key must not reach the store at all -- ``_own_ledger`` answers only
    # whether a ledger exists, which every caller could otherwise probe.
    denied = await require_owner_dashboard_request(request, "work_ledger_board_action")
    if denied is not None:
        return denied

    # Same as the read: the authenticated dashboard owner is the caller of record,
    # not the conductor named in the body. The conductor and item are resources.
    caller = str(request.get("user") or "unknown")

    try:
        body = await request.json()
    except Exception:
        return _refuse("invalid_body", "a JSON object body is required", 400)
    if not isinstance(body, dict):
        return _refuse("invalid_body", "a JSON object body is required", 400)

    action = str(body.get("action") or "").strip()
    if action == _OPEN_ACTION:
        return await _open_worker_session(request, body, caller)

    requested = str(body.get("conductor") or "").strip()
    item_id = str(body.get("item_id") or "").strip()
    if not requested:
        return _refuse("missing_conductor", "a conductor session key is required", 400)
    if not item_id:
        return _refuse("missing_item_id", "an item_id is required", 400)
    if action not in _BOARD_ACTIONS:
        return _refuse(
            "unknown_action",
            f"unknown action {action!r}; expected one of {sorted(_BOARD_ACTIONS)}",
            400,
        )

    key = session_ledger.ledger_key(requested)
    # Same single hold as the GET, for a sharper reason: this handler goes on to
    # STOP the worker the item names, so a read of uncommitted cache state would
    # end a session the record never bound. The lookup below is pure filtering of
    # the list the hold already returned, so it needs no hold of its own.
    try:
        async with _board_lock(key):
            record, refusal = await _own_ledger(key, "work_ledger_board_action", caller)
            if refusal is not None:
                return refusal
            dirty = await _refuse_if_dirty(key, caller, "work_ledger_board_action")
            if dirty is not None:
                return dirty
            items = await asyncio.to_thread(work_ledger.list_work_items, key)
    except work_ledger.WorkLedgerError as exc:
        if exc.code != work_ledger.CODE_INVALID_VALUE:
            raise
        return _refuse(_INVALID_CONDUCTOR[0], _INVALID_CONDUCTOR[1], 400)
    assert record is not None

    state: DashboardState = request.app["state"]
    item = next((candidate for candidate in items if candidate.item_id == item_id), None)
    if item is None:
        _audit(
            caller,
            "crew_board_action",
            "denied",
            resources=f"conductor={key} item={item_id}:item_not_found",
        )
        return _refuse("item_not_found", "no such item on this conductor's ledger", 404)

    # Read after the item is known to exist, so the two refusals cannot be told
    # apart by timing on a guessed id any more than by their bodies.
    if not work_ledger.is_orphaned(item, conductor_slot_exists=_slot_open(state, key)):
        _audit(
            caller,
            "crew_board_action",
            "denied",
            resources=f"conductor={key} item={item_id}:not_orphaned",
        )
        return _refuse(
            "not_orphaned",
            "this item is not orphaned, so it has no take-over or stop to perform",
            409,
        )

    if action == "take_over":
        _audit(
            caller,
            "crew_board_action",
            "denied",
            resources=f"conductor={key} item={item_id}:take_over_unavailable",
        )
        return _refuse(
            _TAKE_OVER_UNAVAILABLE_CODE,
            "this gateway has no server-side take-over primitive to delegate to",
            501,
        )

    worker_key = item.worker_session_key or ""
    if not worker_key:
        _audit(
            caller,
            "crew_board_action",
            "denied",
            resources=f"conductor={key} item={item_id}:no_worker_bound",
        )
        return _refuse("no_worker_bound", "this item has no bound worker to stop", 409)
    slot = _find_slot(state, worker_key)
    if slot is None:
        _audit(
            caller,
            "crew_board_action",
            "denied",
            resources=f"conductor={key} item={item_id}:worker_closed",
        )
        return _refuse("worker_closed", "this item's worker session is no longer open", 409)

    # Deferred, matching ``session_control.stop_target``'s own import: importing
    # ``chat_handlers`` at module scope closes an import cycle back through
    # ``server``.
    from kiro_crew.dashboard.chat_handlers import stop_slot_turn

    # ``escalate=False`` for the same reason ``stop_target`` passes it: a browser
    # whose request timed out re-sends it, and an escalation would discard the
    # worker's queue and pending steers for what is a retry rather than a second
    # decision. A repeat that finds the worker running still stops it.
    result = await stop_slot_turn(state, slot, source=_STOP_SOURCE, escalate=False)

    # The audit outcome comes from the result, not from having reached this line.
    # The delegate answers 200 with ``ok: False`` when it cannot reach the worker's
    # session, and an unreachable worker recorded as ``ok`` is an audit line that
    # disagrees with what happened -- written once, read later, never corrected. The
    # refusal code goes in ``resources`` so the line says which failure it was.
    stopped = bool(result.get("ok", False))
    if stopped:
        _audit(caller, "crew_board_action", "ok", resources=f"conductor={key} item={item_id}:stop")
    else:
        code = str(result.get("code") or "unknown")
        _audit(
            caller,
            "crew_board_action",
            "failure",
            resources=f"conductor={key} item={item_id}:stop:{code}",
        )
    # ALLOW-listed, not masked. The item rows above use a deny-list so a field added
    # to ``WorkItem`` later reaches the page by default; the opposite is right here,
    # because this body belongs to another module. A field it gains later must not
    # reach a browser that is deliberately not allowed to know which session this
    # acted on, so only these three are copied out.
    #
    # ``ok`` is load-bearing, not decoration: the delegate answers 200 with
    # ``ok: False`` when it cannot reach the worker's session, so a page that reads
    # only the status code cannot tell a stopped worker from a running one.
    return web.json_response(
        {
            "ok": stopped,
            "action": "stop",
            "item_id": item_id,
        }
    )
