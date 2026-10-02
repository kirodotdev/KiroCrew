"""scripts/e2e_gen/generate.py: from a decision-model run to a Playwright spec."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parent.parent / "scripts" / "e2e_gen" / "generate.py"
_spec = importlib.util.spec_from_file_location("e2e_gen_generate", _PATH)
assert _spec and _spec.loader
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)


def _act(desc, kind="click", value=None):
    return {"kind": "act", "desc": desc, "action_kind": kind, "value": value}


def _verify(accepted=True, undone=False):
    return {"kind": "verify", "accepted": accepted, "undone": undone}


def test_an_action_description_yields_role_and_accessible_name():
    assert gen.parse_action('button "Dark" | not selected') == ("button", "Dark")
    assert gen.parse_action('option "Theme"') == ("option", "Theme")
    assert gen.parse_action("go back to the previous page") is None


def test_the_trajectory_keeps_accepted_steps_only_and_folds_a_repeated_click():
    events = [
        _act('button "Settings"'),
        _verify(),
        _act('button "Settings"'),
        _verify(),
        _act('button "Focus mode" | not selected'),
        _verify(accepted=False),
        _act('textbox "Search settings…"', kind="type", value="theme"),
        _verify(),
        _act('button "Display"'),
        _verify(undone=True),
        _act("go back to the previous page"),
        _verify(),
    ]
    assert gen.trajectory(events) == [
        {"kind": "click", "role": "button", "name": "Settings", "value": None},
        {"kind": "type", "role": "textbox", "name": "Search settings…", "value": "theme"},
        {"kind": "back"},
    ]


def test_the_start_path_drops_the_origin_and_the_token():
    assert gen.start_path("{BASE}/?token={TOKEN}") == "/"
    assert gen.start_path("{BASE}/settings/display/theme") == "/settings/display/theme"
    assert gen.start_path("{BASE}") == "/"


def test_a_spec_replays_the_steps_by_role_and_asserts_the_end_state():
    case = {
        "id": "settings-dark-mode",
        "goal": "Switch the Mode to Dark.",
        "expect": {"selected": "Dark"},
    }
    steps = [
        {"kind": "click", "role": "button", "name": "Settings", "value": None},
        {"kind": "type", "role": "textbox", "name": 'Say "hi"', "value": "a"},
        {"kind": "click", "role": "button", "name": "Dark", "value": None},
    ]
    spec = gen.render_spec(case, "/", steps)
    assert 'test("settings-dark-mode"' in spec
    assert 'await page.goto("/"' in spec
    assert 'page.getByRole("button", { name: "Settings", exact: true }).click()' in spec
    assert 'page.getByRole("textbox", { name: "Say \\"hi\\"", exact: true }).fill("a")' in spec
    assert spec.index('name: "Settings"') < spec.index('name: "Dark"')
    assert '.filter({ hasText: "Dark" })' in spec


def test_a_url_end_state_becomes_an_escaped_url_assertion():
    case = {"id": "x", "goal": "g", "expect": {"url_contains": "/settings/privacy"}}
    spec = gen.render_spec(case, "/", [])
    assert 'await expect(page).toHaveURL(new RegExp("/settings/privacy"))' in spec


def test_a_case_without_a_code_checked_end_state_is_refused(tmp_path):
    path = tmp_path / "cases.jsonl"
    path.write_text(
        json.dumps({"id": "a", "start": "{BASE}/", "goal": "g"}) + "\n", encoding="utf-8"
    )
    with pytest.raises(SystemExit, match="code-checked"):
        gen._load_cases(path, [])


def test_a_keyboard_hint_is_dropped_from_the_locator_name():
    assert gen.accessible_name("Schedule Alt + S") == "Schedule"
    assert gen.accessible_name("Task Runner Alt + P") == "Task Runner"
    assert gen.accessible_name("Previous Ctrl + Shift + [") == "Previous"
    assert gen.accessible_name("Alt text") == "Alt text"
    spec = gen.render_spec(
        {"id": "x", "goal": "g", "expect": {"url_contains": "/schedule"}},
        "/",
        [{"kind": "click", "role": "button", "name": "Schedule Alt + S", "value": None}],
    )
    assert 'name: "Schedule", exact: true' in spec


def test_a_case_setup_request_runs_from_the_page_before_the_case_starts():
    setup = [{"method": "PUT", "path": "/api/config/theme", "json": {"mode": "light"}}]
    case = {"id": "x", "goal": "g", "expect": {"selected": "Dark"}, "setup": setup}
    spec = gen.render_spec(case, "/settings", [])
    assert '[{"method": "PUT", "path": "/api/config/theme", "json": {"mode": "light"}}]' in spec
    assert "expect(setup).toEqual([])" in spec
    # Setup runs on a loaded page, then the case's own start is a fresh load that reads it.
    assert spec.index("page.evaluate") < spec.index('page.goto("/settings"')
    assert "page.evaluate" not in gen.render_spec({**case, "setup": []}, "/", [])
