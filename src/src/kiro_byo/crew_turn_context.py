"""Small, factual, per-turn KiroCrew context for the BYO provider boundary.

This is not an agent persona, a permission grant, or a durable session setting.
Only the current route decision and verified host tool names may be advertised.
"""
from __future__ import annotations

import re
from typing import Iterable


# Treat ordinary file changes differently from an explicit Crew artifact/widget.
_VISUAL_TERMS = re.compile(
    r"(?:\b(?:widget|visuali[sz]e|interactive|dashboard|chart|diagram|infographic)\b"
    r"|小组件|小組件|可视化|視覺化|交互式|互動式|图表|圖表|信息图|資訊圖|对比图|對比圖|画.{0,16}图|畫.{0,16}圖|绘.{0,16}图|繪.{0,16}圖"
    r"|ウィジェット|可視化|図表|図を作|위젯|시각화|차트|다이어그램)",
    re.IGNORECASE,
)
_ARTIFACT_TERMS = re.compile(r"(?:\b(?:artifact|widget)\b|产物|產物|小组件|小組件|ウィジェット|アーティファクト|成果物|위젯|아티팩트|산출물)", re.IGNORECASE)


def wants_crew_visual_artifact(text: str) -> bool:
    """Return a hint for extra host affordances, never tool authorization."""
    return bool(_VISUAL_TERMS.search(str(text or "")[:8000]))


def wants_crew_artifact_tools(text: str) -> bool:
    """Explicit library/widget requests, not arbitrary local-file operations."""
    return bool(_ARTIFACT_TERMS.search(str(text or "")[:8000]))


def _safe_identifier(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]", "", str(value or ""))[:80]


def crew_turn_context(
    *,
    agent_id: str = "",
    configured_mode: str = "",
    active_mode: str = "",
    active_type: str = "",
    concrete_model: bool = False,
    user_text: str = "",
    verified_tool_names: Iterable[str] = (),
) -> str:
    """Construct factual host/product identity at the current turn boundary.

    When a concrete model is selected, a configured Mode is not a claim that
    its adaptive Type router is active. Crew owns the agent persona and grants.
    """
    tools = set(verified_tool_names)
    lines = [
        "Host application: KiroCrew (Kiro Crew).",
        "Execution backend: Kiro BYO, a third-party ACP Agent Harness inside KiroCrew.",
        "You are acting as the Crew agent/assistant in this session, not as a separate BYO chat product. "
        "Keep the Crew-projected agent name, persona and instructions when provided; "
        "do not invent an upstream provider's branded identity. If asked, explain the distinction "
        "between KiroCrew (host), Kiro BYO (backend), and the served AI model.",
    ]
    if agent_id:
        lines.append(f"Crew-projected agent id: {_safe_identifier(agent_id)}.")
    if concrete_model:
        lines.append(
            "Model selection: concrete model direct; Type adaptive routing is bypassed for this turn."
        )
        if configured_mode:
            lines.append(f"Configured BYO Mode (not active Type routing): {_safe_identifier(configured_mode)}.")
    elif active_mode and active_type:
        lines.append(
            f"Active BYO Mode: {_safe_identifier(active_mode)}; "
            f"active Type: {_safe_identifier(active_type)}. "
            "These are routing policies, not the model's personal identity."
        )
    else:
        lines.append("Active BYO Mode/Type was not verified for this turn; do not guess it.")

    if wants_crew_visual_artifact(user_text):
        lines.append(
            "Crew visual deliverable requested: when the user wants something rendered directly "
            "in chat, prefer the host's inline <mcwidget title=\"...\">HTML</mcwidget> format "
            "over silently writing a local HTML file. Follow the host widget Skill if available. "
            "An inline widget is not the same as an arbitrary filesystem file. "
            "Do not claim it was saved until the host actually acknowledges it. "
            "Use real source data; never invent factual numeric scores for a comparison."
        )
        if "mcp__kirocrew-core__skill_search" in tools:
            lines.append(
                "Crew's verified skill_search tool is available: search for 'widgets' or "
                "'interactive widgets' to load its format and theme guidance before building."
            )
        if "mcp__kirocrew-core__artifact_save" in tools:
            lines.append(
                "The verified Crew artifact_save tool is available for explicit persistent artifact "
                "requests; only call it when needed and permitted."
            )
    elif wants_crew_artifact_tools(user_text):
        lines.append(
            "Crew artifact request: use the verified host artifact MCP tools if supplied; "
            "discover missing tools via the provided tool-search mechanism. "
            "Do not equate local workspace files with registered Crew artifacts."
        )
    return "\n".join(lines)


def crew_artifact_tool_names_for_turn(session: object, allowed: object) -> tuple[str, ...]:
    """Reveal only the host's verified artifact capabilities on relevant turns."""
    mcp = getattr(session, "mcp", None)
    if not getattr(session, "crew_artifact_tools_requested", False) or mcp is None:
        return ()
    if not hasattr(mcp, "crew_artifact_tool_names"):
        return ()
    return tuple(mcp.crew_artifact_tool_names(allowed=lambda binding: allowed(session, binding)))


def extend_crew_context(
    base_context: str, session: object, config: object, decision: object,
    user_text: str, allowed: object,
) -> str:
    """Refresh turn-local host context after actual routing (never persist it)."""
    session.crew_artifact_tools_requested = wants_crew_artifact_tools(user_text)
    verified_tools: tuple[str, ...] = ()
    mcp = getattr(session, "mcp", None)
    if mcp is not None and hasattr(mcp, "host_capability_snapshot"):
        snapshot = mcp.host_capability_snapshot(allowed=lambda binding: allowed(session, binding))
        verified_tools = tuple(snapshot.get("available_tools") or ())
    extra = crew_turn_context(
        agent_id=str(getattr(getattr(session, "agent", None), "agent_id", "") or ""),
        configured_mode=str(getattr(config, "mode_pack", "") or ""),
        active_mode=str(getattr(decision, "pack", "") or "") if decision is not None else "",
        active_type=str(getattr(decision, "policy", "") or "") if decision is not None else "",
        concrete_model=decision is None,
        user_text=user_text,
        verified_tool_names=verified_tools,
    )
    return "\n\n".join(part for part in (base_context, extra) if part)
