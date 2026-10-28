"""The "Open in kiro-cli" Feature Preview (``dashboard.feature_previews.open_in_kiro_cli``).

Off by default. Off, the gateway refuses every new handover (the availability
read, the terminal's open and the open's re-plan under its locks), so hiding the
menu row is not the only thing keeping a chat in Crew's sandbox. A chat a
terminal already holds still comes back to Crew when kiro-cli exits, and an open
a restart left still brings its turns in.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from test_kiro_cli_launch import (  # noqa: F401 -- the fixtures are bound for pytest
    KEY,
    ORIGIN,
    _config_with_previews,
    _fake_kiro_cli,
    _FakeMap,
    _isolate,
    _leave_an_open,
    _read_until,
    _request,
    _route_app,
    _slot,
    _state,
    _turn,
    switch_fixture,
    wired_fixture,
)

from kiro_crew import kiro_cli_handoff
from kiro_crew.config import live as config_live
from kiro_crew.config.sections import FEATURE_PREVIEW_FLAGS
from kiro_crew.dashboard.handlers import kiro_cli_launch as kcl
from kiro_crew.dashboard.handlers import terminal
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS, async_wait_until

_BOOT_PROBE = """
import sys
from aiohttp import web
import kiro_crew.dashboard.server
import kiro_crew.dashboard.server_runtime.api_entrypoint
from kiro_crew.dashboard.routes import connections
connections.register(web.Application())
print("loaded" if "kiro_crew.dashboard.handlers.kiro_cli_launch" in sys.modules else "")
"""


def test_the_boot_path_does_not_import_the_preview_module(tmp_path):
    """Starting the dashboard and registering its routes loads no Open in
    kiro-cli code; the availability route imports it on its first request."""
    import os
    import subprocess

    env = {**os.environ, "HOME": str(tmp_path)}
    proc = subprocess.run(  # noqa: S603 - this interpreter, fixed argv
        [sys.executable, "-c", _BOOT_PROBE],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=FALLBACK_TIMEOUT_SECS,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert proc.stdout.strip().splitlines()[-1:] in ([""], []), proc.stdout


def _set_preview(monkeypatch, previews):
    monkeypatch.setattr(config_live, "snapshot", lambda: _config_with_previews(previews))


def test_the_preview_is_a_known_feature_preview_and_off_by_default():
    from kiro_crew.config.loader import KiroCrewConfig

    assert kcl.PREVIEW_NAME in FEATURE_PREVIEW_FLAGS
    assert KiroCrewConfig().dashboard.feature_previews.get(kcl.PREVIEW_NAME) is not True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "previews",
    [{}, {kcl.PREVIEW_NAME: False}, {kcl.PREVIEW_NAME: "yes"}, {"explore": True}],
    ids=["absent", "false", "not-a-bool", "another-preview"],
)
async def test_the_preview_reads_off_unless_set_true(monkeypatch, previews):
    _set_preview(monkeypatch, previews)
    assert await kcl.preview_on() is False
    _set_preview(monkeypatch, {kcl.PREVIEW_NAME: True})
    assert await kcl.preview_on() is True


@pytest.mark.asyncio
async def test_with_no_watcher_the_preview_is_read_from_disk_and_fails_closed(monkeypatch):
    from kiro_crew.config import loader

    monkeypatch.setattr(config_live, "snapshot", lambda: None)
    monkeypatch.setattr(
        loader.KiroCrewConfig, "load", lambda: _config_with_previews({kcl.PREVIEW_NAME: True})
    )
    assert await kcl.preview_on() is True

    def unreadable():
        raise ValueError("torn config.json")

    monkeypatch.setattr(loader.KiroCrewConfig, "load", unreadable)
    assert await kcl.preview_on() is False


@pytest.mark.asyncio
async def test_the_availability_route_refuses_while_the_preview_is_off(wired, monkeypatch):
    _set_preview(monkeypatch, {})
    state = _state(_slot(wired.project))
    async with TestClient(TestServer(_route_app(state))) as client:
        body = await (await client.get("/api/terminal/kiro-cli/main")).json()
    assert body == {
        "available": False,
        "code": kcl.CODE_PREVIEW_OFF,
        "reason": kcl.refusal_message(kcl.CODE_PREVIEW_OFF),
    }
    assert "Feature previews" in body["reason"]


@pytest.mark.asyncio
async def test_an_open_is_refused_with_nothing_held_or_reset_while_the_preview_is_off(
    wired, switch, monkeypatch
):
    _set_preview(monkeypatch, {})
    state = _state(_slot(wired.project))
    launch, code = await kcl.prepare_kiro_cli_launch(_request(state), "main", "t")
    assert (launch, code) == (None, kcl.CODE_PREVIEW_OFF)
    assert not kiro_cli_handoff.is_held(KEY)
    assert switch.reset == []
    assert [p.name for p in wired.agents.iterdir()] == ["myagent.json"], "no launch copy"
    assert not wired.records.exists() or not list(wired.records.iterdir()), "no open record"


@pytest.mark.asyncio
async def test_the_preview_turned_off_during_an_open_refuses_it_at_the_re_plan(
    wired, switch, monkeypatch
):
    """The open plans again under the switch locks; the switch is read there too."""
    state = _state(_slot(wired.project))
    real_plan = kcl.plan_kiro_cli_launch
    calls = []

    async def plan_then_switch_off(request, slot_name):
        plan = await real_plan(request, slot_name)
        calls.append(plan.code)
        _set_preview(monkeypatch, {})
        return plan

    monkeypatch.setattr(kcl, "plan_kiro_cli_launch", plan_then_switch_off)
    launch, code = await kcl.prepare_kiro_cli_launch(_request(state), "main", "t")
    assert calls[0] == "", "the first plan ran with the preview on"
    assert (launch, code) == (None, kcl.CODE_PREVIEW_OFF)
    assert not kiro_cli_handoff.is_held(KEY)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX PTY")
@pytest.mark.asyncio
async def test_the_terminal_refuses_an_open_and_spawns_nothing_while_the_preview_is_off(
    wired, switch, monkeypatch
):
    from terminal_handler_helpers import _make_app

    _set_preview(monkeypatch, {})
    _fake_kiro_cli(wired.binary)
    app = _make_app()
    state = app["state"]
    state._slots = {"main": _slot(wired.project)}
    state.sessions._session_map = _FakeMap()
    state.sessions.mirror_accepts_inbound = MagicMock(return_value=False)
    state.sessions._has_allocation_reservation = MagicMock(return_value=False)
    state.sessions.get_slack_link = MagicMock(return_value=(None, None))
    async with TestClient(TestServer(app)) as client:
        ws = await client.ws_connect(
            "/api/ws/terminal/kc3?kiro_cli_slot=main&kiro_cli_open=1", headers=ORIGIN
        )
        frame = await ws.receive_json(timeout=FALLBACK_TIMEOUT_SECS)
        assert frame["type"] == "error" and frame["code"] == kcl.CODE_PREVIEW_OFF
        await ws.receive(timeout=FALLBACK_TIMEOUT_SECS)
        assert ws.closed
    assert "kc3" not in state._terminal_sessions
    assert not kiro_cli_handoff.is_held(KEY)
    assert switch.reset == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX PTY")
@pytest.mark.asyncio
async def test_a_chat_held_when_the_preview_goes_off_still_comes_back_on_exit(
    wired, switch, monkeypatch
):
    from terminal_handler_helpers import _make_app

    _fake_kiro_cli(wired.binary)
    slot = _slot(wired.project)
    app = _make_app()
    state = app["state"]
    state._slots = {"main": slot}
    state.sessions._session_map = _FakeMap()
    state.sessions.mirror_accepts_inbound = MagicMock(return_value=False)
    state.sessions._has_allocation_reservation = MagicMock(return_value=False)
    state.sessions.get_slack_link = MagicMock(return_value=(None, None))
    state.sessions.get_provider = MagicMock(return_value=None)
    state.push_slots_update = MagicMock()
    async with TestClient(TestServer(app)) as client:
        ws = await client.ws_connect(
            "/api/ws/terminal/kc4?kiro_cli_slot=main&kiro_cli_open=1", headers=ORIGIN
        )
        try:
            await _read_until(ws, b"CWD:")
            assert kiro_cli_handoff.holder_of(KEY) == "kc4"
            _set_preview(monkeypatch, {})
            await ws.send_bytes(b"q\n")
            await async_wait_until(
                lambda: not kiro_cli_handoff.is_held(KEY), describe=lambda: "the hold released"
            )
            assert "kiro-cli exited" in slot.appended[-1][1]
            assert list(wired.records.iterdir()) == [], "the open's record removed"
        finally:
            await ws.close()
            for sid in list(state._terminal_sessions):
                sess = state._terminal_sessions.pop(sid, None)
                if sess is not None:
                    await terminal._kill_session(sess)


@pytest.mark.asyncio
async def test_an_open_a_restart_left_still_brings_its_turns_in_while_the_preview_is_off(
    wired, monkeypatch
):
    _set_preview(monkeypatch, {})
    _leave_an_open(wired, "", [_turn("Prompt", "q"), _turn("AssistantMessage", "a")])
    written = []
    monkeypatch.setattr(
        kcl, "_append_to_archived_transcript", lambda *a: written.append(a) or len(a[3])
    )
    state = _state(None)
    state.conversation_log = object()
    assert await kcl.recover_left_open(state, None, KEY)
    assert [r[1] for _log, _key, _c, rows in written for r in rows][:2] == ["q", "a"]
    assert list(wired.records.iterdir()) == []
    kiro_cli_handoff.refuse_if_held(KEY)  # the session allocates again
