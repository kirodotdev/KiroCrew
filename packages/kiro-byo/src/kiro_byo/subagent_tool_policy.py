"""Read-only vs isolated-writer tool visibility for child Agent turns.

This module only filters *advertised* tools. Runtime dispatch, sandbox checks,
ACP permissions and tool side effects remain enforced by the existing runner.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from .mcp_client import DEFERRED_TOOL_SEARCH_NAME


def filter_isolated_subagent_tools(
    tools: Iterable[dict[str, Any]], *, writer_mode: bool, native: Any,
    is_gathering_tool: Callable[[str], bool],
) -> list[dict[str, Any]]:
    allowed: list[dict[str, Any]] = []
    for tool in tools:
        name = str(((tool or {}).get("function") or {}).get("name") or "")
        if name == DEFERRED_TOOL_SEARCH_NAME:
            continue
        if writer_mode:
            if native is not None and native.has_tool(name):
                allowed.append(tool)
        elif is_gathering_tool(name):
            allowed.append(tool)
    return allowed


def subagent_system_instruction(objective: str, *, writer_mode: bool) -> str:
    if writer_mode:
        return (
            "Kiro BYO isolated writer. You work only inside your private git worktree. "
            f"Your objective is: {objective}. Implement a candidate solution, run only "
            "the checks needed for your candidate, never modify the parent workspace, and return "
            "a concise hand-back describing files changed, checks run, and unresolved risk."
        )
    return (
        "Kiro BYO isolated subagent. You are a read-only child of the lead agent. "
        f"Your objective is: {objective}. "
        "Inspect independently and avoid duplicating obvious work from other children. "
        "You may only use the read-only tools exposed to you. Never propose or perform writes, "
        "and do not answer the end user. When enough evidence is gathered, return a concise "
        "factual hand-back containing findings, evidence and unresolved uncertainty."
    )
