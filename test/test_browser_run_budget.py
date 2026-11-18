"""``browser.run.max_repeats`` (default 0, off): the op that would repeat the last one
too often is refused with a report the model reads and never reaches the browser. The
count is per chat and restarts on a takeover or when the chat's session ends."""

from __future__ import annotations

import importlib
import json
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import make_mocked_request

from kiro_crew.browser_harness import live_hub as hub_mod
from kiro_crew.config import KiroCrewConfig
from kiro_crew.dashboard.handlers import browser_ops as mod
from kiro_crew.mcp_tools import browser as browser_tool


@pytest.fixture(autouse=True)
def _quiet_sel(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    _floor_monkeypatch.setattr(mod, "_sel", lambda: MagicMock())


@pytest.fixture(autouse=True)
def _fresh_hub() -> Iterator[None]:
    hub_mod.reset_live_hub()
    yield
    hub_mod.reset_live_hub()


def check(count: Any, name: str, args: dict[str, Any], max_repeats: int) -> Any:
    """``run_budget.check``, imported at call time so the route tests still collect on main."""
    budget = importlib.import_module("kiro_crew.browser_harness.run_budget")
    return budget.check(count, name, args, max_repeats=max_repeats)


def new_count() -> Any:
    return importlib.import_module("kiro_crew.browser_harness.run_budget").RunCount()


class Supervisor:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.relay = MagicMock()
        self.relay.agent_targets.return_value = ["T1"]
        self.relay.context_of.return_value = ""  # every tab in the default context

    def status(self) -> dict[str, Any]:
        return {"browser": {"kind": "kiro_chromium", "headless": True}, "connection": "connected"}

    async def call(self, helper: str, **kwargs: Any) -> Any:
        self.calls.append(helper)
        if helper == "current_tab":
            return {"targetId": "T1", "url": "https://T1", "title": "t"}
        if helper == "page_info":
            return {"w": 800, "h": 600, "dialog": None}
        if helper in ("js", "js_isolated"):
            return {"url": "https://T1", "title": "t", "nodes": []}
        return {}


@pytest.fixture
def sup(monkeypatch: pytest.MonkeyPatch) -> Supervisor:
    supervisor = Supervisor()

    async def _sup(hub: Any, key: str) -> Any:
        return supervisor

    async def _start(self: Any, key: str, relay: Any, target: str, **kw: Any) -> None:
        self._session(key)

    monkeypatch.setattr(mod, "_supervisor", _sup)
    monkeypatch.setattr(hub_mod.LiveHub, "start_stream", _start)
    return supervisor


async def op(name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    req = make_mocked_request("POST", "/api/browser/op")
    req["internal_auth"] = True
    req._read_bytes = json.dumps({"op": name, "args": args or {}, "session_key": "chat-a"}).encode()
    answer: dict[str, Any] = json.loads((await mod.api_browser_op(req)).body)
    return answer


# --- the counting, on its own ------------------------------------------------------


def test_the_repeat_detector_stops_the_same_op_on_the_same_ref() -> None:
    count = new_count()
    for _ in range(3):
        assert check(count, "click", {"ref": "e5"}, 3) is None
    stopped = check(count, "click", {"ref": "e5"}, 3)
    assert stopped["ok"] is False and stopped["code"] == "run_budget"
    assert "browser.run.max_repeats" in stopped["error"] and "3 times in a row" in stopped["error"]
    assert "was not run" in stopped["error"] and "start a new chat" in stopped["error"]
    assert check(count, "click", {"ref": "e5"}, 3) is not None, "stays stopped until a different op"


def test_a_different_ref_or_op_starts_the_run_again() -> None:
    count = new_count()
    steps = [("click", {"ref": "e1"}), ("click", {"ref": "e2"}), ("hover", {"ref": "e2"})]
    steps += [("click", {"x": 10, "y": 20}), ("click", {"x": 10, "y": 21})]
    for name, args in steps:
        for _ in range(2):
            assert check(count, name, args, 2) is None, (name, args)


def test_typing_different_text_is_not_a_repeat_and_no_text_is_kept() -> None:
    count = new_count()
    for text in ("hunter2-secret", "b", "c", "d"):
        assert check(count, "type", {"ref": "e1", "text": text}, 2) is None
    assert "hunter2" not in repr(count)


def test_off_never_stops_and_counts_nothing() -> None:
    count = new_count()
    for _ in range(200):
        assert check(count, "click", {"ref": "e1"}, 0) is None
    assert count == new_count()


# --- the gateway's op route ----------------------------------------------------------


@pytest.mark.asyncio
async def test_with_the_setting_off_the_route_answers_as_it_always_did(sup: Supervisor) -> None:
    """Passes on main: no setting, no counter, no refusal."""
    for _ in range(25):
        assert (await op("snapshot"))["ok"] is True
    for _ in range(25):
        assert (await op("click", {"ref": "nope"}))["code"] == "bad_ref"


@pytest.mark.asyncio
async def test_a_repeated_scroll_is_stopped_and_a_new_distance_goes_on(
    sup: Supervisor, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mod, "_run_limits", lambda: 2)
    for _ in range(2):
        assert (await op("scroll", {"dy": 800}))["ok"] is True
    seen = list(sup.calls)
    stopped = await op("scroll", {"dy": 800})
    assert stopped["code"] == "run_budget" and "browser.run.max_repeats" in stopped["error"]
    assert "js" not in sup.calls[len(seen) :], "the refused op never ran"
    assert (await op("scroll", {"dy": 400}))["ok"] is True


@pytest.mark.asyncio
async def test_a_takeover_starts_the_count_again(
    sup: Supervisor, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mod, "_run_limits", lambda: 1)
    hub = hub_mod.get_live_hub()
    assert (await op("snapshot"))["ok"] is True
    assert (await op("snapshot"))["code"] == "run_budget"
    assert await hub.claim("chat-a")
    await hub.release("chat-a")
    assert (await op("snapshot"))["ok"] is True


@pytest.mark.asyncio
async def test_the_end_of_the_chats_session_starts_the_count_again(
    sup: Supervisor, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mod, "_run_limits", lambda: 1)
    assert (await op("snapshot"))["ok"] is True
    assert (await op("snapshot"))["code"] == "run_budget"
    with monkeypatch.context() as ended:
        ended.setattr(mod, "_live_handlers", lambda: SimpleNamespace(_current_relay=lambda: None))
        await mod._close_tabs_of(SimpleNamespace(_slots={}), "chat-a")
    assert (await op("snapshot"))["ok"] is True


# --- the setting ---------------------------------------------------------------------


@pytest.mark.parametrize("stored, expected", [(5, 5), (None, 0)])
def test_the_gateway_reads_the_setting_for_each_op(
    monkeypatch: pytest.MonkeyPatch, stored: int | None, expected: int
) -> None:
    """A set value is read as it is; one that cannot be read leaves the detector off."""
    cfg = KiroCrewConfig()
    cfg.browser.run.max_repeats = stored or 0

    def load(_cls: Any) -> Any:
        if stored is None:
            raise OSError("config unreadable")
        return cfg

    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(load))
    assert mod._run_limits() == expected


def test_the_setting_defaults_to_off_and_clamps_what_is_stored() -> None:
    browser = type(KiroCrewConfig().browser)
    assert browser().run.max_repeats == 0
    for stored, kept in (("30", 30), (-4, 0), (10**9, 10_000), (True, 0)):
        assert browser.from_raw({"run": {"max_repeats": stored}}).run.max_repeats == kept


def test_the_tool_hands_the_model_the_stop_report_as_its_result() -> None:
    stopped = {"ok": False, "code": "run_budget", "error": "repeated action stopped: `click`"}
    text = browser_tool._settle("click", 200, stopped)
    assert text is not None and text.startswith("Error (run_budget): repeated action")
