"""``find_ui`` browse mode (``area``) and the novice-demand corpus it was built for.

A demand evaluation (100 held-out novice questions, EN and zh-CN, ten areas,
plus ten out-of-scope negatives) showed that the search's bottleneck is
RECALL: most misses target a location the index already has, under words the
question does not use. Browse mode lists one area whole, so the caller can pick
the entry that means the question. This file pins:

- the listing's shape, paging and byte cap, for every area in two locales;
- that the listing is exactly the area's members (no entry lost to paging);
- that every corpus task whose location is indexed is in the listing of the
  area the corpus row names (recall@area), mapped task -> location id here, so
  a rename or a lost descriptor is caught; and the tasks with no indexed
  location (true coverage gaps) are named, so closing one is a visible change;
- that the out-of-scope negatives never produce a search hit.

Committed tiers only (the auto tier is a build artifact; ``test_find_ui_auto``
pins it): every mapped location below is generated or curated.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kiro_crew import mcp_guide, ui_index
from kiro_crew.validation import ValidationError


@pytest.fixture(autouse=True)
def _fresh_cache(_floor_monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ui_index._cache.update(key=None, index=None)
    _floor_monkeypatch.setattr(ui_index, "AUTO_INDEX_PATH", tmp_path / "no-auto-tier.json")


#: (area, lang, question, intended task): the held-out demand corpus, verbatim.
CORPUS: list[tuple[str, str, str, str]] = [
    ("sessions", "en", "Where do I begin a fresh conversation?", "Create chat"),
    ("sessions", "zh-CN", "想重新聊一个话题，在哪新开对话？", "Create chat"),
    (
        "sessions",
        "en",
        "How do I find a conversation I closed yesterday?",
        "Closed session history",
    ),
    ("sessions", "zh-CN", "昨天关掉的聊天记录去哪里找？", "Closed session history"),
    ("sessions", "en", "How do I give this conversation a different name?", "Rename session"),
    ("sessions", "zh-CN", "怎么给当前对话改名字？", "Rename session"),
    ("sessions", "en", "Where can I put related conversations in one folder?", "Session folders"),
    ("sessions", "zh-CN", "怎么把相关的几个对话放进一个文件夹？", "Session folders"),
    (
        "sessions",
        "en",
        "How do I hide a conversation without deleting its history?",
        "Close session",
    ),
    ("sessions", "zh-CN", "不删历史记录，只想把这个对话收起来，怎么操作？", "Close session"),
    ("composer", "en", "How do I make the assistant stop its current answer?", "Stop generation"),
    ("composer", "zh-CN", "它还在回答，在哪里让它停下来？", "Stop generation"),
    ("composer", "en", "Where do I attach a PDF to my message?", "Attach file"),
    ("composer", "zh-CN", "在哪里把PDF发给助手看？", "Attach file"),
    (
        "composer",
        "en",
        "How do I choose a different AI model for this conversation?",
        "Session model picker",
    ),
    ("composer", "zh-CN", "这次聊天想换一个模型，在哪选？", "Session model picker"),
    ("composer", "en", "Where can I dictate a message instead of typing?", "Voice input"),
    ("composer", "zh-CN", "我想用语音输入，不想打字，按钮在哪？", "Voice input"),
    ("composer", "en", "Where can I see how much of the context window is used?", "Context usage"),
    ("composer", "zh-CN", "在哪看这次对话用了多少上下文？", "Context usage"),
    (
        "crewmates",
        "en",
        "How do I create my own assistant with a separate memory?",
        "Create crewmate",
    ),
    ("crewmates", "zh-CN", "想建一个有独立记忆的助手，去哪里？", "Create crewmate"),
    (
        "crewmates",
        "en",
        "Where can I change a crewmate's instructions?",
        "Edit crewmate instructions",
    ),
    ("crewmates", "zh-CN", "在哪里修改助手成员的指令？", "Edit crewmate instructions"),
    ("crewmates", "en", "How do I start a chat with a particular crewmate?", "Chat with crewmate"),
    ("crewmates", "zh-CN", "怎么单独和某个助手成员聊天？", "Chat with crewmate"),
    ("crewmates", "en", "Where can I look at what a crewmate remembers?", "Crewmate memory"),
    ("crewmates", "zh-CN", "在哪看这个助手记住了什么？", "Crewmate memory"),
    ("crewmates", "en", "How do I change a crewmate's picture?", "Crewmate avatar"),
    ("crewmates", "zh-CN", "怎么换助手成员的头像？", "Crewmate avatar"),
    ("schedule", "en", "Where do I set up a reminder for tomorrow?", "Create scheduled job"),
    ("schedule", "zh-CN", "想设置明天的提醒，在哪里添加？", "Create scheduled job"),
    ("schedule", "en", "How do I temporarily stop a recurring job?", "Pause scheduled job"),
    ("schedule", "zh-CN", "怎么暂停一个定时任务，以后再继续？", "Pause scheduled job"),
    (
        "schedule",
        "en",
        "Where can I see whether yesterday's scheduled task failed?",
        "Job run history",
    ),
    ("schedule", "zh-CN", "昨天定时任务有没有失败，在哪里看？", "Job run history"),
    ("schedule", "en", "How do I change the time a scheduled task runs?", "Edit scheduled job"),
    ("schedule", "zh-CN", "在哪修改定时任务的执行时间？", "Edit scheduled job"),
    ("schedule", "en", "How do I run a scheduled job immediately for a test?", "Run job now"),
    ("schedule", "zh-CN", "不等到预定时间，怎样马上试跑一次任务？", "Run job now"),
    (
        "artifacts",
        "en",
        "Where can I find documents the assistant saved for me?",
        "Artifact library",
    ),
    ("artifacts", "zh-CN", "助手保存的文档去哪里找？", "Artifact library"),
    (
        "artifacts",
        "en",
        "How do I bring an existing file into the artifact library?",
        "Import artifact",
    ),
    ("artifacts", "zh-CN", "怎么把已有文件导入作品库？", "Import artifact"),
    (
        "artifacts",
        "en",
        "Where do I restore an earlier version of an artifact?",
        "Artifact versions",
    ),
    ("artifacts", "zh-CN", "作品改坏了，在哪里恢复以前的版本？", "Artifact versions"),
    ("artifacts", "en", "How do I leave feedback on a saved document?", "Artifact comments"),
    ("artifacts", "zh-CN", "在哪里给保存的文档写评论？", "Artifact comments"),
    ("artifacts", "en", "Where can I find only the artifacts I bookmarked?", "Starred artifacts"),
    ("artifacts", "zh-CN", "我收藏的作品在哪里集中查看？", "Starred artifacts"),
    ("apps", "en", "Where can I browse extra apps I can install?", "App store"),
    ("apps", "zh-CN", "想安装更多应用，在哪里浏览？", "App store"),
    ("apps", "en", "How do I get a newer version of an installed app?", "Update app"),
    ("apps", "zh-CN", "已安装的应用怎么升级到新版？", "Update app"),
    ("apps", "en", "How do I turn an app off but keep it installed?", "Disable app"),
    ("apps", "zh-CN", "不卸载，只想暂时停用应用，在哪操作？", "Disable app"),
    ("apps", "en", "Where can I change an app's configuration?", "App settings"),
    ("apps", "zh-CN", "在哪里修改应用的配置？", "App settings"),
    ("apps", "en", "How do I remove an app I no longer need?", "Uninstall app"),
    ("apps", "zh-CN", "不再需要的应用在哪里卸载？", "Uninstall app"),
    ("connections", "en", "Where do I connect this assistant to Slack?", "Slack connection"),
    ("connections", "zh-CN", "在哪里把助手连到Slack？", "Slack connection"),
    ("connections", "en", "How do I add an MCP server for extra tools?", "Add MCP server"),
    ("connections", "zh-CN", "想添加一个MCP服务器来提供工具，入口在哪？", "Add MCP server"),
    ("connections", "en", "Where do I check if my MCP server is connected?", "MCP server status"),
    ("connections", "zh-CN", "在哪里检查MCP服务器有没有连接成功？", "MCP server status"),
    ("connections", "en", "Where do I sign in to my model provider?", "Provider authentication"),
    ("connections", "zh-CN", "在哪里登录模型提供商的账号？", "Provider authentication"),
    (
        "connections",
        "en",
        "How do I connect Telegram instead of using the dashboard?",
        "Telegram connection",
    ),
    ("connections", "zh-CN", "想在Telegram里用助手，在哪里连接？", "Telegram connection"),
    ("settings", "en", "Where can I make the interface Chinese?", "UI language"),
    ("settings", "zh-CN", "怎么把界面语言改成中文？", "UI language"),
    ("settings", "en", "How do I switch the dashboard to dark mode?", "Theme"),
    ("settings", "zh-CN", "深色模式在哪里开启？", "Theme"),
    ("settings", "en", "Where can I make the assistant's answers shorter?", "Response verbosity"),
    ("settings", "zh-CN", "在哪里让助手回答得简短一点？", "Response verbosity"),
    ("settings", "en", "How do I install a new skill?", "Install skill"),
    ("settings", "zh-CN", "在哪里安装新的技能？", "Install skill"),
    (
        "settings",
        "en",
        "Where can I control whether the assistant asks before running commands?",
        "Approval mode",
    ),
    ("settings", "zh-CN", "执行命令前要不要问我，在哪里设置？", "Approval mode"),
    ("notifications", "en", "Where can I see notifications I missed?", "Notification center"),
    ("notifications", "zh-CN", "错过的通知在哪里查看？", "Notification center"),
    ("notifications", "en", "How do I turn off notification sounds?", "Notification sound"),
    ("notifications", "zh-CN", "通知声音在哪里关掉？", "Notification sound"),
    (
        "notifications",
        "en",
        "How do I clear the unread notification badge?",
        "Mark notifications read",
    ),
    ("notifications", "zh-CN", "怎么把通知全部标成已读？", "Mark notifications read"),
    ("notifications", "en", "Where can I choose which events notify me?", "Notification sources"),
    ("notifications", "zh-CN", "哪些事情会发通知，在哪里选择？", "Notification sources"),
    (
        "notifications",
        "en",
        "How do I get desktop alerts when a job finishes?",
        "Desktop notifications",
    ),
    ("notifications", "zh-CN", "任务完成时弹桌面通知，在哪里打开？", "Desktop notifications"),
    ("shell", "en", "Where can I search across the whole dashboard?", "Search everywhere"),
    ("shell", "zh-CN", "想搜索整个仪表盘，搜索框在哪？", "Search everywhere"),
    ("shell", "en", "How do I open the terminal inside the dashboard?", "Terminal panel"),
    ("shell", "zh-CN", "仪表盘里的终端在哪里打开？", "Terminal panel"),
    ("shell", "en", "Where can I see the built-in browser?", "Browser panel"),
    ("shell", "zh-CN", "内置浏览器在哪里？", "Browser panel"),
    ("shell", "en", "How do I use this dashboard on my phone?", "Connect phone"),
    ("shell", "zh-CN", "想在手机上访问这个仪表盘，怎么办？", "Connect phone"),
    ("shell", "en", "Where do I report something that is broken?", "Report problem"),
    ("shell", "zh-CN", "发现问题去哪里反馈？", "Report problem"),
    ("negative", "en", "Where do I order lunch?", "Out of scope"),
    ("negative", "zh-CN", "在哪里订午饭？", "Out of scope"),
    ("negative", "en", "How do I freeze my bank card?", "Out of scope"),
    ("negative", "zh-CN", "银行卡在哪里挂失？", "Out of scope"),
    ("negative", "en", "Where can I reserve a flight seat?", "Out of scope"),
    ("negative", "zh-CN", "机票在哪里选座？", "Out of scope"),
    ("negative", "en", "Where do I install a washing machine?", "Out of scope"),
    ("negative", "zh-CN", "洗衣机怎么安装？", "Out of scope"),
    ("negative", "en", "How do I track my parcel delivery?", "Out of scope"),
    ("negative", "zh-CN", "在哪里看快递送到哪了？", "Out of scope"),
]

#: Intended task -> the indexed location that does it. Every task of the
#: corpus is either here or in COVERAGE_GAPS.
TASK_LOCATION: dict[str, str] = {
    "Create chat": "chat.new-session",
    "Closed session history": "chat.older-sessions",
    "Session folders": "sessions.create-menu.new-folder",
    "Close session": "sessions.row-close",
    "Stop generation": "composer.stop",
    "Attach file": "composer.attach-files",
    "Session model picker": "chat.model-picker",
    "Context usage": "composer.context-usage",
    "Create crewmate": "members.new",
    "Edit crewmate instructions": "members.edit",
    "Crewmate avatar": "agents.edit-avatar",
    "Create scheduled job": "schedule.add-job",
    "Run job now": "schedule.run-now",
    "Artifact library": "page.artifacts",
    "Import artifact": "artifacts.import",
    "Starred artifacts": "artifacts.starred",
    "App store": "page.apps",
    "Update app": "apps.detail.update",
    "Disable app": "apps.library.tile-disable",
    "Uninstall app": "apps.detail.uninstall",
    "Slack connection": "settings.sub.channels.slack",
    "Add MCP server": "mcp.add-server",
    "MCP server status": "connections.mcp-servers-tab",
    "Telegram connection": "settings.sub.channels.telegram",
    "UI language": "setting:display.language",
    "Theme": "setting:display.mode",
    "Response verbosity": "setting:chat.response-verbosity",
    "Approval mode": "settings.sub.security.approval",
    "Notification center": "page.notifications",
    "Notification sound": "setting:notifications.play-sound-on-new-notifications",
    "Mark notifications read": "notifications.mark-all-read",
    "Notification sources": "settings.sub.notifications.sources",
    "Desktop notifications": "settings.sub.notifications.alerts",
    "Search everywhere": "shell.search",
    "Terminal panel": "shell.terminal",
    "Connect phone": "shell.connect-phone",
    "Report problem": "shell.report-problem",
    # Closed coverage gaps (each a registered control, checked at its render site).
    "Rename session": "chat.session-title",
    "Voice input": "composer.voice",
    "Chat with crewmate": "agents.chat",
    "Crewmate memory": "agents.manage-memory",
    "Pause scheduled job": "schedule.pause",
    "Job run history": "schedule.view-executions",
    "Edit scheduled job": "schedule.edit-when",
    "Artifact versions": "artifacts.detail.versions",
    "Artifact comments": "artifacts.detail.comments",
    # Indexed, but in a RELATED area of the one the corpus names (the listing
    # names that area under `related`): Settings -> Customize, Connections ->
    # Settings (its Agent Harness tab holds the model sign-in), Shell -> Sessions (the
    # built-in browser is a view of a chat's side panel, not shell chrome).
    "Install skill": "skills.add",
    "Provider authentication": "settings.tab.agent",
    "Browser panel": "chat.side-panel.browser",
}

#: The TASK_LOCATION entries reached only through a related area, and which one.
RELATED_ONLY: dict[str, str] = {
    "Install skill": "customize",
    "Provider authentication": "settings",
    "Browser panel": "sessions",
}

#: Tasks with no indexed location anywhere: real coverage gaps, not recall.
#: "App settings": an app's detail page has no configuration control, only a
#: read-only Configuration card of the manifest's own guidance
#: (AppDetailPage); an app's settings live inside that app's own page.
COVERAGE_GAPS: frozenset[str] = frozenset({"App settings"})
_NEGATIVE = "negative"


def _tasks() -> dict[str, str]:
    """Intended task -> the corpus area its rows name."""
    return {task: area for area, _lang, _q, task in CORPUS if area != _NEGATIVE}


def _listing(area: str, lang: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Every page of one area's listing: (entries in order, raw pages)."""
    entries: list[dict[str, Any]] = []
    pages: list[dict[str, Any]] = []
    offset = 0
    while True:
        d = ui_index.browse_ui(area, lang, offset)
        assert d["status"] == "ok", d
        pages.append(d)
        entries += d["entries"]
        if not d["truncated"]:
            return entries, pages
        assert d["next_offset"] > offset, "a page must make progress"
        offset = d["next_offset"]


