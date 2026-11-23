"""The ``[TOOL NAMES]`` block: how this session's harness names and loads a tool.

Kiro Crew's instructions, skills and memory name MCP operations bare, and say
"load it as the ``[TOOL NAMES]`` block says" instead of naming a loader, because
each harness loads a deferred tool its own way:

* kiro-cli (``acp``) defers MCP specs behind its ``tool_search`` built-in, keyed
  by ``<server>::<tool>`` ids (``agent_sdk.tool_search``).
* KAS separates direct calls from executor IDs. Legacy loaders can ask for
  direct invocation using a bare name; that path needs the qualified-name
  fallback. Loaders that specify an executor instead supply its IDs, which must
  not be rewritten using the direct-call naming rule.
* Claude Code defers MCP tools behind its ``ToolSearch`` built-in, named
  ``mcp__<server>__<tool>`` (``test/fixtures/acp_frames/claude``).
* MiMo Code searches its deferred MCP catalog with ``mcp_tool_search``
  (``acp.harness_tool_names.HARNESS_UNASKED_TOOL_IDS``).
* Every other harness lists each mounted tool directly; Crew turns on no
  deferral there (``agent_sdk.backends_mcp.ACP_BACKENDS_TOOL_SEARCH_OVERLAY``,
  pi's ``exposure: "direct"``).

The provider-specific block reaches sessions at startup, including minimal runs,
and is sent again after compaction. A label this build does not know gets none.
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


_OPENING = (
    "[TOOL NAMES]\n"
    "Kiro Crew's instructions, skills and memory use bare MCP operation names "
    "(`work_report`, `monitor_start`) as lookup hints. "
)
_CLOSING = "[End of tool names]\n\n"


def _kiro_cli_block() -> str:
    return (
        _OPENING + "On this harness (kiro-cli) call a tool by the name in your tool list.\n"
        "- With MCP Tool Search on, an MCP tool can be deferred: listed in the deferred-tool "
        "catalogue by `tool_id` (`<server>::<tool>`) but absent from your tool list. "
        "`A tool with the name '<name>' does not exist` means DEFERRED, not missing.\n"
        "- Load it with the `tool_search` built-in by exact id, "
        '`tool_search(tool_id="<server>::<tool>")` (for example '
        "`kirocrew-core::monitor_start`, `kirocrew-dashboard::session_create`), then repeat "
        "the original call. Prefer `tool_id`: a keyword `query` can score below the match "
        "threshold.\n"
        "- If the exact id still misses, the server is not mounted for this session; say "
        "so instead of trying other spellings.\n" + _CLOSING
    )


def _claude_code_block() -> str:
    return (
        _OPENING + "On this harness (Claude Code) an MCP tool is named "
        "`mcp__<server>__<tool>` (for example `mcp__kirocrew-work__work_report`).\n"
        "- Claude Code can defer MCP tools: a deferred tool is listed by name only and must "
        "be loaded before it is called.\n"
        "- Load it with the `ToolSearch` built-in, "
        '`query: "select:mcp__<server>__<tool>"` (several names comma-separated), then call '
        "it by that name. Copy the name from the deferred-tool list; a keyword query can "
        "miss.\n"
        "- If no listed name matches, the server is not mounted for this session; say so "
        "instead of trying other spellings.\n" + _CLOSING
    )


def _mimocode_block() -> str:
    return (
        _OPENING + "On this harness (MiMo Code) call a tool by the name in your tool list; "
        "an MCP tool is named `<server>_<tool>`.\n"
        "- If an MCP tool is not in your list, search the deferred MCP catalogue with the "
        "`mcp_tool_search` built-in, then call it by the name it returns.\n" + _CLOSING
    )


def _direct_block() -> str:
    return (
        _OPENING + "On this harness every tool you can call is already in your tool list, "
        "so no loading step is needed: call the listed tool whose name carries that bare "
        "name. If none does, the tool is not mounted for this session.\n" + _CLOSING
    )


def tool_names_block(provider_type: str | None) -> str:
    """The tool-calling guidance for the selected provider, otherwise ``\"\"``."""
    from kiro_crew.agent_sdk import backends as b
    from kiro_crew.agent_sdk.kas_tool_names import backend_of_provider_label

    builders = {
        b.ACP_BACKEND_KIRO: _kiro_cli_block,
        b.ACP_BACKEND_KAS: _kas_block,
        b.ACP_BACKEND_CLAUDE: _claude_code_block,
        b.ACP_BACKEND_MIMOCODE: _mimocode_block,
        b.ACP_BACKEND_CODEX: _direct_block,
        b.ACP_BACKEND_OPENCODE: _direct_block,
        b.ACP_BACKEND_PI: _direct_block,
        b.ACP_BACKEND_GOOSE: _direct_block,
        b.ACP_BACKEND_DEEPSEEK: _direct_block,
        b.ACP_BACKEND_COPILOT: _direct_block,
        b.ACP_BACKEND_STRANDS: _direct_block,
        b.ACP_BACKEND_ANTIGRAVITY: _direct_block,
    }
    backend = backend_of_provider_label(provider_type)
    build = builders.get(backend) if backend is not None else None
    return build() if build is not None else ""


def _kas_block() -> str:
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
