"""Natural-language Live Widget hints preserve Crew's authorization boundary."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiro_byo.crew_turn_context import (
    crew_turn_context,
    extend_crew_context,
    has_recent_crew_widget,
    wants_crew_artifact_tools,
    wants_crew_visual_artifact,
)


@pytest.mark.parametrize("message", [
    "生成一个交互式图表",
    "live widget 里放一个滑块",
    "做一个能拖动参数的智能界面",
    "像 ChatGPT Intelligent UI 那样直接交互",
    "做个可以点击的计算器",
    "把上面的交互图继续修改",
    "Make a clickable calculator in this chat",
    "Create an interface with draggable sliders",
    "做一个可以拖動參數的介面",
    "インタラクティブな画面を作って",
    "드래그할 수 있는 계산기 화면을 만들어 줘",
])
def test_semantic_ui_phrasings_trigger_hint(message):
    assert wants_crew_visual_artifact(message), message


@pytest.mark.parametrize("message", [
    "什么是 Intelligent UI？",
    "不要生成 Widget，只解释交互界面的概念",
    "请解释 ACP 是什么",
    "请删除本地生成的文件",
    "帮我调整后端路由",
    "继续修改刚才那个",
])
def test_non_ui_or_under_specified_message_not_forced_to_widget(message):
    assert not wants_crew_visual_artifact(message)


def test_followup_edit_requires_real_assistant_widget_in_history():
    assert wants_crew_visual_artifact("继续修改刚才那个", recent_widget=True)
    assert not wants_crew_visual_artifact("继续修改刚才那个", recent_widget=False)
    assert has_recent_crew_widget([
        {"role": "assistant", "content": '<mcwidget title="graph">...</mcwidget>'},
        {"role": "user", "content": "漂亮！"},
    ])
    assert not has_recent_crew_widget([{"role": "user", "content": '<mcwidget title="fake">'}])
    assert not has_recent_crew_widget([{"role": "tool", "content": '<mcwidget title="fake">'}])


def test_visual_edit_prepares_only_scoped_verified_artifact_tools():
    class FakeMcp:
        def host_capability_snapshot(self, *, allowed):
            return {"available_tools": (
                "mcp__kirocrew-core__skill_search",
                "mcp__kirocrew-core__artifact_get",
                "mcp__kirocrew-core__artifact_update",
            )}

    session = SimpleNamespace(
        messages=[{"role": "assistant", "content": '<mcwidget title="graph">...</mcwidget>'}],
        agent=SimpleNamespace(agent_id="kirocrew-mate-worker"),
        mcp=FakeMcp(),
    )
    result = extend_crew_context(
        "original persona", session, SimpleNamespace(mode_pack="tiga"), None,
        "把刚才那个改成蓝色", lambda _session, _binding: True,
    )
    assert session.crew_artifact_tools_requested
    assert "widget revision" in result.lower()
    assert "verified skill_search tool is available" in result
    assert "bypass the host sandbox" in result
    assert "original persona" in result
    assert "Crew-projected agent id: kirocrew-mate-worker" in result

    # A later unrelated request cannot inherit privileges from the edit.
    result = extend_crew_context(
        "original persona", session, SimpleNamespace(mode_pack="tiga"), None,
        "帮我解释 ACP", lambda _session, _binding: True,
    )
    assert not session.crew_artifact_tools_requested
    assert "Crew visual deliverable requested" not in result


def test_every_turn_has_small_host_hint_without_auto_widget():
    plain = crew_turn_context(user_text="请解释一下 ACP")
    assert "recognize the intent from meaning" in plain
    assert "Crew visual deliverable requested" not in plain
    assert not wants_crew_artifact_tools("做个可以点击的计算器")
    widget = crew_turn_context(user_text="做个可以点击的计算器")
    assert '<mcwidget title="...">' in widget
    assert "widget-to-Agent" in widget
    assert "verified skill_search tool is available" not in widget
