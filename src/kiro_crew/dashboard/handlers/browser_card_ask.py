"""The in-band card ``js``, ``cdp``, ``http_get`` and ``upload`` wait on, every call.

The owner's ruling ("改成这三个每次都弹卡": these three ask on a card every time)
must hold whatever the agent's spec auto-approves, and ``upload``, which hands a
page files from this computer, asks the same way. The default spec grants the
whole ``@kirocrew-core`` server in ``allowedTools``, so the host never raises a
permission request for a ``browser`` call and no approval surface sees it. This
check runs in the op route instead (:func:`kiro_crew.dashboard.handlers.browser_ops.run_browser_op`),
which every call reaches:

* a call with no answered card opens one on the caller's own dashboard slot, a
  blocking agent ask (``/api/agent-ask/*``) with two choices, Allow and Deny, and
  answers ``needs_approval`` with its ``ask_id``; the ``browser`` tool parks on
  that ask and posts the call again with ``approval: <ask_id>`` once it is answered;
* a call that carries ``approval`` runs only when that ask belongs to the same
  session, was opened for this exact call (op and arguments, by digest), and was
  answered Allow; the ask is then spent, so one answer runs one call;
* where nobody can answer (no dashboard slot, no open window, a Crew Mode worker),
  the call is refused;
* the card shows the whole call (the script and the tab it runs in, the DevTools
  method and its params, the address with its query and headers, or the files), redacted as every question card is,
  so a call too long to show is refused rather than cut.

Every ``js``, ``cdp``, ``http_get`` and ``upload`` reaches this route, whatever surface or
approval let the tool call through, so no approval surface needs a rule of its own.

The ask lives in the dashboard's own agent-ask record, which drops it after its
window; no other store holds an approval.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from kiro_crew.browser_harness import ops as harness_ops

logger = logging.getLogger(__name__)

#: The ops that ask on every call, and what each can do, for the card.
OPS: dict[str, str] = {
    "js": "runs a script that can read and change anything the page shows",
    "cdp": "can read every site's cookies and control every tab",
    "http_get": "fetches a URL directly, outside the browser",
    "upload": "sends files from this computer to the page",
}
#: The two choices on the card; only :data:`ALLOW` runs the call.
ALLOW = "Allow"
DENY = "Deny"
#: The ask record's field naming the call it was opened for.
_CALL_FIELD = "browser_call"
#: The longest call the card shows. The card shows the whole call, so a longer one
#: is refused rather than cut: the person must see everything an Allow runs.
SHOWN_MAX = 500

NOBODY_TEXT = (
    "js, cdp, http_get and upload ask the user on a card every time, and this "
    "session has no dashboard window that can show one, so the step was not run"
)
DENIED_TEXT = "the user did not allow this step on its card, so it was not run"


def _digest(op: str, args: dict[str, Any]) -> bytes:
    from kiro_crew.browser_host_trust import _call_digest

    return _call_digest(op, args)


def _shown(op: str, args: dict[str, Any]) -> str:
    """The whole call as the person reads it on the card: everything an Allow runs."""
    if op == "cdp":
        params = json.dumps(args.get("params") or {}, ensure_ascii=False, default=str)
        return f"cdp {args.get('method') or ''} {params}"
    if op == "http_get":
        shown = f"http_get {args.get('url') or ''}"
        headers = args.get("headers")
        if headers is not None:
            shown += "\n" + (
                "\n".join(f"{k}: {v}" for k, v in headers.items())
                if isinstance(headers, dict)
                else json.dumps(headers, default=str)
            )
        return shown
    if op == "upload":
        paths = args.get("path")
        listed = paths if isinstance(paths, list) else [paths]
        return f"upload into {args.get('ref') or ''}:\n" + "\n".join(str(p) for p in listed)
    target = args.get("target_id")
    where = f" (in tab or frame {target})" if target is not None else ""
    return f"js{where} {args.get('expression') or ''}"


def card_notice(op: str) -> str:
    """What the step can do, under the card's Allow."""
    return f"This step {OPS[op]}. It asks every time, even if you trust all tools."


