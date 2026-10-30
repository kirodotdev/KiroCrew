"""``browser_harness.ops``: every op against a fake supervisor and a fake page.

The fake records each ``call(helper, **kwargs)`` and answers the walker's
methods from a scripted page, so a test proves WHICH helper an op reaches for
and what it refuses, with no browser, no daemon and no real clock: ``sleep``
advances an injected clock. Pinned here:

* the ``js`` helper is only ever given the walker source plus one method call;
* ``type`` refuses a password field by ref AND by whatever holds focus;
* ``tabs`` lists and switches only the targets the relay says the agent owns;
* every op returns ``user_in_control`` at once while the person holds the browser;
* each op's argument validation, and the wait loops on an injected clock.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.browser_harness import ops

_METHOD_RE = re.compile(r"\.(\w+)\((.*)\)$", re.DOTALL)


class FakeBrowser:
    """A scripted supervisor + page. ``page`` maps walker method -> answer or callable."""

    def __init__(self, tmp_path: Path) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.page: dict[str, Any] = {"askedValues": 0, "passwordBoxes": 0}
        self.helpers: dict[str, Any] = {}
        self.steps: list[str] = []
        self.held = False
        self.targets: list[str] = ["T-agent"]
        self.clock = 1000.0
        self.tmp = tmp_path

    async def call(self, helper: str, **kwargs: Any) -> Any:
        if helper == "current_tab" and helper not in self.helpers:
            return {"url": "https://example.com/"}
        self.calls.append((helper, kwargs))
        if helper == "js":
            expression = kwargs["expression"]
            assert expression.startswith(ops.WALKER_SOURCE), "js must carry the walker"
            m = _METHOD_RE.search(expression[len(ops.WALKER_SOURCE) :])
            assert m, expression[-80:]
            method, raw = m.group(1), m.group(2)
            args = json.loads(f"[{raw}]") if raw else []
            answer = self.page.get(method)
            return answer(*args) if callable(answer) else answer
        answer = self.helpers.get(helper)
        return answer(**kwargs) if callable(answer) else answer

    async def sleep(self, seconds: float) -> None:
        self.clock += seconds

    def now(self) -> float:
        return self.clock

    def ctx(self) -> ops.OpContext:
        return ops.OpContext(
            helper=self.call,
            step=self.steps.append,
            in_control=lambda: self.held,
            agent_targets=lambda: list(self.targets),
            screenshot_dir=self.tmp / "shots",
            sleep=self.sleep,
            now=self.now,
        )

    def helper_names(self) -> list[str]:
        return [h for h, _ in self.calls]

    def page_methods(self) -> list[str]:
        out = []
        for h, kw in self.calls:
            if h == "js":
                m = _METHOD_RE.search(kw["expression"][len(ops.WALKER_SOURCE) :])
                out.append(m.group(1) if m else "?")
        return out


@pytest.fixture
def fake(tmp_path: Path) -> FakeBrowser:
    return FakeBrowser(tmp_path)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


# --- the walker file -----------------------------------------------------------


def test_walker_source_is_one_iife_the_ops_call_methods_on() -> None:
    assert ops.WALKER_PATH.exists()
    assert ops.WALKER_SOURCE.startswith("(() => {")
    assert ops.WALKER_SOURCE.endswith("})()")
    for method in (
        "snapshot",
        "resolve",
        "focus",
        "activeField",
        "selectOption",
        "textPresent",
        "stamp",
        "state",
        "back",
        "consoleMessages",
    ):
        assert f"{method}: function" in ops.WALKER_SOURCE, method


def test_every_page_side_call_goes_through_the_walker(fake: FakeBrowser) -> None:
    fake.page["snapshot"] = {"url": "u", "title": "t", "nodes": []}
    run(ops.run_op("snapshot", {}, fake.ctx()))
    (expr,) = [kw["expression"] for h, kw in fake.calls if h == "js"]
    assert expr == ops.WALKER_SOURCE + ".snapshot()"


# --- dispatch ------------------------------------------------------------------


def test_unknown_op_is_refused_before_anything_runs(fake: FakeBrowser) -> None:
    out = run(ops.run_op("evaluate", {}, fake.ctx()))
    assert out["ok"] is False and out["code"] == "bad_op"
    assert fake.calls == []


@pytest.mark.parametrize("op", ops.OPS)
def test_every_op_returns_user_in_control_at_once_while_held(fake: FakeBrowser, op: str) -> None:
    fake.held = True
    out = run(ops.run_op(op, {"url": "https://x", "ref": "e1", "text": "a"}, fake.ctx()))
    assert out == {
        "ok": False,
        "code": "user_in_control",
        "error": "the user has taken over the browser; wait for them to hand it back",
    }
    assert fake.calls == [] and fake.steps == []


def test_in_control_may_be_awaitable(fake: FakeBrowser) -> None:
    async def _held() -> bool:
        return True

    ctx = fake.ctx()
    ctx.in_control = _held
    assert run(ops.run_op("snapshot", {}, ctx))["code"] == "user_in_control"


def test_a_failing_step_hook_never_fails_the_op(fake: FakeBrowser) -> None:
    def _boom(_label: str) -> None:
        raise RuntimeError("card gone")

    ctx = fake.ctx()
    ctx.step = _boom
    fake.page["snapshot"] = {"url": "u", "title": "t", "nodes": []}
    assert run(ops.run_op("snapshot", {}, ctx))["ok"] is True


# --- navigate / back -----------------------------------------------------------


def _committing_page(fake: FakeBrowser, href: str, polls_until_new: int = 2) -> None:
    state = {"token": "", "n": 0}

    def stamp(token: str) -> bool:
        state["token"] = token
        return True

    def page_state() -> dict[str, Any]:
        state["n"] += 1
        new = state["n"] >= polls_until_new
        return {
            "readyState": "complete" if new else "loading",
            "stale": "" if new else state["token"],
            "href": href if new else "https://old.example/",
            "title": "New" if new else "Old",
        }

    fake.page.update(stamp=stamp, state=page_state, consoleMessages=[])


def test_navigate_stamps_goes_and_waits_for_the_new_document(fake: FakeBrowser) -> None:
    _committing_page(fake, "https://example.com/")
    fake.helpers["goto"] = {"frameId": "F", "loaderId": "L"}
    out = run(ops.run_op("navigate", {"url": "https://example.com/"}, fake.ctx()))
    assert out == {"ok": True, "url": "https://example.com/", "title": "New"}
    assert fake.helper_names()[:2] == ["js", "goto"]
    assert fake.calls[1] == ("goto", {"url": "https://example.com/"})
    assert fake.page_methods()[0] == "stamp"
    assert "state" in fake.page_methods()
    assert fake.page_methods()[-1] == "consoleMessages"  # the hook is armed on the new page
    assert fake.steps == ["Open https://example.com/"]


def test_navigate_refuses_a_non_web_url_without_touching_the_browser(fake: FakeBrowser) -> None:
    for url in ("file:///etc/passwd", "javascript:1", "", 7, None):
        out = run(ops.run_op("navigate", {"url": url}, fake.ctx()))
        assert out["ok"] is False and out["code"] == "bad_url", url
    assert fake.calls == []


def test_navigate_surfaces_the_browsers_error_text(fake: FakeBrowser) -> None:
    fake.page.update(stamp=True)
    fake.helpers["goto"] = {"errorText": "net::ERR_NAME_NOT_RESOLVED"}
    out = run(ops.run_op("navigate", {"url": "https://nope.invalid/"}, fake.ctx()))
    assert out == {"ok": False, "code": "nav_error", "error": "net::ERR_NAME_NOT_RESOLVED"}


def test_navigate_times_out_on_the_injected_clock_when_the_old_document_stays(
    fake: FakeBrowser,
) -> None:
    fake.page.update(
        stamp=lambda tok: True,
        state=lambda: {
            "readyState": "complete",
            "stale": "same",
            "href": "https://old/",
            "title": "",
        },
    )

    # The stamp the op wrote is what ``state`` must echo for "still old".
    def stamp(tok: str) -> bool:
        fake.page["state"] = lambda: {
            "readyState": "complete",
            "stale": tok,
            "href": "https://old/",
            "title": "",
        }
        return True

    fake.page["stamp"] = stamp
    fake.helpers["goto"] = {}
    start = fake.clock
    out = run(ops.run_op("navigate", {"url": "https://new/"}, fake.ctx()))
    assert out["ok"] is False and out["code"] == "nav_timeout"
    assert "still old doc" in out["error"]
    assert fake.clock - start >= ops.NAV_SETTLE_S - ops.NAV_POLL_S  # injected sleep only


def test_same_document_navigation_commits_on_href_match(fake: FakeBrowser) -> None:
    def stamp(tok: str) -> bool:
        fake.page["state"] = lambda: {
            "readyState": "complete",
            "stale": tok,  # same document: the stamp survives
            "href": "https://x/#a",
            "title": "X",
        }
        return True

    fake.page.update(stamp=stamp, consoleMessages=[])
    fake.helpers["goto"] = {}
    out = run(ops.run_op("navigate", {"url": "https://x/#a"}, fake.ctx()))
    assert out["ok"] is True and out["url"] == "https://x/#a"


def test_back_uses_history_and_waits_for_commit(fake: FakeBrowser) -> None:
    _committing_page(fake, "https://prev.example/")
    fake.page["back"] = {"ok": True}
    out = run(ops.run_op("back", {}, fake.ctx()))
    assert out["ok"] is True and out["url"] == "https://prev.example/"
    assert fake.page_methods()[:2] == ["stamp", "back"]
    assert all(h == "js" for h in fake.helper_names())


def test_back_with_no_history_is_refused(fake: FakeBrowser) -> None:
    fake.page.update(stamp=True, back={"ok": False, "code": "no_history"})
    assert run(ops.run_op("back", {}, fake.ctx()))["code"] == "no_history"


# --- snapshot ------------------------------------------------------------------


def test_snapshot_formats_the_outline_with_refs_and_states(fake: FakeBrowser) -> None:
    fake.page["snapshot"] = {
        "url": "https://x/",
        "title": "X",
        "nodes": [
            {"depth": 0, "role": "heading", "name": "Sign in"},
            {"depth": 1, "role": "textbox", "name": "Email", "ref": "e1", "state": {}},
            {"depth": 1, "role": "textbox", "name": "", "ref": "e2", "state": {"password": True}},
            {
                "depth": 1,
                "role": "checkbox",
                "name": "Remember",
                "ref": "e3",
                "state": {"checked": True},
            },
            {"depth": 1, "role": "button", "name": "Go", "ref": "e4", "state": {"disabled": True}},
            {
                "depth": 0,
                "role": "button",
                "name": "More",
                "ref": "e5",
                "state": {"expanded": False},
            },
        ],
    }
    out = run(ops.run_op("snapshot", {}, fake.ctx()))
    assert out["ok"] is True
    assert out["snapshot"].splitlines() == [
        '- heading "Sign in"',
        '  - textbox "Email" [ref=e1]',
        '  - textbox "" [ref=e2] [password]',
        '  - checkbox "Remember" [ref=e3] [checked]',
        '  - button "Go" [ref=e4] [disabled]',
        '- button "More" [ref=e5] [collapsed]',
    ]
    assert fake.steps == ["Read the page"]


def test_snapshot_with_no_nodes_is_an_error_and_partial_is_reported(fake: FakeBrowser) -> None:
    fake.page["snapshot"] = {"url": "u", "title": "t"}
    assert run(ops.run_op("snapshot", {}, fake.ctx()))["code"] == "snapshot_error"
    fake.page["snapshot"] = {"url": "u", "title": "t", "nodes": [], "error": "boom"}
    assert run(ops.run_op("snapshot", {}, fake.ctx()))["partial"] == "boom"


# --- click / hover / drag / scroll --------------------------------------------


def test_click_by_ref_resolves_then_clicks_the_centre(fake: FakeBrowser) -> None:
    fake.page["resolve"] = {"ok": True, "x": 10.5, "y": 20, "tag": "button"}
    out = run(ops.run_op("click", {"ref": "e1"}, fake.ctx()))
    assert out["ok"] is True
    assert fake.calls[-1] == ("click", {"x": 10.5, "y": 20.0, "button": "left", "clicks": 1})
    assert fake.steps == ["Click e1"]


def test_click_by_coordinates_double_and_right(fake: FakeBrowser) -> None:
    out = run(ops.run_op("click", {"x": 5, "y": 6, "clicks": 2, "button": "right"}, fake.ctx()))
    assert out["ok"] is True
    assert fake.calls == [("click", {"x": 5.0, "y": 6.0, "button": "right", "clicks": 2})]
    assert (
        run(ops.run_op("click", {"x": 5, "y": 6, "doubleClick": True}, fake.ctx()))["clicks"] == 2
    )


def test_click_validation(fake: FakeBrowser) -> None:
    assert run(ops.run_op("click", {}, fake.ctx()))["code"] == "bad_target"
    assert run(ops.run_op("click", {"x": -1, "y": 2}, fake.ctx()))["code"] == "bad_target"
    assert run(ops.run_op("click", {"ref": "button"}, fake.ctx()))["code"] == "bad_ref"
    assert (
        run(ops.run_op("click", {"x": 1, "y": 1, "button": "x"}, fake.ctx()))["code"]
        == "bad_button"
    )
    assert (
        run(ops.run_op("click", {"x": 1, "y": 1, "clicks": 3}, fake.ctx()))["code"] == "bad_clicks"
    )
    fake.page["resolve"] = {"ok": False, "code": "ref_stale"}
    out = run(ops.run_op("click", {"ref": "e9"}, fake.ctx()))
    assert out["code"] == "ref_stale" and "re-snapshot" in out["error"]
    assert "click" not in fake.helper_names()


def test_hover_moves_the_mouse(fake: FakeBrowser) -> None:
    fake.page["resolve"] = {"ok": True, "x": 1, "y": 2}
    assert run(ops.run_op("hover", {"ref": "e1"}, fake.ctx()))["ok"] is True
    assert fake.calls[-1] == ("mouse_move", {"x": 1.0, "y": 2.0})


def test_drag_resolves_both_ends(fake: FakeBrowser) -> None:
    fake.page["resolve"] = lambda ref: {"ok": True, "x": 1 if ref == "e1" else 9, "y": 2}
    out = run(ops.run_op("drag", {"ref": "e1", "to_ref": "e2"}, fake.ctx()))
    assert out == {"ok": True, "from": [1.0, 2.0], "to": [9.0, 2.0]}
    assert fake.calls[-1] == ("drag", {"x": 1.0, "y": 2.0, "to_x": 9.0, "to_y": 2.0})
    assert run(ops.run_op("drag", {"x": 1, "y": 1}, fake.ctx()))["code"] == "bad_target"


def test_scroll_defaults_to_the_viewport_centre_and_a_page_down(fake: FakeBrowser) -> None:
    fake.helpers["page_info"] = {"w": 800, "h": 600}
    out = run(ops.run_op("scroll", {}, fake.ctx()))
    assert out["ok"] is True
    assert fake.calls[-1] == ("scroll", {"x": 400.0, "y": 300.0, "dy": 300.0, "dx": 0.0})
    fake.page["resolve"] = {"ok": True, "x": 7, "y": 8}
    run(ops.run_op("scroll", {"ref": "e1", "dy": -50}, fake.ctx()))
    assert fake.calls[-1] == ("scroll", {"x": 7.0, "y": 8.0, "dy": -50.0, "dx": 0.0})


# --- type / press_key ----------------------------------------------------------


def test_type_by_ref_focuses_then_inserts_and_submits(fake: FakeBrowser) -> None:
    fake.page["focus"] = {"ok": True, "focused": True, "activeTag": "input"}
    fake.page["activeField"] = {"focused": True, "password": False, "tag": "input"}
    out = run(ops.run_op("type", {"ref": "e1", "text": "hello", "submit": True}, fake.ctx()))
    assert out == {"ok": True, "ref": "e1", "chars": 5, "submitted": True}
    assert fake.calls[-2:] == [
        ("type_text", {"text": "hello"}),
        ("press_key", {"key": "Enter", "modifiers": 0}),
    ]


def test_type_refuses_a_password_field_by_ref(fake: FakeBrowser) -> None:
    fake.page["focus"] = {"ok": False, "code": "password_field"}
    out = run(ops.run_op("type", {"ref": "e2", "text": "hunter2"}, fake.ctx()))
    assert out["code"] == "password_field"
    assert "take over" in out["error"]
    assert "type_text" not in fake.helper_names()
    assert "hunter2" not in json.dumps(fake.calls)


def test_type_refuses_when_focus_sits_in_a_password_field(fake: FakeBrowser) -> None:
    fake.page["activeField"] = {"focused": True, "password": True, "tag": "input"}
    out = run(ops.run_op("type", {"text": "hunter2"}, fake.ctx()))
    assert out["code"] == "password_field"
    assert "type_text" not in fake.helper_names()


def test_type_refuses_when_focus_did_not_land_or_nothing_is_focused(fake: FakeBrowser) -> None:
    fake.page["focus"] = {"ok": True, "focused": False, "disabled": True, "activeTag": "body"}
    out = run(ops.run_op("type", {"ref": "e1", "text": "x"}, fake.ctx()))
    assert out["code"] == "not_focusable" and "(disabled)" in out["error"]
    fake.page["activeField"] = {"focused": False, "password": False, "tag": ""}
    assert run(ops.run_op("type", {"text": "x"}, fake.ctx()))["code"] == "no_focus"
    assert run(ops.run_op("type", {"ref": "e1"}, fake.ctx()))["code"] == "bad_text"
    assert run(ops.run_op("type", {"ref": "nope", "text": "x"}, fake.ctx()))["code"] == "bad_ref"
    assert "type_text" not in fake.helper_names()


def test_press_key_named_single_and_modifiers(fake: FakeBrowser) -> None:
    assert run(ops.run_op("press_key", {"key": "Enter"}, fake.ctx()))["ok"] is True
    assert fake.calls[-1] == ("press_key", {"key": "Enter", "modifiers": 0})
    run(ops.run_op("press_key", {"key": "a", "modifiers": ["Control", "Shift"]}, fake.ctx()))
    assert fake.calls[-1] == ("press_key", {"key": "a", "modifiers": 10})
    run(ops.run_op("press_key", {"key": "Space"}, fake.ctx()))
    assert fake.calls[-1] == ("press_key", {"key": " ", "modifiers": 0})
    assert run(ops.run_op("press_key", {"key": "Bogus"}, fake.ctx()))["code"] == "bad_key"
    assert run(ops.run_op("press_key", {}, fake.ctx()))["code"] == "bad_key"


# --- select_option / wait_for / console ---------------------------------------


def test_select_option(fake: FakeBrowser) -> None:
    fake.page["selectOption"] = lambda ref, values: {"ok": True, "selected": values}
    out = run(ops.run_op("select_option", {"ref": "e1", "values": ["b", 2]}, fake.ctx()))
    assert out == {"ok": True, "ref": "e1", "selected": ["b", "2"]}
    out = run(ops.run_op("select_option", {"ref": "e1", "value": "a"}, fake.ctx()))
    assert out["selected"] == ["a"]
    assert run(ops.run_op("select_option", {"ref": "e1"}, fake.ctx()))["code"] == "bad_values"
    assert (
        run(ops.run_op("select_option", {"ref": "x", "value": "a"}, fake.ctx()))["code"]
        == "bad_ref"
    )
    fake.page["selectOption"] = {"ok": False, "code": "not_select"}
    assert (
        run(ops.run_op("select_option", {"ref": "e1", "value": "a"}, fake.ctx()))["code"]
        == "not_select"
    )


def test_wait_for_time_sleeps_on_the_injected_clock_and_caps(fake: FakeBrowser) -> None:
    start = fake.clock
    assert run(ops.run_op("wait_for", {"time": 2}, fake.ctx())) == {"ok": True, "waited": 2.0}
    assert fake.clock - start == 2.0
    assert run(ops.run_op("wait_for", {"time": 500}, fake.ctx()))["waited"] == ops.WAIT_MAX_S


def test_wait_for_text_polls_until_present_and_times_out(fake: FakeBrowser) -> None:
    seen = {"n": 0}

    def present(text: str) -> bool:
        seen["n"] += 1
        return seen["n"] >= 3

    fake.page["textPresent"] = present
    out = run(ops.run_op("wait_for", {"text": "Done"}, fake.ctx()))
    assert out == {"ok": True, "matched": "present", "text": "Done"}
    fake.page["textPresent"] = True
    start = fake.clock
    out = run(ops.run_op("wait_for", {"textGone": "Loading", "timeout": 1}, fake.ctx()))
    assert out["code"] == "wait_timeout" and fake.clock - start >= 1.0
    assert run(ops.run_op("wait_for", {}, fake.ctx()))["code"] == "bad_wait"


def test_wait_for_load_uses_the_helper(fake: FakeBrowser) -> None:
    fake.helpers["wait_for_load"] = True
    assert run(ops.run_op("wait_for", {"load": True}, fake.ctx())) == {"ok": True, "loaded": True}
    assert fake.calls[-1] == ("wait_for_load", {"timeout": ops.WAIT_DEFAULT_S})
    fake.helpers["wait_for_load"] = False
    assert (
        run(ops.run_op("wait_for", {"load": True, "timeout": 2}, fake.ctx()))["code"]
        == "wait_timeout"
    )


def test_console_reads_the_pages_buffer_with_a_bounded_limit(fake: FakeBrowser) -> None:
    fake.page["consoleMessages"] = lambda limit: [{"level": "log", "text": str(limit)}]
    assert run(ops.run_op("console", {"limit": 5}, fake.ctx()))["messages"][0]["text"] == "5"
    assert run(ops.run_op("console", {"limit": 10**9}, fake.ctx()))["messages"][0]["text"] == str(
        ops.CONSOLE_CAP
    )
    assert run(ops.run_op("console", {"limit": "x"}, fake.ctx()))["messages"][0]["text"] == str(
        ops.CONSOLE_CAP
    )


# --- screenshot ---------------------------------------------------------------


def test_screenshot_writes_under_the_screenshot_dir_and_returns_the_path(
    fake: FakeBrowser,
) -> None:
    def shoot(path: str) -> str:
        Path(path).write_bytes(b"\x89PNG")
        return path

    fake.helpers["screenshot"] = shoot
    out = run(ops.run_op("screenshot", {}, fake.ctx()))
    assert out["ok"] is True
    path = Path(out["path"])
    assert path.is_absolute() and path.parent == fake.tmp / "shots" and path.suffix == ".png"
    assert path.read_bytes() == b"\x89PNG"


def test_screenshot_with_no_file_is_an_error(fake: FakeBrowser) -> None:
    fake.helpers["screenshot"] = None
    assert run(ops.run_op("screenshot", {}, fake.ctx()))["code"] == "screenshot_error"


def test_screenshot_dir_is_a_fixed_leaf_of_the_home(tmp_path: Path) -> None:
    assert ops.screenshot_dir(tmp_path) == tmp_path / "browser-snapshots"


# --- tabs ---------------------------------------------------------------------


def test_tabs_list_shows_only_the_agents_targets(fake: FakeBrowser) -> None:
    fake.helpers["list_tabs"] = [
        {"targetId": "T-agent", "url": "https://a/", "title": "A"},
        {"targetId": "T-user", "url": "https://bank/", "title": "Bank"},
    ]
    out = run(ops.run_op("tabs", {}, fake.ctx()))
    assert out == {
        "ok": True,
        "tabs": [{"target_id": "T-agent", "url": "https://a/", "title": "A"}],
    }


def test_tabs_switch_refuses_a_tab_the_agent_did_not_open(fake: FakeBrowser) -> None:
    out = run(ops.run_op("tabs", {"action": "switch", "target_id": "T-user"}, fake.ctx()))
    assert out["code"] == "not_agent_tab"
    assert "switch_tab" not in fake.helper_names()
    out = run(ops.run_op("tabs", {"action": "switch", "target_id": "T-agent"}, fake.ctx()))
    assert out == {"ok": True, "target_id": "T-agent"}
    assert fake.calls[-1] == ("switch_tab", {"target": "T-agent"})


def test_tabs_new_opens_a_web_url_or_blank(fake: FakeBrowser) -> None:
    fake.helpers["new_tab"] = "T-new"
    out = run(ops.run_op("tabs", {"action": "new", "url": "https://x/"}, fake.ctx()))
    assert out == {"ok": True, "target_id": "T-new"}
    assert run(ops.run_op("tabs", {"action": "new"}, fake.ctx()))["ok"] is True
    assert fake.calls[-1] == ("new_tab", {"url": "about:blank"})
    assert (
        run(ops.run_op("tabs", {"action": "new", "url": "file:///x"}, fake.ctx()))["code"]
        == "bad_url"
    )
    assert run(ops.run_op("tabs", {"action": "close"}, fake.ctx()))["code"] == "bad_action"


# --- availability -------------------------------------------------------------


def test_browsing_available_fails_closed_without_the_install_plane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys
    import types

    monkeypatch.setitem(sys.modules, "kiro_crew.browser_harness.install", None)
    assert ops.browsing_available(tmp_path) is False
    fake_install = types.ModuleType("kiro_crew.browser_harness.install")
    fake_install.install_state = lambda home: types.SimpleNamespace(installed=True)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "kiro_crew.browser_harness.install", fake_install)
    assert ops.browsing_available(tmp_path) is True


def test_press_key_refuses_a_printable_key_into_a_password_field(fake: FakeBrowser) -> None:
    """A password cannot be spelled one key at a time: the same refusal as ``type``."""
    fake.page["activeField"] = {"focused": True, "password": True, "tag": "input"}
    for key in ("p", " ", "Space", "1"):
        out = run(ops.run_op("press_key", {"key": key}, fake.ctx()))
        assert out["code"] == "password_field", key
    assert "press_key" not in fake.helper_names()
    assert "p" not in [c for c in fake.calls if c and c[0] == "press_key"]


def test_press_key_still_lets_the_agent_leave_a_password_field(fake: FakeBrowser) -> None:
    """Named keys and Ctrl/Meta chords insert no text, so Tab/Escape/Ctrl+A stay allowed."""
    fake.page["activeField"] = {"focused": True, "password": True, "tag": "input"}
    for args in (
        {"key": "Tab"},
        {"key": "Escape"},
        {"key": "Enter"},
        {"key": "a", "modifiers": ["control"]},
        {"key": "a", "modifiers": 4},
        {"key": "Tab", "modifiers": ["shift"]},
        {"key": "ArrowUp", "modifiers": ["alt"]},
    ):
        out = run(ops.run_op("press_key", args, fake.ctx()))
        assert out["ok"] is True, args
    assert fake.helper_names().count("press_key") == 7


def test_press_key_asks_the_page_only_for_printable_keys(fake: FakeBrowser) -> None:
    fake.page["activeField"] = {"focused": True, "password": False, "tag": "input"}
    run(ops.run_op("press_key", {"key": "x"}, fake.ctx()))
    assert any("activeField" in str(c) for c in fake.calls)
    fake.calls.clear()
    run(ops.run_op("press_key", {"key": "ArrowDown"}, fake.ctx()))
    assert not any("activeField" in str(c) for c in fake.calls)


def test_a_field_the_person_filled_takes_no_key_and_no_copy_chord(fake: FakeBrowser) -> None:
    """A quick-ask field: no character, and no select-all / copy / cut chord either, so
    its value cannot be carried into a field the agent reads. Tab still leaves it."""
    fake.page["activeField"] = {"focused": True, "password": False, "asked": True, "tag": "input"}
    for args in (
        {"key": "x"},
        {"key": "a", "modifiers": ["control"]},
        {"key": "c", "modifiers": ["control"]},
        {"key": "c", "modifiers": ["meta"]},
        {"key": "x", "modifiers": ["meta"]},
        {"key": "Home", "modifiers": ["shift"]},
        {"key": "Delete", "modifiers": ["shift"]},
        {"key": "ArrowLeft", "modifiers": ["shift"]},
    ):
        out = run(ops.run_op("press_key", args, fake.ctx()))
        assert out["code"] == "asked_field", args
    assert "press_key" not in fake.helper_names()
    assert run(ops.run_op("press_key", {"key": "Tab"}, fake.ctx()))["ok"] is True
    out = run(ops.run_op("type", {"text": "x"}, fake.ctx()))
    assert out["code"] == "asked_field"


def test_type_by_ref_into_a_field_the_person_filled_is_refused(fake: FakeBrowser) -> None:
    fake.page["focus"] = {"ok": False, "code": "asked_field"}
    out = run(ops.run_op("type", {"ref": "e3", "text": "x"}, fake.ctx()))
    assert out["code"] == "asked_field"
    assert "type_text" not in fake.helper_names()


def _gated_ctx(fake: FakeBrowser, gate: Any) -> ops.OpContext:
    ctx = fake.ctx()
    ctx.url_allowed = gate
    return ctx


def test_navigate_judges_the_url_the_page_landed_on_not_only_the_one_asked_for(
    fake: FakeBrowser,
) -> None:
    """The shim's gate is one hop deep; the gateway re-applies it to the COMMITTED URL,
    so a public page that 302s to loopback or link-local is refused and left."""
    _committing_page(fake, "http://127.0.0.1:5173/admin")
    fake.helpers["goto"] = {"frameId": "F", "loaderId": "L"}
    seen: list[str] = []

    def gate(url: str) -> bool:
        seen.append(url)
        return not url.startswith("http://127.0.0.1")

    out = run(ops.run_op("navigate", {"url": "https://public.example/r"}, _gated_ctx(fake, gate)))
    assert out["ok"] is False and out["code"] == "redirect_refused"
    assert "127.0.0.1:5173" in out["error"] and "about:blank" in out["error"]
    assert seen == ["http://127.0.0.1:5173/admin"]
    assert fake.calls[-1] == ("goto", {"url": "about:blank"}), "the landed page is left"
    assert "consoleMessages" not in fake.page_methods(), "nothing more is read off that page"
    # The same gate lets a public landing through untouched.
    fake.calls.clear()
    _committing_page(fake, "https://public.example/home")
    out = run(ops.run_op("navigate", {"url": "https://public.example/r"}, _gated_ctx(fake, gate)))
    assert out["ok"] is True and out["url"] == "https://public.example/home"
    assert ("goto", {"url": "about:blank"}) not in fake.calls


def test_back_judges_where_history_landed_too(fake: FakeBrowser) -> None:
    _committing_page(fake, "http://169.254.169.254/latest")
    fake.page["back"] = {"ok": True}
    gate = lambda url: not url.startswith("http://169.254.")  # noqa: E731
    out = run(ops.run_op("back", {}, _gated_ctx(fake, gate)))
    assert out["ok"] is False and out["code"] == "redirect_refused"


def test_the_default_context_admits_every_landing_and_the_route_wires_the_real_gate() -> None:
    """``OpContext.url_allowed`` defaults open (tests, no gateway); the route binds the
    shared predicate so the gateway and the shim judge by ONE rule."""
    from kiro_crew.browser_harness import url_gate
    from kiro_crew.dashboard.handlers import browser_ops as route
    from kiro_crew.mcp_tools import browser as shim

    async def _noop(*_a: Any, **_k: Any) -> None:
        return None

    assert ops.OpContext(helper=_noop).url_allowed("http://127.0.0.1:1/") is True
    assert shim._navigate_target_is_public("http://127.0.0.1:5173/") is False
    assert shim._navigate_target_is_public("https://example.com/") is True
    assert route.committed_url_allowed("https://example.com/") is True
    assert route.committed_url_allowed("http://169.254.169.254/latest") is False
    assert route.committed_url_allowed("http://localhost:5173/") is False, "unlisted loopback"
    listed = frozenset({"localhost:5173", "localhost:6776"})
    assert url_gate.target_is_allowed_local("http://localhost:5173/", listed, 6776)
    assert not url_gate.target_is_allowed_local("http://localhost:5173/", listed, None)
    assert not url_gate.target_is_allowed_local("http://localhost:6776/", listed, 6776)


@pytest.mark.parametrize("op", [op for op in ops.OPS if op not in {"navigate", "tabs", "ask"}])
def test_current_private_page_is_refused_before_reads_actions_or_decisions(fake, op) -> None:
    from kiro_crew.browser_harness.url_gate import url_allowed

    fake.helpers["current_tab"] = {"url": "http://127.0.0.1:5173/admin"}
    ctx = _gated_ctx(fake, lambda url: url_allowed(url, frozenset(), 6776))

    class Decisions:
        async def run(self, *_args):
            pytest.fail("a decision must not read a refused page")

    ctx.decisions = Decisions()
    out = run(ops.run_op(op, {"x": 1, "y": 2, "text": "x"}, ctx))
    assert out["code"] == "redirect_refused"
    assert fake.calls == [("current_tab", {}), ("goto", {"url": "about:blank"})]


def test_link_to_private_page_is_refused_before_the_card_follows(fake) -> None:
    from kiro_crew.browser_harness.url_gate import url_allowed

    tab = {"url": "https://example.com/"}
    fake.helpers["current_tab"] = lambda: dict(tab)
    fake.helpers["click"] = lambda **_kw: tab.update(url="http://169.254.169.254/latest")
    ctx = _gated_ctx(fake, lambda url: url_allowed(url, frozenset(), 6776))
    out = run(ops.run_op("click", {"x": 1, "y": 2}, ctx))
    assert out["code"] == "redirect_refused"
    assert fake.calls[-1] == ("goto", {"url": "about:blank"})
