"""MCP server ``kirocrew-guide`` — an agent guides the human through the dashboard.

The agent names one or more REGISTERED actions (show a setting, create a
crewmate, open the existing add-MCP-server page) and the dashboard walks the
person through them with a non-modal pointer. The agent never sends a route,
selector, markup or code, and it never performs a mutation: the human's own click
on the existing owner-only save does, and only the gateway's record of that save
completes a mutation step.

The same server carries change cards (``list_change_kinds`` / ``propose_change`` /
``get_change_status``): the agent proposes one registered kind of change with its
parameters, the gateway derives what changes, the risk and the exact requests,
and only the owner's browser applies it through the existing settings routes
(:mod:`kiro_crew.dashboard.handlers.change_cards`). None of these tools can
change anything by itself.

It also carries ``find_ui``: a read-only search of the packaged dashboard
location index (``docs/ui-index.generated.json``, plus the build-time auto tier
``static/dist/ui-index.auto.json`` when the dashboard bundle is built;
:mod:`kiro_crew.ui_index`), so "where is X?" is answered with a click path
generated from the dashboard source rather than recalled. With ``area`` instead
of ``query`` it lists one area of that index whole (``ui_index.browse_ui``,
paged under the same byte cap), so a search miss can be answered by choosing
the entry that means the question. Like ``search_docs`` it reads packaged files
for its answer; it makes two bounded gateway calls, neither of which changes
the static answer's content: which language the user's dashboard renders
(``GET /api/guide/agent/language``), so labels are spelled as the screen
spells them, and a live observation (``POST /api/guide/agent/observe``) that
adds ``live`` per result.

It also carries ``search_docs``: a read-only search and page read over the
packaged user docs (``kiro_crew/docs/*.md``), so a how-to question ("how do I
connect Slack?") is answered from the documentation without a shell command or a
file read that would stop for approval. It reads only files of that one
directory, named from its own listing, and calls no gateway route.

Why this is its own server
--------------------------
The server is the unit of assignment, authorization and governance: an
operator or a policy can withhold the whole guide set from an agent at once,
and the set's grants (``agent._GUIDE_AUTO_GRANTS``) are reviewed together. It is
mounted on every crewmate and dashboard session (not ``opt_in`` in
``agent._MANAGED_MCP_SERVERS``). Its guide and card tools only work for a
conversation open in a dashboard tab, so off the dashboard (Slack, Discord, the
CLI, a schedule, a subagent) each of them refuses with a one-line reason
(:func:`_off_dashboard`), while ``find_ui`` and ``search_docs`` still answer in
text.

The behaviour rules a guide or card answer depends on live in the tool
descriptions and in each result's ``next`` hint, not in any one agent's prompt,
so they reach every agent and survive context compaction.

Why no tool takes a session, slot or tab
----------------------------------------
The guide a call starts, reads or cancels is the CALLING session's own, resolved
strictly (``require_strict_session_key``) and sent as the verified key, so the
value that was checked is the value that is used. The gateway derives the slot
from that key against its live slot table. A subagent has no tab of its own and
is refused rather than walked up to its parent's.

Stateless: every call is one round trip to the gateway, which owns all guide
state. Nothing here holds per-caller data between calls.
"""

from __future__ import annotations

import functools
import json
import logging
import re
import urllib.parse
from pathlib import Path
from typing import Any

from kiro_crew.mcp_core import (
    _get,
    _post,
    _resolve_session_key,
    require_strict_session_key,
)
from kiro_crew.mcp_shared import call_tool_with_logging, run_mcp_stdio_loop
from kiro_crew.platform import redact_via_context as redact
from kiro_crew.validation import MAX_SHORT_STRING, MCP_GUIDE_SCHEMAS, validate_tool_args

logger = logging.getLogger(__name__)

SERVER_NAME = "kirocrew-guide"
SERVER_VERSION = "1.0.0"

_ACTION_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {
            "type": "string",
            "enum": ["settings.show", "crewmate.create", "mcp.open_add", "ui.show", "ui.find"],
            "description": "A registered action id from guide_list_actions.",
        },
        "params": {
            "type": "object",
            "description": "The action's parameters, per its params_schema.",
        },
        "note": {
            "type": "string",
            "maxLength": 160,
            "description": (
                "Optional, your words shown under this action's final step: why it "
                "matters for what the user asked. Plain text, one line, no links "
                "or markup."
            ),
        },
    },
    "required": ["id"],
    "additionalProperties": False,
}


#: The browse areas named in ``find_ui``'s description (``ui_index.AREAS``),
#: listed here so the tool list needs no index read.
_FIND_UI_AREAS = (
    "sessions",
    "composer",
    "crewmates",
    "schedule",
    "artifacts",
    "apps",
    "connections",
    "customize",
    "notifications",
    "shell",
    "settings",
    "developer",
)