def card_questions(op: str, args: dict[str, Any]) -> list[dict[str, Any]]:
    """The card for one call: the whole call, what it can do, Allow or Deny."""
    return [
        {
            "question": _shown(op, args),
            "header": f"Browser {op}",
            "options": [{"label": ALLOW, "description": card_notice(op)}, {"label": DENY}],
            "multiSelect": False,
        }
    ]


def _allowed(ask: dict[str, Any]) -> bool:
    future = ask.get("future")
    if future is None or not future.done() or ask.get("reason") != "answered":
        return False
    answers = future.result() or {}
    questions = ask.get("questions") or []
    return bool(questions) and answers.get(questions[0].get("question")) == ALLOW


def take_approval(
    state: Any, session_key: str, op: str, args: dict[str, Any], approval: str
) -> dict[str, Any] | None:
    """``None`` when *approval* is an Allow answer to this very call, else the refusal.

    Spends the answer either way it is read, so a second post of the same ask runs
    nothing.
    """
    asks = getattr(state, "_agent_asks", None)
    ask = asks.get(approval) if isinstance(asks, dict) else None
    if (
        not isinstance(ask, dict)
        or ask.get("session_key") != session_key
        or ask.get(_CALL_FIELD) != _digest(op, args)
    ):
        return harness_ops.refusal("approval_mismatch", "that card was not for this step")
    ask[_CALL_FIELD] = None
    if not _allowed(ask):
        return harness_ops.refusal("not_allowed", DENIED_TEXT)
    return None


async def open_card(state: Any, session_key: str, op: str, args: dict[str, Any]) -> dict[str, Any]:
    """Put this call's card on the caller's slot: ``needs_approval`` or a refusal."""
    if len(_shown(op, args)) > SHOWN_MAX:
        return harness_ops.refusal(
            "too_long_for_card",
            f"the card shows the whole {op} call so the user sees all of it, and this one "
            f"is over {SHOWN_MAX} characters; make it shorter or split it into steps",
        )
    from kiro_crew.dashboard.handlers.ask_question import (
        _crew_worker_refusal,
        _slot_key_from_session,
    )

    slots = getattr(state, "_slots", None)
    slot_key = _slot_key_from_session(session_key) if session_key.startswith("dashboard:") else ""
    if not slot_key or not isinstance(slots, dict) or slot_key not in slots:
        return harness_ops.refusal("needs_person", NOBODY_TEXT)
    if _crew_worker_refusal(state, slot_key, session_key) is not None:
        return harness_ops.refusal(
            "needs_person",
            "js, cdp, http_get and upload ask the user on a card every time, and a Crew Mode "
            "worker does not put cards in front of the user: report a question to your "
            "conductor instead",
        )
    ask_id = uuid.uuid4().hex
    try:
        clients = await state.open_agent_ask(
            ask_id, slot_key, session_key, card_questions(op, args)
        )
    except Exception:  # noqa: BLE001 - no card is no approval
        logger.warning("browser %s: could not open its card", op, exc_info=True)
        clients = 0
    ask = state._agent_asks.get(ask_id) if clients else None
    if not isinstance(ask, dict):
        return harness_ops.refusal("needs_person", NOBODY_TEXT)
    ask[_CALL_FIELD] = _digest(op, args)
    return {"ok": False, "code": "needs_approval", "ask_id": ask_id}


async def gate(
    state: Any, session_key: str, op: str, args: dict[str, Any], approval: str
) -> dict[str, Any] | None:
    """``None`` when *op* may run now; else the route's answer (a card to wait on, or a refusal)."""
    if op not in OPS:
        return None
    if approval:
        return take_approval(state, session_key, op, args, approval)
    return await open_card(state, session_key, op, args)


__all__ = [
    "ALLOW",
    "DENY",
    "OPS",
    "card_notice",
    "card_questions",
    "gate",
    "open_card",
    "take_approval",
]
