"""``find_ui`` goldens for the shell rail rows, the bell and the notification feed.

Wave-3 batch 3 (areas ``shell`` and ``notifications``). Reads the committed
(packaged) index.
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


def _by_id(d: dict[str, Any]) -> dict[str, dict[str, Any]]:
    assert d["status"] == "ok", d
    return {r["id"]: r for r in d["results"]}


def _path(p: dict[str, Any]) -> list[str]:
    return [seg["label"] for seg in p["path"]]


def _ids(p: dict[str, Any]) -> list[str]:
    return [seg["id"] for seg in p["path"]]


def _req(p: dict[str, Any]) -> list[str]:
    return [r.get("id") or r.get("value") for r in p["requires"]]


# Novice phrasings: (query, locale, location id that must be among the results).
PHRASINGS = [
    ("where are my notifications", "en", "shell.notifications"),
    ("notification bell", "en", "shell.notifications"),
    ("通知在哪", "zh-CN", "shell.notifications"),
    ("铃铛", "zh-CN", "shell.notifications"),
    ("open the inbox", "en", "shell.notifications.open-inbox"),
    ("打开收件箱", "zh-CN", "shell.notifications.open-inbox"),
    ("mark everything read", "en", "notifications.mark-all-read"),
    ("mark everything read", "en", "notifications.page-mark-all-read"),
    ("全部标为已读", "zh-CN", "notifications.mark-all-read"),
    ("全部标为已读", "zh-CN", "notifications.page-mark-all-read"),
    ("clear all notifications", "en", "notifications.clear-all"),
    ("delete all notifications", "en", "notifications.page-clear-all"),
    ("清空通知", "zh-CN", "notifications.clear-all"),
    ("清空通知", "zh-CN", "notifications.page-clear-all"),
    ("mark a notification unread", "en", "notifications.detail.mark-unread"),
    ("标为未读", "zh-CN", "notifications.detail.mark-unread"),
    ("mute an app", "en", "notifications.mute-channel"),
    ("屏蔽渠道", "zh-CN", "notifications.mute-channel"),
    ("expand the navigation", "en", "shell.nav-toggle"),
    ("展开导航栏", "zh-CN", "shell.nav-toggle"),
    ("open the terminal", "en", "shell.terminal"),
    ("打开终端", "zh-CN", "shell.terminal"),
    ("developer settings page", "en", "shell.developer"),
    ("开发者页面", "zh-CN", "shell.developer"),
    ("connect my phone", "en", "shell.connect-phone"),
    ("连接手机", "zh-CN", "shell.connect-phone"),
    ("kiro account balance", "en", "shell.kiro-account"),
    ("账户余额", "zh-CN", "shell.kiro-account"),
    ("report a bug", "en", "shell.report-problem"),
    ("报告问题", "zh-CN", "shell.report-problem"),
    ("focus mode", "en", "shell.focus-mode"),
    ("hide distractions", "en", "shell.focus-mode"),
    ("专注模式", "zh-CN", "shell.focus-mode"),
]


@pytest.mark.parametrize(("query", "lang", "target"), PHRASINGS)
def test_novice_phrasing_finds_the_control(query: str, lang: str, target: str) -> None:
    assert target in _by_id(ui_index.find_ui(query, lang)), query


@pytest.mark.parametrize(
    ("query", "lang", "top"),
    [
        ("where are my notifications", "en", "shell.notifications"),
        ("open the terminal", "en", "shell.terminal"),
        ("connect my phone", "en", "shell.connect-phone"),
        ("report a bug", "en", "shell.report-problem"),
        ("hide distractions", "en", "shell.focus-mode"),
        ("mark a notification unread", "en", "notifications.detail.mark-unread"),
        ("打开终端", "zh-CN", "shell.terminal"),
        ("开发者页面", "zh-CN", "shell.developer"),
        ("专注模式", "zh-CN", "shell.focus-mode"),
    ],
)
def test_the_clear_phrasings_rank_the_control_first(query: str, lang: str, top: str) -> None:
    d = ui_index.find_ui(query, lang)
    assert d["status"] == "ok" and d["results"][0]["id"] == top, d


@pytest.mark.parametrize(
    ("query", "lang"),
    [
        ("notify my boss", "en"),
        ("report my taxes", "en"),
        ("where are my keys", "en"),
        ("chat about dinner", "en"),
        ("给老板发通知", "zh-CN"),
    ],
)
def test_unrelated_phrasings_are_no_match(query: str, lang: str) -> None:
    assert ui_index.find_ui(query, lang)["status"] == "no_match", query


# ── exact placements ──


def _loc(query: str, target: str, lang: str = "en") -> dict[str, Any]:
    return _by_id(ui_index.find_ui(query, lang))[target]


@pytest.mark.parametrize(
    ("query", "target", "label", "gate"),
    [
        ("open the terminal", "shell.terminal", "Terminal", "terminal_enabled"),
        ("developer tools", "shell.developer", "Developer", "developer_mode"),
        (
            "connect my phone",
            "shell.connect-phone",
            "Connect your phone",
            "phone_connect_available",
        ),
    ],
)
def test_a_rail_row_is_the_desktop_rail_or_the_phone_menu(
    query: str, target: str, label: str, gate: str
) -> None:
    loc = _loc(query, target)
    assert loc["label"] == label
    desktop, phone = loc["placements"]
    assert desktop["on_every_page"] is True and "route" not in desktop
    assert desktop["entry"] == "rail"
    assert _path(desktop) == [label]
    assert _req(desktop) == ["desktop", gate]
    assert phone["entry"] == "menu"
    assert _ids(phone) == ["shell.mobile-menu", target]
    assert _path(phone) == ["Open menu", label]
    assert _req(phone) == ["mobile", "not_on_sessions_page", gate]


def test_the_kiro_account_row_is_phone_menu_only() -> None:
    loc = _loc("kiro account balance", "shell.kiro-account")
    (p,) = loc["placements"]
    assert _path(p) == ["Open menu", "Kiro Account"]
    assert p["entry"] == "menu"
    assert _req(p) == ["mobile", "not_on_sessions_page", "kiro_account_entry"]
    zh = _loc("账户余额", "shell.kiro-account", "zh-CN")
    assert _path(zh["placements"][0]) == ["打开菜单", "Kiro 账户"]


def test_report_problem_quotes_the_visible_link_text() -> None:
    loc = _loc("report a bug", "shell.report-problem")
    assert loc["label"] == "Report issue"
    rail, menu = loc["placements"]
    # Desktop: the rail row, behind the rail toggle while the rail is collapsed.
    assert _path(rail) == ["Report issue"] and rail["entry"] == "rail"
    assert rail["requires"][0] == {"kind": "viewport", "value": "desktop"}
    assert rail["requires"][1]["kind"] == "shown_by"
    assert rail["requires"][1]["path"] == ["Expand sidebar"]
    assert rail["requires"][1]["when"] == "nav_rail_collapsed"
    assert _path(menu) == ["Open menu", "Report issue"]
    assert _req(menu) == ["mobile", "not_on_sessions_page"]
    zh = _loc("报告问题", "shell.report-problem", "zh-CN")
    assert zh["label"] == "反馈问题"


def test_mute_channel_is_one_site_in_both_notification_hosts() -> None:
    loc = _loc("mute an app", "notifications.mute-channel")
    assert loc["label"] == "Mute channel"
    popover, page = loc["placements"]
    assert _ids(popover) == ["shell.notifications", "notifications.mute-channel"]
    assert _ids(page) == ["page.notifications", "notifications.mute-channel"]
    assert _req(popover) == _req(page) == ["new_channel_prompt"]


def test_the_bell_is_on_every_page_at_every_width() -> None:
    loc = _loc("notification bell", "shell.notifications")
    (p,) = loc["placements"]
    assert loc["label"] == "Notifications"
    assert p["on_every_page"] is True and p["entry"] == "header"
    assert _path(p) == ["Notifications"]
    assert p["requires"] == []
    # Drawn over every page, so a page filter keeps it.
    assert "shell.notifications" in _by_id(ui_index.find_ui("notification bell", "en", "schedule"))


def test_open_inbox_hangs_under_the_bell() -> None:
    loc = _loc("open the inbox", "shell.notifications.open-inbox")
    (p,) = loc["placements"]
    assert loc["kind"] == "link" and loc["label"] == "Open inbox"
    assert _ids(p) == ["shell.notifications", "shell.notifications.open-inbox"]
    assert p["entry"] == "menu" and p["requires"] == []


def test_focus_mode_is_a_desktop_header_toggle() -> None:
    loc = _loc("focus mode", "shell.focus-mode")
    (p,) = loc["placements"]
    assert loc["kind"] == "toggle"
    assert p["entry"] == "header" and _path(p) == ["Focus mode"]
    assert _req(p) == ["desktop"]


def test_the_bell_feed_and_the_page_feed_are_separate_sites() -> None:
    bell = _loc("mark everything read", "notifications.mark-all-read")
    page = _loc("mark everything read", "notifications.page-mark-all-read")
    (bp,) = bell["placements"]
    (pp,) = page["placements"]
    assert bell["label"] == "Mark all as read"
    assert _ids(bp) == ["shell.notifications", "notifications.mark-all-read"]
    assert bp["entry"] == "menu" and bp["on_every_page"] is True
    assert _req(bp) == ["has_unread_notifications"]
    assert page["label"] == "All"
    assert _ids(pp) == ["page.notifications", "notifications.page-mark-all-read"]
    assert pp["route"] == "/notifications" and pp["entry"] == "toolbar"
    assert _req(pp) == ["has_unread_notifications"]

    bell_clear = _loc("clear all notifications", "notifications.clear-all")
    page_clear = _loc("delete all notifications", "notifications.page-clear-all")
    assert bell_clear["label"] == "Clear all notifications?"
    assert _ids(bell_clear["placements"][0]) == ["shell.notifications", "notifications.clear-all"]
    assert _req(bell_clear["placements"][0]) == ["has_notifications"]
    assert page_clear["label"] == "Clear"
    assert _ids(page_clear["placements"][0]) == [
        "page.notifications",
        "notifications.page-clear-all",
    ]
    assert _req(page_clear["placements"][0]) == ["has_notifications"]


def test_mark_unread_is_in_the_detail_panel_of_both_hosts() -> None:
    loc = _loc("mark a notification unread", "notifications.detail.mark-unread")
    popover, page = loc["placements"]
    assert loc["label"] == "Mark unread"
    assert _ids(popover) == ["shell.notifications", "notifications.detail.mark-unread"]
    assert popover["on_every_page"] is True and popover["entry"] == "content"
    # Only a READ notification shows Mark unread (`n.acked`).
    assert _req(popover) == ["notification_selected", "notification_read"]
    assert _ids(page) == ["page.notifications", "notifications.detail.mark-unread"]
    assert page["route"] == "/notifications" and page["entry"] == "content"
    assert _req(page) == ["notification_selected", "notification_read"]
    zh = _loc("标为未读", "notifications.detail.mark-unread", "zh-CN")
    assert _path(zh["placements"][1]) == ["通知", "标为未读"]
