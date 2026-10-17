"""Held-out, cross-area ``find_ui`` goldens: real beginner wording.

The per-area files pin what each area's descriptors say. This file is the
other half: questions written the way a newcomer asks them (wave-3 review),
answered against the whole committed index, so a term added in one area that
steals another area's question shows up here. Each positive has a contrastive
or negative partner; the unrelated negatives must stay ``no_match`` however
the vocabulary grows (the global coverage threshold is not lowered for recall).
"""

from __future__ import annotations

import json
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


def _ids(d: dict[str, Any]) -> list[str]:
    return [r["id"] for r in d.get("results", [])]


# --------------------------------------------------------------------- recall

#: Clear intents that must not be no_match, EN and zh-CN, with the one place each means.
HELD_OUT: list[tuple[str, str, str]] = [
    ("How do I copy this conversation?", "en", "sessions.row-duplicate"),
    ("How do I organize chats into folders?", "en", "sessions.create-menu.new-folder"),
    ("Where can I show chats as a board?", "en", "sessions.list-menu.view"),
    ("How do I stop it talking?", "en", "composer.stop"),
    ("Where can I check my alerts?", "en", "shell.notifications"),
    ("How do I publish a public link?", "en", "artifacts.deploy"),
    ("How do I change the model for all chats?", "en", "sessions.list-menu.switch-model"),
    ("How do I update an app?", "en", "apps.detail.update"),
    ("怎么复制这个会话", "zh-CN", "sessions.row-duplicate"),
    ("怎么给聊天建文件夹", "zh-CN", "sessions.create-menu.new-folder"),
    ("怎么切换看板", "zh-CN", "sessions.list-menu.view"),
    ("怎么改代理的头像", "zh-CN", "agents.edit-avatar"),
    ("怎么更新应用", "zh-CN", "apps.detail.update"),
    ("怎么关闭这些通知", "zh-CN", "setting:notifications.sources"),
    ("收藏的文档在哪", "zh-CN", "artifacts.starred"),
    # Already answered before; kept so a later term cannot steal them.
    ("close a chat", "en", "sessions.row-close"),
    ("take a screenshot", "en", "composer.add-menu.screenshot"),
    ("how much context is left", "en", "composer.context-usage"),
    ("delete an agent", "en", "agents.delete"),
    ("disable an app", "en", "apps.library.tile-disable"),
    ("uninstall an app", "en", "apps.library.tile-uninstall"),
    ("command line", "en", "shell.terminal"),
    ("connect my phone", "en", "shell.connect-phone"),
    ("report a bug", "en", "shell.report-problem"),
    ("delete a reminder", "en", "schedule.delete"),
    ("run a task now", "en", "schedule.run-now"),
    ("upload a document", "en", "artifacts.import"),
    ("install a skill", "en", "skills.add"),
    # A live run: the exact query Mate sent, then the user's own wording.
    ("stop button while agent is answering", "en", "composer.stop"),
    ("I want to stop it while it is answering", "en", "composer.stop"),
    ("stop the reply", "en", "composer.stop"),
    ("stop generating", "en", "composer.stop"),
    ("停止回复", "zh-CN", "composer.stop"),
    ("让它别说了", "zh-CN", "composer.stop"),
    ("How do I change the model for all my chats?", "en", "sessions.list-menu.switch-model"),
]


@pytest.mark.parametrize(("query", "lang", "target"), HELD_OUT)
def test_held_out_beginner_wording_reaches_its_place(query: str, lang: str, target: str) -> None:
    d = ui_index.find_ui(query, lang)
    assert d["status"] == "ok", d
    assert _ids(d)[0] == target, _ids(d)
    assert d["ambiguous"] is False, _ids(d)


def test_sending_a_picture_names_the_desktop_menu_row_and_the_touch_picker() -> None:
    d = ui_index.find_ui("How do I send a picture?", "en")
    assert set(_ids(d)[:2]) == {"composer.add-menu.upload", "composer.attach-files"}


# ------------------------------------------------------------ contrastive pairs


@pytest.mark.parametrize(
    ("query", "lang", "target", "not_first"),
    [
        # Update installs a newer release; Sync only reloads the installed files.
        ("update an app", "en", "apps.detail.update", "apps.detail.sync"),
        ("sync an app", "en", "apps.detail.sync", "apps.detail.update"),
        ("更新应用", "zh-CN", "apps.detail.update", "apps.detail.sync"),
        ("同步应用", "zh-CN", "apps.detail.sync", "apps.detail.update"),
        # "all chats" is the bulk item's; "this chat" is the chip's.
        (
            "change the model for all chats",
            "en",
            "sessions.list-menu.switch-model",
            "chat.model-picker",
        ),
        (
            "change the model for this chat",
            "en",
            "chat.model-picker",
            "sessions.list-menu.switch-model",
        ),
        # Copy a chat (row action) is not copy a message or a file.
        ("copy a conversation", "en", "sessions.row-duplicate", "composer.add-menu.upload"),
    ],
)
def test_contrastive_pairs_keep_their_own_answer(
    query: str, lang: str, target: str, not_first: str
) -> None:
    ids = _ids(ui_index.find_ui(query, lang))
    assert ids and ids[0] == target and ids[0] != not_first, ids


