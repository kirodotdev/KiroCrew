"""The helper worker (``browser_harness/helper_worker.py``) run in-process.

The worker normally runs under the venv's interpreter; here its functions are called
directly with a fake ``helpers`` module, a fake ``browser_harness`` package and
in-memory stdin/stdout, so each request path is observed by its reply.
"""

from __future__ import annotations

import io
import json
import sys
import types
from typing import Any

import pytest

from kiro_crew.browser_harness import helper_worker


class _RecordingHelpers:
    """A ``helpers`` stand-in that records every ``cdp`` call it is given."""

    def __init__(self, *, resume_fails: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._resume_fails = resume_fails

    def cdp(self, method: str, **params: Any) -> None:
        self.calls.append((method, params))
        if method == "Debugger.resume" and self._resume_fails:
            raise RuntimeError("Debugger is not paused")

    def methods(self) -> list[str]:
        return [method for method, _params in self.calls]


def test_mouse_move_hovers_the_point_it_was_given() -> None:
    helpers = _RecordingHelpers()
    reply = helper_worker._handle(
        helpers, {"id": 1, "helper": "mouse_move", "kwargs": {"x": 10, "y": 20}}
    )
    assert reply == {"id": 1, "ok": True, "result": {"x": 10, "y": 20}}
    assert helpers.calls == [("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": 10, "y": 20})]


def test_drag_presses_moves_in_steps_and_releases_at_the_target() -> None:
    helpers = _RecordingHelpers()
    reply = helper_worker._handle(
        helpers,
        {"id": 2, "helper": "drag", "kwargs": {"x": 0, "y": 0, "to_x": 80, "to_y": 40}},
    )
    assert reply["ok"] is True
    assert reply["result"] == {"from": [0, 0], "to": [80, 40]}
    types_seen = [params["type"] for _m, params in helpers.calls]
    assert types_seen[0] == "mouseMoved"
    assert types_seen[1] == "mousePressed"
    assert types_seen[-1] == "mouseReleased"
    moves = [p for _m, p in helpers.calls[2:-1]]
    assert len(moves) == 8 and all(p["type"] == "mouseMoved" for p in moves)
    assert (moves[-1]["x"], moves[-1]["y"]) == (80, 40)
    assert (helpers.calls[-1][1]["x"], helpers.calls[-1][1]["y"]) == (80, 40)


def test_page_clock_hold_sets_a_breakpoint_on_every_page_callback() -> None:
    helpers = _RecordingHelpers()
    reply = helper_worker._handle(
        helpers, {"id": 3, "helper": "page_clock", "kwargs": {"state": "hold"}}
    )
    assert reply == {"id": 3, "ok": True, "result": {"state": "hold"}}
    assert helpers.methods()[0] == "Debugger.enable"
    names = [p["eventName"] for m, p in helpers.calls[1:]]
    assert names == list(helper_worker._HELD_CALLBACKS)


def test_page_clock_run_removes_breakpoints_and_tolerates_a_page_not_paused() -> None:
    helpers = _RecordingHelpers(resume_fails=True)
    reply = helper_worker._handle(
        helpers, {"id": 4, "helper": "page_clock", "kwargs": {"state": "run"}}
    )
    assert reply == {"id": 4, "ok": True, "result": {"state": "run"}}
    assert "Debugger.resume" in helpers.methods()
    assert "Debugger.disable" not in helpers.methods(), "run keeps the debugger attached"


def test_page_clock_release_also_detaches_the_debugger() -> None:
    helpers = _RecordingHelpers()
    reply = helper_worker._handle(
        helpers, {"id": 5, "helper": "page_clock", "kwargs": {"state": "release"}}
    )
    assert reply["ok"] is True
    assert helpers.methods()[-1] == "Debugger.disable"


def test_page_clock_refuses_an_unknown_state_as_an_error_reply() -> None:
    helpers = _RecordingHelpers()
    reply = helper_worker._handle(
        helpers, {"id": 6, "helper": "page_clock", "kwargs": {"state": "pause"}}
    )
    assert reply["ok"] is False and "page_clock state must be one of" in reply["error"]
    assert helpers.calls == []


def test_a_helper_the_harness_release_lacks_is_reported_by_name() -> None:
    helpers = types.SimpleNamespace(goto_url="not callable")
    reply = helper_worker._handle(helpers, {"id": 7, "helper": "page_info"})
    assert reply["ok"] is False
    assert "helper missing in this Harness release: page_info" in reply["error"]
    reply = helper_worker._handle(helpers, {"id": 8, "helper": "goto", "kwargs": {"url": "x"}})
    assert reply["ok"] is False and "goto_url" in reply["error"]