def _reachable(area: str, lang: str) -> set[str]:
    """Ids an area's listing reaches: its pages, its sub-areas' pages, its related areas'."""
    entries, pages = _listing(area, lang)
    ids = {e["id"] for e in entries}
    for sub in pages[0].get("sub_areas", []):
        ids |= {e["id"] for e in _listing(sub["area"], lang)[0]}
    return ids


# ------------------------------------------------------------ corpus coverage


def test_every_corpus_task_is_classified() -> None:
    tasks = set(_tasks())
    assert tasks == set(TASK_LOCATION) | COVERAGE_GAPS, tasks ^ (set(TASK_LOCATION) | COVERAGE_GAPS)
    assert not set(TASK_LOCATION) & COVERAGE_GAPS


@pytest.mark.parametrize("task", sorted(TASK_LOCATION))
@pytest.mark.parametrize("lang", ["en", "zh-CN"])
def test_each_indexed_task_is_in_its_corpus_area_listing(task: str, lang: str) -> None:
    """recall@area: the location is in the listing of the area the row names,
    or (for the named RELATED_ONLY exceptions) of the area that listing names
    as related."""
    area = _tasks()[task]
    target = TASK_LOCATION[task]
    reach = _reachable(area, lang)
    if task in RELATED_ONLY:
        via = RELATED_ONLY[task]
        assert target not in reach, (task, "now listed directly: drop it from RELATED_ONLY")
        assert via in ui_index.browse_ui(area, lang).get("related", []), (task, area, via)
        assert target in _reachable(via, lang), (task, via)
    else:
        assert target in reach, (task, area)


