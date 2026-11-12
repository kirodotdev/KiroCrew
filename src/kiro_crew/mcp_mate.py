"""MCP server ``kirocrew-mate`` — a crewmate changes its own record.

The tools are the crewmate's own settings, each named for what it does, in two
tiers that follow who owns the field:

* **Direct** -- ``update_self``: the presentation the crewmate owns (its display
  name, its description, its ghost face). The gateway writes the record at once
  and draws one row in the crewmate's chat with an **Undo** the user can press;
  nothing waits on a card. Only when the user has just asked for the change:
  the tool description says so, and the gateway admits only a turn the user
  sent (:mod:`kiro_crew.dashboard.handlers.member_self`).
* **Catalog** -- ``find_capabilities`` reads what the crewmate's worker has and
  what it could be given (:mod:`kiro_crew.dashboard.handlers.member_capability_catalog`);
  a read, admitted on any turn in the crewmate's own thread.
* **Goals** -- ``goal_read``, ``key_result_progress`` and ``save_goal_report``
  read the goal, record key-result readings and file a weekly or monthly
  report at once, with no card: a reading is the crewmate's own measurement
  and a report its own account. Admitted on any turn in the crewmate's own
  dashboard thread, including the Daily Goal Reminder's hidden turn
  (:mod:`kiro_crew.dashboard.handlers.member_goals`).
* **Notes** -- ``save_notes`` replaces the crewmate's own briefing, the
  ``[CURRENT ASSIGNMENT]`` section it keeps for itself
  (:mod:`kiro_crew.member_notes`). Its chat runs on the conductor, which has no
  file tool, so this is how it keeps them. Admitted like the goal tools.
* **Card-backed** -- ``set_model``, ``set_approval_mode``,
  ``request_capabilities``, ``set_goal`` (add or change one goal), ``set_key_results``
  (how the goals are measured, the whole set at once): what the crewmate
  works toward, runs on and what it may do
  without asking. These are the user's to grant, so each becomes the
  existing change card (:mod:`kiro_crew.dashboard.change_cards`) for the caller's
  own member, and nothing changes until the user presses it. The crewmate names
  the ask; the gateway fixes the kind and the member. The outcome reaches the
  crewmate on its next turn, the way every card outcome does. They need a turn
  the user sent, except that the gateway's own Monthly review of this crewmate
  may raise ``set_goal`` and ``set_key_results``.

Why its own server
------------------
The server is the unit of assignment, authorization and governance: an operator
or a policy withholds the whole set at once, and the set's grants
(``agent._MATE_AUTO_GRANTS``) are reviewed together. It is mounted on every
crewmate; the guide server (``kirocrew-guide``) stays what it is, the user's
help with the dashboard. Every tool here acts on the CALLING crewmate -- never
on another, never on a name passed in -- and refuses off the dashboard (a
channel, a schedule, a subagent, the CLI) with a one-line reason.

Stateless: every call is one round trip to the gateway, which owns the record,
the undo and the cards. Nothing here holds per-caller data between calls.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from kiro_crew.mcp_core import (
    _post,
    _resolve_session_key,
    require_strict_session_key,
)
from kiro_crew.mcp_mate_tool_list import (  # noqa: F401
    _CARD_NOTE,
    _ONLY_WHEN_ASKED,
    GHOST_TRAIT_VOCABULARY,
    SERVER,
    SERVER_NAME,
    SERVER_VERSION,
    _list_tools,
    _tool_definitions,
)
from kiro_crew.mcp_shared import call_tool_with_logging, run_mcp_stdio_loop
from kiro_crew.platform import redact_via_context as redact
from kiro_crew.validation import MCP_MATE_SCHEMAS, validate_tool_args

logger = logging.getLogger(__name__)


def _strict_session_key() -> tuple[str, str]:
    return require_strict_session_key(
        "this session's identity could not be verified strictly, so it is not a "
        "Bot's own chat (a subagent has none; ask from the parent session)",
        SERVER_NAME,
    )


#: Gateway refusal codes that mean "this turn is not a crewmate's own dashboard
#: turn" (``handlers.guide._resolve_agent_caller`` and ``handlers.member_self``).
_OFF_DASHBOARD_CODES = frozenset(
    {
        "no_live_slot",
        "no_dashboard_turn",
        "channel_caller",
        "subagent_caller",
        "unattended_caller",
        "app_caller",
        "app_scoped_caller",
        "missing_session_key",
        "not_user_turn",
        "unattested_caller",
        "not_a_crewmate",
    }
)


def _off_dashboard(name: str, reason: str) -> str:
    reason = reason.removeprefix("Error: ").strip().rstrip(".")
    return redact(
        f"Error: {name} needs your own chat in the dashboard: {reason}. Nothing changed; "
        "do not say it did."
    )


def _error(d: dict[str, Any]) -> str | None:
    if d.get("error"):
        code = d.get("code")
        return redact(f"Error: {d['error']}" + (f" ({code})" if code else ""))
    return None


def _error_for(name: str, d: dict[str, Any]) -> str | None:
    if d.get("error") and d.get("code") in _OFF_DASHBOARD_CODES:
        return _off_dashboard(name, str(d["error"]))
    return _error(d)


def _render(result: dict[str, Any]) -> str:
    return redact(json.dumps(result, ensure_ascii=False, indent=2, default=str))


def _validate_args(name: str, args: dict[str, Any]) -> dict[str, Any]:
    schema = MCP_MATE_SCHEMAS.get(name)
    if schema is None:
        return args
    return validate_tool_args(args, schema)


_TOOL_NAMES = frozenset(MCP_MATE_SCHEMAS)

#: Tool -> the gateway's request name for a card-backed ask.
_PROPOSALS = {
    "set_model": "model",
    "set_approval_mode": "approval_mode",
    "request_capabilities": "capabilities",
    "set_goal": "goal",
    "set_key_results": "key_results",
    "set_check_ins": "check_ins",
    "offer_dashboard_page": "dashboard_page",
}


def _goal_call(name: str, args: dict[str, Any], sk: str) -> str:
    """``goal_read`` / ``key_result_progress`` / ``save_goal_report``: direct, no card."""
    if name == "goal_read":
        d = _post("/api/member/self/goals/read", {}, session_key=sk)
        refused = _error_for(name, d)
        return refused or _render(d)
    if name == "key_result_progress":
        d = _post(
            "/api/member/self/goals/key-result",
            {"id": args.get("id", ""), "current": args.get("current", "")},
            session_key=sk,
        )
        refused = _error_for(name, d)
        if refused:
            return f"{refused}\nThe reading was not saved; do not say it was."
        return _render({**d, "next": "Saved; it shows on your Goals tab."})
    if name == "save_goal_report":
        d = _post(
            "/api/member/self/goals/report",
            {k: args.get(k, "") for k in ("kind", "title", "markdown", "html")},
            session_key=sk,
        )
        refused = _error_for(name, d)
        if refused:
            return f"{refused}\nThe report was not saved; do not say it was."
        return _render(
            {
                **d,
                "next": "Saved and the user was notified. Reply with one short line "
                f"pointing at it ([{d.get('title', 'report')}]({d.get('url', '')})); "
                "keep the report's text out of the chat.",
            }
        )
    return f"Error: unknown tool '{name}'"


def _call_tool_inner(name: str, args: dict[str, Any]) -> str:
    if name not in _TOOL_NAMES:
        return f"Error: unknown tool '{name}'"
    sk, err = _strict_session_key()
    if err:
        return _off_dashboard(name, err)

    if name in (
        "goal_read",
        "key_result_progress",
        "save_goal_report",
    ):
        return _goal_call(name, args, sk)

    if name == "save_notes":
        d = _post("/api/member/self/notes", {"text": args.get("text", "")}, session_key=sk)
        refused = _error_for(name, d)
        if refused:
            return f"{refused}\nYour notes were not saved; do not say they were."
        return _render({**d, "next": "Saved; they open your next turn."})

    if name == "find_capabilities":
        d = _post("/api/member/self/capabilities", {"query": args.get("query", "")}, session_key=sk)
        refused = _error_for(name, d)
        return refused or _render(d)

    if name == "update_self":
        fields = {k: args[k] for k in ("display_name", "description", "avatar") if k in args}
        if not fields:
            return "Error: update_self needs at least one of display_name, description, avatar."
        d = _post("/api/member/self/update", {"fields": fields}, session_key=sk)
        refused = _error_for(name, d)
        if refused:
            return f"{refused}\nNothing about you changed; do not say it did."
        changed = d.get("changed") or []
        if not changed:
            return _render({**d, "next": "Those were already your values; nothing changed."})
        short_name = str(d.get("member") or "").strip()
        shown = {
            "display_name": "Your name now shows as "
            + str((d.get("after") or {}).get("display_name") or "")
            + " everywhere in the dashboard; answer to it from now on. The new name is "
            "a label: other Bots still reach you by your short name"
            + (f", {short_name}" if short_name else "")
            + ", which does not change. Tell the user that in the same reply.",
            "description": "Your description is updated on the roster and in your prompt.",
            "avatar": "Your face is updated everywhere the dashboard shows you.",
        }
        return _render(
            {
                **d,
                "next": " ".join(shown[k] for k in changed if k in shown)
                + " The user sees one line about it in this chat with an Undo; say what "
                "changed in one sentence and do not offer to undo it yourself.",
            }
        )

    params = {k: v for k, v in args.items() if k != "reason"}
    body: dict[str, Any] = {"request": _PROPOSALS[name], "params": params}
    if args.get("reason") is not None:
        body["reason"] = args["reason"]
    d = _post("/api/member/self/propose", body, session_key=sk)
    refused = _error_for(name, d)
    if refused:
        return f"{refused}\nNo card was shown; answer in words."
    if d.get("opt_in"):
        # The crewmate's Dynamic capabilities is off: its first request raised the
        # Preview opt-in card, and the request itself waits on the user's answer.
        return _render(
            {
                **d,
                "next": "This is a Preview opt-in card, not your request: it asks the "
                "user to turn on Dynamic capabilities for you. If they press Turn on, "
                "your request becomes its own card on its own; do not ask again. "
                "Refer to the card once and end your turn.",
            }
        )
    return _render(
        {
            **d,
            "next": "The card is in this chat above your reply. Refer to its button "
            "once and end your turn; nothing changes until the user presses it.",
        }
    )


def _call_tool(name: str, raw_args: dict[str, Any]) -> str:
    """Guarded entry point — schema validation and SEL audit live in the wrapper."""
    return call_tool_with_logging(
        name,
        raw_args,
        _validate_args,
        _call_tool_inner,
        session_key=_resolve_session_key() or SERVER_NAME,
        downstream_service=SERVER_NAME,
    )


#: Consumes the per-call caller block the gateway injects rather than reading
#: identity from its own process, and refuses a caller the gateway cannot name.
#: Kept in step with ``mcp_discovery._MANAGED_SERVERS_CALLER_AWARE``.
ADVERTISE_CALLER_IDENTITY = SERVER.advertise_caller_identity


def run_mcp_server() -> None:
    """Run the MCP stdio server — reads JSON-RPC from stdin, writes to stdout."""
    run_mcp_stdio_loop(
        SERVER_NAME, SERVER_VERSION, _list_tools, _call_tool, **SERVER.loop_options()
    )


if __name__ == "__main__":  # pragma: no cover - process entry
    logging.basicConfig(level=logging.INFO)
    run_mcp_server()
