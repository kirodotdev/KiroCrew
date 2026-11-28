"""Small, factual, per-turn KiroCrew context for the BYO provider boundary.

This is not an agent persona, a permission grant, or a durable session setting.
Only the current route decision and verified host tool names may be advertised.
"""
from __future__ import annotations

import re
from typing import Iterable


# Widget intent is a usability hint, not authority to invoke any Crew tool.
# Keep explicit library operations separate from inline UI rendering.
_VISUAL_TERMS = re.compile(
    r"(?:\b(?:widget|visuali[sz]e|interactive|dashboard|chart|diagram|infographic)\b"
    r"|小组件|小組件|可视化|視覺化|交互式|互動式|图表|圖表|信息图|資訊圖|对比图|對比圖|画.{0,16}图|畫.{0,16}圖|绘.{0,16}图|繪.{0,16}圖"
    r"|ウィジェット|可視化|図表|図を作|위젯|시각화|차트|다이어그램)",
    re.IGNORECASE,
)
_ARTIFACT_TERMS = re.compile(r"(?:\b(?:artifact|widget)\b|产物|產物|小组件|小組件|ウィジェット|アーティファクト|成果物|위젯|아티팩트|산출물)", re.IGNORECASE)

# Users should not need to know the host's special vocabulary to request UI.
_INTERACTION_TERMS = re.compile(
    r"(?:\b(?:intelligent\s*ui|live\s*widget|interactive|clickable|drag(?:gable)?|slider|sliders)\b"
    r"|交互|互動|点击|點擊|拖动|拖動|滑块|滑塊|滑杆|滑桿|调节参数|調節參數"
    r"|インタラクティブ|ドラッグ|スライダー|クリックできる|인터랙티브|드래그|슬라이더)",
    re.IGNORECASE,
)
_UI_TARGET_TERMS = re.compile(
    r"(?:\b(?:ui|interface|calculator|widget|chart|dashboard|graph|visualization|simulation)\b"
    r"|界面|介面|面板|计算器|計算器|交互图|互動圖|图形|圖形|图表|圖表|函数图|函數圖|曲线|曲線|模拟器|模擬器"
    r"|画布|畫布|组件|組件|ウィジェット|画面|グラフ|計算機|위젯|계산기|화면)",
    re.IGNORECASE,
)
_EDIT_TERMS = re.compile(
    r"(?:\b(?:edit|modify|update|revise|change|tweak|continue|improve|adjust)\b"
    r"|修改|继续改|繼續改|接着改|接著改|调整|調整|改成|改一下|更新|优化|優化|完善|编辑|編輯|再改|改为|改為"
    r"|修正|直して|変更|修正して|수정|바꿔)",
    re.IGNORECASE,
)
_REFERENCE_TERMS = re.compile(
    r"(?:\b(?:above|previous|earlier|last|existing|that|it)\b"
    r"|上面|之前|刚才|剛才|那个|那個|这个|這個|原来|原來|刚做|剛做|上一版|前面|先前|那个图|那個圖"
    r"|さっき|前の|それ|아까|이전|그거)",
    re.IGNORECASE,
)
# Explicit "explain only" requests must not be turned into UI-building tasks.
_EXPLAIN_ONLY = re.compile(
    r"(?:^(?:什么是|什麼是|解释一下|解釋一下|介绍一下|介紹一下|什么叫|什麼叫|what\s+is|what\s+are)"
    r"|(?:不要|不用|无需|無需|别|別|不需要|do not|don['’]t|no need to).{0,24}(?:生成|制作|创建|建立|build|create|render|做|widget|界面|UI))",
    re.IGNORECASE,
)


def has_recent_crew_widget(messages: object) -> bool:
    """Use only recent assistant output as evidence of an existing inline widget."""
    if not isinstance(messages, (list, tuple)):
        return False
    assistant_count = 0
    # Tool-heavy turns may contain dozens of messages between two user turns.
    # Limit both the scan size and assistant distance; never trust a user/tool
    # quoting a widget tag as evidence that Crew actually rendered one.
    for row in reversed(messages[-200:]):
        if not isinstance(row, dict) or row.get("role") != "assistant":
            continue
        assistant_count += 1
        content = row.get("content")
        if isinstance(content, str) and "<mcwidget" in content.lower():
            return True
        if assistant_count >= 12:
            break
    return False


