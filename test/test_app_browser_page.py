"""``ctx.browser.page(slot)``: an app reads and operates its own slot's page.

``rfc-app-sdk-browser.md`` section 4. Each test goes red when its rule is removed:

- the success path runs ``pick``, ``locate`` and ``click`` through ``run_browser_op``
  (the Bot's door), on a port from a real ``build_app_context``;
- a verb the owner did not approve answers ``permission_denied``, and an approval
  withdrawn after the port was handed out applies to that port (re-read per call);
- the preview turned off answers ``preview_off`` on a port already held;
- the app turned off answers ``permission_denied`` on a port already held;
- another app's slot, or one that is not live, is refused (``not_your_slot``);
- ``pick`` and ``locate`` are an app's only: the Bot's route refuses them;
- the example app ``docs/app-kit/examples/browser-reader`` calls it.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from test_browser_ops_route import _no_stream, _request, _Supervisor

from kiro_crew.apps import browser_sdk, execution, manager_records
from kiro_crew.apps.context import build_app_context
from kiro_crew.apps.manager import (
    APP_MANIFEST_FILENAME,
    _read_installed,
    _write_installed,
    disable_app,
    enable_app,
    install_app,
)
from kiro_crew.browser_harness import live_hub as hub_mod
from kiro_crew.browser_harness import ops as harness_ops
from kiro_crew.dashboard.handlers import browser_ops as mod

pytestmark = pytest.mark.asyncio

_REPO = Path(__file__).resolve().parents[1]


class _Slot:
    def __init__(self, key: str, app: str) -> None:
        self.key = key
        self._app = app


class _State:
    def __init__(self, *slots: _Slot) -> None:
        self.slots = {s.key: s for s in slots}

    def get_slot(self, key: str) -> Any:
        return self.slots.get(key)


#: A credential the redactor matches whole; its first 19 characters it does not.
_TOKEN = "sk-ant-api03-" + "Ab3dE6gH9jK2mN5pQ8sT1vW4yZ7" * 3


class _Page(_Supervisor):
    """A supervisor whose walker answers ``pick`` and ``locate``."""

    picked = "$19.99"

    async def call(self, helper: str, **kwargs: Any) -> Any:
        expression = str(kwargs.get("expression", ""))
        if helper == "js_isolated" and re.search(r"\.pick\(", expression):
            self.calls.append((helper, {"method": "pick"}))
            return {
                "ok": True,
                "ref": "e7",
                "tag": "span",
                "text": self.picked,
                "box": {"x": 10, "y": 20, "w": 40, "h": 12},
            }
        if helper == "js_isolated" and re.search(r"\.locate\(", expression):
            self.calls.append((helper, {"method": "locate"}))
            return {
                "ok": True,
                "found": [{"text": "$19.99", "boxes": [{"x": 1, "y": 2, "w": 3, "h": 4}]}],
            }
        return await super().call(helper, **kwargs)


def _config(home: Path, *, preview: bool) -> None:
    (home / "config.json").write_text(
        json.dumps(
            {
                "agent": {"apps_allow_third_party": True},
                "dashboard": {"feature_previews": {"app_browser_sdk": preview}},
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture
def home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _floor_monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    home = tmp_path / "kirocrew-home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    _config(home, preview=True)
    monkeypatch.setattr(execution, "third_party_ceiling_closed", lambda name: None)
    _floor_monkeypatch.setattr(mod, "_sel", lambda: MagicMock())
    hub_mod.reset_live_hub()
    yield home
    hub_mod.reset_live_hub()


def _install(tmp_path: Path, declared: list[str], approved: list[str]) -> None:
    source = tmp_path / "source" / "pager"
    source.mkdir(parents=True)
    manifest = {
        "name": "pager",
        "version": "1.0.0",
        "displayName": "Pager",
        "description": "reads its own tabs",
        "permissions": {"chatSlots": True, "browser": declared},
    }
    (source / APP_MANIFEST_FILENAME).write_text(json.dumps(manifest), encoding="utf-8")
    assert install_app(source).ok
    meta = _read_installed("pager")
    assert meta is not None
    meta.enabled = True
    _write_installed("pager", meta)
    assert enable_app("pager", grants_consent={"browser": approved}).ok


def _sdk(tmp_path: Path, declared: list[str]) -> Any:
    ctx = build_app_context(
        "pager", tmp_path / "data", permissions={"chatSlots": True, "browser": declared}
    )
    assert ctx.browser is not None
    return ctx.browser


def _supervisor(monkeypatch: pytest.MonkeyPatch) -> _Page:
    sup = _Page()

    async def _get(hub: Any, key: str) -> Any:
        return sup

    monkeypatch.setattr(mod, "_supervisor", _get)
    _no_stream(monkeypatch)
    return sup


async def test_read_and_act_go_through_the_bots_door(home, tmp_path, monkeypatch):
    _install(tmp_path, ["read", "act"], ["read", "act"])
    sup = _supervisor(monkeypatch)
    doors: list[str] = []
    real = mod.run_browser_op

    async def _door(session_key: str, op: str, args: dict[str, Any], **kw: Any) -> Any:
        doors.append(f"{session_key} {op}")
        return await real(session_key, op, args, **kw)

    monkeypatch.setattr(mod, "run_browser_op", _door)
    page = _sdk(tmp_path, ["read", "act"]).page("pager-1", state=_State(_Slot("pager-1", "pager")))

    picked = await page.pick(12, 24)
    assert picked == {
        "ok": True,
        "ref": "e7",
        "tag": "span",
        "text": "$19.99",
        "box": {"x": 10.0, "y": 20.0, "w": 40.0, "h": 12.0},
        "viewport": None,
    }
    located = await page.locate(["$19.99"])
    assert located["found"] == [
        {"text": "$19.99", "boxes": [{"x": 1.0, "y": 2.0, "w": 3.0, "h": 4.0}]}
    ]
    await page.click("e7")
    assert doors == [
        "dashboard:pager-1 pick",
        "dashboard:pager-1 locate",
        "dashboard:pager-1 click",
    ]
    assert ("js_isolated", {"method": "pick"}) in sup.calls


async def test_a_verb_not_approved_or_withdrawn_is_permission_denied(home, tmp_path, monkeypatch):
    _install(tmp_path, ["read", "act"], ["read"])
    sup = _supervisor(monkeypatch)
    page = _sdk(tmp_path, ["read", "act"]).page("pager-1", state=_State(_Slot("pager-1", "pager")))
    for out in [await page.click("e1"), await page.navigate("https://example.com/")]:
        assert out["ok"] is False and out["code"] == "permission_denied", out
    assert sup.calls == []
    assert (await page.pick(1, 1))["ok"] is True
    # A verb withdrawn after the port was handed out applies to that same port.
    grants = manager_records._read_approved_grants("pager") or {}
    manager_records._write_approved_grants("pager", {**grants, "browser": []})
    assert browser_sdk.app_browser_verbs("pager") == ()
    assert (await page.pick(1, 1))["code"] == "permission_denied"


async def test_the_preview_turned_off_refuses_a_port_already_held(home, tmp_path, monkeypatch):
    _install(tmp_path, ["read"], ["read"])
    sup = _supervisor(monkeypatch)
    page = _sdk(tmp_path, ["read"]).page("pager-1", state=_State(_Slot("pager-1", "pager")))
    _config(home, preview=False)
    for out in [await page.pick(1, 1), await page.locate(["x"]), await page.tabs()]:
        assert out["ok"] is False and out["code"] == "preview_off", out
    assert sup.calls == []


async def test_the_app_turned_off_refuses_a_port_already_held(home, tmp_path, monkeypatch):
    _install(tmp_path, ["read"], ["read"])
    sup = _supervisor(monkeypatch)
    page = _sdk(tmp_path, ["read"]).page("pager-1", state=_State(_Slot("pager-1", "pager")))
    assert disable_app("pager").ok
    out = await page.pick(1, 1)
    assert out["ok"] is False and out["code"] == "permission_denied"
    assert sup.calls == []


async def test_only_the_apps_own_live_slot(home, tmp_path, monkeypatch):
    _install(tmp_path, ["read"], ["read"])
    sup = _supervisor(monkeypatch)
    sdk = _sdk(tmp_path, ["read"])
    rows: list[tuple[str, str, str]] = []
    state = _State(_Slot("pager-1", "pager"), _Slot("pager-bar-1", "pager-bar"), _Slot("x-1", "x"))
    with monkeypatch.context() as mp:
        mp.setattr(
            mod,
            "_audit",
            lambda request, outcome, error="": rows.append((request.get("user"), outcome, error)),
        )
        for key in ("pager-bar-1", "x-1", "pager-9"):
            with pytest.raises(browser_sdk.BrowserRefused) as refused:
                sdk.page(key, state=state)
            assert refused.value.code == "not_your_slot"
        with pytest.raises(browser_sdk.BrowserRefused) as refused:
            sdk.page("pager-1")  # no route state and no gateway Application
        assert refused.value.code == "no_state"
    # Each refusal is an audit row, as the app, like a PagePort refusal.
    assert rows == [("app:pager", "denied", "not_your_slot")] * 3 + [
        ("app:pager", "denied", "no_state")
    ]
    # A slot archived after the port was handed out answers on that port.
    page = sdk.page("pager-1", state=state)
    del state.slots["pager-1"]
    assert (await page.pick(1, 1))["code"] == "not_your_slot"
    assert sup.calls == []


async def test_pick_and_locate_are_never_the_bots(home, monkeypatch):
    sup = _supervisor(monkeypatch)
    assert not set(harness_ops.APP_OPS) & set(harness_ops.OPS)
    for op, args in (("pick", {"x": 1, "y": 1}), ("locate", {"texts": ["a"]})):
        resp = await mod.api_browser_op(
            _request({"op": op, "session_key": "pager-1", "args": args})
        )
        assert resp.status == 400 and json.loads(resp.body)["code"] == "bad_op"
        # A request-shaped caller on the shared function is refused the same way.
        body, status = await mod.run_browser_op(
            "dashboard:pager-1", op, args, caller=_request({"op": op})
        )
        assert status == 400 and body["code"] == "bad_op"
    assert sup.calls == []


async def test_bad_pick_and_locate_arguments_are_refused_before_the_page(
    home, tmp_path, monkeypatch
):
    _install(tmp_path, ["read"], ["read"])
    sup = _supervisor(monkeypatch)
    page = _sdk(tmp_path, ["read"]).page("pager-1", state=_State(_Slot("pager-1", "pager")))
    assert (await page.pick(-1, 5))["code"] == "bad_target"
    for texts in ([], [""], ["x" * 201], [1], ["a"] * 21):
        assert (await page.locate(texts))["code"] == "bad_texts", texts
    assert not [c for c in sup.calls if c[0] == "js_isolated" and c[1].get("method")]


async def test_the_example_app_reads_its_slot_through_ctx_browser(home, tmp_path, monkeypatch):
    _install(tmp_path, ["read", "act"], ["read", "act"])
    _supervisor(monkeypatch)
    spec = importlib.util.spec_from_file_location(
        "browser_reader_routes",
        _REPO / "docs/app-kit/examples/browser-reader/backend/routes.py",
    )
    assert spec is not None and spec.loader is not None
    routes = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, routes)  # its dataclass resolves its module
    spec.loader.exec_module(routes)
    handlers = {(r.method, r.path): r.handler for r in routes.register_routes(None)}
    ctx = build_app_context(
        "pager", tmp_path / "data", permissions={"chatSlots": True, "browser": ["read", "act"]}
    )
    state = _State(_Slot("pager-1", "pager"))
    request = MagicMock()
    request.app = {"state": state}
    request.match_info = {"slot": "pager-1"}
    resp = await handlers[("GET", "/slots/{slot}/outline")](request, ctx)
    assert json.loads(resp.body)["ok"] is True
    request.match_info = {"slot": "other-1"}
    resp = await handlers[("GET", "/slots/{slot}/outline")](request, ctx)
    assert json.loads(resp.body)["code"] == "not_your_slot"
    # An empty or malformed click body is a JSON answer, never a 500.
    request.match_info = {"slot": "pager-1"}
    request.json = AsyncMock(side_effect=json.JSONDecodeError("empty", "", 0))
    resp = await handlers[("POST", "/slots/{slot}/click")](request, ctx)
    assert json.loads(resp.body)["code"] == "bad_ref"


async def test_pick_locate_and_view_state_carry_the_viewport(home, tmp_path, monkeypatch):
    from kiro_crew.browser_harness.screencast import LiveStream

    _install(tmp_path, ["view", "read"], ["view", "read"])
    _supervisor(monkeypatch)
    page = _sdk(tmp_path, ["view", "read"]).page("pager-1", state=_State(_Slot("pager-1", "pager")))
    # No frame yet: no viewport to scale by.
    assert (await page.view_state())["viewport"] is None
    assert (await page.pick(1, 1))["viewport"] is None

    # The size comes from the frame's own metadata, not the frame's pixels.
    stream = LiveStream.__new__(LiveStream)
    stream.scale, stream.device_width, stream.device_height = 1.0, 0.0, 0.0
    stream._note_scale({"deviceWidth": 1280, "deviceHeight": 800}, 640)
    hub_mod.get_live_hub()._session("pager-1").stream = stream
    viewport = {"w": 1280.0, "h": 800.0}
    assert (await page.view_state())["viewport"] == viewport
    assert (await page.pick(1, 1))["viewport"] == viewport
    assert (await page.locate(["$19.99"]))["viewport"] == viewport
    # A refusal carries no viewport.
    assert "viewport" not in await page.locate([])


async def test_an_app_op_leaves_the_tab_move_notice_for_the_bot(home, tmp_path, monkeypatch):
    _install(tmp_path, ["read", "act"], ["read", "act"])
    _supervisor(monkeypatch)
    page = _sdk(tmp_path, ["read", "act"]).page("pager-1", state=_State(_Slot("pager-1", "pager")))
    hub = hub_mod.get_live_hub()
    hub._session("pager-1")
    moved = {"popup_closed": {"target_id": "T2", "returned_to": "T1"}}
    hub.note_tab_moved("pager-1", moved)

    # The app's stale ref is refused like the Bot's would be, and the answer tells it.
    refused = await page.click("e7")
    assert refused["code"] == "popup_closed" and "popup_closed" in refused
    # Told once, the app acts again on a fresh ref; the notice stays for the Bot.
    assert (await page.click("e7")).get("code") != "popup_closed"
    assert hub.pending_tab_move("pager-1") == moved
    # A new move is told to the app again.
    hub.note_tab_moved("pager-1", {"popup": {"target_id": "T3"}})
    assert (await page.click("e7"))["code"] == "popup_opened"
    assert (await page.click("e7")).get("code") != "popup_opened"
    resp = await mod.api_browser_op(_request({"op": "snapshot", "session_key": "pager-1"}))
    assert "popup_closed" in json.loads(resp.body)["result"]
    assert hub.pending_tab_move("pager-1") is None
    assert hub.pending_app_tab_move("pager-1") is None


async def test_an_app_op_does_not_count_against_the_bots_run_budget(home, tmp_path, monkeypatch):
    _install(tmp_path, ["read"], ["read"])
    _supervisor(monkeypatch)
    monkeypatch.setattr(mod, "_run_limits", lambda: (1, 0, 0))
    page = _sdk(tmp_path, ["read"]).page("pager-1", state=_State(_Slot("pager-1", "pager")))
    for _ in range(3):
        assert (await page.tabs())["ok"] is True
    # The Bot's one allowed op still runs, and the next is the one refused.
    first = await mod.api_browser_op(_request({"op": "snapshot", "session_key": "pager-1"}))
    assert json.loads(first.body).get("code") != "run_budget"
    second = await mod.api_browser_op(_request({"op": "snapshot", "session_key": "pager-1"}))
    assert json.loads(second.body).get("code") == "run_budget"


async def test_every_page_string_is_redacted_whole_before_any_cut(home, tmp_path, monkeypatch):
    from kiro_crew.apps.browser_page_port import PagePort
    from kiro_crew.browser_harness.ops import TEXT_CAP

    _install(tmp_path, ["view", "read"], ["view", "read"])
    sup = _supervisor(monkeypatch)
    page = _sdk(tmp_path, ["view", "read"]).page("pager-1", state=_State(_Slot("pager-1", "pager")))
    # pick: a name whose 200-character display cut would fall inside the credential.
    sup.picked = "a" * 180 + " " + _TOKEN
    text = (await page.pick(1, 1))["text"]
    assert _TOKEN[:19] not in text and len(text) <= 200

    # outline: redacted before the budget cut, and the op's own cut word withheld.
    shown = {"snapshot": ""}

    async def _raw(self: Any, verb: str, op: str, args: dict[str, Any]) -> dict[str, Any]:
        if op == "snapshot":
            return {"ok": True, "url": "u", "title": "t", "snapshot": shown["snapshot"]}
        return {
            "ok": True,
            "tabs": [{"target_id": "T1", "url": f"https://e.com/?access_token={_TOKEN}"}],
        }

    monkeypatch.setattr(PagePort, "_op_raw", _raw)
    shown["snapshot"] = "b" * 5980 + " " + _TOKEN
    assert _TOKEN[:19] not in (await page.outline(budget=6000))["snapshot"]
    many = (_TOKEN + " ") * (TEXT_CAP // (len(_TOKEN) + 1))
    shown["snapshot"] = (many + _TOKEN)[:TEXT_CAP]  # the op cut the last one short
    assert "sk-ant" not in (await page.outline(budget=6000))["snapshot"]

    # tabs: the address carries no credential; the tab id is kept as minted.
    tab = (await page.tabs())["tabs"][0]
    assert _TOKEN not in tab["url"] and tab["target_id"] == "T1"

    # view_state: the live card's address and title.
    hub_mod.get_live_hub().page_changed(
        "pager-1", url=f"https://e.com/cb?access_token={_TOKEN}", title=f"key {_TOKEN}"
    )
    state = await page.view_state()
    assert _TOKEN not in state["url"] and _TOKEN not in state["title"]


async def test_the_bot_taking_its_move_leaves_the_apps_refusal(home, tmp_path, monkeypatch):
    _install(tmp_path, ["act"], ["act"])
    _supervisor(monkeypatch)
    page = _sdk(tmp_path, ["act"]).page("pager-1", state=_State(_Slot("pager-1", "pager")))
    hub = hub_mod.get_live_hub()
    hub._session("pager-1")
    hub.note_tab_moved("pager-1", {"popup": {"target_id": "T2"}})
    # The Bot snapshots the popup first and is told.
    await mod.api_browser_op(_request({"op": "snapshot", "session_key": "pager-1"}))
    assert hub.pending_tab_move("pager-1") is None
    # The app's ref still belongs to the page it saw: refused, then told.
    assert (await page.click("e1"))["code"] == "popup_opened"
    assert (await page.click("e1")).get("code") != "popup_opened"


async def test_a_screenshot_that_cannot_be_read_is_audited(home, tmp_path, monkeypatch):
    from kiro_crew.apps.browser_page_port import PagePort

    _install(tmp_path, ["read"], ["read"])
    page = _sdk(tmp_path, ["read"]).page("pager-1", state=_State(_Slot("pager-1", "pager")))
    outside = tmp_path / "not-a-shot.png"
    outside.write_bytes(b"x")

    async def _raw(self: Any, verb: str, op: str, args: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "path": str(outside)}

    rows: list[tuple[str, str, str]] = []
    monkeypatch.setattr(PagePort, "_op_raw", _raw)
    monkeypatch.setattr(
        mod,
        "_audit",
        lambda request, outcome, error="": rows.append((request.get("user"), outcome, error)),
    )
    assert (await page.screenshot())["code"] == "screenshot_error"
    assert rows == [("app:pager", "error", "screenshot_error")]


async def test_every_sdk_refusal_writes_a_denied_audit_row_as_the_app(home, tmp_path, monkeypatch):
    _install(tmp_path, ["read", "act"], ["read"])
    sup = _supervisor(monkeypatch)
    rows: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        mod,
        "_audit",
        lambda request, outcome, error="": rows.append((request.get("user"), outcome, error)),
    )
    state = _State(_Slot("pager-1", "pager"))
    page = _sdk(tmp_path, ["read", "act"]).page("pager-1", state=state)

    def denied(code: str) -> tuple[str, str, str]:
        return ("app:pager", "denied", code)

    # A verb the owner did not approve.
    assert (await page.click("e1"))["code"] == "permission_denied"
    assert rows[-1] == denied("permission_denied")
    # The preview turned off.
    _config(home, preview=False)
    assert (await page.pick(1, 1))["code"] == "preview_off"
    assert rows[-1] == denied("preview_off")
    _config(home, preview=True)
    # The app turned off.
    assert disable_app("pager").ok
    assert (await page.pick(1, 1))["code"] == "permission_denied"
    assert rows[-1] == denied("permission_denied")
    assert enable_app("pager").ok
    # A slot that is not the app's live slot now, on a port already held.
    del state.slots["pager-1"]
    assert (await page.pick(1, 1))["code"] == "not_your_slot"
    assert rows[-1] == denied("not_your_slot")
    assert sup.calls == []

    # The example app's route: another slot is refused through page(), audited too.
    spec = importlib.util.spec_from_file_location(
        "browser_reader_routes_audit",
        _REPO / "docs/app-kit/examples/browser-reader/backend/routes.py",
    )
    assert spec is not None and spec.loader is not None
    routes = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, routes)
    spec.loader.exec_module(routes)
    handlers = {(r.method, r.path): r.handler for r in routes.register_routes(None)}
    ctx = build_app_context(
        "pager", tmp_path / "data", permissions={"chatSlots": True, "browser": ["read", "act"]}
    )
    request = MagicMock()
    request.app = {"state": _State(_Slot("pager-1", "pager"))}
    for route in (("GET", "/slots/{slot}/outline"), ("POST", "/slots/{slot}/click")):
        request.match_info = {"slot": "other-1"}
        resp = await handlers[route](request, ctx)
        assert json.loads(resp.body)["code"] == "not_your_slot"
        assert rows[-1] == denied("not_your_slot")