@pytest.mark.parametrize(
    ("query", "lang"), [(q, lang) for a, lang, q, _t in CORPUS if a == _NEGATIVE]
)
def test_out_of_scope_questions_never_hit(query: str, lang: str) -> None:
    assert ui_index.find_ui(query, lang)["status"] == "no_match"


# ------------------------------------------------------------ listing shape


def _all_areas() -> list[str]:
    return ui_index.area_ids(ui_index.load_index())


@pytest.mark.parametrize("lang", ["en", "zh-CN"])
def test_every_area_is_paged_within_the_cap_and_lists_every_member_once(lang: str) -> None:
    idx = ui_index.load_index()
    for area in _all_areas():
        entries, pages = _listing(area, lang)
        for page in pages:
            assert ui_index._size(page) <= ui_index.MAX_RESPONSE_BYTES, (area, page["offset"])
            assert page["total"] == pages[0]["total"]
        ids = [e["id"] for e in entries]
        assert len(ids) == len(set(ids)) == pages[0]["total"], area
        members = ui_index.area_members(idx, area)
        if area == "settings":
            held = {
                m for sub in pages[0]["sub_areas"] for m in ui_index.area_members(idx, sub["area"])
            }
            assert set(ids) == set(members) - held and held, area
            assert sum(sub["count"] for sub in pages[0]["sub_areas"]) >= len(held)
        else:
            assert set(ids) == set(members), area
        assert ids, f"area {area!r} lists nothing"