def _tool_definitions() -> list[dict[str, Any]]:
    return [
        {
            "name": "guide_list_actions",
            "description": (
                "List the registered dashboard actions you can guide the user "
                "through, each with its parameter schema and whether it changes "
                "anything. Call it before guide_start when unsure."
            ),
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "guide_start",
            "description": (
                "Offer the user a step-by-step pointer through 1-8 registered "
                "actions in THEIR dashboard tab for this conversation. Nothing "
                "changes until the user clicks the real Save/Create button, and "
                "you are told only the saved identity, never setting values or "
                "credentials. Returns the guide; delivered_clients=0 means it is "
                "queued until the tab loads, not shown. A new offer replaces this "
                "conversation's unfinished guide by itself (superseded lists it), so "
                "never check or cancel one first. Never say a guide is shown unless this "
                "returned one. With no guide_ref to offer, a ui.find "
                "action (a page and the control's on-screen name, from find_ui's "
                "find_ref or a docs page) still points at it. After offering "
                "it and any proposals the request needs, write ONE reply and end "
                "your turn; check progress later with guide_status."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "actions": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 8,
                        "items": _ACTION_ITEM_SCHEMA,
                    },
                    "intro": {
                        "type": "string",
                        "maxLength": 200,
                        "description": (
                            "Optional, your words shown on the offer card and the "
                            "first step: what this guide gets the user. Plain text, "
                            "one line, no links or markup."
                        ),
                    },
                },
                "required": ["actions"],
            },
        },
        {
            "name": "guide_status",
            "description": (
                "Read this conversation's guide: status (offered, active, "
                "target_missing, completed, cancelled, expired), the current action "
                "and step, and each finished action's result. Omit guide_id for "
                "the latest one. A paused guide's reason says why: gate_off (a "
                "setting is off; blocker.setting_id names it, the guide goes on "
                "once it is on and never changes it), needs_selection (nothing to "
                "choose yet; blocker.selection), predicate_unmet, ambiguous_target, "
                "not_found, stale_tab or build_mismatch. A ui.find action carries "
                "find: {result found|ambiguous|none, count, role}. ambiguous: "
                "the page lists the matches for the user, who picks one "
                "there; never offer ui.find again with a number as its "
                "container. not_found: the control is not on that page or "
                "in anything the tab could open, and the user is asked to open "
                "the menu or section holding it; give the path in words and "
                "say it was not on screen."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"guide_id": {"type": "string", "maxLength": 64}},
            },
        },
        {
            "name": "guide_cancel",
            "description": (
                "Stop this conversation's guide. Call it only when the user asks "
                "to stop the guide. A change the user already saved is not undone."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"guide_id": {"type": "string", "maxLength": 64}},
                "required": ["guide_id"],
            },
        },
        {
            "name": "list_change_kinds",
            "description": (
                "List the change-card kinds you can propose (settings, schedules, "
                "crewmates, capabilities, templates, MCP servers, connections, "
                "secrets, app trust, denied commands), each with its params schema "
                "and which fields the user may edit on the card."
            ),
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "find_setting",
            "description": (
                "Search the Settings page for a setting to change, e.g. 'shorter "
                "replies' or 'restore sessions'. Returns up to 10 matches: "
                "setting_id, label, path (the Settings click path), description, tab, "
                "writable (whether a "
                "setting.change card can change it), current_value and "
                "allowed_values; a list setting adds value_type 'string_list' "
                "and ops; restart_required marks the few settings that apply "
                "only after a restart. label and path are spelled as the user's "
                "dashboard shows them (label_locale): quote them unchanged. To turn "
                "something on or off, offer only the change current_value does not "
                "already have. Pass the setting_id to propose_change."
                " Keep findings for your final reply; if the request needs a proposal or guide, offer it before writing anything."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"query": {"type": "string", "maxLength": 200}},
                "required": ["query"],
            },
        },
        {
            "name": "get_member_capabilities",
            "description": (
                "Read one crewmate's current tools, tool approvals, MCP servers and "
                "skills in exactly the shape a crewmate.capabilities draft takes: "
                "rows {section, id, label, state, editable}, its template, and "
                "whether the draft needs enroll=true. Call it before proposing "
                "crewmate.capabilities. Your own approvals live on your own "
                "member key, from [MEMBER IDENTITY]."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"member": {"type": "string", "maxLength": 128}},
                "required": ["member"],
            },
        },
        {
            "name": "diagnose_settings",
            "description": (
                "Explain why Kiro Crew behaves differently from its defaults. Returns "
                "JSON: findings (read-only symptom checks of this install, problems "
                "first: id, status ok|warn|problem|unknown, summary, evidence, and "
                "fix, either {card: {kind, params}} to propose or {steps: [...]} for "
                "the user), non_default (every setting whose current value differs from "
                "the shipped default: key, store, setting_id and label when it is on "
                "the Settings page, current, default; a credential-like setting shows "
                "only whether it is set) and recent_changes (the newest 'Dashboard: "
                "...' change records, made by hand or by a card, with their dates). "
                "Pass topic (e.g. 'model', 'verbosity') to keep only matching rows. "
                "Read-only; call it first for any 'why' question."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"topic": {"type": "string", "maxLength": 200}},
            },
        },
        {
            "name": "propose_change",
            "description": (
                "Propose ONE change as a confirmation card in the user's dashboard "
                "chat for this conversation. You pick the kind and fill its params; "
                "the gateway writes what changes and how risky it is, and nothing "
                "changes until the user presses the card's button. `reason` (plain "
                "text, at most 500 characters) is shown separately as your "
                "reasoning. For a secret, propose secret.save with only the name: "
                "the user types the value into the card and you never see it. "
                "In a dashboard turn, prefer a card for a setting change the user "
                "asked for over a terminal command or a config edit. Never use a "
                "card to get past a denial, a block or a refusal: propose a "
                "denied-command, approval or secret change only when the user "
                "explicitly asked for that exact change. "
                "Returns the card, prepared but not applied. Make any other "
                "proposals or guide offers the request needs, then write ONE reply "
                "that refers to the button above and end your turn; never write "
                "the answer before proposing, and read the outcome next turn "
                "(or with get_change_status)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "description": "A kind id from list_change_kinds."},
                    "params": {"type": "object", "description": "Per the kind's params_schema."},
                    "reason": {"type": "string", "maxLength": 500},
                },
                "required": ["kind", "params"],
            },
        },
        {
            "name": "get_change_status",
            "description": (
                "Read one of this conversation's change cards: status (pending, "
                "applying, applied, partial, failed, cancelled, expired, undone), "
                "what changed and the result. Omit change_id for the latest."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"change_id": {"type": "string", "maxLength": 48}},
            },
        },
        {
            "name": "rename_self",
            "description": (
                "Change the name the user sees for you, in this chat, the roster "
                "and the sidebar. Call it only when the user has just told you what "
                "they want to call you; never on your own initiative and never to "
                "rename anyone else. Pass the name exactly as they gave it. Only a "
                "crewmate in its own chat can call it, from a message the user sent "
                "in the dashboard."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "minLength": 1, "maxLength": MAX_SHORT_STRING}
                },
                "required": ["name"],
                "additionalProperties": False,
            },
        },
        {
            "name": "search_docs",
            "description": (
                "Search the Kiro Crew user documentation for how a feature works or "
                "how to set something up, e.g. 'connect Slack' or 'one-time "
                "reminder'. Returns up to 5 pages: page, title and the matching "
                "lines. Pass `page` (a name from the results) to read that page; "
                "long pages continue from `offset`. Read-only; use it instead of a "
                "shell command or a file read."
                " Keep findings for your final reply; if the request needs a proposal or guide, offer it before writing anything."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "maxLength": 200},
                    "page": {"type": "string", "maxLength": 80},
                    "offset": {"type": "integer", "minimum": 0},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "find_ui",
            "description": (
                "Find where something is in the dashboard, e.g. 'older sessions' or "
                "'较早的会话'. Call it first for every where-is or how-do-I-find question "
                "and quote only what it returns: never guess or recall a location. Pass the user's words as `query` and the language of "
                "their words as `lang` (e.g. 'zh-CN') when known. Labels and paths "
                "come back in the language the user's dashboard shows "
                "(locale_source: dashboard), which can differ from the conversation's: "
                "quote them unchanged, never translated, because they must match the "
                "screen. Returns status ok|no_match|"
                "unavailable and up to 8 results: the click path (path labels, "
                "parent first), the route, prerequisites (requires: viewport, "
                "shown_by another control, preview_flag with the setting that turns "
                "it on, condition) and setting_id/guide_ref when there is one; "
                "a guide_ref is an action you can pass to guide_start as-is. "
                "A top-bar control has on_every_page: true and no route. "
                "Quote labels and routes exactly as returned; when ambiguous is "
                "true, name the results as alternatives instead of picking one. "
                "A result with description instead of label is a control whose "
                "on-screen text is live data (e.g. the model chip shows the model's "
                "name): describe it in your own words from description and its path, "
                "and never quote description as what the button says. "
                "A control whose text flips with state carries label_by_state; "
                "label is the one for the state the question implies. "
                "A result with tier: auto (conditions_unknown: true) only means a "
                "control with this label is on that page (or tab); its exact place "
                "and prerequisites are unknown. Relay it hedged, e.g. 'on the X page "
                "there should be a button called Y', never as a click-by-click path, "
                "and if the user cannot find it point them to the guide or docs. "
                "An auto result WITH a guide_ref (a control drawn by a reviewed "
                "shared control) may be shown with ui.show like any other. A "
                "result without a guide_ref may carry find_ref: a ui.find action "
                "(route, label, role) to pass to guide_start as-is; the user's tab "
                "then looks for that name on the page, inside menus and collapsed "
                "sections too. Prefer guide_ref, then find_ref; never say you "
                "cannot point at something while either is there. "
                "A result with caution: true (or a find_ref with caution) is a destructive control (delete, "
                "uninstall, clear, remove): offer its guide only when the user asked "
                "for that very action. Its caution_text is the page's own account of "
                "what pressing it removes and keeps: describe the consequences only "
                "from it, and without one say only that it is a removal and to read "
                "the page's warning first, never what it deletes or that it is "
                "permanent; the guide only points, the user still presses it. "
                "needs_object means the question named an action but not what to "
                "act on: ask what they want to add/delete instead of guessing. "
                "prose_note means condition text is English: translate it into "
                "the reply's language when relaying. "
                "no_match means not in "
                "this build's index, not that the feature is missing; with "
                "auto_tier: unavailable, unregistered controls were not searched at "
                "all, so a no_match there says even less. Read-only. Each result "
                "carries live: what the user's own tab shows right now (pointable, "
                "offscreen, hidden, unmounted, disabled, ambiguous, unknown, with "
                "observed_at), or not_observed when their tab did not answer; it "
                "never reads page text. A result may also carry blocker: "
                "gate_off (a setting such as developer mode is off; setting_id "
                "names it, or is null when no setting turns it on), "
                "needs_selection (the user must first open one item, e.g. a "
                "crewmate, named in selection), "
                "predicate_unmet (something the path needs is not true in their "
                "tab, named in predicates; see conditions for what each means), "
                "hidden_in_scope (a sidebar, menu or panel on the path is closed, "
                "named in scopes), or not_observed. A blocker is for you, not the "
                "user: when a result has a guide_ref, offer that guide (it shows the "
                "same blocker itself) and never mention the blocker; without one, "
                "say in plain words what to open first. "
                "Keep findings for your final reply; if the request needs a proposal "
                "or guide, offer it before writing anything. "
                "A control that decides what an agent may do without asking (Settings "
                "→ Security, approvals, the Approval mode picker, Computer Use, Secrets) "
                "is answered in words with its path: no guide, and never an offer to "
                "change it yourself. "
                "BROWSE: pass `area` instead of `query` to list every indexed entry "
                "of one area (id, label, path, short needs, tier), e.g. when a "
                "search misses or is ambiguous. Areas: "
                + ", ".join(_FIND_UI_AREAS)
                + "; settings lists its settings.<tab> sub-areas (settings.chat, "
                "settings.display, ...). A long listing is paged: truncated: true "
                "with next_offset, passed back as `offset`."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1, "maxLength": 200},
                    "lang": {"type": "string", "maxLength": 16},
                    "surface": {
                        "type": "string",
                        "maxLength": 64,
                        "description": "Optional: keep only one surface, e.g. 'chat' or 'settings'.",
                    },
                    "area": {
                        "type": "string",
                        "maxLength": 82,
                        "description": "Browse instead of search: an area id from the list above, e.g. 'composer' or 'settings.chat'.",
                    },
                    "offset": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "Browse only: next_offset from the previous page.",
                    },
                },
                "additionalProperties": False,
            },
        },
    ]