def test_one_shared_content_word_of_a_term_is_no_answer() -> None:
    """A demand-corpus confident-wrong: "app settings" came back as Discover >
    Install, because the term "get an app" is the one word {app} once question
    words go, and so is the question. Such a term answers only when said whole."""
    for query in ("app settings", "Where are the app settings?", "app options"):
        d = ui_index.find_ui(query, "en")
        assert "apps.detail.install" not in _ids(d), (query, _ids(d))
    # Its contrastive partner: the term said whole still finds Install.
    for query in ("get an app", "where do I get an app"):
        assert _ids(ui_index.find_ui(query, "en"))[:1] == ["apps.detail.install"], query


# ------------------------------------------------------------ vague and unrelated


@pytest.mark.parametrize(
    ("query", "lang"),
    [
        ("add", "en"),
        ("delete", "en"),
        ("remove", "en"),
        ("update", "en"),
        ("Delete it", "en"),
        ("where is the delete button", "en"),
        ("how do I add", "en"),
        ("添加", "zh-CN"),
        ("删除", "zh-CN"),
        ("怎么删除", "zh-CN"),
        ("更新", "zh-CN"),
        # A control noun is not an object when several controls start with the verb.
        ("delete button", "en"),
        ("where is the add button", "en"),
        ("删除按钮", "zh-CN"),
    ],
)
def test_a_bare_action_verb_asks_what_to_act_on(query: str, lang: str) -> None:
    d = ui_index.find_ui(query, lang)
    assert d["status"] == "no_match" and d["results"] == [], d
    assert d["needs_object"].startswith("the question names an action but not what it acts on")


@pytest.mark.parametrize("query", ["import button", "where is the import button", "import icon"])
def test_a_control_noun_answers_a_verb_only_one_control_starts_with(query: str) -> None:
    """Import is the only control labelled with "import", so "import button"
    names it; bare "import" still asks (pinned above)."""
    d = ui_index.find_ui(query, "en")
    assert d["status"] == "ok" and "needs_object" not in d, d
    assert _ids(d) == ["artifacts.import"] and d["ambiguous"] is False, d
    assert d["results"][0]["match"] == "verb_control"


@pytest.mark.parametrize(("query", "lang"), [("stop button", "en"), ("停止按钮", "zh-CN")])
def test_a_control_noun_asks_when_two_controls_start_with_the_verb(query: str, lang: str) -> None:
    """Stop generation and Stop monitor both start with "stop": the noun
    names neither, so the answer asks what to stop."""
    d = ui_index.find_ui(query, lang)
    assert d["status"] == "no_match" and "needs_object" in d, d


def test_a_control_noun_never_lifts_a_bare_verb_without_it() -> None:
    for query in ("stop", "how do I stop"):
        d = ui_index.find_ui(query, "en")
        assert d["status"] == "no_match" and "needs_object" in d, d


@pytest.mark.parametrize(
    ("query", "lang", "target"),
    [
        ("add an agent", "en", "agents.add"),
        ("delete a reminder", "en", "schedule.delete"),
        ("删除定时任务", "zh-CN", "schedule.delete"),
        ("添加附件", "zh-CN", "composer.add-menu"),
    ],
)
def test_the_same_verb_with_an_object_still_answers(query: str, lang: str, target: str) -> None:
    d = ui_index.find_ui(query, lang)
    assert d["status"] == "ok" and "needs_object" not in d, d
    assert target in _ids(d)[:2], _ids(d)


@pytest.mark.parametrize(
    ("query", "lang"),
    [
        ("help", "en"),
        ("stop", "en"),
        ("帮助", "zh-CN"),
        ("停止", "zh-CN"),
        ("where are my keys", "en"),
        ("chat about dinner", "en"),
        ("how do I uninstall my refrigerator", "en"),
        ("我的钥匙在哪", "zh-CN"),
        ("晚饭吃什么", "zh-CN"),
        ("xqzv blorp flim", "en"),
        ("zzzz", "zh-CN"),
    ],
)
def test_unrelated_and_vague_questions_stay_no_match(query: str, lang: str) -> None:
    assert ui_index.find_ui(query, lang)["status"] == "no_match", query


# ------------------------------------------------------------ state-flipping labels


