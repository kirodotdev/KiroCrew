"""``PagePort`` and ``run_browser_op``: an app's browser calls go through the Bot's door.

RFC ``rfc-app-sdk-browser.md`` step 1b. Pinned here, each going red when the
extraction (one gateway function for the route and the SDK) is skipped:

- a ``navigate`` to the metadata address is refused before any request leaves the
  gateway, for the SDK and for a direct POST to the route alike;
- while a Bot op holds the gateway-wide lock, an SDK ``click`` waits for it;
- with ``capabilities.browse`` off every ``PagePort`` method answers
  ``policy_denied`` and the engine is stopped, and the check is asked for the slot's
  own session key, never the app's name;
- ``foo`` cannot reach ``foo-bar-1`` (key prefix AND stamp), and a verb the owner did
  not approve answers ``permission_denied``;
- a takeover answers ``user_holds`` without parking the slot.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any
from unittest.mock import MagicMock

import pytest
from test_browser_ops_route import _no_stream, _request, _Supervisor

from kiro_crew.apps import browser_page_port as port_mod
from kiro_crew.apps.browser_page_port import PagePort
from kiro_crew.browser_harness import live_hub as hub_mod
from kiro_crew.dashboard.handlers import browser_ops as mod
from kiro_crew.dashboard.handlers import browser_policy as policy
from kiro_crew.testing.wait import await_until, until_parked

pytestmark = pytest.mark.asyncio

_ALL = ("view", "read", "act")


@pytest.fixture(autouse=True)
def _quiet(_floor_monkeypatch: pytest.MonkeyPatch) -> Any:
    _floor_monkeypatch.setattr(mod, "_sel", lambda: MagicMock())
    hub_mod.reset_live_hub()
    yield
    hub_mod.reset_live_hub()


class _Slot:
    def __init__(self, key: str, app: str) -> None:
        self.key = key
        self._app = app


class _State:
    def __init__(self, *slots: _Slot) -> None:
        self._slots = {s.key: s for s in slots}

    def get_slot(self, key: str) -> Any:
        return self._slots.get(key)


def _sup(monkeypatch: pytest.MonkeyPatch, sup: Any) -> list[str]:
    """Install *sup* as the gateway's supervisor; return the keys it was started for."""
    started: list[str] = []

    async def _get(hub: Any, key: str) -> Any:
        started.append(key)
        return sup

    monkeypatch.setattr(mod, "_supervisor", _get)
    return started


def _port(app: str = "foo", key: str = "foo-1", verbs: Any = _ALL, state: Any = None) -> PagePort:
    return PagePort(app, key, verbs=verbs, state=state or _State(_Slot(key, app)))


# ── one door: the URL gate and the lock ─────────────────────────────────────


async def test_an_sdk_navigate_to_the_metadata_address_never_leaves_the_gateway(monkeypatch):
    sup = _Supervisor()
    started = _sup(monkeypatch, sup)
    out = await _port().navigate("http://169.254.169.254/latest/meta-data/")
    assert out["ok"] is False and out["code"] == "url_blocked"
    # Refused before the browser was even started: nothing was requested.
    assert started == [] and sup.calls == []


async def test_a_direct_post_to_the_route_is_refused_by_the_same_gate(monkeypatch):
    sup = _Supervisor()
    started = _sup(monkeypatch, sup)
    resp = await mod.api_browser_op(
        _request({"op": "navigate", "session_key": "k", "args": {"url": "http://169.254.169.254/"}})
    )
    assert resp.status == 200 and json.loads(resp.body)["code"] == "url_blocked"
    assert started == [] and sup.calls == []
    # A `tabs new` that opens such a page is the same request.
    resp = await mod.api_browser_op(
        _request(
            {"op": "tabs", "session_key": "k", "args": {"action": "new", "url": "http://10.0.0.1/"}}
        )
    )
    assert json.loads(resp.body)["code"] == "url_blocked" and sup.calls == []
    # An address the URL parser cannot read is refused the same way, never a 500.
    resp = await mod.api_browser_op(
        _request({"op": "navigate", "session_key": "k", "args": {"url": "http://[::1"}})
    )
    assert resp.status == 200 and json.loads(resp.body)["code"] == "url_blocked"
    assert started == [] and sup.calls == []


async def test_an_sdk_click_waits_for_the_lock_a_bot_op_holds(monkeypatch):
    sup = _Supervisor()
    _sup(monkeypatch, sup)
    _no_stream(monkeypatch)
    await mod._OP_LOCK.acquire()  # the Bot's op, mid-run
    try:
        click = asyncio.ensure_future(_port().click("e1"))
        await until_parked(mod._OP_LOCK, describe=lambda: "the SDK click awaits the lock")
        assert sup.calls == [], "nothing of the SDK op ran inside the Bot's"
    finally:
        mod._OP_LOCK.release()
    out = await await_until(click, what="the SDK click")
    assert sup.calls, "it ran once the lock was free"
    assert isinstance(out, dict) and "ok" in out


# ── the on/off switch, for the slot's own key ──────────────────────────────


