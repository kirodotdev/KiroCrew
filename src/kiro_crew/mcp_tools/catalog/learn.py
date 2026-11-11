"""Tool declarations of the ``learn`` domain of ``kirocrew-core``.

The handlers are in :mod:`kiro_crew.mcp_tools.learn`, which reads every name here as
its own. This module imports none of them (see the package docstring).
"""

from __future__ import annotations

from typing import Any

from kiro_crew.constants import MAX_SHORT_STRING
from kiro_crew.lesson_validation import LESSON_APPLIES_INSTRUCTION
from kiro_crew.validation_parts.core_agent_schemas import LEARN_ADD_SCHEMA, LEARN_UPDATE_SCHEMA
from kiro_crew.validation_parts.input_bounds import (
    LESSON_LIST_LIMIT,
    LESSON_LIST_LIMIT_MAX,
    LESSON_LIST_OFFSET_MAX,
)


def schemas() -> list[dict[str, Any]]:
    """Descriptors for the learn tools."""
    # Derive the learn_add rule/negative char limit from the schema field the
    # validator actually enforces (single source of truth) so the tool hint
    # tracks the enforced limit — including a future config-driven value —
    # instead of a parallel constant that can silently drift.
    _rule_max = next(
        (f.max_len for f in LEARN_ADD_SCHEMA.fields if f.name == "rule"),
        MAX_SHORT_STRING,
    )
    _neg_max = next(
        (f.max_len for f in LEARN_ADD_SCHEMA.fields if f.name == "negative"),
        MAX_SHORT_STRING,
    )
    _scope_max = next(
        (f.max_len for f in LEARN_ADD_SCHEMA.fields if f.name == "repo_scope"),
        MAX_SHORT_STRING,
    )
    _update_rule_max = next(f.max_len for f in LEARN_UPDATE_SCHEMA.fields if f.name == "rule")
    return [
        {
            "name": "memory_recall",
            "description": (
                "Retrieve relevant facts, experiences and corrections from the memory "
                "bound to this session: Global V1 for an unowned session, or this Crew "
                "Member's private V2. Search is on demand, not run automatically for "
                "every message. Ask a specific question when earlier decisions or "
                "events are needed; skip it if the current conversation suffices. "
                "Returns bounded context and sources. The caller cannot choose another "
                "store; private members cannot access Global V1 or sibling memories. "
                "It returns distilled facts, not transcript text: for a past "
                "conversation's exact words, follow up with search_chat_history."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"query": {"type": "string", "minLength": 1, "maxLength": 2000}},
                "required": ["query"],
                "additionalProperties": False,
            },
        },
        {
            "name": "learn_add",
            "description": (
                "Save a learned correction or preference that changes behavior in "
                "unrelated future sessions. MUST be called only when a user correction "
                "defines reusable behavior, including 'always do X', 'never do Y', or "
                "'remember that'. Do NOT save volatile session or task facts such as "
                "the active model identity or which concrete model ID the assistant "
                "is running as. A 'running as' phrase needs an unambiguous model "
                "noun, a qualified 'backend' that ends its clause, or a concrete "
                "model ID; backend service-account and process wording remains "
                "durable. The tool rejects recognized runtime identity assertions "
                "and model-selection imperatives whose selected object is a "
                "concrete model ID at the end of its clause in the rule or negative "
                "clause. Clause endings are the field end, a newline, punctuation, or "
                "the documented closed connector class. A following plain noun makes "
                "the ID a durable tooling qualifier. "
                "The check covers only a fixed set of model-ID families; other "
                "backend IDs are not lesson-refused. A model version mentioned by "
                "itself is allowed. Free-form wording remains a best-effort check, "
                "so do not disguise either refused class. Save only what changes "
                "future unrelated sessions: not ticket facts, package details, "
                "steering duplicates or changelog notes. There is no `scope` "
                "parameter: pass repo_scope to restrict a lesson to one codebase. "
                "A refused, deduped or unchanged result means nothing was written: "
                "never tell the user the lesson was saved. Give the rule (what to "
                "do); negative (what not to do) is optional."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "rule": {
                        "type": "string",
                        "maxLength": _rule_max,
                        "description": (
                            f"The lesson to remember. HARD LIMIT {_rule_max} "
                            "characters — longer rules are REJECTED (not truncated), "
                            "so keep it concise. Put 'what not to do' in the separate "
                            "'negative' field rather than inlining a long '-- NOT: ...' "
                            "clause here, and split unrelated corrections into multiple "
                            "learn_add calls instead of one oversized rule."
                        ),
                    },
                    "category": {
                        "type": "string",
                        "enum": ["tool", "preference", "knowledge"],
                        "description": "Category: tool, preference, or knowledge",
                    },
                    "negative": {
                        "type": "string",
                        "maxLength": _neg_max,
                        "description": (
                            f"What NOT to do (optional). HARD LIMIT {_neg_max} "
                            "characters — rejected if exceeded."
                        ),
                    },
                    "repo_scope": {
                        "type": "string",
                        "maxLength": _scope_max,
                        "description": (
                            "Optional. Restrict this correction to ONE repository, "
                            "given as a path fragment that repository contains "
                            "(e.g. 'src/kiro_crew'). The correction then applies "
                            "only in sessions whose project is inside that tree, "
                            "and is withheld everywhere else. Use it for a rule "
                            "that is only true of one codebase; omit it for a "
                            "durable preference that should always apply. Omitted "
                            "means it applies everywhere."
                        ),
                    },
                    "applies": {
                        "type": "string",
                        "enum": ["always", "on_topic"],
                        "description": LESSON_APPLIES_INSTRUCTION,
                    },
                },
                "required": ["rule", "category"],
            },
        },
        {
            "name": "learn_list",
            "description": (
                "List saved lessons and corrections, one window at a time. "
                f"Returns the newest `limit` lessons (default {LESSON_LIST_LIMIT}) "
                "and, when the store holds more, a first line saying how many are "
                "shown of how many exist. Pass `offset` to page back to older "
                "lessons; a lesson you are looking for and do not see may be on a "
                "later page."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": LESSON_LIST_LIMIT_MAX,
                        "description": (
                            "Optional. How many lessons to return in this window "
                            f"(default {LESSON_LIST_LIMIT}, at most "
                            f"{LESSON_LIST_LIMIT_MAX})."
                        ),
                    },
                    "offset": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": LESSON_LIST_OFFSET_MAX,
                        "description": (
                            "Optional. How many of the newest lessons to skip before "
                            "the window starts (default 0). The 'showing N of M' line "
                            "names the offset that reaches the next older page."
                        ),
                    },
                },
            },
        },
        {
            "name": "learn_remove",
            "description": "Remove lessons whose rule contains the given substring",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Substring to match"},
                    "repo_scope": {
                        "type": "string",
                        "maxLength": _scope_max,
                        "description": (
                            "Optional. Only remove lessons carrying this repo "
                            "scope, given as the same path fragment used to store "
                            "them (e.g. 'src/kiro_crew'). A lesson's identity is "
                            "the pair (rule, repo_scope), so the same rule scoped "
                            "to a repo and stored globally are two separate "
                            "lessons; without this the substring removes both. "
                            "Omit to match every scope. Pass an empty string to "
                            "remove only the unscoped (global) lessons."
                        ),
                    },
                    "scope": {
                        "type": "string",
                        "enum": ["global", "workspace"],
                        "description": (
                            "Optional. Which lessons file to delete from, as "
                            "learn_list reports it: a row shown with "
                            "'(workspace: NAME)' lives in that workspace's file "
                            "and is reached only with scope='workspace' plus "
                            "workspace=NAME; every other row is in the global "
                            "file, which is also the default."
                        ),
                    },
                    "workspace": {
                        "type": "string",
                        "description": (
                            "Optional. The workspace name from the row's "
                            "'(workspace: NAME)' marker; required with "
                            "scope='workspace'."
                        ),
                    },
                    "keep_one": {
                        "type": "boolean",
                        "description": (
                            "Optional, default false. Remove only the extra "
                            "copies: of the matching lessons, those with the "
                            "same stored rule, NOT clause, tier and scope are collapsed to the "
                            "oldest one. Different lessons are never removed "
                            "in this mode, so use it to clean up duplicates."
                        ),
                    },
                },
                "required": ["query"],
            },
        },
        {
            "name": "learn_update",
            "description": (
                "Change one saved lesson's tier in place, keeping its text and history. "
                "Pass its text as learn_list shows it and the tier you read there as "
                "current (an unmarked lesson is 'always'); nothing changes if it differs."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "rule": {
                        "type": "string",
                        "maxLength": _update_rule_max,
                        "description": "The lesson's text as learn_list shows it.",
                    },
                    "repo_scope": {
                        "type": "string",
                        "maxLength": _scope_max,
                        "description": (
                            "Optional. The '(scope: X)' learn_list shows, when two "
                            "lessons share this text; an empty string names the "
                            "unscoped one."
                        ),
                    },
                    "current": {"type": "string", "enum": ["always", "on_topic"]},
                    "applies": {"type": "string", "enum": ["always", "on_topic"]},
                },
                "required": ["rule", "current", "applies"],
            },
        },
    ]