@pytest.mark.parametrize(
    ("query", "lang", "loc_id", "label", "states"),
    [
        (
            "switch to list view",
            "en",
            "sessions.list-menu.view",
            "Switch to list view",
            {
                "Switch to board view": "sessions_list_view",
                "Switch to list view": "sessions_board_view",
            },
        ),
        (
            "Where can I show chats as a board?",
            "en",
            "sessions.list-menu.view",
            "Switch to board view",
            {
                "Switch to board view": "sessions_list_view",
                "Switch to list view": "sessions_board_view",
            },
        ),
        (
            "collapse the navigation",
            "en",
            "shell.nav-toggle",
            "Collapse sidebar",
            {"Expand sidebar": "nav_rail_collapsed", "Collapse sidebar": "nav_rail_expanded"},
        ),
        (
            "expand the navigation",
            "en",
            "shell.nav-toggle",
            "Expand sidebar",
            {"Expand sidebar": "nav_rail_collapsed", "Collapse sidebar": "nav_rail_expanded"},
        ),
        (
            "切换看板",
            "zh-CN",
            "sessions.list-menu.view",
            "切换到看板视图",
            {"切换到看板视图": "sessions_list_view", "切换到列表视图": "sessions_board_view"},
        ),
    ],
)
def test_a_flipping_label_answers_with_the_state_the_question_is_in(
    query: str, lang: str, loc_id: str, label: str, states: dict[str, str]
) -> None:
    rec = ui_index.find_ui(query, lang)["results"][0]
    assert rec["id"] == loc_id
    # The label quoted is the one on screen in the state the question implies…
    assert rec["label"] == label
    assert rec["placements"][0]["path"][-1]["label"] == label
    # …and every state's label is said, with the state it is shown in.
    assert {s["label"]: s["when"] for s in rec["label_by_state"]} == states
    assert all(s["only_if"] for s in rec["label_by_state"])


def test_a_location_with_one_label_carries_no_state_labels() -> None:
    rec = ui_index.find_ui("close a chat", "en")["results"][0]
    assert "label_by_state" not in rec


def test_malformed_state_labels_make_the_index_unavailable(tmp_path: Path) -> None:
    real = json.loads(ui_index.INDEX_PATH.read_text(encoding="utf-8"))
    loc = next(x for x in real["locations"] if x["id"] == "sessions.list-menu.view")
    loc["state_labels"][1]["label_key"] = "pages.chatSidebar.not_this_elements_label"
    path = tmp_path / "ui-index.generated.json"
    path.write_text(json.dumps(real), encoding="utf-8")
    assert ui_index.find_ui("kanban", "en", path=path)["status"] == "unavailable"


# ------------------------------------------------------------ prose locale relay


def test_a_zh_answer_says_its_prerequisite_prose_is_english_to_translate() -> None:
    d = ui_index.find_ui("批准密钥", "zh-CN")
    rec = d["results"][0]
    assert rec["id"] == "schedule.secret-approve"
    # Labels are Chinese; the prose is the English table, and the answer says
    # so, naming the fields the caller must translate when relaying.
    assert rec["label"] == "批准"
    assert d["prose_locale"] == "en"
    for field in ("description", "only_if", "otherwise"):
        assert field in d["prose_note"]
    tab = next(r for r in rec["placements"][0]["requires"] if r.get("id") == "job_details_tab")
    assert tab["description"].startswith("the job panel's Details tab is selected")


def test_an_english_answer_carries_no_prose_note() -> None:
    d = ui_index.find_ui("approve a secret for a job", "en")
    assert "prose_locale" not in d and "prose_note" not in d


# ------------------------------------------------------------ response size


#: Questions that fan out across areas at the current size.
HIGH_FANOUT = [
    "sessions",
    "new folder",
    "skills",
    "model",
    "attach a file",
    "notifications",
    "通知",
    "新建文件夹",
]


@pytest.mark.parametrize("query", HIGH_FANOUT)
@pytest.mark.parametrize("lang", ["en", "zh-CN"])
def test_high_fanout_answers_fit_the_6_kib_cap_whole(query: str, lang: str) -> None:
    d = ui_index.find_ui(query, lang)
    assert ui_index._size(d) <= ui_index.MAX_RESPONSE_BYTES, (query, ui_index._size(d))
    if d["status"] == "ok":
        assert len(d["results"]) <= ui_index.MAX_RESULTS


def test_the_cap_drops_whole_records_from_a_real_high_fanout_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    full = ui_index.find_ui("new folder", "en")
    assert full["truncated"] is False and len(full["results"]) >= 3
    cap = ui_index._size({**full, "results": full["results"][:2]}) + 10
    monkeypatch.setattr(ui_index, "MAX_RESPONSE_BYTES", cap)
    cut = ui_index.find_ui("new folder", "en")
    assert cut["truncated"] is True
    assert [r["id"] for r in cut["results"]] == [r["id"] for r in full["results"][:2]]
    assert ui_index._size(cut) <= cap
