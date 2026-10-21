"""A dashboard card's reply, sent as a turn into the crewmate's own thread.

A crewmate's Dashboard tab runs agent-written HTML in a sandboxed frame. The frame
posts ``{type: 'kirocrew-dashboard:send', text}`` to the host, the host calls
``POST /api/chat`` with ``origin: "dashboard_card"``, and :func:`card_send_refusal`
decides here whether that call may go through. A refusal is not an error for the
reader: the host falls back to putting the text in the chat box, which is what the
older ``act`` message always did.

What the server enforces, because the frame cannot be trusted to:

* the caller is the owner's own dashboard (never an app token or a bare internal key);
* the target is an EXISTING crewmate DM thread whose binding matches. The page names
  no target; the host names its own tab's thread, and the server re-checks it;
* the crewmate's ``dashboard_card_send`` setting is on;
* the text obeys the same rules the host applied (trimmed, at most
  :data:`CARD_MAX_CHARS`, no control characters except a newline);
* the thread is idle, with nothing queued, no sub-agent hold, and NO open decision.
  An open tool approval, permission prompt or question card is refused outright. A
  user row retires a question card and a turn start sweeps unresolved permissions,
  so a card turn that got through would count as an answer to something the person
  never saw. Approvals themselves resolve only through their own id-keyed routes,
  which a chat turn cannot reach, so refusing here is what keeps the two apart;
* at most one send per :data:`CARD_MIN_INTERVAL_SECS` per thread.

The turn that does run carries :data:`CARD_TURN_PREFIX` in its text, so the person
sees it in the transcript and the agent reads it, and it is NOT given a person's
authority over session directives. A first word of ``/new`` or ``/clear`` is not a
command either, because the prefix is the first word.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from typing import TYPE_CHECKING, Any

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import _ChatSlot

logger = logging.getLogger(__name__)

#: The ``origin`` a ``POST /api/chat`` body names to claim card origin. Any other
#: value is treated as no claim at all.
CARD_ORIGIN = "dashboard_card"

#: Row-meta key marking a user row as card-sent. Reserved: a caller's own ``meta``
#: cannot write it (see ``RESERVED_ROW_META_KEYS``).
CARD_ORIGIN_META_KEY = "cardOrigin"

#: First words of the text the agent receives and the transcript shows.
CARD_TURN_PREFIX = "[Dashboard card] "

#: Same bound as the host's ``actText`` (``ACT_MAX_CHARS`` in the frame component).
CARD_MAX_CHARS = 600

#: One send per thread per this many seconds.
CARD_MIN_INTERVAL_SECS = 3.0

_CONTROL_CHARS = re.compile(r"[\u0000-\u0009\u000b-\u001f\u007f]")

#: Most threads tracked at once; the oldest entries are dropped past it.
_MAX_TRACKED_SLOTS = 256

_lock = threading.Lock()
_last_send: dict[str, float] = {}

#: Session key -> how many card turns are running on it right now. In-process and
#: written only by :func:`card_turn` (the exit guard of ``_run_chat``), so nothing
#: that rewrites a session's approval policy can clear it. The spawn gate reads it.
_running_card_turns: dict[str, int] = {}


class card_turn:
    """Marks *session_key* as running a card's turn for the length of a ``with`` block.

    Counted, because the ``/prompts get`` branch runs a nested turn on the same
    session inside the outer one.
    """

    def __init__(self, session_key: str) -> None:
        self._key = session_key

    def __enter__(self) -> None:
        with _lock:
            _running_card_turns[self._key] = _running_card_turns.get(self._key, 0) + 1

    def __exit__(self, *_exc: object) -> None:
        with _lock:
            left = _running_card_turns.get(self._key, 0) - 1
            if left > 0:
                _running_card_turns[self._key] = left
            else:
                _running_card_turns.pop(self._key, None)


#: The routes through which a running turn hands work to something that is NOT that turn:
#: another session (create, fork, send, broadcast, continue, retry, revive), a workflow
#: run, a task run, a cron job, a nudge loop or a monitor. Each starts a turn or an agent
#: that reads the thread's trust, or one that nobody marked as a card's. Sub-agents are
#: refused by the spawn gate itself (``subagent_manager/admission/gate.py``).
_DELEGATION_ROUTES = re.compile(
    r"^/api/("
    r"session-control/(create|fork|send|broadcast|continue|retry|revive)"
    r"|workflows/(run|run_intent)"
    r"|workflows/definitions/[^/]+/run"
    r"|workflows/runs/[^/]+/(promote|rerun)"
    r"|crons|crons/[^/]+|crons/[^/]+/(run|enable|secrets)"
    r"|spawn/[^/]+/(steer|retry|continue)"
    r"|autonudge|autonudge/[^/]+|autonudge/[^/]+/fire"
    r"|monitors|monitors/[^/]+|monitors/[^/]+/restart"
    r")/?$"
)
_DELEGATION_METHODS = frozenset({"POST", "PUT", "PATCH"})
#: The task runner is refused as a whole prefix (plan, from-chat, execute, retry, task
#: edits and start are all ways to start or steer a run), except the routes that only
#: stop one.
_TASKRUNNER_PREFIX = "/api/taskrunner"
_TASKRUNNER_STOPS = re.compile(r"^/api/taskrunner/(cancel|plan/cancel|[^/]+/pause)/?$")


def is_delegation_route(method: str, path: str) -> bool:
    """Whether *method* *path* is one of the routes a card turn may not delegate through."""
    if method not in _DELEGATION_METHODS:
        return False
    if path == _TASKRUNNER_PREFIX or path.startswith(_TASKRUNNER_PREFIX + "/"):
        return _TASKRUNNER_STOPS.match(path) is None
    return _DELEGATION_ROUTES.match(path) is not None


@web.middleware
async def card_turn_delegation_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    """Refuse a delegation route called from inside a Dashboard card's turn.

    A tool call a card turn makes reaches the gateway as an internal request that names
    its session in ``X-Session-Key``. While that session runs a card turn
    (:class:`card_turn`), the routes in :data:`_DELEGATION_ROUTES` answer 403: what they
    start would read the thread's trust or run as a turn nobody marked as a card's.
    Every other request, and every request from a session that runs no card turn,
    passes untouched. Written to the SEL like every other card decision.
    """
    if (
        request.get("internal_auth") is True
        and is_delegation_route(request.method, request.path)
        and card_turn_running(request.headers.get("X-Session-Key", "").strip())
    ):
        refusal = _refuse(
            403,
            "card_turn_delegation_refused",
            "a turn started by a Dashboard card cannot hand work to another session or job",
        )
        _audit(request, None, refusal)
        return refusal
    return await handler(request)


def latest_user_row_is_card(messages: list[dict]) -> bool:
    """Whether the newest user row in *messages* is a Dashboard card's.

    Continue and retry re-run "the user's most recent request"; when that request is a
    card's, the re-run is a card turn too.
    """
    for row in reversed(messages):
        if row.get("role") == "user":
            return (row.get("meta") or {}).get(CARD_ORIGIN_META_KEY) is True
    return False


def card_turn_running(session_key: str) -> bool:
    """Whether a Dashboard card's turn is running on *session_key* right now."""
    with _lock:
        return _running_card_turns.get(session_key, 0) > 0


