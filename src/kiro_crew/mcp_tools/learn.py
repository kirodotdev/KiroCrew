"""The durable lessons and corrections tools: what they advertise and what they do.

``schemas()`` returns the ADVERTISEMENT half of each tool -- its name, the
model-facing description, and the JSON Schema a call is validated against.
``HANDLERS`` maps each of those names to the function that runs it. Both halves
of a tool live here so its contract and its behavior are read together, and
``test_mcp_tool_registry`` fails if one arrives without the other.

Handlers reach this server's shared plumbing as attributes of ``mcp_core`` --
``mcp_core._post``, the identity resolvers, the governance vets. That is
deliberate rather than untidy: an attribute lookup resolves at CALL time, so a
test that rebinds one on the module still intercepts the handler. Importing
those names directly here would bind them at import time and silently escape
every existing patch site.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from kiro_crew import mcp_core
from kiro_crew.validation import LEARN_ADD_SCHEMA, MAX_SHORT_STRING


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
    return [
        {
            "name": "learn_add",
            "description": (
                "Save a learned correction or preference that persists across all "
                "future sessions. MUST be called when the user corrects you, says "
                "'always do X', 'never do Y', or 'remember that'. Include both "
                "the rule (what to do) and negative (what not to do)."
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
                },
                "required": ["rule", "category"],
            },
        },
        {
            "name": "learn_list",
            "description": "List all saved lessons and corrections",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "learn_remove",
            "description": "Remove lessons whose rule contains the given substring",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Substring to match"},
                },
                "required": ["query"],
            },
        },
    ]


def learn_add(name: str, args: dict[str, Any]) -> str:
    rule = args.get("rule", "")
    category = args.get("category", "knowledge")
    if not rule:
        return "Error: rule is required"
    # Governance: a durable lesson write is re-injected into every future
    # session, so it is gated by capabilities.memory_writes (default on; a
    # policy/profile may disable it for a sandboxed surface/app).
    _gov_mem = mcp_core._vet_memory_writes_governance(mcp_core._resolve_session_key())
    if _gov_mem:
        return f"Error: {_gov_mem}"
    # A stale client still holding the old advertised schema can send
    # scope="workspace". Silently forcing that to "global" would take a correction
    # meant for one workspace and inject it into EVERY session -- the exact harm
    # this change exists to end, and worse than the old behaviour, where the tier
    # was inert and reached no prompt at all. So a legacy scope asking for anything
    # narrower than global is REFUSED, with the replacement named. Refusing loses
    # nothing that ever worked, and it tells the caller instead of widening silently.
    _legacy_scope = args.get("scope")
    if isinstance(_legacy_scope, str) and _legacy_scope.strip() not in ("", "global"):
        return (
            f"Error: scope={_legacy_scope.strip()!r} is no longer accepted. That tier "
            "never reached a prompt, and saving it as a global lesson would apply it "
            "in every session. Use repo_scope to restrict a lesson to one repository."
        )
    # The tool no longer offers a workspace scope: that tier never reached a
    # prompt, so a lesson saved under it reported success and changed nothing.
    # Restricting a correction to one codebase is what repo_scope does, and the
    # context builder enforces it before injection.
    payload: dict[str, str] = {"rule": rule, "category": category, "scope": "global"}
    # The tool schema advertises ``negative`` -- and the ``rule`` description
    # explicitly tells the model to prefer it over inlining a "-- NOT: ..."
    # clause -- but this payload never forwarded it, so the clause was dropped
    # client-side before /api/lessons could see it. The route validates the
    # field via LEARN_ADD_SCHEMA and passes it through to write_lesson.
    negative = args.get("negative", "")
    if negative:
        payload["negative"] = negative
    repo_scope = args.get("repo_scope", "")
    if repo_scope:
        payload["repo_scope"] = repo_scope
    d = mcp_core._post("/api/lessons", payload)
    err_val = d.get("error")
    if err_val:
        # Map the backend session-scope error to a user-actionable
        # message so the LLM can explain the situation instead of
        # leaking an opaque HTTP 400 as a "transport failed" error.
        # See api_lessons_create in dashboard/handlers/cron.py: the
        # "unknown session" response is returned when the X-Session-Key
        # matches neither a live in-memory slot, a restricted key, the
        # slack: namespace, nor a persisted session JSONL — so the
        # remaining cases are genuinely unrecognised keys (forged, or
        # ephemeral/incognito sessions that never wrote to disk), not
        # merely evicted real sessions.
        if "unknown session" in str(err_val):
            return (
                "Lesson was NOT saved: this session is not recognised "
                "by the gateway (no active slot, restricted key, or "
                "persisted history found for this session key). Start "
                "a new Slack thread or dashboard tab and re-state the "
                "lesson you want to save — it will not carry over "
                "from this session automatically."
            )
        # ``err_val`` is already redacted at the trust boundary by
        # ``_http_error_body`` (HTTP bodies are untrusted external content), and
        # an internal-auth mismatch has already been rewritten there into a
        # sentence naming the instance mix-up -- so every tool gets that copy,
        # not just this one.
        return f"Error: {err_val}"
    if repo_scope:
        return f"Saved lesson (applies only in {repo_scope}): {rule}"
    return f"Saved lesson: {rule}"


def learn_list(name: str, args: dict[str, Any]) -> str:
    d = mcp_core._get("/api/lessons")
    # Surface transport/auth failures instead of rendering them as "no
    # lessons". ``_get`` returns ``{"error": ...}`` on a non-2xx, which has
    # no ``lessons`` key — reporting that as an empty list told the agent its
    # memory was empty when the real cause was an HTTP 403 from a mismatched
    # gateway credential, and sent a debugging session after the wrong bug.
    err_val = d.get("error")
    if err_val:
        return f"Error: {err_val}"
    lessons = d.get("lessons", [])
    if not lessons:
        return "No lessons saved."
    lines = []
    for le in lessons:
        lines.append(f"[{le.get('category', '?')}] {le['rule']}")
    return "\n".join(lines)


def learn_remove(name: str, args: dict[str, Any]) -> str:
    query = args["query"]
    d = mcp_core._delete("/api/lessons", {"rule": query})
    err_val = d.get("error")
    if err_val:
        # Same session-scope mapping as ``learn_add``, but dispatched on the
        # machine-readable ``code`` the delete route emits (and ``_delete``
        # preserves) rather than the error wording, so a rephrased message
        # cannot break the mapping. Make explicit that NOTHING was deleted, so
        # a remove-then-re-add consolidation knows it failed closed at step
        # one instead of assuming the destructive half went through.
        if d.get("code") == "unknown_session":
            return (
                "No lessons were removed: this session is not recognised "
                "by the gateway (no active slot, restricted key, or "
                "persisted history found for this session key). Retry "
                "from an established session (dashboard tab or Slack "
                "thread), or use `kirocrew learn remove` from a shell."
            )
        return f"Error: {err_val}"
    return f"Removed lessons matching: {query}"


HANDLERS: dict[str, Callable[[str, dict[str, Any]], str]] = {
    "learn_add": learn_add,
    "learn_list": learn_list,
    "learn_remove": learn_remove,
}
