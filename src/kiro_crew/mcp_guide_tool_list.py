"""The tool catalogue of the ``kirocrew-guide`` MCP server, and the server's declaration.

What ``tools/list`` returns and what ``initialize`` says about the server. Nothing here
imports a tool handler: an agent engine holds a session's first prompt until every MCP
server has listed its tools, so this is all the server loads before it answers
(``kiro_crew.mcp_shared_listing``). The handlers stay in ``kiro_crew.mcp_guide``, which
reads every name here as its own.
"""

from __future__ import annotations

from typing import Any

from kiro_crew.mcp_instructions import GUIDE as _INSTRUCTIONS
from kiro_crew.mcp_shared_listing import StdioServer

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
                "Bots, capabilities, templates, MCP servers, connections, "
                "secrets, app trust, denied commands, saved workflows), each with "
                "its params schema and which fields the user may edit on the card."
            ),
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "find_setting",
            "description": (
                "Search the Settings page for a setting to change, e.g. 'shorter "
                "replies' or 'restore sessions'. Returns up to 10 matches: "
                "setting_id, label, path (the Settings click path), description "
                "(help text the row shows), hint (help text behind the row's info "
                "tip, never shown on the row; only when the row has one), tab, "
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
                "Read one Bot's current tools, tool approvals, MCP servers and "
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
                "'较早的会话'. Call it first for every where-is or how-do-I-find question, "
                "with no text before the call (no 'let me check the layout first': the "
                "user sees every word), "
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
                "gate_off (a setting such as a feature preview is off; setting_id "
                "names it, or is null when no setting turns it on), "
                "needs_selection (the user must first open one item, e.g. a "
                "Bot, named in selection), "
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


#: The server as ``initialize`` describes it. ``kiro_crew.mcp_guide.run_mcp_server``
#: passes the loop these same values, so the answers written before the handlers load
#: and the ones the loop writes after cannot differ.
SERVER = StdioServer(
    name=SERVER_NAME,
    version=SERVER_VERSION,
    list_tools=_list_tools,
    advertise_caller_identity=True,
    instructions=_INSTRUCTIONS,
)