@pytest.fixture
def deny(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A deny for the session keys in ``state["keys"]``; records each key asked and each stop."""
    state: dict[str, Any] = {"keys": set(), "asked": [], "stops": 0}

    async def _denied(session_key: str = "", *, audit: bool = True) -> bool:
        state["asked"].append(session_key)
        return session_key in state["keys"]

    def _stop() -> None:
        state["stops"] += 1

    for target in (mod, policy):
        monkeypatch.setattr(target, "browse_denied_async", _denied)
        monkeypatch.setattr(target, "stop_running_harness", _stop)
    return state


def _every_method(port: PagePort) -> list[Any]:
    return [
        port.aim(1, 2),
        port.view_state(),
        port.tabs(),
        port.outline(),
        port.screenshot(),
        port.navigate("https://example.com/"),
        port.click("e1"),
        port.type("e1", "x"),
    ]


async def test_with_browsing_off_every_method_is_policy_denied_and_the_engine_stops(
    monkeypatch, deny
):
    sup = _Supervisor()
    started = _sup(monkeypatch, sup)
    deny["keys"] = {"dashboard:foo-1"}
    methods = _every_method(_port())
    for call in methods:
        out = await call
        assert out["ok"] is False and out["code"] == "policy_denied", out
    assert deny["stops"] >= len(methods)
    assert started == [] and sup.calls == []


async def test_the_switch_is_asked_for_the_slots_session_key_never_the_apps_name(monkeypatch, deny):
    _sup(monkeypatch, _Supervisor())
    _no_stream(monkeypatch)
    deny["keys"] = {"foo"}  # a profile bound to the app's name must not be the one asked
    out = await _port().tabs()
    assert out["ok"] is True
    assert "foo" not in deny["asked"] and "dashboard:foo-1" in deny["asked"]


# ── which slots, which verbs ────────────────────────────────────────────────


async def test_foo_cannot_reach_foo_bar_1(monkeypatch):
    sup = _Supervisor()
    _sup(monkeypatch, sup)
    state = _State(_Slot("foo-bar-1", "foo-bar"), _Slot("foo-2", "foo-bar"))
    # The prefix matches (``foo-`` + ``bar-1``), the stamp does not.
    for out in [
        await PagePort("foo", "foo-bar-1", verbs=_ALL, state=state).navigate("https://e.com/"),
        await PagePort("foo", "foo-bar-1", verbs=_ALL, state=state).aim(1, 1),
        # The stamp is checked even where the key is spelled as foo's.
        await PagePort("foo", "foo-2", verbs=_ALL, state=state).tabs(),
        # A slot that is not live answers the same.
        await PagePort("foo", "foo-9", verbs=_ALL, state=state).tabs(),
    ]:
        assert out["code"] == "not_your_slot", out
    assert sup.calls == []
    _no_stream(monkeypatch)
    assert (await PagePort("foo-bar", "foo-bar-1", verbs=_ALL, state=state).tabs())["ok"]


async def test_a_verb_the_owner_did_not_approve_is_permission_denied(monkeypatch):
    sup = _Supervisor()
    _sup(monkeypatch, sup)
    port = _port(verbs={"read", "nonsense"})
    for out in [
        await port.navigate("https://e.com/"),
        await port.click("e1"),
        await port.aim(1, 1),
    ]:
        assert out == {"ok": False, "code": "permission_denied", "error": out["error"]}
    assert sup.calls == []


# ── takeover ────────────────────────────────────────────────────────────────


async def test_a_takeover_answers_user_holds_and_parks_nothing(monkeypatch):
    sup = _Supervisor()
    _sup(monkeypatch, sup)
    hub = hub_mod.get_live_hub()
    monkeypatch.setattr(hub, "in_control", lambda key: key == "foo-1")
    parked: list[str] = []
    monkeypatch.setattr(hub, "park", parked.append)
    out = await _port().click("e1")
    assert out["ok"] is False and out["code"] == "user_holds" and "hold" not in out
    assert parked == [] and sup.calls == []
    # The Bot's own call through the route still parks.
    await mod.api_browser_op(_request({"op": "click", "session_key": "foo-1", "args": {}}))
    assert parked == ["foo-1"]


async def test_an_app_op_leaves_the_persons_hand_back_note_for_the_bot(monkeypatch):
    sup = _Supervisor()
    _sup(monkeypatch, sup)
    _no_stream(monkeypatch)
    hub = hub_mod.get_live_hub()
    hub.turn_started("foo-1", "t1")  # the Bot's turn, its click parked on a takeover
    note = {"choice": "", "note": "do not click"}
    hub._session("foo-1").handback = note
    out = await _port().tabs()
    assert out["ok"] is True and "handed_back" not in out
    assert hub.get("foo-1").turn_id == "t1", "an app op joins the Bot's turn"
    # The Bot's next op is the one told.
    resp = await mod.api_browser_op(_request({"op": "snapshot", "session_key": "foo-1"}))
    assert json.loads(resp.body)["handed_back"] == note


async def test_an_app_op_opens_no_card_turn_nothing_would_end(monkeypatch):
    _sup(monkeypatch, _Supervisor())
    _no_stream(monkeypatch)
    assert (await _port().tabs())["ok"] is True
    sess = hub_mod.get_live_hub().get("foo-1")
    assert sess is None or not sess.turn_id


# ── reads ───────────────────────────────────────────────────────────────────


async def test_outline_is_cut_to_its_budget_and_screenshot_hands_back_bytes(monkeypatch, tmp_path):
    from kiro_crew.browser_harness.ops import screenshot_dir
    from kiro_crew.config.paths import config_dir

    shots = screenshot_dir(config_dir())
    shots.mkdir(parents=True, exist_ok=True)
    png = shots / "shot.png"
    png.write_bytes(b"\x89PNG-bytes")
    shown = {"path": str(png)}

    async def _op(self: PagePort, verb: str, op: str, args: dict[str, Any]) -> dict[str, Any]:
        if op == "snapshot":
            return {"ok": True, "url": "u", "title": "t", "snapshot": "x" * 50}
        return {"ok": True, "path": shown["path"]}

    monkeypatch.setattr(PagePort, "_op_raw", _op)
    port = _port()
    out = await port.outline(budget=10)
    assert out["snapshot"] == "x" * 10 and out["truncated"] is True
    assert (await port.outline())["snapshot"] == "x" * 50
    shot = await port.screenshot()
    assert shot == {"ok": True, "png": b"\x89PNG-bytes"}
    with monkeypatch.context() as mp:
        mp.setattr(port_mod, "SCREENSHOT_MAX_BYTES", 3)
        assert (await port.screenshot())["code"] == "screenshot_too_large"
    # An image outside the screenshot leaf is never read.
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"credentials")
    shown["path"] = str(secret)
    assert (await port.screenshot())["code"] == "screenshot_error"


@pytest.mark.skipif(sys.platform == "win32", reason="planting a symlink needs privilege there")
async def test_a_link_planted_where_the_screenshot_was_is_never_followed(monkeypatch, tmp_path):
    from kiro_crew.browser_harness.ops import screenshot_dir
    from kiro_crew.config.paths import config_dir

    shots = screenshot_dir(config_dir())
    shots.mkdir(parents=True, exist_ok=True)
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"credentials")
    png = shots / "planted.png"
    png.symlink_to(secret)

    async def _op(self: PagePort, verb: str, op: str, args: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "path": str(png)}

    monkeypatch.setattr(PagePort, "_op_raw", _op)
    assert (await _port().screenshot())["code"] == "screenshot_error"


async def test_a_screenshot_folder_replaced_by_a_link_is_never_read_through(monkeypatch, tmp_path):
    from kiro_crew.browser_snapshots import SNAPSHOT_DIR_NAME
    from kiro_crew.testing.links import make_dir_link

    home, elsewhere = tmp_path / "home", tmp_path / "elsewhere"
    home.mkdir()
    elsewhere.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    make_dir_link(home / SNAPSHOT_DIR_NAME, elsewhere)
    (elsewhere / "shot.png").write_bytes(b"\x89PNG-bytes")
    shown = str(home / SNAPSHOT_DIR_NAME / "shot.png")

    async def _op(self: PagePort, verb: str, op: str, args: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "path": shown}

    monkeypatch.setattr(PagePort, "_op_raw", _op)
    assert (await _port().screenshot())["code"] == "screenshot_error"


async def test_refusals_and_view_calls_are_audited_as_the_app(monkeypatch):
    rows: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        mod,
        "_audit",
        lambda request, outcome, error="": rows.append((request.get("user"), outcome, error)),
    )
    state = _State(_Slot("foo-1", "foo"), _Slot("foo-bar-1", "foo-bar"))
    await PagePort("foo", "foo-1", verbs={"read"}, state=state).navigate("https://e.com/")
    await PagePort("foo", "foo-bar-1", verbs=_ALL, state=state).tabs()
    await PagePort("foo", "foo-1", verbs=_ALL, state=state).aim(1, 1)
    assert rows == [
        ("app:foo", "denied", "permission_denied"),
        ("app:foo", "denied", "not_your_slot"),
        ("app:foo", "allowed", ""),
    ]


# ── view ────────────────────────────────────────────────────────────────────


async def test_aim_and_view_state_read_the_slots_own_live_card(monkeypatch):
    port = _port()
    assert await port.aim(10, 20, "here") == {"ok": True, "shown": False}
    assert (await port.view_state())["state"] == ""
    hub = hub_mod.get_live_hub()
    hub._session("foo-1").stream = MagicMock()
    hub.page_changed("foo-1", url="https://example.com/", title="Example")
    assert await port.aim(10, 20, "here") == {"ok": True, "shown": True}
    assert hub.get("foo-1").aim == {"x": 10.0, "y": 20.0, "label": "here", "seq": 1}
    state = await port.view_state()
    assert state["url"] == "https://example.com/" and state["user_holds"] is False
    # Another app's port draws nothing on this slot.
    other = PagePort("bar", "foo-1", verbs=_ALL, state=_State(_Slot("foo-1", "foo")))
    assert (await other.aim(1, 1))["code"] == "not_your_slot"
    assert hub.get("foo-1").aim["seq"] == 1