def _list_tools() -> list[dict[str, Any]]:
    """Unconditional: reaching this process means a spec granted the set."""
    return _tool_definitions()


def _strict_session_key() -> tuple[str, str]:
    return require_strict_session_key(
        "this session's identity could not be verified strictly, so it has no "
        "dashboard tab (a subagent has none of its own; ask from the parent session)",
        SERVER_NAME,
    )


#: Gateway refusal codes that mean "this turn was not sent from the dashboard"
#: (``dashboard.handlers.guide._resolve_agent_caller``): not open in a tab, a
#: message from a messaging channel, a subagent, a scheduled run, an app, or no
#: session key at all.
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
    }
)


def _off_dashboard(name: str, reason: str) -> str:
    """The one-line refusal of a guide or card tool called without a dashboard tab."""
    reason = reason.removeprefix("Error: ").strip().rstrip(".")
    return redact(
        f"Error: {name} needs the dashboard: {reason}. Nothing was shown "
        "and nothing changed. Answer in words; find_ui and search_docs still work here."
    )


def _error_for(name: str, d: dict[str, Any]) -> str | None:
    """:func:`_error`, with an off-dashboard refusal said as one."""
    if d.get("error") and d.get("code") in _OFF_DASHBOARD_CODES:
        return _off_dashboard(name, str(d["error"]))
    return _error(d)


def _validate_args(name: str, args: dict[str, Any]) -> dict[str, Any]:
    schema = MCP_GUIDE_SCHEMAS.get(name)
    if schema is None:
        return args
    return validate_tool_args(args, schema)


#: Told to the model in the result of an offer, at the moment it writes the
#: reply: the card is the question, so the reply only frames it once. The text
#: follows the offer's real state: a queued offer is not on screen yet.
#: Appended to a refused ``guide_start``: the refusal is the whole outcome.
GUIDE_NOT_SHOWN_NOTE = (
    "No guide card was shown: nothing was added to the chat. Do not say you placed, "
    "showed or offered a card; answer in words with the location instead."
)