def _is_widget_edit(text: str, *, recent_widget: bool) -> bool:
    if not _EDIT_TERMS.search(text):
        return False
    return bool(
        (_UI_TARGET_TERMS.search(text) and _INTERACTION_TERMS.search(text))
        or (recent_widget and _REFERENCE_TERMS.search(text))
    )


def wants_crew_visual_artifact(text: str, *, recent_widget: bool = False) -> bool:
    """Hint at Crew UI affordances without mistaking them for tool grants."""
    message = str(text or "")[:8000].strip()
    if not message or _EXPLAIN_ONLY.search(message):
        return False
    return bool(
        _VISUAL_TERMS.search(message)
        or (_INTERACTION_TERMS.search(message) and _UI_TARGET_TERMS.search(message))
        or _is_widget_edit(message, recent_widget=recent_widget)
    )


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
    recent_widget: bool = False,
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

    # Always available: the model can understand paraphrases our small, fast
    # heuristic will never enumerate. This guidance does not expose new tools.
    lines.append(
        "When the user requests an interactive UI, clickable controls, sliders, "
        "a live chart/calculator, or revisions to an earlier interactive UI, "
        "recognize the intent from meaning, not the literal words 'widget'. "
        "Use KiroCrew's own inline widget rendering when available. "
        "For ordinary explanations or non-UI requests, answer normally; "
        "do not create a widget without the user's intent."
    )
    if wants_crew_visual_artifact(user_text, recent_widget=recent_widget):
        lines.append(
            "Crew visual deliverable requested: when the user wants something rendered directly "
            "in chat, prefer the host's inline <mcwidget title=\"...\">HTML</mcwidget> format "
            "over silently writing a local HTML file. Follow the host widget Skill if available. "
            "An inline widget is not the same as an arbitrary filesystem file. "
            "Interactive controls should work locally; do not invent a widget-to-Agent "
            "action bridge or bypass the host sandbox. "
            "Do not claim it was saved until the host actually acknowledges it. "
            "Use real source data; never invent factual numeric scores for a comparison."
        )
        if "mcp__kirocrew-core__skill_search" in tools:
            lines.append(
                "Crew's verified skill_search tool is available: search for 'widgets' "
                "and optionally 'living-ui' to load the host's current format, "
                "theme, and accessibility guidance before building."
            )
        if "mcp__kirocrew-core__artifact_save" in tools:
            lines.append(
                "The verified Crew artifact_save tool is available for explicit persistent artifact "
                "requests; only call it when needed and permitted."
            )
        if _is_widget_edit(user_text, recent_widget=recent_widget):
            lines.append(
                "This is a widget revision: preserve existing controls and user data "
                "where possible. If the widget is a registered Crew artifact, "
                "inspect its actual identifier/version with host tools before updating it; "
                "otherwise revise and re-render the inline widget without claiming a save."
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
    recent_widget = has_recent_crew_widget(getattr(session, "messages", ()))
    session.crew_artifact_tools_requested = (
        wants_crew_artifact_tools(user_text)
        or _is_widget_edit(str(user_text or ""), recent_widget=recent_widget)
    )
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
        recent_widget=recent_widget,
    )
    return "\n\n".join(part for part in (base_context, extra) if part)


def crew_browser_action_requested(text: str) -> bool:
    """Detect an explicit browser action before waiting on Crew's tool schema.

    This controls catalogue readiness only; it does not grant permissions.
    """
    value = str(text or "").lower()
    browser = ("browser" in value or "浏览器" in value or "網頁" in value
               or "网页" in value or "網頁" in value or "网站" in value
               or "網站" in value or "web page" in value)
    action = any(word in value for word in (
        "打开", "打開", "访问", "訪問", "点击", "點擊", "操作", "控制",
        "输入", "輸入", "截图", "截圖", "导航", "導航", "浏览网页", "瀏覽網頁",
        "open", "navigate", "click", "interact", "screenshot", "control browser",
    ))
    return browser and action
