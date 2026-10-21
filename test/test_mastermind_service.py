"""Mastermind in the gateway: the switch, the turn hooks, the routes and the tools."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp.test_utils import make_mocked_request

from kiro_crew.dashboard.handlers import mastermind as routes
from kiro_crew.mastermind import service
from kiro_crew.mastermind import slot as mm_slot
from kiro_crew.mastermind.constants import ENABLED_FILE, KIND_EVENT, KIND_USER
from kiro_crew.mastermind.prompts import VIEW_GUIDE
from kiro_crew.mastermind.slot import MASTERMIND_SLOT_KEY
from kiro_crew.mcp_tools import mastermind as tools


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Own data home, and the summarizer's backend call replaced (the one seam
    a unit test swaps: everything else is the real tree, worker and service)."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))

    async def summarize(prompt: str) -> str:
        return "a summary"

    monkeypatch.setattr(service, "_summarize_with", lambda _state: summarize)


@pytest.fixture
def mids(monkeypatch):
    """Read a row's id from ``meta.mid`` and treat every assistant row as speech."""
    import kiro_crew.dashboard.member_message_notes as notes
    import kiro_crew.dashboard.state as dash_state

    monkeypatch.setattr(dash_state, "row_mid", lambda row: (row.get("meta") or {}).get("mid"))
    monkeypatch.setattr(notes, "is_member_message", lambda row: row.get("role") == "assistant")


def _state(on: bool = True) -> Any:
    state = SimpleNamespace(sessions=None, _slots={})
    mm_slot.set_enabled(state, on)
    return state


def _reply(mid: str, text: str) -> dict[str, Any]:
    return {"role": "assistant", "content": text, "meta": {"mid": mid}}


def _slot(key: str = MASTERMIND_SLOT_KEY, *, mode: str = "persistent") -> Any:
    return SimpleNamespace(
        key=key,
        messages=[],
        _pending_discard_conversation_key=None,
        is_restricted=mode != "persistent",
        blocks_reads=mode == "temporary",
        linked_session_key="",
    )


async def _turn(state, slot, text, rows, *, synthetic=False, land=True) -> str:
    prefix = await service.turn_prefix(state, slot, text, synthetic=synthetic)
    slot.messages.extend(rows)
    if land:
        service.turn_landed(state, slot, [r["meta"]["mid"] for r in rows])
    mm_slot.turn_ended(state, slot, "dashboard:mastermind")
    await service.service_for(state).settle_writes()
    return prefix


def _run(scenario) -> None:
    async def go() -> None:
        held: list[Any] = []
        try:
            await scenario(held)
        finally:
            for state in held:
                await service.close(state)

    asyncio.run(go())


async def _count(state) -> int:
    return await service.service_for(state).worker.run(lambda t: t.count)


# The switch


def test_the_switch_is_a_file_read_once_and_kept():
    state = SimpleNamespace()
    assert not mm_slot.enabled(state)
    mm_slot.set_enabled(state, True)
    assert (mm_slot.tree_dir() / ENABLED_FILE).exists()
    assert mm_slot.enabled(SimpleNamespace())  # a new gateway reads it
    assert mm_slot.is_mastermind_turn(state, _slot())
    assert not mm_slot.is_mastermind_turn(state, _slot("chat-1"))
    mm_slot.set_enabled(state, False)
    assert not mm_slot.is_mastermind_turn(state, _slot())


# Turns


def test_a_turn_sees_the_view_and_the_last_messages(mids):
    async def scenario(held):
        state = _state()
        held.append(state)
        slot = _slot()
        first = await _turn(state, slot, "my color is teal", [_reply("r1", "Noted: teal.")])
        assert first.startswith(VIEW_GUIDE) and "<chat>\n</chat>" in first
        second = await _turn(state, slot, "what is my color?", [_reply("r2", "Teal.")])
        assert "0+1|user: my color is teal" in second
        recent = "<recent>\n0+0|user: my color is teal\n1+0|agent: Noted: teal.\n</recent>"
        assert recent in second

    _run(scenario)