def test_a_long_area_is_paged_never_cut(monkeypatch: pytest.MonkeyPatch) -> None:
    whole = {e["id"] for e in _listing("settings.channels", "en")[0]}
    monkeypatch.setattr(ui_index, "MAX_RESPONSE_BYTES", 2500)
    entries, pages = _listing("settings.channels", "en")
    assert len(pages) >= 5
    assert [e["id"] for e in entries] and {e["id"] for e in entries} == whole
    assert all(p["truncated"] and "next_offset" in p for p in pages[:-1])
    assert not pages[-1]["truncated"] and "next_offset" not in pages[-1]
    assert all(ui_index._size(p) <= 2500 for p in pages)


def test_a_cap_too_small_for_one_entry_is_a_bounded_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ui_index, "MAX_RESPONSE_BYTES", 900)
    d = ui_index.browse_ui("sessions", "en")
    assert d["status"] == "unavailable" and d["entries"] == []


def test_sessions_in_english_and_chinese() -> None:
    en = ui_index.browse_ui("sessions", "en")
    zh = ui_index.browse_ui("sessions", "zh-CN")
    assert en["title"] == "Sessions" and zh["title"] == "会话"
    assert zh["prose_locale"] == "en" and "prose_locale" not in en
    assert en["related"] == ["composer"]
    by_id = {e["id"]: e for e in en["entries"]}
    older = by_id["chat.older-sessions"]
    assert older["path"] == "Sessions > Older Sessions" and older["tier"] == "curated"
    assert older["needs"] == [
        "desktop only",
        "after Show sessions sidebar if sessions_sidebar_collapsed",
    ]
    # A flipping label lists the label each state shows.
    assert by_id["sessions.list-menu.view"]["states"] == {
        "sessions_list_view": "Switch to board view",
        "sessions_board_view": "Switch to list view",
    }
    # Composer controls are their own area, not repeated here.
    assert not any(i.startswith("composer.") for i in by_id)
    zh_ids = {e["id"]: e for e in zh["entries"]}
    assert zh_ids["chat.older-sessions"]["path"].startswith("会话 > ")
    # A menu's items follow the menu.
    order = [e["id"] for e in en["entries"]]
    menu = order.index("sessions.create-menu")
    assert order[menu + 1 : menu + 4] == [
        "sessions.create-menu.incognito",
        "sessions.create-menu.new-folder",
        "sessions.create-menu.temporary",
    ]


