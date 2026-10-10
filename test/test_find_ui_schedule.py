"""``find_ui`` goldens for the wave-3 Schedule, Artifacts and Customize controls.

Each newcomer phrasing, in English and zh-CN, must reach its location first,
with the exact path, route and prerequisites; and wording that only shares a
verb with these controls must stay ``no_match``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kiro_crew import ui_index


@pytest.fixture(autouse=True)
def _fresh_cache(_floor_monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ui_index._cache.update(key=None, index=None)
    # The committed tiers only, whatever dashboard build this checkout has:
    # the build-time auto tier is pinned in test_find_ui_auto.py.
    _floor_monkeypatch.setattr(ui_index, "AUTO_INDEX_PATH", tmp_path / "no-auto-tier.json")


def _top(query: str, lang: str) -> dict[str, Any]:
    d = ui_index.find_ui(query, lang)
    assert d["status"] == "ok", d
    return d["results"][0]


def _path(placement: dict[str, Any]) -> list[str]:
    return [seg["label"] for seg in placement["path"]]


def _conditions(placement: dict[str, Any]) -> list[str]:
    return [r["id"] for r in placement["requires"] if r["kind"] == "condition"]


# id -> (English path, zh-CN path, route, conditions, viewport or None)
_EXPECTED: dict[str, tuple[list[str], list[str], str, list[str], str | None]] = {
    "schedule.templates": (
        ["Schedule", "Browse all templates"],
        ["日程", "浏览全部模板"],
        "/schedule",
        ["no_schedules"],
        None,
    ),
    "schedule.new-folder": (
        ["Schedule", "New folder"],
        ["日程", "新建文件夹"],
        "/schedule",
        # List-only: the Calendar and Executions views hide them.
        ["has_schedules", "schedule_list_view"],
        None,
    ),
    "schedule.select-all": (
        ["Schedule", "Select all jobs"],
        ["日程", "选择所有任务"],
        "/schedule",
        # List-only: the Calendar and Executions views hide them.
        ["has_schedules", "schedule_list_view"],
        None,
    ),
    "schedule.run-now": (
        ["Schedule", "Run Now"],
        ["日程", "立即运行"],
        "/schedule",
        ["job_open", "job_not_running"],
        None,
    ),
    "schedule.row-run": (
        ["Schedule", "Run"],
        ["日程", "运行"],
        "/schedule",
        ["has_schedules", "schedule_list_view"],
        None,
    ),
    "schedule.cancel-run": (
        ["Schedule", "Cancel Run"],
        ["日程", "取消运行"],
        "/schedule",
        ["job_open", "job_running"],
        None,
    ),
    "schedule.delete": (["Schedule", "Delete"], ["日程", "删除"], "/schedule", ["job_open"], None),
    "schedule.secret-approve": (
        ["Schedule", "Approve"],
        ["日程", "批准"],
        "/schedule",
        # The secrets panel is in the Details tab; Logs replaces that body.
        ["job_open", "job_details_tab", "job_secret_request_pending"],
        None,
    ),
    "artifacts.starred": (["Artifacts", "Starred"], ["产物", "已加星"], "/artifacts", [], None),
    "artifacts.deploy": (
        ["Artifacts", "Artifact Deploy"],
        ["产物", "产物部署"],
        "/artifacts",
        ["cloud_deploy_available"],
        "desktop",
    ),
    "artifacts.new-folder": (
        ["Artifacts", "New folder"],
        ["产物", "新建文件夹"],
        "/artifacts",
        [],
        "desktop",
    ),
    "skills.create": (
        ["Customize", "Skills", "Create New Skill"],
        ["自定义", "技能", "新建技能"],
        "/capabilities?tab=skills",
        [],
        None,
    ),
    "skills.add": (
        ["Customize", "Skills", "Add Skill"],
        ["自定义", "技能", "添加技能"],
        "/capabilities?tab=skills",
        [],
        None,
    ),
    "skills.refresh": (
        ["Customize", "Skills", "Refresh skills"],
        ["自定义", "技能", "刷新技能"],
        "/capabilities?tab=skills",
        [],
        None,
    ),
    "mcp.probe": (
        ["Customize", "Connections", "MCP Servers", "Probe MCP servers"],
        ["自定义", "连接", "MCP 服务器", "探测 MCP 服务器"],
        "/capabilities?tab=mcp",
        [],
        None,
    ),
}

_PHRASINGS: list[tuple[str, str, str]] = [
    # The row's own Run: on screen without opening the job first.
    ("run a job now", "en", "schedule.row-run"),
    ("run my schedule immediately", "en", "schedule.row-run"),
    ("run now in the job panel", "en", "schedule.run-now"),
    ("手动运行任务", "zh-CN", "schedule.row-run"),
    ("立即运行", "zh-CN", "schedule.run-now"),
    ("stop a running job", "en", "schedule.cancel-run"),
    ("停止运行中的任务", "zh-CN", "schedule.cancel-run"),
    ("delete a scheduled job", "en", "schedule.delete"),
    ("删除定时任务", "zh-CN", "schedule.delete"),
    ("approve a secret for a job", "en", "schedule.secret-approve"),
    ("批准密钥", "zh-CN", "schedule.secret-approve"),
    ("job templates", "en", "schedule.templates"),
    ("任务模板", "zh-CN", "schedule.templates"),
    ("schedule folder", "en", "schedule.new-folder"),
    ("任务文件夹", "zh-CN", "schedule.new-folder"),
    ("select all jobs", "en", "schedule.select-all"),
    ("全选任务", "zh-CN", "schedule.select-all"),
    # Moving every job is Select all's; the folder button moves only what is ticked.
    ("move all jobs to a folder", "en", "schedule.select-all"),
    ("批量移动任务到文件夹", "zh-CN", "schedule.select-all"),
    ("starred artifacts", "en", "artifacts.starred"),
    ("已加星标的产物", "zh-CN", "artifacts.starred"),
    ("make an artifact folder", "en", "artifacts.new-folder"),
    ("新建产物文件夹", "zh-CN", "artifacts.new-folder"),
    ("publish an artifact", "en", "artifacts.deploy"),
    ("发布产物", "zh-CN", "artifacts.deploy"),
    ("create a skill", "en", "skills.create"),
    ("创建技能", "zh-CN", "skills.create"),
    ("add a skill", "en", "skills.add"),
    ("添加技能", "zh-CN", "skills.add"),
    ("refresh skills", "en", "skills.refresh"),
    ("刷新技能", "zh-CN", "skills.refresh"),
    ("test my MCP servers", "en", "mcp.probe"),
    ("检测 MCP 服务器", "zh-CN", "mcp.probe"),
]


@pytest.mark.parametrize(("query", "lang", "loc_id"), _PHRASINGS)
def test_a_newcomer_phrasing_reaches_its_location_first(query: str, lang: str, loc_id: str) -> None:
    top = _top(query, lang)
    assert top["id"] == loc_id
    en_path, zh_path, route, conditions, viewport = _EXPECTED[loc_id]
    (placement,) = top["placements"]
    assert _path(placement) == (zh_path if lang == "zh-CN" else en_path)
    assert placement["route"] == route
    assert _conditions(placement) == conditions
    viewports = [r["value"] for r in placement["requires"] if r["kind"] == "viewport"]
    assert viewports == ([viewport] if viewport else [])


def test_every_expected_location_has_an_english_and_a_chinese_phrasing() -> None:
    for loc_id in _EXPECTED:
        langs = {lang for _, lang, hit in _PHRASINGS if hit == loc_id}
        assert langs == {"en", "zh-CN"}, loc_id


def test_a_condition_is_returned_with_its_description() -> None:
    (placement,) = _top("approve a secret for a job", "en")["placements"]
    described = {
        r["id"]: r["description"] for r in placement["requires"] if r["kind"] == "condition"
    }
    assert described["job_open"].startswith("a scheduled job's detail panel is open")
    assert (
        described["job_secret_request_pending"] == "the open job is waiting for a secret approval"
    )


def test_the_secret_approval_never_carries_a_guide() -> None:
    assert "guide_ref" not in _top("approve a secret for a job", "en")


@pytest.mark.parametrize(
    ("query", "lang"),
    [
        ("run a marathon", "en"),
        ("delete my account", "en"),
        ("where are my keys", "en"),
        ("chat about dinner", "en"),
        ("跑马拉松", "zh-CN"),
    ],
)
def test_wording_that_only_shares_a_verb_is_no_match(query: str, lang: str) -> None:
    assert ui_index.find_ui(query, lang)["status"] == "no_match"