def claims_card_origin(body: dict) -> bool:
    """Whether a ``POST /api/chat`` body claims card origin."""
    return body.get("origin") == CARD_ORIGIN


def valid_card_text(text: str) -> bool:
    """The host's ``actText`` rules, re-applied on the server."""
    return bool(text) and len(text) <= CARD_MAX_CHARS and not _CONTROL_CHARS.search(text)


def card_turn_text(text: str) -> str:
    """The text a card-sent turn carries: the tag, then the page's words.

    The words come from a page, not from the person's keyboard, so they get the
    redaction any non-human origin gets (credential-shaped runs and exfiltration
    URLs), once, before the tag is added. The row is stored and served as a user
    row, which is otherwise exempt.
    """
    from kiro_crew.dashboard.chat_handlers import queued_text_for_display

    return CARD_TURN_PREFIX + queued_text_for_display(text, user_origin=False)


def _refuse(
    status: int, code: str, error: str, *, retry_after: float | None = None
) -> web.Response:
    headers = {"Retry-After": str(max(1, int(retry_after + 0.999)))} if retry_after else None
    return web.json_response({"error": error, "code": code}, status=status, headers=headers)


def has_open_decision(state: Any, slot: "_ChatSlot") -> bool:
    """A tool approval, permission prompt, question card or parked question is open on *slot*."""
    from kiro_crew.dashboard.responsive_chat import pending_interaction

    return bool(
        pending_interaction(slot)
        or any(p.get("slot") == slot.key for p in state._pending_questions.values())
    )


def pending_decision_refusal(
    request: web.Request, state: Any, slot: "_ChatSlot"
) -> web.Response | None:
    """The refusal for a card send that finds a decision open AFTER the gate admitted it.

    ``card_send_refusal`` looked once; the handler then awaits (the member config, the
    binding), and a typed turn can start and end in a question in between. Appending the
    card's row would retire that question as if the card answered it. Written to the SEL
    like every other decision; does not claim the rate limit again.
    """
    if not has_open_decision(state, slot):
        return None
    refusal = _refuse(
        409, "card_send_pending_decision", "this thread is waiting on an approval or a question"
    )
    _audit(request, slot, refusal)
    return refusal


