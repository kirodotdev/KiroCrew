"""The tool catalogue of the ``kirocrew-mate`` MCP server, and the server's declaration.

What ``tools/list`` returns and what ``initialize`` says about the server. Nothing here
imports a tool handler: an agent engine holds a session's first prompt until every MCP
server has listed its tools, so this is all the server loads before it answers
(``kiro_crew.mcp_shared_listing``). The handlers stay in ``kiro_crew.mcp_mate``, which
reads every name here as its own.
"""

from __future__ import annotations

from typing import Any

from kiro_crew import member_goals as _goals
from kiro_crew.constants import MAX_SHORT_STRING
from kiro_crew.mcp_shared_listing import StdioServer

SERVER_NAME = "kirocrew-mate"


SERVER_VERSION = "1.0.0"


#: The trait axes of the ghost face and the values the shipped renderer draws.
#: Told to the model in the tool description so it names a real option; an
#: unknown value is kept by the validator and renders as the axis's default.
GHOST_TRAIT_VOCABULARY: dict[str, tuple[str, ...]] = {
    "eyes": (
        "canon",
        "closed",
        "sleepy",
        "wink",
        "wide",
        "sparkle",
        "visor",
        "glasses",
        "cross",
        "squint",
        "swirl",
        "heart",
        "cyclops",
    ),
    "brows": ("none", "raised", "angry", "flat"),
    "mouth": ("none", "smile", "open", "cat", "oh", "grin", "tongue", "wobble", "smirk"),
    "accessory": (
        "none",
        "antenna",
        "halo",
        "cap",
        "phones",
        "bow",
        "crown",
        "beanie",
        "party",
        "flower",
        "bandana",
        "hardhat",
    ),
    "prop": ("none", "mug", "glass", "wrench", "bolt", "star", "term", "heart"),
}


_ONLY_WHEN_ASKED = (
    "Call it only when the user has just asked for exactly this change, in their "
    "own words; never on your own initiative, never to change anyone else. Only a "
    "Bot in its own chat can call it, from a message the user sent in the "
    "dashboard."
)


_CARD_NOTE = (
    "Nothing changes when you call this: the user sees a card with a button in "
    "this chat and decides. Write ONE reply that refers to the card and end your "
    "turn; the outcome is given to you at the start of your next turn."
)


