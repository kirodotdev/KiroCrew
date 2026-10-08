"""Explicit multi-step guides that the index plans for the common how-to questions.

Each question is answered by find_ui with a ``guide_ref`` to a packaged
``ui.show`` plan, and the plan walks every step the task needs: the person is
shown each control in turn and still clicks it themselves.
"""

from __future__ import annotations

from typing import Any

import pytest

from kiro_crew.guide_catalog import ui_show_plans
from kiro_crew.mcp_guide import _find_ui_next
from kiro_crew.ui_index import find_ui


def _steps(location_id: str, placement: str) -> list[dict[str, Any]]:
    plan = ui_show_plans()[location_id]
    for pl in plan["placements"]:
        if pl["id"] == placement:
            return list(pl["steps"])
    raise AssertionError(f"{location_id} has no {placement} placement")


def test_reference_a_file_opens_a_session_then_the_plus_menu_then_its_item() -> None:
    steps = _steps("composer.add-menu.reference-file", "desktop")
    kinds = [(s.get("kind"), s["location"]) for s in steps]
    assert ("select", "sessions.list") in kinds
    assert kinds[-2][1] == "composer.add-menu"
    assert kinds[-1] == (None, "composer.add-menu.reference-file")
    # The "+" menu is drawn for a mouse: the opener carries the predicate, so a
    # touchscreen gets a blocker instead of an outline on nothing.
    assert "mouse_input" in steps[-2].get("requires", [])
    assert ui_show_plans()["composer.add-menu.reference-file"]["placements"][0]["route"] == "/chat"


def test_pairing_a_phone_points_at_the_rail_button_and_names_its_blocker() -> None:
    desktop = _steps("shell.connect-phone", "desktop")
    assert [s["location"] for s in desktop] == ["shell.connect-phone"]
    assert desktop[-1]["requires"] == ["phone_connect_available"]
    mobile = _steps("shell.connect-phone", "mobile")
    assert [s["location"] for s in mobile] == ["shell.mobile-menu", "shell.connect-phone"]


@pytest.mark.parametrize(
    ("question", "location_id", "named"),
    [
        ("How do I add a reference file to my message?", "composer.add-menu.reference-file", None),
        (
            "How do I connect a new device to the gateway with a QR code?",
            "shell.connect-phone",
            None,
        ),
        ("怎么扫码连接设备？", "shell.connect-phone", None),
        ("怎么卸载 Command Bar 这个应用？", "apps.library.tile-uninstall", "Command Bar"),
        ("How do I uninstall the Command Bar app?", "apps.library.tile-uninstall", "Command Bar"),
    ],
)
def test_the_question_gets_the_planned_guide(
    question: str, location_id: str, named: str | None
) -> None:
    d = find_ui(question)
    assert d.get("status") == "ok", d
    top = d["results"][0]
    assert top["id"] == location_id
    assert isinstance(top.get("guide_ref"), dict)
    assert d.get("named_item") == named
    nxt = _find_ui_next(d) or ""
    assert f"{location_id} has a guide_ref" in nxt


def test_a_named_app_is_never_a_target_name_without_the_index() -> None:
    # The name only binds the choose-the-app step; an app the index does not
    # know is not a name find_ui hands back at all.
    d = find_ui("怎么卸载 Not A Real App 这个应用？")
    assert d.get("named_item") is None


def _locations(location_id: str, placement: str) -> list[tuple[str | None, str]]:
    return [(s.get("kind"), s["location"]) for s in _steps(location_id, placement)]


def test_moving_an_artifact_chooses_it_in_the_library_then_points_on_its_own_page() -> None:
    # The pick is made on /artifacts and used on /artifacts/<slug>: one
    # placement route covers both, and the move step is bound to the pick.
    assert _locations("artifacts.detail.move-to-folder", "any") == [
        ("select", "artifacts.list"),
        (None, "artifacts.detail.move-to-folder"),
    ]
    assert (
        ui_show_plans()["artifacts.detail.move-to-folder"]["placements"][0]["route"] == "/artifacts"
    )


def test_disabling_an_app_goes_through_its_card_menu_like_uninstall() -> None:
    assert _locations("apps.library.tile-disable", "any") == [
        ("select", "apps.library.app-list"),
        (None, "apps.library.tile-disable"),
    ]


@pytest.mark.parametrize("item", ["sessions.row-menu.rename", "sessions.row-menu.pin"])
def test_renaming_or_pinning_opens_a_session_then_its_rows_menu(item: str) -> None:
    locs = _locations(item, "desktop")
    assert locs[-3:] == [("select", "sessions.list"), (None, "sessions.row-menu"), (None, item)]
    # Nothing in the plan depends on where the pointer is.
    assert not any(
        "pointer_on_session_row" in s.get("requires", []) for s in _steps(item, "desktop")
    )


@pytest.mark.parametrize(
    ("target", "fact"),
    [
        ("composer.automation.pause", "goal_loop_running"),
        ("composer.automation.stop-monitor", "monitor_running"),
    ],
)
def test_stopping_automation_checks_that_something_runs_before_opening_its_panel(
    target: str, fact: str
) -> None:
    for placement in ("desktop", "mobile"):
        steps = _steps(target, placement)
        opener = next(s for s in steps if s["location"] == "composer.automation")
        assert fact in opener.get("requires", [])
        assert fact not in steps[-1].get("requires", [])
    assert _steps("composer.automation.stop-monitor", "desktop")[-1].get("caution") is True


@pytest.mark.parametrize(
    ("question", "location_id"),
    [
        ("Where do I add a new MCP server with an environment variable?", "mcp.add-custom"),
        ("How do I move an artifact into a folder?", "artifacts.detail.move-to-folder"),
        ("How do I rename a session in the sidebar?", "sessions.row-menu.rename"),
        ("怎么让某个会话固定在侧边栏顶部？", "sessions.row-menu.pin"),
        ("stop auto nudge", "composer.automation.pause"),
    ],
)
def test_the_multistep_question_is_offered_its_guide(question: str, location_id: str) -> None:
    d = find_ui(question)
    nxt = _find_ui_next(d) or ""
    assert f"{location_id} has a guide_ref" in nxt, (d, nxt)


def test_a_crewmate_control_is_not_the_pick_while_its_preview_is_off() -> None:
    # The Crewmates page is still a preview; a tab that does not show it as on
    # is not handed a guide onto it as the settled answer.
    d = find_ui("怎么给队友加工具")
    nxt = _find_ui_next(d) or ""
    assert "members.permissions has a guide_ref" not in nxt, (d, nxt)


def test_a_tie_with_no_pick_forbids_claiming_a_guide() -> None:
    d = find_ui("add mcp server")
    assert d.get("ambiguous") is True
    nxt = _find_ui_next(d) or ""
    assert "guide_ref" not in nxt and "never write that a guide" in nxt


def test_a_channel_page_guide_stops_before_its_credential_fields() -> None:
    d = find_ui("怎么把 Slack 机器人连上？")
    nxt = _find_ui_next(d) or ""
    assert "settings.sub.channels.slack has a find_ref" in nxt
    assert "never guide to a" in nxt