def test_only_a_landed_turn_drops_its_conversation(mids):
    async def scenario(held):
        state = _state()
        held.append(state)
        slot = _slot()
        await _turn(state, slot, "hello", [_reply("r1", "hi")], land=False)
        assert slot._pending_discard_conversation_key is None
        await _turn(state, slot, "hello", [_reply("r2", "hi")])
        assert slot._pending_discard_conversation_key == "dashboard:mastermind"

    _run(scenario)


def test_a_recovery_turn_records_the_message_it_continues(mids):
    async def scenario(held):
        state = _state()
        held.append(state)
        slot = _slot()
        await _turn(state, slot, "build the kite", [], land=False)  # cut short
        recovery = "[Stalled turn — automatic recovery] go on"
        await _turn(state, slot, recovery, [_reply("r1", "done")], synthetic=True)
        worker = service.service_for(state).worker
        assert await worker.run(lambda t: t.zoom(0, 1)) == "0+0|user: build the kite"
        assert await _count(state) == 2

    _run(scenario)


def test_a_private_turn_is_never_remembered(mids):
    async def scenario(held):
        state = _state()
        held.append(state)
        for mode in ("incognito", "temporary"):
            prefix = await _turn(state, _slot(mode=mode), f"secret {mode}", [_reply(mode, "ok")])
            assert (prefix == "") is (mode == "temporary")
        assert await _count(state) == 0

    _run(scenario)


def test_automation_messages_are_events():
    assert service.message_kind('[Cron notification from "daily"] ...') == KIND_EVENT
    assert service.message_kind("hello [Cron notification from x]") == KIND_USER


def test_a_slash_command_after_a_cut_turn_records_nothing_stale(mids):
    async def scenario(held):
        state = _state()
        held.append(state)
        slot = _slot()
        await _turn(state, slot, "stale", [], land=False)
        service.service_for(state).pending.pop(slot.key, None)  # the runner, for "/x"
        service.turn_landed(state, slot, [])
        await service.service_for(state).settle_writes()
        assert await _count(state) == 0

    _run(scenario)


# The tools' routes


def _request(method: str, path: str, state: Any, *, session_key: str = "", body: Any = None):
    req = make_mocked_request(method, path, headers={"X-Session-Key": session_key})
    req.app["state"] = state  # type: ignore[index]
    if body is not None:

        async def read_json():
            return body

        req.json = read_json  # type: ignore[method-assign]
    return req


@pytest.fixture
def recognized(monkeypatch):
    async def ok(state, sk, operation, *, blocks_persisted_mode):
        return None

    monkeypatch.setattr(routes, "_recognize_session", ok)


@pytest.fixture
def owner(monkeypatch):
    async def ok(request, operation):
        return None

    monkeypatch.setattr(routes, "require_owner_dashboard_request", ok)


def test_zoom_answers_the_session_the_mastermind_slot_runs_on(recognized, mids):
    async def scenario(held):
        state = _state()
        held.append(state)
        slot = _slot()
        state._slots[MASTERMIND_SLOT_KEY] = slot
        await _turn(state, slot, "remember 42", [_reply("r1", "ok")])
        mine = _request("GET", "/z?id=0&n=1", state, session_key="dashboard:mastermind")
        resp = await routes.api_mastermind_zoom(mine)
        assert resp.status == 200
        assert json.loads(resp.body)["text"] == "0+0|user: remember 42"
        other = _request("GET", "/z?id=0", state, session_key="dashboard:chat-1")
        assert (await routes.api_mastermind_zoom(other)).status == 403

    _run(scenario)


def test_zoom_refuses_while_mastermind_is_off(recognized):
    async def scenario(held):
        state = _state(on=False)
        held.append(state)
        state._slots[MASTERMIND_SLOT_KEY] = _slot()
        req = _request("GET", "/z?id=0", state, session_key="dashboard:mastermind")
        resp = await routes.api_mastermind_zoom(req)
        assert resp.status == 403
        assert json.loads(resp.body)["code"] == "not_mastermind_session"

    _run(scenario)


# Settings > Mastermind