def test_a_live_data_label_is_a_description_never_a_label() -> None:
    entries = {e["id"]: e for e in ui_index.browse_ui("composer", "en")["entries"]}
    chip = entries["chat.model-picker"]
    assert "label" not in chip and chip["description"].startswith("The model button")
    assert chip["path"] == "Sessions"


def test_settings_lists_its_tabs_as_sub_areas_in_the_users_language() -> None:
    d = ui_index.browse_ui("settings", "zh-CN")
    subs = {s["area"]: s for s in d["sub_areas"]}
    assert subs["settings.display"]["label"] == "显示" and subs["settings.chat"]["count"] > 30
    for sub in subs.values():
        assert sub["count"] == ui_index.browse_ui(sub["area"], "zh-CN")["total"], sub
    assert d["related"] == ["customize"]
    assert [e["id"] for e in d["entries"]] == ["page.settings"]
    chat = {e["id"]: e for e in _listing("settings.chat", "en")[0]}
    assert chat["setting:chat.response-verbosity"]["path"] == (
        "Settings > Chat > Transcript > Response Verbosity"
    )


def test_a_shell_control_is_on_every_page() -> None:
    entries = {e["id"]: e for e in ui_index.browse_ui("shell", "en")["entries"]}
    assert entries["shell.search"]["on_every_page"] is True
    assert entries["shell.search"]["path"] == "Search sessions, files, and commands"


