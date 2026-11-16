"""The gateway's ``[headless run cli:<id>]`` marker on an owner's unattended CLI send.

``kirocrew chat --headless`` sends its seed through the owner's ``POST /api/chat``
with ``"unattended": true`` and a ``run_id``. Nobody is at the dashboard for that
turn, so the gateway opens it with its own honest marker, which the goal-conductor
skill lists with the other nobody-typed openers (``[cron notification``,
``[work-ledger wake]``): a conductor dispatches its first round instead of waiting at its
plan gate for a go-ahead nobody can give. Design: rfc-builtin-eval.md §12.3.

Boundaries:

* Only the marker this module prepends counts. Every caller's text, the owner's
  own included, has a marker-shaped opener defanged to ``[quoted: headless run``
  (the same normalize-then-match rule ``session_messaging`` applies to a forged
  ``[sent by session``), so typing the prefix cannot skip a gate.
* Only the dashboard owner may ask for it: an app token, an internal caller, a
  member or teammate gets 403 ``unattended_not_owner``; a ``run_id`` that is not
  1-64 of ``[A-Za-z0-9_-]`` gets 400 ``invalid_run_id``.
* A seed that opens with ``/`` or ``@`` gets 400 ``unattended_command_seed``: with
  the marker first, a command or saved prompt would not run.
* Every marked send is written to the security event log with the run id and slot.
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any

from aiohttp import web

from kiro_crew.context_assembly.markers import _apply_marker_spans, _marker_spans

#: The opener the gateway writes. Also listed in the goal-conductor skill.
HEADLESS_MARKER = "[headless run cli:{run_id}]\n\n"
_QUOTED = "[quoted: headless run"
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_PS_OPENERS = "".join(chr(cp) for cp in range(0x10000) if unicodedata.category(chr(cp)) == "Ps")
_FORGED_RE = re.compile(rf"[{re.escape(_PS_OPENERS)}]\s*headless\s*run\b", re.IGNORECASE)


def neutralize(text: str) -> str:
    """*text* with every ``[headless run``-shaped opener defanged."""
    return _apply_marker_spans(text, _marker_spans(text, (_FORGED_RE,)), _QUOTED)


def _refuse(status: int, error: str, code: str) -> web.HTTPException:
    """The refusal as an aiohttp HTTP exception carrying the usual JSON body."""
    cls = web.HTTPForbidden if status == 403 else web.HTTPBadRequest
    return cls(text=json.dumps({"error": error, "code": code}), content_type="application/json")


def apply(request: web.Request, body: dict[str, Any], message: str) -> str:
    """The message to dispatch. Raises the refusal when the unattended ask is not admitted."""
    message = neutralize(message)
    if body.get("unattended") is not True:
        return message
    from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request

    if request.get("internal_auth") is True or not is_owner_dashboard_request(request):
        _audit(request, body, "denied", "unattended_not_owner")
        raise _refuse(
            403, "only the dashboard owner may send an unattended turn", "unattended_not_owner"
        )
    run_id = body.get("run_id")
    if not isinstance(run_id, str) or not _RUN_ID_RE.match(run_id):
        _audit(request, body, "denied", "invalid_run_id")
        raise _refuse(400, "run_id must be 1-64 of A-Z a-z 0-9 _ -", "invalid_run_id")
    if message.lstrip()[:1] in ("/", "@"):
        # The marker goes first, so the turn reads it as the first word: a slash
        # command, quick prompt or `@prompt` would reach the model as literal text.
        # shortcut: refused rather than run wrong; add the marker after command
        # parsing and prompt expansion when headless runs need them.
        _audit(request, body, "denied", "unattended_command_seed")
        raise _refuse(
            400,
            "an unattended seed cannot open with / or @ (commands and saved prompts)",
            "unattended_command_seed",
        )
    _audit(request, body, "allowed", "")
    return HEADLESS_MARKER.format(run_id=run_id) + message


def _audit(request: web.Request, body: dict[str, Any], outcome: str, error: str) -> None:
    from kiro_crew.sel import sel

    run_id = body.get("run_id")
    try:
        sel().log_api_access(
            caller=str(request.get("user") or request.get("app") or "unknown"),
            operation="chat_headless_run",
            outcome=outcome,
            source="dashboard",
            resources=f"slot={str(body.get('slot') or '')[:128]} run={str(run_id)[:64] if isinstance(run_id, str) else ''}",
            error=error,
        )
    except Exception:  # pragma: no cover - audit is best-effort, never the refusal
        pass