#: The instruction on a ``find_ui`` miss: search again with the control's words.
_FIND_UI_RETRY_NOTE = (
    "No match for these words. Before answering, call find_ui again with only the "
    "control's own words (e.g. 'add schedule', 'delete schedule', 'rename session'), "
    "or browse its area; never say a feature or control does not exist unless that "
    "and search_docs also miss. When they do, say plainly that the dashboard has no "
    "such control, without hedging ('it may be', 'it should be') and offering no card."
)
#: The instruction for a preview feature whose switch is not on in the user's tab.
_PREVIEW_NOTE = (
    "This is a preview feature, off unless its Developer setting is turned on. Answer "
    "in words: say it is a preview and where it will be once turned on. Offer no "
    "guide, and never guide to or offer to flip the Developer preview switch unless "
    "the user asks to turn the preview on. Never say you can show, point at or guide "
    "to it: no card follows. No reply option offers to take the user to Developer "
    "settings or to the switch either."
)
#: The instruction for results none of which carries a guide (a control of the
#: agent's own ceiling, the approval-mode picker; a page no rail entry opens).
_NO_GUIDE_NOTE = (
    "None of these results has a guide: answer in words with its path from the "
    "result. Never say you can show, point at or guide to it, and never offer a card."
)
#: A messaging channel's Settings sub-page (Slack, Telegram, ...).
_CHANNEL_SUB_PREFIX = "settings.sub.channels."
#: Added for a channel's page: its token fields are described, never guided to.
_CHANNEL_TOKENS_NOTE = (
    " The guide stops on that page. Its token fields are credentials: describe them "
    "in words (which token goes in which field, and where the page says to get it), "
    "never guide to a token field, and never ask for a token's value in chat."
)
#: The instruction for an ambiguous search no result of which can be guided to
#: on its own: no card follows, so the reply must not refer to one.
_AMBIGUOUS_NO_GUIDE_NOTE = (
    "These results tie and none is clearly the one asked about, so no guide is "
    "offered: ask which one the user means, or call find_ui again with the "
    "control's own words. Until guide_start has returned a guide, never write that "
    "a guide, a card or a highlight shows the way ('the guide above', 'I'll point "
    "you'): none is on the screen."
)


def _requirements(r: dict[str, Any]) -> list[dict[str, Any]]:
    """Every prerequisite on a result's placements, in placement order."""
    out: list[dict[str, Any]] = []
    for p in r.get("placements") or []:
        if isinstance(p, dict):
            out.extend(q for q in p.get("requires") or [] if isinstance(q, dict))
    return out


def _needs_preview(r: dict[str, Any]) -> bool:
    """A result behind a preview switch the user's tab does not show as on."""
    if not any(q.get("kind") == "preview_flag" for q in _requirements(r)):
        return False
    return (r.get("live") or {}).get("status") != "pointable"


def _guide_target(r: dict[str, Any]) -> str | None:
    """Where a result's guide leads: its ``guide_ref`` (else ``find_ref``) action and
    params, canonically, or None when it has neither."""
    ref = r.get("guide_ref") if isinstance(r.get("guide_ref"), dict) else r.get("find_ref")
    if not isinstance(ref, dict):
        return None
    return json.dumps(
        {"action_id": ref.get("action_id"), "params": ref.get("params")}, sort_keys=True
    )


#: Live statuses of a control on the page the user's tab shows now.
_ON_SCREEN = frozenset({"pointable", "offscreen"})


def _same_place(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Two results that are one control: the same location, or guides to one place."""
    a_id, a_to = str(a.get("id") or ""), _guide_target(a)
    return (bool(a_id) and a_id == str(b.get("id") or "")) or (
        a_to is not None and a_to == _guide_target(b)
    )


def _home(r: dict[str, Any]) -> tuple[int, str]:
    """How strongly a result's page is its feature's own home, and that page.

    2: the result is the page itself (the Webhooks page). 1: the control is
    drawn on the page its id's namespace names (``members.new`` on
    ``page.members``). 0: it is drawn somewhere else (``agents.add`` on
    Customize), or its page is unknown.
    """
    rid = str(r.get("id") or "")
    if r.get("kind") == "page" and rid.startswith("page."):
        return 2, rid
    placements = r.get("placements") or []
    first = placements[0] if placements and isinstance(placements[0], dict) else {}
    path = first.get("path") or []
    page = str(path[0].get("id") or "") if path and isinstance(path[0], dict) else ""
    if page.startswith("page.") and rid.split(".", 1)[0] == page[len("page.") :]:
        return 1, page
    return 0, page


def _tie_breakable(r: dict[str, Any]) -> bool:
    """A result a tie may be settled on: never one that removes something or
    sits under the agent's own ceiling (those are offered only when asked for
    by themselves, never by elimination)."""
    from kiro_crew.guide_catalog import is_trust_root

    ref = r.get("find_ref")
    # A find_ref is ``{action, params}``: its caution flag and route live in
    # ``params``, so that is where they are read.
    ref_params = ref.get("params") if isinstance(ref, dict) else None
    ref_params = ref_params if isinstance(ref_params, dict) else {}
    if r.get("caution") or ref_params.get("caution"):
        return False
    routes = [str(p.get("route") or "") for p in r.get("placements") or [] if isinstance(p, dict)]
    routes.append(str(ref_params.get("route") or ""))
    return not is_trust_root(str(r.get("id") or ""), [x for x in routes if x])


def _tie_pick(
    rows: list[dict[str, Any]], results: list[Any], tied: object
) -> dict[str, Any] | None:
    """The one tied result to guide to, or None to ask the user which.

    Tied results that are one control are guided to the first. Otherwise the
    tie is settled only by facts: the one control on the page the user's tab
    shows now, else the results on the feature's own page (the "New
    crewmate" of the Crewmates page over the one on Customize), first-ranked.
    Two candidates on different pages, or a pick that removes something or is
    the agent's own ceiling, are asked about instead.
    """
    n = tied if isinstance(tied, int) and not isinstance(tied, bool) and tied > 1 else 2
    tied_ids = {str(r.get("id") or "") for r in results[:n] if isinstance(r, dict)}
    cands = [r for r in rows if str(r.get("id") or "") in tied_ids]
    if len(cands) < 2:
        return None
    pick = _tie_candidate(cands)
    # Every way a tie is settled ends here: a pick that removes something or
    # is the agent's own ceiling is asked about instead.
    return pick if pick is not None and _tie_breakable(pick) else None


def _tie_candidate(cands: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The tied result the facts point at, before the safety check, or None."""
    if all(_same_place(cands[0], c) for c in cands[1:]):
        return cands[0]
    # A tied result with no guide at all (a page with no entry to point at)
    # is answered in words, so it never wins a tie over a control that has
    # one: the Webhooks page loses to the Settings tab that opens it.
    guided = [c for c in cands if _guide_target(c) is not None]
    if guided and len(guided) < len(cands):
        cands = guided
        if len(cands) == 1:
            return cands[0]
    on_screen = [c for c in cands if (c.get("live") or {}).get("status") in _ON_SCREEN]
    if on_screen:
        cands = on_screen
    if all(_same_place(cands[0], c) for c in cands[1:]):
        pick: dict[str, Any] | None = cands[0]
    else:
        homes = [(_home(c), c) for c in cands]
        best = max(h[0] for h, _c in homes)
        top = [(page, c) for (s, page), c in homes if s == best]
        pick = top[0][1] if best > 0 and len({page for page, _c in top}) == 1 else None
    return pick


