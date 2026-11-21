"""The in-band card ``js``, ``cdp``, ``http_get`` and ``upload`` wait on.

The owner's ruling ("改成这三个每次都弹卡": these three ask on a card every time)
must hold whatever the agent's spec auto-approves, and ``upload``, which hands a
page files from this computer, asks the same way, except on a site the person
trusted for reading, clicking, typing and uploading: that "act" grant covers
``upload`` on its host (:func:`upload_grant_host`). The default spec grants the
whole ``@kirocrew-core`` server in ``allowedTools``, so the host never raises a
permission request for a ``browser`` call and no approval surface sees it. This
check runs in the op route instead (:func:`kiro_crew.dashboard.handlers.browser_ops.run_browser_op`),
which every call reaches:

* an ``upload`` on a host the caller's slot holds a live "act" site grant for runs
  without a card (:func:`upload_grant_host`): the op route hands that host to the
  op, which reads the tab's real address once it is bound and refuses
  ``trust_scope_changed`` off that host, and refuses files under the strict
  sandbox's hidden folders. A "read" grant, "Trust all tools" and an app's slot
  never cover it;
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

import asyncio
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
    "js, cdp, http_get and upload ask the user on a card (upload not on a site they "
    "trusted for reading, clicking, typing and uploading), and this session has no "
    "dashboard window that can show one, so the step was not run"
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
        how = {"chooser": " (clicks it, then fills its file picker)", "drop": " (drops on it)"}
        target = args.get("ref")
        if target is None:
            x, y = harness_ops._number(args.get("x")), harness_ops._number(args.get("y"))
            target = f"the point ({x:g}, {y:g})" if x is not None and y is not None else "?"
        head = f"upload into {target}{how.get(str(args.get('via')), '')}"
        return head + ":\n" + "\n".join(str(p) + _entry_kind(p) for p in listed)
    target = args.get("target_id")
    where = f" (in tab or frame {target})" if target is not None else ""
    return f"js{where} {args.get('expression') or ''}"


def _entry_kind(entry: Any) -> str:
    """What an ``upload`` entry stands for when it is more than one file, for the card.

    A folder or a pattern is one line that can send up to 100 files; the card says
    so, so an Allow is not given for files the person did not know were in it.
    """
    import glob
    import os

    from kiro_crew import hooks

    if not isinstance(entry, str) or not entry:
        return ""
    path = os.path.expanduser(entry) if entry.startswith("~") else entry
    if not os.path.isabs(path):
        return ""
    # The shared screen first, and only its answer is probed: on Windows a probe of
    # a UNC path opens a connection.
    checked = hooks.validate_file_path(path)
    if checked is None:
        return ""
    if not os.path.lexists(checked):
        return " (pattern: every matching file)" if glob.has_magic(checked) else ""
    return (
        " (folder: every file inside except hidden files and links)"
        if os.path.isdir(checked)
        else ""
    )


def card_notice(op: str) -> str:
    """What the step can do, under the card's Allow."""
    if op == "upload":
        return (
            f"This step {OPS[op]}. It asks each time, even if you trust all tools, except "
            "on a site you trusted for reading, clicking, typing and uploading from an "
            "approval card's Trust menu (offered when your agent asks before each step)."
        )
    return f"This step {OPS[op]}. It asks every time, even if you trust all tools."


def card_questions(op: str, args: dict[str, Any], shown: str | None = None) -> list[dict[str, Any]]:
    """The card for one call: the whole call, what it can do, Allow or Deny."""
    return [
        {
            "question": _shown(op, args) if shown is None else shown,
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
    # Off the loop: an upload's entries are probed on disk (a folder, a pattern).
    shown = await asyncio.to_thread(_shown, op, args)
    if len(shown) > SHOWN_MAX:
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
            "js, cdp, http_get and upload ask the user on a card, and a Crew Mode worker "
            "does not put cards in front of the user: report a question to your "
            "conductor instead",
        )
    ask_id = uuid.uuid4().hex
    try:
        clients = await state.open_agent_ask(
            ask_id, slot_key, session_key, card_questions(op, args, shown)
        )
    except Exception:  # noqa: BLE001 - no card is no approval
        logger.warning("browser %s: could not open its card", op, exc_info=True)
        clients = 0
    ask = state._agent_asks.get(ask_id) if clients else None
    if not isinstance(ask, dict):
        return harness_ops.refusal("needs_person", NOBODY_TEXT)
    ask[_CALL_FIELD] = _digest(op, args)
    return {"ok": False, "code": "needs_approval", "ask_id": ask_id}


def upload_grant_host(state: Any, session_key: str, op: str) -> str:
    """The host whose live "act" site grant covers this ``upload``, or ``""``.

    The host is the page the live hub last saw the chat on. The op route passes it
    down to the op itself, which re-reads the bound tab's real address before the
    files go (``browser_host_trust.recheck``), so a page that moved in between gets
    nothing. An app's slot never gets one: its ``browserSites`` does not cover
    ``upload``, and neither does a person's grant held on that slot. A call the op
    then finds reaching a folder the strict sandbox hides answers ``needs_card``, and
    the route puts it on the card after all, where the person decides.
    """
    from kiro_crew import browser_host_trust as trust
    from kiro_crew.browser_harness.live_hub import get_live_hub
    from kiro_crew.browser_harness.op_classes import CLASS_ACT
    from kiro_crew.dashboard.handlers.ask_question import _slot_key_from_session

    if op != "upload" or not session_key.startswith("dashboard:"):
        return ""
    slots = getattr(state, "_slots", None)
    slot = slots.get(_slot_key_from_session(session_key)) if isinstance(slots, dict) else None
    if slot is None or getattr(slot, "_app", None):
        return ""
    sess = get_live_hub().get(trust.browser_session_key(session_key))
    host = trust.page_host(getattr(sess, "url", "") if sess is not None else "")
    if not host or not trust.site_grant_covers(trust.trust_key(host, CLASS_ACT), slot):
        return ""
    return host


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
    "upload_grant_host",
]
