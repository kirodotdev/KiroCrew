"""The ``[TOOL NAMES]`` block: follow the active tool-calling contract.

Kiro Crew's instructions, skills and memory name MCP operations bare. A session
may expose a tool directly or require a schema load followed by a separate
executor. The live definitions and discovery results supply the callable names
and IDs; a fixed spelling rule cannot cover both paths.

This guidance belongs in the per-harness session context so the shared agent
instructions keep their bare operation names. The provider-specific block reaches
sessions at startup, including minimal runs, and is sent again after compaction.
"""

from __future__ import annotations

#: Server/operation pairs help the model find a tool without inventing its ID.
_EXAMPLES: tuple[tuple[str, str], ...] = (
    ("kirocrew-work", "work_brief"),
    ("kirocrew-work", "work_report"),
    ("kirocrew-work", "work_ledger_record"),
    ("kirocrew-core", "memory_recall"),
)


def _examples() -> str:
    spelled = [f"`{tool}` on `{server}`" for server, tool in _EXAMPLES]
    return ", ".join(spelled[:-1]) + f", and {spelled[-1]}"


def tool_names_block(provider_type: str | None) -> str:
    """The tool-calling guidance for the selected provider, otherwise ``""``."""
    from kiro_crew.agent_sdk.kas_tool_names import is_kas_provider_label

    if not is_kas_provider_label(provider_type):
        return ""
    return (
        "[TOOL NAMES]\n"
        "Kiro Crew's instructions, skills and memory use bare MCP operation names as "
        "lookup hints. Use the callable names and IDs shown by the active tool "
        "definitions and discovery results.\n"
        "- For a directly exposed tool, call the exact name in its active tool definition.\n"
        "- For a deferred tool, follow the available loader's instructions and wait for "
        "the schema result before calling. Use the execution method that result specifies; "
        "if it names `tool_call` as the executor, pass its `tool_id` unchanged. Do not "
        "construct or rewrite that ID from a server name, tool name or display name. "
        "Do not reload a tool whose schema-load result is still in context.\n"
        f"- Lookup examples: {_examples()}. These pairs help find the matching catalogue "
        "entry; they are not execution IDs.\n"
        "- If a tool is unavailable, check the current catalogue and the loader's recovery "
        "guidance. It may be missing or outside this session's scope; do not assume a "
        "name rewrite will fix it or bypass restrictions.\n"
        "[End of tool names]\n\n"
    )