#: How closely a result's own words matched the question (find_ui's ``match``),
#: weakest first; a basis not listed is unknown.
_MATCH_ORDER = (
    "tokens",
    "phrase_overlap",
    "search_term",
    "query_phrase",
    "label_phrase",
    "exact_label",
)


def _match_strength(r: dict[str, Any]) -> int:
    basis = str(r.get("match") or "")
    if basis in ("search_term", "query_phrase"):
        return 3
    if basis == "phrase_overlap":
        return 2
    return _MATCH_ORDER.index(basis) + 1 if basis in _MATCH_ORDER else 0


def _find_ui_next(d: dict[str, Any]) -> str | None:
    """The reply instruction for a search whose results the model may guide to.

    A result with a guide_ref is offered with ``guide_start`` before any reply,
    whatever its ``blocker`` says (the guide walks the user past it). Without
    this the model answered such a search in prose, describing the blocker as
    a click step, and no card appeared.
    """
    results = d.get("results")
    if d.get("status") == "no_match" and not d.get("needs_object"):
        # A whole question ("how do I add a schedule that runs every morning")
        # can miss where the control's own words ("add schedule") match; a
        # miss on the sentence is not evidence that the feature is absent.
        return _FIND_UI_RETRY_NOTE
    if not isinstance(results, list):
        return None
    rows = [r for r in results if isinstance(r, dict)]
    flags = {
        str(req.get("setting_id"))
        for r in rows
        for req in _requirements(r)
        if req.get("kind") == "preview_flag" and req.get("setting_id")
    }
    if flags:
        # A preview feature is answered in words while its switch is off: the
        # switch is a Developer setting, not a step for an everyday user, so
        # neither the switch nor the feature behind it is offered as a guide.
        if rows and _needs_preview(rows[0]):
            return _PREVIEW_NOTE
        rows = [
            r for r in rows if str(r.get("setting_id") or "") not in flags and not _needs_preview(r)
        ]
    if d.get("ambiguous"):
        pick = _tie_pick(rows, results, d.get("tied"))
        if pick is None:
            return _AMBIGUOUS_NO_GUIDE_NOTE if rows else None
        pick_id, pick_target = str(pick.get("id") or ""), _guide_target(pick)
        rows = [
            r
            for r in rows
            if str(r.get("id") or "") == pick_id
            or (pick_target is not None and _guide_target(r) == pick_target)
        ]
    guided = [r for r in rows if isinstance(r.get("guide_ref"), dict)]
    found = [r for r in rows if isinstance(r.get("find_ref"), dict)]
    if guided and found and rows.index(found[0]) < rows.index(guided[0]):
        # A planned guide wins over a find, but never over a better match for
        # what was asked: "move an artifact to a folder" is the artifact's own
        # folder button (a find), not the Schedule page's Move to folder,
        # merely because that one has a planned walk.
        strength = _match_strength(found[0])
        if strength and _match_strength(guided[0]) <= strength:
            guided = []
    if guided:
        pick, kind = guided[0], "guide_ref"
        how = "pass it to guide_start now"
    elif found:
        # No planned guide, but the tab can still look the control up by name:
        # offering that beats describing a path the user then has to hunt for.
        pick, kind = found[0], "find_ref"
        how = "pass it to guide_start now as a ui.find action"
    else:
        return _NO_GUIDE_NOTE if rows and not d.get("ambiguous") else None
    caution = bool(pick.get("caution")) or bool(
        isinstance(pick.get("find_ref"), dict) and pick["find_ref"].get("caution")
    )
    text = (
        f"{pick.get('id')} has a {kind}: {how}, before writing any reply, even when it "
        "carries a blocker (the guide handles that) and even when you could also make "
        "the change yourself (offer that after the guide, never instead of it). Only "
        "after guide_start returns, write your one reply from its result"
    )
    if kind == "find_ref":
        text += ", hedged ('it should be on the X page')"
    text += "."
    if str(pick.get("id") or "").startswith(_CHANNEL_SUB_PREFIX):
        # A channel's page asks for its tokens: credentials are never a guide
        # step, so the walk ends on the page and the reply names the fields.
        text += _CHANNEL_TOKENS_NOTE
    named = d.get("named_item")
    if isinstance(named, str) and named and kind == "guide_ref":
        # The question named the app to act on; its choose-the-app step then
        # outlines that one app (find_ui took the name out of the search).
        text += f' The user named “{named}”: add "pick": "{named}" to its ui.show params.'

    if caution:
        # The removal the user asked for still gets its guide: the guide only
        # points and its last step never finishes by itself.
        text += (
            " It removes something: when the user asked for exactly this, the guide "
            "is still offered first. Say what it removes and keeps only from its "
            "caution_text (the page's own words); without one, say only that it is a "
            "removal and that the page states what it removes, never that it is permanent."
        )
    return text


def _offer_next(d: dict[str, Any], surface: str) -> str:
    if not d.get("delivered_clients"):
        # No dashboard tab took the frame, so nothing is on the user's screen
        # now. Saying "I put a card here" would describe something they cannot see.
        return (
            "Queued, not shown yet: no open dashboard tab received it, so nothing is on "
            "the user's screen now. Never say you placed or showed it, or that they can "
            "press anything; answer in words and say it will appear when they open this "
            "chat. It is prepared, not applied or started."
        )
    # Both hints carry the one-reply rule: the reply after the card is the only
    # text the user reads, so nothing said before it is repeated, and no
    # quick-reply option presses the card's own button. The reply never names
    # the card's buttons either: the card does, and a guide's Start is gone once
    # it ends while the reply's text stays.
    if surface == "guide":
        return (
            "Shown above your reply; it starts only when the user presses its button. "
            "Finish any other offers, then write your one reply: the answer in one short "
            "sentence, in the language of the user's latest message. Never name or point "
            "at the card's buttons, ask whether to show it, mention this tool or the "
            "guide's state, repeat anything said before the card, describe the steps, "
            "recap, or offer a reply option that starts it."
        )
    ack = (
        " Applying needs the “I understand” box ticked first: say the change needs that "
        "confirmation."
        if d.get("widen") or d.get("risk") == "widen"
        else ""
    )
    return (
        "Shown above your reply, prepared but not applied. Finish any other offers, then "
        "write your one reply, in the language of the user's latest message: frame it "
        "briefly; for setup, also give the next step after it." + ack + " Never name or "
        "point at the card's buttons, and do not repeat anything said before the card, "
        "describe it, ask again, recap, or offer a reply option that presses its button. "
        "The user's press arrives next turn."
    )