def test_related_areas_and_area_titles_name_real_things() -> None:
    idx = ui_index.load_index()
    for name, area in ui_index.AREAS.items():
        assert set(area.related) | set(area.exclude) <= set(ui_index.AREAS), name
        assert area.title is None or area.title in idx.by_id, name
        assert all(a in idx.by_id for a in area.anchors), name
    # The model sign-in lives on Settings' Agent Harness tab, which Connections
    # names as related.
    assert ui_index._loc_label(idx, "settings.tab.agent", "en") == "Agent Harness"
    assert "settings" in ui_index.AREAS["connections"].related
    # The tool description lists the same vocabulary.
    assert list(mcp_guide._FIND_UI_AREAS) == list(ui_index.AREAS)


def _auto_file(tmp_path: Path) -> Path:
    """A build-time auto tier against the REAL committed index: two Sessions-page entries."""
    real = json.loads(ui_index.INDEX_PATH.read_text(encoding="utf-8"))
    labels = {"k.t.reindex": "Reindex sources", "k.t.back": "Back"}
    zh = {"k.t.reindex": "重新索引来源", "k.t.back": "返回"}
    auto = {
        "schema_version": 1,
        "artifact": "auto",
        "base_input_digest": real["input_digest"],
        "input_digest": "sha256:t",
        "coverage": {"scope": "test auto"},
        "locales": real["locales"],
        "locations": [
            {
                "id": f"auto:page.chat:{k}",
                "kind": "button",
                "tier": "auto",
                "conditions_unknown": True,
                "label_key": k,
                "placements": [
                    {
                        "surface_id": "chat",
                        "route": "/chat",
                        "parent_ids": ["page.chat"],
                        "entry_kind": "content",
                        "requires": [],
                    }
                ],
            }
            for k in labels
        ],
        "labels": {loc: (zh if loc == "zh-CN" else labels) for loc in real["locales"]},
    }
    out = tmp_path / "auto.json"
    out.write_text(json.dumps(auto), encoding="utf-8")
    return out


