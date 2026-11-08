"""The ``[TOOL NAMES]`` block: how to call an MCP tool on a harness that resolves
only qualified names.

KAS resolves a model's tool call by the tool's id (``mcp_<server>_<tool>``) or its
display name (``@<server>/<tool>``) and by nothing else
(:mod:`kiro_crew.agent_sdk.kas_tool_names`). Kiro Crew's instructions, skills and
memory name its tools bare (``work_brief``, ``work_report``, ``memory_recall``), and
KAS's own ``tool_search`` returns the bare ``tool_name`` with the advice to call
exactly that. A model that follows either calls a name KAS answers with
``Tool "work_report" is not available.``, and a dispatched worker that concludes the
tool is broken never reports at all.

Why the session context rather than the instructions or the harness:

* Respelling the shipped prompts (``@kirocrew-work/work_report``) would change what
  every other harness reads too. kiro-cli's own engine lists the bare name, claude
  lists ``mcp__<server>__<tool>``, and none of them is KAS's id.
* KAS has no alias seam: a tool call is matched against the tool list's ``id`` and
  ``displayName`` only, so there is nothing a client can register to make the bare
  name resolve.
* The context is already built per harness (the steering gate reads the same
  ``provider_type``), so this block reaches exactly the sessions KAS serves, every
  MCP tool on every server, and is sent again after a compaction with the rest.
"""

from __future__ import annotations

#: ``(server, tool)`` pairs the block spells out as examples. The work tools come
#: first because a worker that cannot call them cannot report; ``memory_recall``
#: shows the rule is per server, not per tool.
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
    """The block for a session served by *provider_type*, or ``""`` when it needs none.

    Only KAS needs it today. Another harness that resolves an MCP call only by a
    qualified name joins here with its own spelling, not by inheriting KAS's.
    """
    from kiro_crew.agent_sdk.kas_tool_names import is_kas_provider_label

    if not is_kas_provider_label(provider_type):
        return ""
    return (
        "[TOOL NAMES]\n"
        "On this harness an MCP tool is callable only by its qualified name. A bare "
        "MCP tool name is never callable here, even when an instruction, a memory or "
        "tool_search's `tool_name` spells it bare (built-in tools such as `shell` and "
        "`tool_search` keep their own names).\n"
        "- The qualified name is `mcp_<server>_<tool>`, lower-case, with each `-` in "
        "the server name written as `_`; `@<server>/<tool>` also works. For example "
        f"{_examples()}. Kiro Crew's instructions name its tools bare: call each one "
        "this way, including after tool_search loads it.\n"
        '- `Tool "<name>" is not available.` means the call used a bare name. Retry the '
        "same call at once with the qualified name. It does not mean the tool is broken "
        "or missing.\n"
        "[End of tool names]\n\n"
    )