def _authoritative_actions(actions: object) -> object:
    """Each ``ui.find`` action with the label the index gives it.

    The agent's label is checked against the packaged index
    (:func:`ui_index.authoritative_find`): a registered control keeps only one
    of its own labels, and a label nothing carries is replaced by the best
    find_ui match's find_ref. The gateway's catalog still validates the result.
    """
    if not isinstance(actions, list) or not any(
        isinstance(a, dict) and a.get("id") == "ui.find" for a in actions
    ):
        return actions
    from kiro_crew.ui_index import authoritative_find, planned_find

    shown = _dashboard_ui_lang()
    out: list[Any] = []
    for a in actions:
        params = a.get("params") if isinstance(a, dict) else None
        if isinstance(a, dict) and a.get("id") == "ui.find" and isinstance(params, dict):
            try:
                found = authoritative_find(params, label_lang=shown)
                # A control the index plans a walk to (another page, behind a
                # selection or an editor) is guided by that plan, not searched
                # for on a page it is not on yet.
                planned = planned_find(found)
                a = {**a, **planned} if planned else {**a, "params": found}
            except Exception:  # the label check must never fail the offer
                logger.debug("ui.find label check failed", exc_info=True)
        out.append(a)
    return out


def _find_labels(d: dict[str, Any]) -> list[str]:
    """The labels a started guide's ``ui.find`` actions name, as the panel shows them."""
    actions = d.get("actions")
    labels: list[str] = []
    for a in actions if isinstance(actions, list) else []:
        params = a.get("params") if isinstance(a, dict) else None
        if isinstance(a, dict) and a.get("id") == "ui.find" and isinstance(params, dict):
            label = params.get("label")
            if isinstance(label, str) and label and label not in labels:
                labels.append(label)
    return labels


def _render(result: dict[str, Any]) -> str:
    return json.dumps(result, ensure_ascii=False, sort_keys=True)


def _error(d: dict[str, Any]) -> str | None:
    err = d.get("error")
    if not err:
        return None
    return redact(f"Error: {err}")


#: The packaged user docs ``search_docs`` reads; nothing outside this directory.
_DOCS_DIR = Path(__file__).resolve().parent / "docs"
_DOCS_MAX_RESULTS = 5
_DOCS_MAX_LINES = 3
_DOCS_LINE_CHARS = 240
_DOCS_PAGE_CHARS = 12000
_WORD_RE = re.compile(r"[\w-]{2,}", re.UNICODE)


def _doc_pages() -> dict[str, Path]:
    """Page name -> file, from the docs directory's own listing (never a caller path)."""
    try:
        return {f.stem: f for f in sorted(_DOCS_DIR.glob("*.md")) if f.is_file()}
    except OSError:
        return {}


@functools.lru_cache(maxsize=128)
def _read_doc(path: Path, mtime_ns: int, size: int) -> str:
    """*path*'s text, cached under the ``(mtime_ns, size)`` it was read at.

    The shipped docs only, never anything about who asked: a changed file has
    another stamp and is read again.
    """
    return path.read_text(encoding="utf-8", errors="replace")


def _doc_text(path: Path) -> str:
    """*path*'s text, read again only when its mtime or size changed."""
    st = path.stat()
    return _read_doc(path, st.st_mtime_ns, st.st_size)


def _doc_title(text: str, fallback: str) -> str:
    for line in text.splitlines():
        if line.startswith("#"):
            return line.lstrip("#").strip()[:120] or fallback
    return fallback


def search_docs(query: str = "", page: str = "", offset: int = 0) -> dict[str, Any]:
    """Search the packaged docs, or read one page of them. Read-only.

    A ``page`` is looked up by name in the directory's own listing, so a caller
    string never becomes a path: ``../config`` or an absolute path simply names
    no page.
    """
    pages = _doc_pages()
    if page:
        name = page.strip().removesuffix(".md")
        path = pages.get(name)
        if path is None:
            return {"error": f"no documentation page '{name[:80]}'; search first"}
        text = _doc_text(path)
        start = max(0, int(offset or 0))
        out: dict[str, Any] = {
            "page": name,
            "title": _doc_title(text, name),
            "text": text[start : start + _DOCS_PAGE_CHARS],
        }
        if start + _DOCS_PAGE_CHARS < len(text):
            out["next_offset"] = start + _DOCS_PAGE_CHARS
        return out
    words = list(dict.fromkeys(w.lower() for w in _WORD_RE.findall(query or "")))
    if not words:
        return {"error": "give a 'query' to search or a 'page' to read"}
    scored: list[tuple[int, str, str, list[str]]] = []
    for name, path in pages.items():
        try:
            text = _doc_text(path)
        except OSError:
            continue
        lower, lname = text.lower(), name.lower()
        hits = [w for w in words if w in lower or w in lname]
        if not hits:
            continue
        score = 100 * len(hits) + 50 * sum(1 for w in hits if w in lname)
        score += min(sum(lower.count(w) for w in hits), 99)
        lines = [
            ln.strip()[:_DOCS_LINE_CHARS]
            for ln in text.splitlines()
            if ln.strip() and any(w in ln.lower() for w in words)
        ]
        lines.sort(key=lambda ln: -sum(1 for w in words if w in ln.lower()))
        scored.append((score, name, _doc_title(text, name), lines[:_DOCS_MAX_LINES]))
    scored.sort(key=lambda r: (-r[0], r[1]))
    return {
        "results": [
            {"page": name, "title": title, "lines": lines}
            for _score, name, title, lines in scored[:_DOCS_MAX_RESULTS]
        ]
    }


#: How long ``find_ui`` waits for the user's tab, end to end. The gateway
#: itself gives up a little sooner (``guide_observe.OBSERVE_WAIT_SECONDS``).
_LIVE_WAIT_SECONDS = 1.5
#: Ceiling on ids one ``find_ui`` asks about (``guide_observe.MAX_TARGETS``).
_LIVE_MAX_TARGETS = 16
#: How long ``find_ui`` waits for the gateway to say which language the user's
#: dashboard shows (an in-memory read; no tab is asked).
_UI_LANG_WAIT_SECONDS = 1.0