def test_settings_read_view_export_and_clear(owner, mids):
    async def scenario(held):
        state = _state()
        held.append(state)
        slot = _slot()
        state._slots[MASTERMIND_SLOT_KEY] = slot
        await _turn(state, slot, "remember me", [_reply("r1", "sure")])
        got = json.loads((await routes.api_mastermind_get(_request("GET", "/m", state))).body)
        assert got["enabled"] is True and got["slot_exists"] is True and got["messages"] == 2
        view = json.loads((await routes.api_mastermind_view(_request("GET", "/v", state))).body)
        assert [line["ref"] for line in view["lines"]] == ["0+1", "1+1"]
        export = await routes.api_mastermind_export(_request("GET", "/e", state))
        assert "attachment" in export.headers["Content-Disposition"]
        assert len(json.loads(export.text)["messages"]) == 2
        await routes.api_mastermind_clear(_request("POST", "/c", state))
        got = json.loads((await routes.api_mastermind_get(_request("GET", "/m", state))).body)
        assert got["messages"] == 0

    _run(scenario)


def test_the_switch_route_wants_a_boolean(owner):
    async def scenario(held):
        state = _state(on=False)
        held.append(state)
        req = _request("POST", "/api/mastermind/enabled", state, body={"enabled": "yes"})
        resp = await routes.api_mastermind_set_enabled(req)
        assert resp.status == 400 and json.loads(resp.body)["code"] == "enabled_not_bool"

    _run(scenario)


def test_the_mastermind_session_cannot_be_deleted_while_on(monkeypatch):
    import kiro_crew.dashboard.chat_handlers  # noqa: F401 -- binds the lifecycle's names
    from kiro_crew.dashboard.chat_api import slot_lifecycle

    async def scenario(held):
        state = _state()
        held.append(state)
        state._slots[MASTERMIND_SLOT_KEY] = _slot()
        monkeypatch.setattr(slot_lifecycle, "deny_app_slot_access", lambda *a: None, raising=False)
        req = make_mocked_request(
            "DELETE", "/api/chat/slots/mastermind", match_info={"slot": "mastermind"}
        )
        req.app["state"] = state  # type: ignore[index]
        resp = await slot_lifecycle.api_chat_slot_delete(req)
        assert resp.status == 409 and json.loads(resp.body)["code"] == "mastermind_protected"

    _run(scenario)


# The tools


def test_the_tools_send_the_strictly_verified_key(monkeypatch):
    seen: dict[str, Any] = {}

    def get(route, *, session_key):
        seen.update(route=route, session_key=session_key)
        return {"text": "0+0|user: hi"}

    monkeypatch.setattr(
        tools.mcp_core, "require_strict_session_key", lambda msg: ("dashboard:mastermind", "")
    )
    monkeypatch.setattr(tools.mcp_core, "_get", get)
    assert tools.mastermind_zoom("mastermind_zoom", {"id": 0}) == "0+0|user: hi"
    assert seen == {
        "route": "/api/mastermind/zoom?id=0&n=1&at=0",
        "session_key": "dashboard:mastermind",
    }


def test_the_tools_refuse_an_unverified_session(monkeypatch):
    monkeypatch.setattr(
        tools.mcp_core, "require_strict_session_key", lambda msg: ("", "Error: nope")
    )
    assert tools.mastermind_date("mastermind_date", {"id": 0}) == "Error: nope"


def test_the_tools_are_listed_only_while_mastermind_is_on():
    assert tools.advertised() == []
    mm_slot.set_enabled(SimpleNamespace(), True)
    assert [t["name"] for t in tools.advertised()] == ["mastermind_zoom", "mastermind_date"]


# The summary model


def test_the_summary_model_is_an_editable_role_that_never_inherits_the_chat_model():
    from kiro_crew.config.sections_agent_fields import ROLE_MODEL_KEYS
    from kiro_crew.dashboard.handlers.core_config_schema import _EDITABLE_CONFIG

    assert service.MASTERMIND_ROLE in ROLE_MODEL_KEYS
    background = _EDITABLE_CONFIG["agent.role_models.background"]
    assert _EDITABLE_CONFIG["agent.role_models.mastermind"] == background
    assert service.summary_model() == "auto"  # unpinned: the provider picks
