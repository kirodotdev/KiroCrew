"""The ``[TOOL NAMES]`` block: distinguish direct calls from executor IDs.

Kiro Crew's instructions, skills and memory name MCP operations bare. Legacy
loaders can ask for direct invocation using a bare name; that path needs the
qualified-name fallback. Loaders that specify an executor instead supply its
IDs, which must not be rewritten using the direct-call naming rule.

The provider-specific block reaches sessions at startup, including minimal runs,
and is sent again after compaction.
"""

from __future__ import annotations

#: Direct-call examples only; never use these names as catalogue or executor IDs.
_EXAMPLES: tuple[tuple[str, str], ...] = (
    ("kirocrew-work", "work_brief"),
    ("kirocrew-work", "work_report"),
    ("kirocrew-work", "work_ledger_record"),
    ("kirocrew-core", "memory_recall"),
)


def _examples() -> str:
    from kiro_crew.agent_sdk.kas_tool_names import kas_mcp_tool_id

    spelled = [
        f"`{tool}` on `{server}` is `{kas_mcp_tool_id(server, tool)}`" for server, tool in _EXAMPLES
    ]
    return ", ".join(spelled[:-1]) + f", and {spelled[-1]}"


def tool_names_block(provider_type: str | None) -> str:
    """The tool-calling guidance for the selected provider, otherwise ``\"\"``."""
    from kiro_crew.agent_sdk.kas_tool_names import is_kas_provider_label

    if not is_kas_provider_label(provider_type):
        return ""
    return (
        "[TOOL NAMES]\n"
        "Kiro Crew's instructions, skills and memory use bare MCP operation names as "
        "lookup hints. Loading a tool, invoking it directly and passing an ID to an "
        "executor are different operations. Built-in tools keep their own names.\n"
        "Direct MCP calls:\n"
        "- For a directly exposed tool, call the exact name in its active tool definition. "
        "If a legacy loader asks for direct invocation but returns only a bare `tool_name` "
        "and no executor, use the qualified direct-call name, not the bare name. "
        "The direct-call spelling is `mcp_<server>_<tool>`, lower-case, with each `-` in "
        "the server name written as `_`; `@<server>/<tool>` is also a direct-call alias. "
        "An exact active definition takes precedence over this recipe. "
        f"Direct-call examples: {_examples()}.\n"
        '- If a direct call using a bare name returns `Tool "<name>" is not available.`, '
        "retry with the qualified direct-call name. This fallback applies only to direct "
        "calls, never to loader inputs or executor IDs.\n"
        "Deferred tools and executors:\n"
        "- Before loading, copy the exact ID from the current catalogue; do not construct "
        "it from the direct-call examples above. Follow the available loader's instructions "
        "and wait for the schema result before calling.\n"
        "- If the result specifies an executor, use that executor, not a direct MCP call; "
        "if it names `tool_call` as the executor, pass its `tool_id` unchanged. Do not "
        "construct or rewrite that ID from a server name, tool name or display name. "
        "Do not reload a tool whose schema-load result is still in context.\n"
        "- If a loader or executor reports a tool unavailable, check the current catalogue "
        "and the loader's recovery guidance. It may be missing or outside this session's "
        "scope; do not assume a name rewrite will fix it or bypass restrictions.\n"
        "[End of tool names]\n\n"
    )