def test_kwargs_that_are_not_an_object_are_refused() -> None:
    helpers = types.SimpleNamespace(page_info=lambda: {"url": "about:blank"})
    reply = helper_worker._handle(helpers, {"id": 9, "helper": "page_info", "kwargs": [1, 2]})
    assert reply == {"id": 9, "ok": False, "error": "ValueError: kwargs must be an object"}
    # A missing kwargs is the same as an empty object.
    assert helper_worker._handle(helpers, {"id": 10, "helper": "page_info"})["ok"] is True
    extra = helper_worker._handle(
        helpers, {"id": 13, "helper": "page_info", "kwargs": {"target": "t"}}
    )
    assert extra["ok"] is False and "unexpected arguments ['target']" in extra["error"]


def test_a_long_error_is_capped() -> None:
    def boom() -> None:
        raise RuntimeError("e" * (helper_worker._ERROR_CAP * 2))

    helpers = types.SimpleNamespace(page_info=boom)
    reply = helper_worker._handle(helpers, {"id": 11, "helper": "page_info"})
    assert reply["ok"] is False and len(reply["error"]) == helper_worker._ERROR_CAP


def test_a_result_json_cannot_carry_is_sent_as_its_text() -> None:
    marker = object()
    helpers = types.SimpleNamespace(current_tab=lambda: marker)
    reply = helper_worker._handle(helpers, {"id": 12, "helper": "current_tab"})
    assert reply == {"id": 12, "ok": True, "result": str(marker)}
    assert helper_worker._jsonable({"a": [1, 2]}) == {"a": [1, 2]}
    assert helper_worker._jsonable({1, 2}) == str({1, 2})


# ── main(): the request loop over stdin/stdout ──


def _run_main(
    monkeypatch: pytest.MonkeyPatch, stdin_text: str, helpers: Any | None
) -> tuple[int, list[dict[str, Any]]]:
    """Run ``main()`` with *helpers* importable as ``browser_harness.helpers``."""
    if helpers is None:
        # A package with no ``helpers`` attribute: ``from ... import helpers`` fails.
        package = types.ModuleType("browser_harness")
        package.__path__ = []  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "browser_harness", package)
        monkeypatch.delitem(sys.modules, "browser_harness.helpers", raising=False)
    else:
        package = types.ModuleType("browser_harness")
        package.__path__ = []  # type: ignore[attr-defined]
        package.helpers = helpers  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "browser_harness", package)
        monkeypatch.setitem(sys.modules, "browser_harness.helpers", helpers)
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin_text))
    monkeypatch.setattr(sys, "stdout", out)
    code = helper_worker.main()
    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    return code, replies


def test_main_answers_each_line_in_order_and_exits_cleanly_at_eof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helpers = types.ModuleType("browser_harness.helpers")
    helpers.goto_url = lambda url: {"url": url}  # type: ignore[attr-defined]
    lines = "\n".join(
        [
            json.dumps({"id": 1, "ping": True}),
            "",
            "   ",
            "{not json",
            json.dumps([1, 2, 3]),
            json.dumps({"id": 2, "helper": "goto", "kwargs": {"url": "https://a.test"}}),
            json.dumps({"id": 3, "helper": "http_get", "kwargs": {"url": "x"}}),
        ]
    )
    code, replies = _run_main(monkeypatch, lines + "\n", helpers)
    assert code == 0
    assert replies[0] == {"id": 1, "ok": True, "result": {"pong": True}}
    assert replies[1] == {"id": None, "ok": False, "error": "invalid JSON"}
    # A JSON value that is not an object is handled as an empty request.
    assert replies[2]["id"] is None and replies[2]["ok"] is False
    assert "helper not allowed" in replies[2]["error"]
    assert replies[3] == {"id": 2, "ok": True, "result": {"url": "https://a.test"}}
    assert replies[4]["id"] == 3 and "not allowed: http_get" in replies[4]["error"]
    assert len(replies) == 5, "blank lines get no reply"


def test_main_reports_a_missing_harness_on_the_pipe_and_exits_1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code, replies = _run_main(monkeypatch, json.dumps({"id": 1, "ping": True}) + "\n", None)
    assert code == 1
    assert len(replies) == 1
    assert replies[0]["id"] is None and replies[0]["ok"] is False
    assert replies[0]["error"].startswith("import failed:")