def busy_refusal(request: web.Request, slot: "_ChatSlot | None") -> web.Response:
    """The refusal for a card send that finds its thread busy (and its SEL record)."""
    refusal = _refuse(409, "card_send_slot_busy", "this thread is busy")
    _audit(request, slot, refusal)
    return refusal


def _thread_is_idle(state: Any, slot: "_ChatSlot") -> bool:
    """Nothing running, reserved, queued or held for sub-agents on *slot*."""
    from kiro_crew.dashboard.chat_handlers import (
        effective_session_key,
        subagents_hold_user_messages,
    )

    if slot.turn_running or slot._turn_admission_reserved or slot._queue:
        return False
    return not subagents_hold_user_messages(state, effective_session_key(slot))


def claim_send_slot(slot_key: str, *, now: float | None = None) -> float:
    """Record a send on *slot_key*; return 0.0 if it may go, else seconds to wait."""
    now = time.monotonic() if now is None else now
    with _lock:
        last = _last_send.get(slot_key)
        if last is not None and now - last < CARD_MIN_INTERVAL_SECS:
            return CARD_MIN_INTERVAL_SECS - (now - last)
        _last_send[slot_key] = now
        if len(_last_send) > _MAX_TRACKED_SLOTS:
            for key in sorted(_last_send, key=_last_send.__getitem__)[
                : len(_last_send) - _MAX_TRACKED_SLOTS
            ]:
                del _last_send[key]
    return 0.0


def _reset_rate_limit_for_tests() -> None:
    with _lock:
        _last_send.clear()


def _audit(request: web.Request, slot: "_ChatSlot | None", refusal: web.Response | None) -> None:
    """One SEL record per card-send decision, like every other ``chat_send`` denial."""
    from kiro_crew.dashboard.chat_handlers import sel

    code = ""
    if refusal is not None:
        try:
            code = str(json.loads(refusal.body or b"{}").get("code", ""))  # type: ignore[arg-type]
        except (ValueError, AttributeError):
            code = "refused"
    try:
        sel().log_api_access(
            caller=request.remote or "",
            operation="chat_send",
            outcome="denied" if refusal is not None else "allowed",
            source="dashboard_card",
            resources=f"slot={slot.key if slot is not None else ''}",
            error=code,
        )
    except Exception:  # an audit failure must not turn a decision into a 500
        logger.debug("card send audit failed", exc_info=True)


async def card_send_refusal(
    request: web.Request, state: Any, slot: "_ChatSlot | None", message: str
) -> web.Response | None:
    """The refusal for a card-origin send, or ``None`` when it may go through.

    *slot* is the thread the body names, already looked up WITHOUT creating it.
    Runs before ``get_or_create_slot``, so a refused card send leaves no trace.
    Every decision, admitted or refused, is written to the SEL.
    """
    refusal = await _decide(request, state, slot, message)
    _audit(request, slot, refusal)
    return refusal


async def _decide(
    request: web.Request, state: Any, slot: "_ChatSlot | None", message: str
) -> web.Response | None:
    from kiro_crew.config import KiroCrewConfig
    from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request
    from kiro_crew.members import DM_SLOT_KEY_PREFIX, DM_SLOT_MODE, read_dm_binding_for_slot

    if request.get("app", "") or not is_owner_dashboard_request(request):
        return _refuse(
            403, "card_origin_not_allowed", "only the owner's dashboard can send as a card"
        )
    if slot is None or slot.mode != DM_SLOT_MODE or not slot.key.startswith(DM_SLOT_KEY_PREFIX):
        return _refuse(
            403, "card_target_invalid", "a card can only send into a crewmate's own thread"
        )
    binding = await asyncio.to_thread(read_dm_binding_for_slot, slot.key)
    if binding is None or binding.get("member", "") != slot.agent:
        return _refuse(
            403, "card_target_invalid", "a card can only send into a crewmate's own thread"
        )
    cfg = await asyncio.to_thread(KiroCrewConfig.load)
    agent = cfg.agents.get(slot.agent)
    if agent is None or not getattr(agent, "dashboard_card_send", True):
        return _refuse(403, "card_send_off", "card sending is off for this crewmate")
    if not valid_card_text(message):
        return _refuse(
            400, "card_text_invalid", "the card text is empty, too long or has control characters"
        )
    if has_open_decision(state, slot):
        return _refuse(
            409, "card_send_pending_decision", "this thread is waiting on an approval or a question"
        )
    if not _thread_is_idle(state, slot):
        return _refuse(409, "card_send_slot_busy", "this thread is busy")
    wait = claim_send_slot(slot.key)
    if wait:
        return _refuse(429, "card_send_rate_limited", "too many card sends", retry_after=wait)
    return None