def _dashboard_ui_lang() -> str | None:
    """The language the caller's dashboard tab renders, or ``None`` when unknown.

    One bounded ask of the gateway (``GET /api/guide/agent/language``), fed by
    the ``X-UI-Lang`` header of the user's own latest send in this chat. Never
    fails the search: a refusal, no tab or a timeout leaves the labels in the
    question's language, as before.
    """
    sk, err = _strict_session_key()
    if err:
        return None
    try:
        d = _get("/api/guide/agent/language", session_key=sk, timeout=_UI_LANG_WAIT_SECONDS)
    except Exception:  # a hint must never fail the search
        logger.debug("find_ui dashboard language lookup failed", exc_info=True)
        return None
    tag = d.get("ui_lang") if isinstance(d, dict) else None
    return tag if isinstance(tag, str) and tag else None


def _observable_ids(d: dict[str, Any]) -> list[str]:
    """The results' ids a tab can be asked about, in result order."""
    results = d.get("results")
    if not isinstance(results, list):
        return []
    from kiro_crew.guide_catalog import ui_build_manifest

    manifest = ui_build_manifest()
    ids: list[str] = []
    for r in results:
        rid = r.get("id") if isinstance(r, dict) else None
        # Curated locations, and auto ones the auto tier made pointable.
        if (
            isinstance(rid, str)
            and (rid in manifest.observable or rid in manifest.auto_sites)
            and rid not in ids
        ):
            ids.append(rid)
    return ids


def _observe(ids: list[str]) -> dict[str, Any]:
    """One bounded ask of the gateway for *ids*; ``{}`` when it could not be asked.

    The answer also carries ``ui_lang``, the language the caller's dashboard
    shows, so a search needs no second round trip for it.
    """
    sk, err = _strict_session_key()
    if err or not ids:
        return {}
    try:
        obs = _post(
            "/api/guide/agent/observe",
            {"targets": ids[:_LIVE_MAX_TARGETS]},
            session_key=sk,
            timeout=_LIVE_WAIT_SECONDS,
        )
    except Exception:  # a hint must never fail the search
        logger.debug("find_ui live observation failed", exc_info=True)
        return {}
    return obs if isinstance(obs, dict) else {}


def _find_with_live(query: str, lang: str | None, surface: str | None) -> dict[str, Any]:
    """``find_ui`` labelled in the dashboard's language, with ``live`` per result.

    The observation and the dashboard's language come back in ONE gateway
    call: the ids to observe do not depend on the label language (that only
    picks the language labels are written in), so the search runs first, the
    tab is asked once, and the results are relabelled in the language it
    reported. Only when no observation could be asked (no observable id, or
    the call failed) is the language asked for on its own.
    """
    from kiro_crew.ui_index import find_ui

    probe = find_ui(query, lang, surface)
    ids = _observable_ids(probe)
    obs = _observe(ids)
    tag = obs.get("ui_lang")
    shown = (tag or None) if isinstance(tag, str) else _dashboard_ui_lang()
    d = find_ui(query, lang, surface, label_lang=shown) if shown else probe
    return _with_live(d, obs=obs)


def _with_live(d: dict[str, Any], *, obs: dict[str, Any] | None = None) -> dict[str, Any]:
    """Add ``live`` to each search result: what the user's tab shows right now.

    One bounded ask of the gateway for the curated ids among the results; the
    gateway asks one tab and answers within the wait or not at all. A result
    the tab did not answer for (no tab, a late reply, another build, an id no
    tab can observe) is ``live: {status: not_observed}``. Never fails the
    search: the static answer stands either way.

    A result whose plan the tab's state blocks also carries ``blocker``:
    ``gate_off`` (a gate on the path is off, with the ``setting_id`` that turns
    it on), ``needs_selection`` (a selection the path needs is not made),
    ``predicate_unmet`` (a runtime predicate a step's control needs is unmet,
    with ``predicates``), ``hidden_in_scope`` (the control is hidden or not
    rendered and a reveal scope on its path reports closed, with ``scopes``),
    or ``not_observed`` (no fresh reply, with ``reason`` when known). See
    :func:`_blocker_for`. *obs* is an observation already made for these
    results (:func:`_find_with_live`); without it one is made here.
    """
    results = d.get("results")
    if not isinstance(results, list) or not results:
        return d
    from kiro_crew.guide_catalog import ui_build_manifest

    manifest = ui_build_manifest()
    ids = _observable_ids(d)
    live: dict[str, dict[str, Any]] = {}
    scope_states: dict[str, str] = {}
    predicate_states: dict[str, str] = {}
    reason = ""
    if ids:
        if obs is None:
            obs = _observe(ids)
        if obs.get("status") == "observed" and isinstance(obs.get("targets"), list):
            at = obs.get("observed_at")
            for t in obs["targets"]:
                if isinstance(t, dict) and isinstance(t.get("id"), str):
                    live[t["id"]] = {"status": t.get("status"), "observed_at": at}
            scope_states = _states(obs.get("scopes"))
            predicate_states = _states(obs.get("predicates"))
        elif isinstance(obs.get("reason"), str):
            reason = obs["reason"]
    for r in results:
        if not isinstance(r, dict):
            continue
        rid = str(r.get("id"))
        seen = live.get(rid)
        if seen is not None:
            r["live"] = seen
            r["availability"] = "observed"
            blocker = _blocker_for(
                str(seen.get("status")),
                manifest.plan_scopes.get(rid, ()),
                manifest.plan_predicates.get(rid, ()),
                scope_states,
                predicate_states,
                manifest.gates,
                manifest.selections,
            )
            if blocker is not None:
                r["blocker"] = blocker
        else:
            r["live"] = {"status": "not_observed", **({"reason": reason} if reason else {})}
            r["blocker"] = {"kind": "not_observed", **({"reason": reason} if reason else {})}
    return d


def _states(raw: object) -> dict[str, str]:
    """``[{id, state}]`` from an observation, as ``{id: state}``; anything else dropped."""
    out: dict[str, str] = {}
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                state = item.get("state")
                if isinstance(state, str):
                    out[item["id"]] = state
    return out


#: Target statuses that mean the control is not on screen at all.
_NOT_SHOWN = frozenset({"hidden", "unmounted"})


def _blocker_for(
    status: str,
    plan_scopes: tuple[str, ...],
    plan_predicates: tuple[str, ...],
    scope_states: dict[str, str],
    predicate_states: dict[str, str],
    gates: dict[str, str | None] | None = None,
    selections: frozenset[str] = frozenset(),
) -> dict[str, Any] | None:
    """What stands between the user and an observed control, or ``None``.

    Only facts the tab reported, outermost first: a gate on the path that is
    off (``gate_off``, with the ``setting_id`` that turns it on, or ``None``),
    then a selection the path needs that nothing satisfies yet
    (``needs_selection``; never which entity), then any other unmet predicate
    on the control's plan, then, for a control that is not shown, the reveal
    scopes on its path that reported closed. A scope or predicate the tab did
    not know about (``unknown``) is never a blocker.
    """
    if status in ("pointable", "offscreen"):
        return None
    gates = gates or {}
    unmet = [p for p in plan_predicates if predicate_states.get(p) == "unmet"]
    for p in unmet:
        if p in gates:
            return {"kind": "gate_off", "gate": p, "setting_id": gates[p]}
    for p in unmet:
        if p in selections:
            return {"kind": "needs_selection", "selection": p}
    if unmet:
        return {"kind": "predicate_unmet", "predicates": unmet}
    if status in _NOT_SHOWN:
        closed = [s for s in plan_scopes if scope_states.get(s) == "closed"]
        if closed:
            return {"kind": "hidden_in_scope", "scopes": closed}
    return None