def _tool_definitions() -> list[dict[str, Any]]:
    vocab = "; ".join(f"{axis}: {', '.join(vals)}" for axis, vals in GHOST_TRAIT_VOCABULARY.items())
    return [
        {
            "name": "update_self",
            "description": (
                "Change what the user sees of you: your display name, your description "
                "(a short line about what you do), or your ghost face. A display name is "
                "a label only: other Bots still reach you by your short name, which it "
                "never changes. Applied at once, "
                "shown in this chat as one line the user can undo. Pass only the fields "
                "the user asked to change, with the values exactly as they gave them. "
                'avatar is {"traits": {...}} naming one or more axes (' + vocab + "; "
                "blush and flip are true/false; tile is a #rrggbb background colour). An "
                "axis you leave out keeps its current value, or the plain Kiro mark when "
                "your face was never pinned. A Bot wearing an uploaded picture or an "
                "appearance pack keeps it: that face is changed in the crew editor. "
                + _ONLY_WHEN_ASKED
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "display_name": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_SHORT_STRING,
                    },
                    "description": {"type": "string", "maxLength": 4000},
                    "avatar": {
                        "type": "object",
                        "properties": {
                            "traits": {
                                "type": "object",
                                "properties": {
                                    "eyes": {"type": "string"},
                                    "brows": {"type": "string"},
                                    "mouth": {"type": "string"},
                                    "accessory": {"type": "string"},
                                    "prop": {"type": "string"},
                                    "blush": {"type": "boolean"},
                                    "flip": {"type": "boolean"},
                                    "tile": {"type": "string", "maxLength": 7},
                                },
                                "additionalProperties": False,
                            }
                        },
                        "required": ["traits"],
                        "additionalProperties": False,
                    },
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "set_model",
            "description": (
                "Ask the user to change the model or the reasoning effort your sessions "
                "run on. Pass an id from GET /api/models (the dashboard's model list) "
                'or "" to inherit the default. ' + _CARD_NOTE
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "model": {"type": "string", "maxLength": 64},
                    "reasoning_effort": {"type": "string", "maxLength": 16},
                    "reason": {"type": "string", "maxLength": 500},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "set_approval_mode",
            "description": (
                "Ask the user to change how your own chat approves tools: normal (look "
                "things up on your own, ask before anything else), trust_reads (also "
                "search your memory, past chats, skills and knowledge library on your "
                "own) or trust (act without asking). " + _CARD_NOTE
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "approval_mode": {"type": "string", "enum": ["normal", "trust_reads", "trust"]},
                    "reason": {"type": "string", "maxLength": 500},
                },
                "required": ["approval_mode"],
                "additionalProperties": False,
            },
        },
        {
            "name": "find_capabilities",
            "description": (
                "Read what your worker can use and what it could be given: first the "
                "MCP servers, tools that run without asking and skills your worker "
                "already has; then the servers and skills this gateway has loaded that "
                "your worker lacks; then, when you pass a query, matching servers from "
                "the MCP registry. Call it when a piece of work needs something your "
                "worker reports it does not have. It changes nothing; when it names "
                "the addition the work needs, call request_capabilities with it "
                "straight away: the card is how the user is asked. While your Dynamic "
                "capabilities (a Preview) is off it is refused; ask with "
                "request_capabilities."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"query": {"type": "string", "maxLength": 200}},
                "additionalProperties": False,
            },
        },
        {
            "name": "request_capabilities",
            "description": (
                "Ask the user for a change to your worker's tools, tool approvals, MCP "
                "servers or skills. To add something find_capabilities listed, pass "
                "draft {additions: [{server, source: gateway | registry, registry_id?, "
                "auto_approve?: [tool]} | {skill}]}: the card names where each comes "
                "from and exactly which tools would run without asking, and the "
                "worker gets it: one already running is reloaded with it, conversation kept. "
                "Any other change is draft "
                "{operations: [{section, id, action, value?}], enroll?} in the shape "
                "get_member_capabilities (kirocrew-guide) returns for you. While your "
                "Dynamic capabilities (a Preview) is off, the first call shows the user "
                "a card asking to turn it on instead, and your request follows once they "
                "do; after a Not now it answers in words. " + _CARD_NOTE
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "draft": {"type": "object"},
                    "reason": {"type": "string", "maxLength": 500},
                },
                "required": ["draft"],
                "additionalProperties": False,
            },
        },
        {
            "name": "goal_read",
            "description": (
                "Read your goals: each goal the user set, with its id and number, and "
                "your key results with the goals each serves, "
                "each with its id, target, latest reading and reading history. They are "
                "also in your context at session start; call this when you need fresh "
                "state or the history."
            ),
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "set_goal",
            "description": (
                "Ask the user to add a goal or to replace one of your goals: what you "
                "work toward. Use it when the user has told you a goal, or agreed to a "
                "new one, in their own words, or when your Monthly review finds a goal "
                "no longer fits the work; pass it as one plain paragraph. Leave goal_id "
                "out to add a goal; pass the id of one of your goals (goal_read lists "
                "them) to replace its text. A few goals are easier to keep track of "
                "than many, so when the list grows, suggest merging goals that overlap "
                "rather than adding another; the user decides. While your Heartbeat is "
                "off and the user never declined it, the card also carries a Heartbeat "
                "checkbox, on by default, so setting the goal and keeping it moving on "
                "your own is one press: do not ask about the Heartbeat separately or "
                "propose set_check_ins for it after this card. " + _CARD_NOTE
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "long_term": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": _goals.LONG_TERM_MAX_CHARS,
                    },
                    "goal_id": {"type": "string", "pattern": "^g[0-9a-f]{8}$"},
                    "reason": {"type": "string", "maxLength": 500},
                },
                "required": ["long_term"],
                "additionalProperties": False,
            },
        },
        {
            "name": "set_key_results",
            "description": (
                "Ask the user to confirm your key results (milestones): readings that "
                "show how far you are from your goals, replacing the current set. Work "
                "them out yourself first: look into the work (the repo, the queue, past "
                "releases you can read) and pick things you can check and report as a "
                "number or a plain yes/no; target is that number or word. Link each "
                "milestone to the goals it serves with goal_ids (the ids goal_read "
                "lists); which goals, and whether any, is your call. A few milestones "
                "are easier to track than many, so propose a small set and suggest "
                "merging when it grows; the user decides. "
                "When a goal cannot be measured, propose none for it and say so instead "
                "of inventing a number. Propose a new set when your goals change. " + _CARD_NOTE
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "properties": {
                                "text": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": _goals.KR_TEXT_MAX_CHARS,
                                },
                                "target": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": _goals.KR_VALUE_MAX_CHARS,
                                },
                                "goal_ids": {
                                    "type": "array",
                                    "items": {"type": "string", "pattern": "^g[0-9a-f]{8}$"},
                                },
                            },
                            "required": ["text", "target"],
                            "additionalProperties": False,
                        },
                    },
                    "reason": {"type": "string", "maxLength": 500},
                },
                "required": ["items"],
                "additionalProperties": False,
            },
        },
        {
            "name": "set_check_ins",
            "description": (
                "Ask the user to turn your check-ins on or off and set when they run: "
                "daily is the Daily Goal Reminder (you read your goal and record where "
                "each milestone stands), weekly the Weekly report (on weekday, 0 = "
                "Sunday), monthly the Monthly review (on month_day, 1-28); all run at "
                "time (HH:MM, 24-hour). heartbeat wakes you every interval minutes "
                f"({_goals.interval_minutes_text()}; to the user say "
                f"{_goals.interval_choices()}) to "
                "move your work forward on your own and "
                "uses credits on each wake, so propose it only when the work suits "
                "it. The card has no timing control: the user turns each check-in "
                "off on its own, or changes its time, in your profile "
                "under Goals, then Check-ins (to the user say: open my profile, "
                "then Goals, then Check-ins); the Heartbeat's switch and interval "
                "are the first row of your profile's Schedules tab (to the user "
                "say: open my profile, then Schedules). Point there rather than at "
                "the card. Give only "
                "what changes. Explain in one plain "
                "sentence what each one you propose does for the user before you call "
                "it; they need a goal first. " + _CARD_NOTE
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "daily": {"type": "boolean"},
                    "weekly": {"type": "boolean"},
                    "monthly": {"type": "boolean"},
                    "heartbeat": {"type": "boolean"},
                    "weekday": {"type": "integer", "minimum": 0, "maximum": 6},
                    "month_day": {"type": "integer", "minimum": 1, "maximum": _goals.MAX_MONTH_DAY},
                    "time": {"type": "string", "pattern": "^([01][0-9]|2[0-3]):[0-5][0-9]$"},
                    "interval": {"type": "integer", "enum": list(_goals.HEARTBEAT_INTERVALS)},
                    "reason": {"type": "string", "maxLength": 500},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "offer_dashboard_page",
            "description": (
                "Offer, once, to build a Dashboard page around your goal: this raises one "
                "card and writes nothing. Call it only when a Kiro Crew note in your turn "
                "says you can offer one. If the user says yes you are woken to write the "
                "page; if they say no, or let the card expire, nothing changes and you do "
                "not ask again. A page the user asks for in chat needs no card: write it. "
                + _CARD_NOTE
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"reason": {"type": "string", "maxLength": 500}},
                "additionalProperties": False,
            },
        },
        {
            "name": "key_result_progress",
            "description": (
                "Record a fresh reading for one key result by its id: current is what you "
                "found, a number in the same form as its target or a short note when the "
                "progress is not a number. Saved at once, with no sign-off, and shown on "
                "your Goals tab. Record one whenever you check."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "maxLength": 16},
                    "current": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": _goals.KR_READING_MAX_CHARS,
                    },
                },
                "required": ["id", "current"],
                "additionalProperties": False,
            },
        },
        {
            "name": "save_goal_report",
            "description": (
                "Save your weekly report or monthly review as a report the user can "
                "open: kind weekly or monthly, a short title, and the report itself as "
                "one of two formats -- html, a whole self-contained page laid out as a "
                "printable document (the check-in prompt's 'How the report looks' "
                "describes it), or markdown for a plain one. Send one of the two. "
                "It is filed under Crewmates/<your name>/Reports and the user "
                "gets a notification linking it. After saving, reply in the chat with "
                "one short line that points at it; the report itself stays out of the "
                "chat."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["weekly", "monthly"]},
                    "title": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": _goals.REPORT_TITLE_MAX_CHARS,
                    },
                    "markdown": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": _goals.REPORT_MAX_CHARS,
                    },
                    "html": {
                        "type": "string",
                        "minLength": 1,
                        # The server holds the real cap, in bytes of UTF-8.
                        "maxLength": _goals.REPORT_HTML_MAX_BYTES,
                    },
                },
                "required": ["kind", "title"],
                "additionalProperties": False,
            },
        },
        {
            "name": "save_notes",
            "description": (
                "Replace your notes: the [CURRENT ASSIGNMENT] section you keep for "
                "your future self (current priorities, work in flight, pointers to "
                "what your workers made). Pass the whole new text; it replaces the "
                "old one, and an empty text clears it. Saved at once and shown on "
                "your Notes tab."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "maxLength": _goals.NOTES_MAX_CHARS},
                },
                "required": ["text"],
                "additionalProperties": False,
            },
        },
    ]


def _list_tools() -> list[dict[str, Any]]:
    """Unconditional: reaching this process means a spec granted the set.

    Dynamic capabilities is per crewmate
    (``kiro_crew.crewmate_dynamic_capabilities``) and read at each capability
    tool's route, not here: a pooled server serves many crewmates and caches a
    session's tool policy, so a cut in the list would outlive a Turn on.
    """
    return _tool_definitions()


#: The server as ``initialize`` describes it. ``kiro_crew.mcp_mate.run_mcp_server``
#: passes the loop these same values, so the answers written before the handlers load
#: and the ones the loop writes after cannot differ.
SERVER = StdioServer(
    name=SERVER_NAME,
    version=SERVER_VERSION,
    list_tools=_list_tools,
    advertise_caller_identity=True,
)