def test_auto_entries_come_last_marked_and_one_word_ones_are_counted_out(tmp_path: Path) -> None:
    auto = _auto_file(tmp_path)
    for lang, label in (("en", "Reindex sources"), ("zh-CN", "重新索引来源")):
        d = ui_index.browse_ui("sessions", lang, path=ui_index.INDEX_PATH, auto_path=auto)
        assert d["auto_tier"] == "available", d
        # The area can run past one page; the order holds across the pages.
        entries = list(d["entries"])
        while d.get("truncated"):
            d = ui_index.browse_ui(
                "sessions", lang, d["next_offset"], path=ui_index.INDEX_PATH, auto_path=auto
            )
            entries += d["entries"]
        tiers = [e["tier"] for e in entries]
        assert tiers[-1] == "auto" and "auto" not in tiers[:-1]
        last = entries[-1]
        assert last["id"] == "auto:page.chat:k.t.reindex" and last["label"] == label
        assert "needs" not in last
        # "Back" / 返回 is one word: never a find_ui answer, so not listed either.
        assert d["omitted_one_word_auto"] == 1
        assert all(e["id"] != "auto:page.chat:k.t.back" for e in d["entries"])


# ------------------------------------------------------------ inputs and wiring


@pytest.mark.parametrize("area", ["", "Sessions", "../settings", "settings/chat", "nope", "a.b.c"])
def test_an_area_must_be_a_known_area_id(area: str) -> None:
    d = ui_index.browse_ui(area, "en")
    if area == "nope":
        # A well-formed unknown id is told the vocabulary.
        assert "sessions" in d["error"] and "settings.chat" in d["error"]
    else:
        # A malformed one is refused by shape, never echoed back.
        assert d["error"].startswith("'area' must be an area id"), d


@pytest.mark.parametrize("offset", [-1, True, "3"])
def test_offset_must_be_a_nonnegative_integer(offset: Any) -> None:
    assert "error" in ui_index.browse_ui("sessions", "en", offset)


def test_an_offset_past_the_end_is_an_empty_last_page() -> None:
    d = ui_index.browse_ui("sessions", "en", 10_000)
    assert d["entries"] == [] and d["truncated"] is False


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"query": "older sessions", "area": "sessions"},
        {"area": "sessions", "surface": "chat"},
        {"query": "older sessions", "offset": 3},
    ],
)
def test_the_tool_takes_a_query_or_an_area(args: dict[str, Any]) -> None:
    assert mcp_guide._call_tool_inner("find_ui", args).startswith("Error:")


@pytest.mark.parametrize("area", ["../x", "/settings", "Settings", "a" * 90])
def test_the_schema_refuses_a_path_shaped_area(area: str) -> None:
    with pytest.raises(ValidationError):
        mcp_guide._validate_args("find_ui", {"area": area})


def test_browsing_needs_no_session_gateway_or_config(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("browsing must not need a session, the gateway or config")

    # No verifiable identity: the dashboard-language ask is skipped (never
    # made), and the listing stands in the question's language.
    monkeypatch.setattr(mcp_guide, "_strict_session_key", lambda: ("", "Error: no identity"))
    monkeypatch.setattr(mcp_guide, "_get", boom)
    monkeypatch.setattr(mcp_guide, "_post", boom)
    import kiro_crew.config.loader as loader

    monkeypatch.setattr(loader, "load_config", boom, raising=False)
    read: list[Path] = []
    real_read = Path.read_text

    def spy(self: Path, *a: Any, **k: Any) -> str:
        read.append(self.resolve())
        return real_read(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", spy)
    out = json.loads(
        mcp_guide._call_tool_inner(
            "find_ui", {"area": "settings.chat", "lang": "zh-CN", "offset": 0}
        )
    )
    assert out["area"] == "settings.chat" and out["entries"]
    assert read and all(p.parent == ui_index.INDEX_PATH.parent for p in read), read