_TOOL_NAMES = frozenset(
    {
        "search_docs",
        "find_ui",
        "guide_list_actions",
        "guide_start",
        "guide_status",
        "guide_cancel",
        "list_change_kinds",
        "find_setting",
        "get_member_capabilities",
        "diagnose_settings",
        "propose_change",
        "get_change_status",
        "rename_self",
    }
)


def _call_tool_inner(name: str, args: dict[str, Any]) -> str:
    if name not in _TOOL_NAMES:
        return f"Error: unknown tool '{name}'"
    if name == "search_docs":
        # Packaged documentation only: no user state and no gateway route, so
        # reading it needs no caller identity.
        d = search_docs(args.get("query") or "", args.get("page") or "", args.get("offset") or 0)
        return _error(d) or _render(d)
    if name == "find_ui":
        # The packaged location index answers; like search_docs it needs no
        # caller identity. Only the optional live observation of a search's
        # results resolves the strict key, and a refusal there leaves every
        # result not_observed instead of failing the search.
        from kiro_crew.ui_index import browse_ui

        query, area = (args.get("query") or "").strip(), (args.get("area") or "").strip()
        if bool(query) == bool(area):
            return "Error: pass `query` to search or `area` to browse (one of them, not both)"
        if area and args.get("surface"):
            return "Error: `surface` filters a search; an `area` listing takes none"
        if not area and args.get("offset"):
            return "Error: `offset` pages an `area` listing; a search takes none"
        # Labels are spelled as the user's own dashboard shows them, whatever
        # language the question is in; ``lang`` only reads the question.
        if area:
            shown = _dashboard_ui_lang()
            d = browse_ui(area, args.get("lang") or None, args.get("offset") or 0, label_lang=shown)
        else:
            d = _find_with_live(query, args.get("lang") or None, args.get("surface") or None)
            hint = _find_ui_next(d)
            if hint and not d.get("error"):
                d = {**d, "next": hint}
        return _error(d) or _render(d)
    sk, err = _strict_session_key()
    if err:
        return _off_dashboard(name, err)

    if name == "list_change_kinds":
        d = _get("/api/cards/agent/kinds", session_key=sk)
        return _error_for(name, d) or _render({"kinds": d.get("kinds") or []})

    if name == "find_setting":
        query = urllib.parse.urlencode({"q": args.get("query") or ""})
        d = _get(f"/api/cards/agent/settings?{query}", session_key=sk)
        return _error_for(name, d) or _render({"settings": d.get("settings") or []})

    if name == "get_member_capabilities":
        query = urllib.parse.urlencode({"member": args.get("member") or ""})
        d = _get(f"/api/cards/agent/capabilities?{query}", session_key=sk)
        return _error_for(name, d) or _render(d)

    if name == "diagnose_settings":
        path = "/api/cards/agent/diagnose"
        topic = (args.get("topic") or "").strip()
        if topic:
            path += "?" + urllib.parse.urlencode({"topic": topic})
        d = _get(path, session_key=sk)
        return _error_for(name, d) or _render(d)

    if name == "propose_change":
        body = {"kind": args.get("kind"), "params": args.get("params")}
        if args.get("reason"):
            body["reason"] = args["reason"]
        d = _post("/api/cards/agent/propose", body, session_key=sk)
        return _error_for(name, d) or _render({**d, "next": _offer_next(d, "change")})

    if name == "get_change_status":
        path = "/api/cards/agent/status"
        change_id = args.get("change_id")
        if change_id:
            path += "?" + urllib.parse.urlencode({"card_id": change_id})
        d = _get(path, session_key=sk)
        return _error_for(name, d) or _render(d)

    if name == "rename_self":
        d = _post("/api/guide/agent/rename", {"name": args.get("name")}, session_key=sk)
        refused = _error_for(name, d)
        if refused:
            return f"{refused}\nYour name did not change; do not say it did."
        return _render(
            {
                **d,
                "next": "Your name now shows as "
                + str(d.get("display_name") or "")
                + " everywhere in the dashboard. Answer to it from now on.",
            }
        )

    if name == "guide_list_actions":
        d = _get("/api/guide/agent/actions", session_key=sk)
        return _error_for(name, d) or _render({"actions": d.get("actions") or []})

    if name == "guide_start":
        start_body: dict[str, Any] = {"actions": _authoritative_actions(args.get("actions"))}
        if args.get("intro") is not None:
            start_body["intro"] = args.get("intro")
        d = _post("/api/guide/agent/start", start_body, session_key=sk)
        refused = _error_for(name, d)
        if refused:
            # A refused offer leaves nothing in the chat; say so in the result
            # itself, so the reply cannot describe a card that does not exist.
            return f"{refused}\n{GUIDE_NOT_SHOWN_NOTE}"
        named = _find_labels(d)
        hint = _offer_next(d, "guide")
        if named and d.get("delivered_clients"):
            hint += (
                " Name the control exactly as the guide does, character for character: "
                + ", ".join(f"“{n}”" for n in named)
                + ". Never rename, shorten or translate it."
            )
        return _render({**d, "next": hint})

    if name == "guide_status":
        path = "/api/guide/agent/status"
        guide_id = args.get("guide_id")
        if guide_id:
            path += "?" + urllib.parse.urlencode({"guide_id": guide_id})
        d = _get(path, session_key=sk)
        return _error_for(name, d) or _render(d)

    d = _post("/api/guide/agent/cancel", {"guide_id": args.get("guide_id")}, session_key=sk)
    return _error_for(name, d) or _render(d)


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
ADVERTISE_CALLER_IDENTITY = True


def run_mcp_server() -> None:
    """Run the MCP stdio server — reads JSON-RPC from stdin, writes to stdout."""
    run_mcp_stdio_loop(
        SERVER_NAME,
        SERVER_VERSION,
        _list_tools,
        _call_tool,
        advertise_caller_identity=ADVERTISE_CALLER_IDENTITY,
    )


if __name__ == "__main__":  # pragma: no cover - process entry
    logging.basicConfig(level=logging.INFO)
    run_mcp_server()
