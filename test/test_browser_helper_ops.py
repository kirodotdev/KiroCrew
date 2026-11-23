"""The ops that are one Harness helper each (``browser_harness.helper_ops``), ``tabs
close``, ``click {clicks: 3}`` and the function keys, against the fake browser of
``test_browser_harness_ops``: which helper each op reaches, with which arguments,
and what it refuses before any helper runs.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from test_browser_harness_ops import FakeBrowser

from kiro_crew.browser_harness import helper_ops, helper_worker, op_classes, ops, supervisor
from kiro_crew.dashboard.handlers import browser_ops as route

_NEW_OPS = ("fill", "dispatch_key", "wait_for_element", "wait_for_network_idle")


@pytest.fixture
def fake(tmp_path: Any) -> FakeBrowser:
    return FakeBrowser(tmp_path)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def _focusable(fake: FakeBrowser, **active: Any) -> None:
    fake.page.update(
        focus={"ok": True, "focused": True},
        activeField={"focused": True, "tag": "input", **active},
    )


def test_each_new_op_is_advertised_and_classed() -> None:
    for op in _NEW_OPS:
        assert op in ops.OPS
    assert op_classes.host_trust_class("fill", {}) == op_classes.CLASS_ACT
    assert op_classes.host_trust_class("dispatch_key", {}) == op_classes.CLASS_ACT
    for op in ("wait_for_element", "wait_for_network_idle"):
        assert op_classes.reads_only(op, {})
        assert op_classes.host_trust_class(op, {}) == op_classes.CLASS_READ
    assert op_classes.host_trust_class("tabs", {"action": "close"}) == ""  # always asks
    assert {"fill", "dispatch_key"} <= route._PAGE_TARGETED_OPS


def test_each_new_helper_is_on_both_allowlists_with_the_harness_function() -> None:
    expected = {
        "fill_input": ("selector", "text", "clear_first"),
        "dispatch_key": ("selector", "key", "event"),
        "wait_for_element": ("selector", "timeout", "visible"),
        "wait_for_network_idle": ("timeout", "idle_ms"),
    }
    for name, kwargs in expected.items():
        assert supervisor.HELPERS[name] == kwargs
        assert helper_worker._ALLOWLIST[name] == (name, kwargs)


# --- fill ----------------------------------------------------------------------


def test_fill_focuses_and_judges_the_ref_then_fills_the_focused_field(fake: FakeBrowser) -> None:
    _focusable(fake)
    out = run(ops.run_op("fill", {"ref": "e1", "text": "Ada"}, fake.ctx()))
    assert out == {"ok": True, "ref": "e1", "chars": 3, "cleared": True}
    assert fake.page_methods()[:2] == ["focus", "activeField"]
    assert fake.calls[-1] == (
        "fill_input",
        {"selector": ":focus", "text": "Ada", "clear_first": True},
    )


def test_fill_keeps_the_old_value_when_clear_is_false(fake: FakeBrowser) -> None:
    _focusable(fake)
    out = run(ops.run_op("fill", {"ref": "e1", "text": "x", "clear": False}, fake.ctx()))
    assert out["cleared"] is False
    assert fake.calls[-1][1]["clear_first"] is False


@pytest.mark.parametrize(
    "active, code",
    [({"password": True}, "password_field"), ({"asked": True}, "asked_field")],
)
def test_fill_refuses_where_type_does(fake: FakeBrowser, active: dict, code: str) -> None:
    _focusable(fake, **active)
    out = run(ops.run_op("fill", {"ref": "e1", "text": "hunter2"}, fake.ctx()))
    assert out["code"] == code
    assert "fill_input" not in fake.helper_names()


@pytest.mark.parametrize(
    "args, code",
    [
        ({"text": "x"}, "bad_ref"),
        ({"ref": "f1e2", "text": "x"}, "bad_ref"),
        ({"ref": "e1"}, "bad_text"),
        ({"ref": "e1", "text": "x" * (helper_ops.FILL_CAP + 1)}, "bad_text"),
        ({"ref": "e1", "text": "x", "clear": "no"}, "bad_args"),
    ],
)
def test_fill_validation(fake: FakeBrowser, args: dict, code: str) -> None:
    assert run(ops.run_op("fill", args, fake.ctx()))["code"] == code
    assert "fill_input" not in fake.helper_names()


# --- dispatch_key --------------------------------------------------------------


def test_dispatch_key_checks_the_element_exists_then_sends_the_event(fake: FakeBrowser) -> None:
    fake.helpers["wait_for_element"] = True
    out = run(
        ops.run_op(
            "dispatch_key", {"selector": "#q", "key": "Enter", "event": "keydown"}, fake.ctx()
        )
    )
    assert out == {"ok": True, "selector": "#q", "key": "Enter", "event": "keydown"}
    assert fake.calls[-2:] == [
        ("wait_for_element", {"selector": "#q", "timeout": helper_ops.FIND_S, "visible": False}),
        ("dispatch_key", {"selector": "#q", "key": "Enter", "event": "keydown"}),
    ]


def test_dispatch_key_answers_not_found_and_sends_nothing(fake: FakeBrowser) -> None:
    fake.helpers["wait_for_element"] = False
    out = run(ops.run_op("dispatch_key", {"selector": "#none"}, fake.ctx()))
    assert out["code"] == "not_found"
    assert "dispatch_key" not in fake.helper_names()


@pytest.mark.parametrize(
    "args, code",
    [
        ({}, "bad_selector"),
        ({"selector": " "}, "bad_selector"),
        ({"selector": "a" * (helper_ops.SELECTOR_MAX + 1)}, "bad_selector"),
        ({"selector": "#q", "key": "Bogus"}, "bad_key"),
        ({"selector": "#q", "event": "click"}, "bad_event"),
    ],
)
def test_dispatch_key_validation(fake: FakeBrowser, args: dict, code: str) -> None:
    assert run(ops.run_op("dispatch_key", args, fake.ctx()))["code"] == code
    assert fake.helper_names() == []


# --- the waits -----------------------------------------------------------------


def test_wait_for_element_passes_its_arguments_and_bounds_the_timeout(fake: FakeBrowser) -> None:
    fake.helpers["wait_for_element"] = True
    args = {"selector": ".done", "visible": True, "timeout": 999}
    assert run(ops.run_op("wait_for_element", args, fake.ctx()))["ok"] is True
    assert fake.calls[-1] == (
        "wait_for_element",
        {"selector": ".done", "timeout": ops.WAIT_MAX_S, "visible": True},
    )


def test_wait_for_element_times_out_as_a_refusal(fake: FakeBrowser) -> None:
    fake.helpers["wait_for_element"] = False
    out = run(ops.run_op("wait_for_element", {"selector": ".x", "timeout": 1}, fake.ctx()))
    assert out["code"] == "wait_timeout" and "1s" in out["error"]
    out = run(ops.run_op("wait_for_element", {"selector": ".x", "visible": 1}, fake.ctx()))
    assert out["code"] == "bad_args"


def test_wait_for_network_idle_bounds_its_numbers(fake: FakeBrowser) -> None:
    fake.helpers["wait_for_network_idle"] = True
    out = run(ops.run_op("wait_for_network_idle", {"idle_ms": 1}, fake.ctx()))
    assert out == {"ok": True, "idle_ms": helper_ops.IDLE_MS_MIN}
    assert fake.calls[-1] == (
        "wait_for_network_idle",
        {"timeout": ops.WAIT_DEFAULT_S, "idle_ms": helper_ops.IDLE_MS_MIN},
    )
    fake.helpers["wait_for_network_idle"] = False
    assert run(ops.run_op("wait_for_network_idle", {}, fake.ctx()))["code"] == "wait_timeout"
    assert (
        run(ops.run_op("wait_for_network_idle", {"idle_ms": "500"}, fake.ctx()))["code"]
        == "bad_args"
    )


# --- tabs close ----------------------------------------------------------------


def test_tabs_close_refuses_a_tab_the_agent_did_not_open(fake: FakeBrowser) -> None:
    for args in ({"action": "close", "target_id": "T-user"}, {"action": "close"}):
        assert run(ops.run_op("tabs", args, fake.ctx()))["code"] == "not_agent_tab"
    assert "close_tab" not in fake.helper_names()


def test_tabs_close_of_another_tab_leaves_the_current_one(fake: FakeBrowser) -> None:
    fake.targets = ["T-agent", "T-popup"]
    fake.helpers["current_tab"] = {"targetId": "T-agent", "url": "https://a/"}
    out = run(ops.run_op("tabs", {"action": "close", "target_id": "T-popup"}, fake.ctx()))
    assert out == {"ok": True, "closed": "T-popup"}
    assert ("close_tab", {"target": "T-popup"}) in fake.calls
    assert "switch_tab" not in fake.helper_names() and "new_tab" not in fake.helper_names()


def test_tabs_close_of_the_current_tab_moves_to_another_agent_tab(fake: FakeBrowser) -> None:
    fake.targets = ["T-agent", "T-popup"]
    fake.helpers["current_tab"] = {"targetId": "T-popup", "url": "https://a/"}
    out = run(ops.run_op("tabs", {"action": "close", "target_id": "T-popup"}, fake.ctx()))
    assert out == {"ok": True, "closed": "T-popup", "current": "T-agent"}
    names = fake.helper_names()
    assert names.index("close_tab") < names.index("switch_tab")


def test_tabs_close_of_the_last_tab_opens_a_blank_one(fake: FakeBrowser) -> None:
    fake.helpers["current_tab"] = {"targetId": "T-agent", "url": "https://a/"}
    fake.helpers["new_tab"] = "T-blank"
    out = run(ops.run_op("tabs", {"action": "close", "target_id": "T-agent"}, fake.ctx()))
    assert out == {"ok": True, "closed": "T-agent", "current": "T-blank"}
    assert ("new_tab", {"url": "about:blank"}) in fake.calls


# --- click and keys ------------------------------------------------------------


def test_a_triple_click_is_one_harness_click_with_count_three(fake: FakeBrowser) -> None:
    out = run(ops.run_op("click", {"x": 5, "y": 6, "clicks": 3}, fake.ctx()))
    assert out["ok"] is True and out["clicks"] == 3
    assert fake.calls == [("click", {"x": 5.0, "y": 6.0, "button": "left", "clicks": 3})]


@pytest.mark.parametrize("key", ["F1", "F2", "F12", "Insert", "ContextMenu"])
def test_function_and_editing_keys_are_pressed(fake: FakeBrowser, key: str) -> None:
    out = run(ops.run_op("press_key", {"key": key}, fake.ctx()))
    assert out == {"ok": True, "key": key, "modifiers": 0}
    assert fake.calls[-1] == ("press_key", {"key": key, "modifiers": 0})


def test_a_key_past_f12_is_still_refused(fake: FakeBrowser) -> None:
    assert run(ops.run_op("press_key", {"key": "F13"}, fake.ctx()))["code"] == "bad_key"


@pytest.mark.parametrize(
    "op",
    [
        ("dispatch_key", {"selector": "[data-kc-asked][value^=a]", "key": "Enter"}),
        ("wait_for_element", {"selector": "[data-kc-asked][value^=a]"}),
    ],
)
def test_a_selector_that_could_probe_a_filled_secret_is_refused(
    fake: FakeBrowser, op: tuple[str, dict]
) -> None:
    fake.page["selectorBlocked"] = lambda sel: "value" in sel
    fake.helpers["wait_for_element"] = True
    out = run(ops.run_op(op[0], op[1], fake.ctx()))
    assert out["code"] == "bad_selector"
    assert not {"wait_for_element", "dispatch_key"} & set(fake.helper_names())
