"""Stale-config detection: the status, the slot badge and ``session_config_status``.

The status and the badge it drives, the read-only ``session_config_status`` verb,
its route and its MCP tool, baseline adoption and the carried-forward selection,
badge bookkeeping that never breaks a turn, and the bounded signature.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from chat_test_helpers import _make_state
from stale_config_helpers import _scan_on_every_os_fixture  # noqa: F401
from stale_config_helpers import kiro_home_fixture  # noqa: F401
from stale_config_helpers import (
    _A,
    _CHANGED,
    _SERVER,
    _fp,
    _idle_chats,
    _pool_manager,
    _Provider,
    _status_slot,
    _write_spec,
)

from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard import config_staleness as cs
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard import stale_config
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.handlers import session_control as handlers_sc
from kiro_crew.dashboard.stale_config import ConfigFingerprint, SpawnInputs, is_stale, make_record
from kiro_crew.mcp_dashboard import SESSION_CONTROL_TOOLS, TABLE
from kiro_crew.mcp_tools.dashboard_client import InMemoryDashboardClient
from kiro_crew.mcp_tools.table import Caller, ToolContext

# ── Status and the badge ─────────────────────────────────────────────────────


def test_the_badge_is_set_only_when_the_config_changed(kiro_home, tmp_path, monkeypatch):
    _write_spec(kiro_home, {"prompt": "a"})
    state, slot = _status_slot(tmp_path, monkeypatch)

    assert asyncio.run(cs.refresh_config_stale(state, slot))["stale"] is False
    assert slot.config_stale is False and slot.config_stale_inputs == ""
    state.push_slots_update.assert_not_called()

    _write_spec(kiro_home, {"prompt": "b"})
    out = asyncio.run(cs.refresh_config_stale(state, slot))

    assert out["stale"] is True
    assert slot.config_stale is True
    assert slot.config_stale_inputs.endswith("kirocrew.json")
    assert str(tmp_path) not in slot.config_stale_inputs
    assert slot.to_dict()["config_stale"] is True
    assert slot.to_dict()["config_stale_inputs"] == slot.config_stale_inputs
    state.push_slots_update.assert_called_once()


def test_the_badge_clears_when_the_config_returns_to_the_spawned_state(
    kiro_home, tmp_path, monkeypatch
):
    _write_spec(kiro_home, {"prompt": "a"})
    state, slot = _status_slot(tmp_path, monkeypatch)
    _write_spec(kiro_home, {"prompt": "b"})
    asyncio.run(cs.refresh_config_stale(state, slot))
    assert slot.config_stale is True

    _write_spec(kiro_home, {"prompt": "a"})
    asyncio.run(cs.refresh_config_stale(state, slot))

    assert slot.config_stale is False and slot.config_stale_inputs == ""


def test_an_mcp_edit_a_hot_reloading_provider_applied_is_not_stale(
    kiro_home, tmp_path, monkeypatch
):
    _write_spec(kiro_home, {"mcpServers": _SERVER})
    state, slot = _status_slot(tmp_path, monkeypatch, hot_reload=True)
    _write_spec(kiro_home, {"mcpServers": {**_SERVER, "b": {"command": "b"}}})

    assert asyncio.run(cs.refresh_config_stale(state, slot))["stale"] is False
    assert slot.config_stale is False


def test_the_same_mcp_edit_is_stale_on_a_provider_that_does_not_hot_reload(
    kiro_home, tmp_path, monkeypatch
):
    _write_spec(kiro_home, {"mcpServers": _SERVER})
    state, slot = _status_slot(tmp_path, monkeypatch, hot_reload=False)
    _write_spec(kiro_home, {"mcpServers": {**_SERVER, "b": {"command": "b"}}})

    assert asyncio.run(cs.refresh_config_stale(state, slot))["stale"] is True


def test_the_status_resolves_no_path_on_the_event_loop(kiro_home, tmp_path, monkeypatch):
    """The agents dir behind the labels is resolved off the loop.

    ``kiro_agents_dir`` resolves ``KIRO_HOME`` (``Path.resolve``), which stalls
    on unreachable storage; on the loop it would stall every gateway request.
    Mutation guard: calling ``kiro_agents_dir()`` inside ``_input_label`` (on
    the loop) fails this test, and the labels still come out right.
    """
    import pathlib

    def _on_loop() -> bool:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return False
        return True

    real_agents_dir = cs.kiro_agents_dir
    real_resolve = pathlib.Path.resolve

    def _agents_dir():
        assert not _on_loop(), "kiro_agents_dir() ran on the event loop"
        return real_agents_dir()

    def _resolve(self, *args, **kwargs):
        assert not _on_loop(), f"Path.resolve({self}) ran on the event loop"
        return real_resolve(self, *args, **kwargs)

    _write_spec(kiro_home, {"prompt": "a"})
    state, slot = _status_slot(tmp_path, monkeypatch)
    _write_spec(kiro_home, {"prompt": "b"})
    monkeypatch.setattr(cs, "kiro_agents_dir", _agents_dir)
    monkeypatch.setattr(pathlib.Path, "resolve", _resolve)

    out = asyncio.run(cs.refresh_config_stale(state, slot))

    assert out["stale"] is True
    assert out["changed"] and out["changed"][0].endswith("agents/kirocrew.json")
    assert slot.config_stale_inputs == out["changed"][0]
    assert str(tmp_path) not in slot.config_stale_inputs


def test_an_unknown_reading_leaves_the_badge_as_it_was(kiro_home, tmp_path, monkeypatch):
    _write_spec(kiro_home, {"prompt": "a"})
    state, slot = _status_slot(tmp_path, monkeypatch)
    slot.config_stale = True
    slot.config_stale_inputs = "~/.kiro/agents/kirocrew.json"
    monkeypatch.setattr(cs, "current_config_fingerprint", MagicMock(side_effect=OSError("x")))

    assert asyncio.run(cs.refresh_config_stale(state, slot))["stale"] is None
    assert slot.config_stale is True


def test_a_live_process_no_record_describes_is_unknown_not_current(tmp_path):
    """Nothing was compared, so the answer is unknown and the badge stays off."""
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("s1")
    state.sessions.get_provider = MagicMock(return_value=_Provider())
    state.push_slots_update = MagicMock()
    slot._spawn_config = make_record(_Provider(), SpawnInputs("kirocrew", ""), _A)

    out = asyncio.run(cs.config_stale_status(state, slot))

    assert out == {"stale": None, "changed": [], "unreadable": []}
    asyncio.run(cs.refresh_config_stale(state, slot))
    assert slot.config_stale is False


def test_a_session_with_no_live_process_is_not_stale(tmp_path):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("s1")
    state.sessions.get_provider = MagicMock(return_value=None)
    slot._spawn_config = make_record(_Provider(), SpawnInputs("kirocrew", ""), _A)

    assert asyncio.run(cs.config_stale_status(state, slot))["stale"] is False


def _respawn_manager(monkeypatch, tmp_path, state, respawned):
    """A session manager whose start returns *respawned*, wired to *state*'s reader."""
    mgr = _pool_manager(monkeypatch, tmp_path)
    mgr.get_or_create = AsyncMock(return_value=(respawned, True, False))
    mgr.release = MagicMock()
    mgr.respawn_config_reader = lambda key: cs.respawn_spawn_config(state, key)
    # The spawn's heal and freshness gate are covered by their own tests.
    monkeypatch.setattr(cs, "ensure_agent_materialized", lambda *_a: None)
    monkeypatch.setattr(cs, "require_fresh_derived_spec", lambda *_a: None)
    return mgr


@pytest.mark.parametrize("path", ["eager_respawn", "reset_successor"])
def test_a_respawned_process_records_its_config_so_a_later_edit_is_stale(
    kiro_home, tmp_path, monkeypatch, path
):
    """A hard stop's eager respawn (or a reset's successor) is recorded with no turn.

    Without the record an edit made before the chat's next turn read as current.
    """
    _write_spec(kiro_home, {"prompt": "a"})
    state, slot = _status_slot(tmp_path, monkeypatch)
    respawned = _Provider()
    mgr = _respawn_manager(monkeypatch, tmp_path, state, respawned)
    key = cs.effective_session_key(slot)
    boundary = mgr._lifecycle_boundary()

    if path == "eager_respawn":
        asyncio.run(boundary._eager_respawn(key))
    else:
        asyncio.run(boundary._respawn_as(key, {}))

    mgr.release.assert_called_once_with(key)
    state.sessions.get_provider = MagicMock(return_value=respawned)
    assert asyncio.run(cs.config_stale_status(state, slot))["stale"] is False
    assert slot._spawn_config.describes(respawned)

    _write_spec(kiro_home, {"prompt": "b"})
    asyncio.run(cs.refresh_all_config_stale(state, guarded=False))

    assert slot.config_stale is True
    assert slot.config_stale_inputs.endswith("kirocrew.json")


def test_a_respawn_fingerprint_for_other_inputs_is_not_adopted(tmp_path):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("s1")
    respawned = _Provider()
    respawned.pool_spawn_config = (SpawnInputs("kirocrew", "/elsewhere"), _A)
    state.sessions.get_provider = MagicMock(return_value=respawned)
    slot._spawn_config = make_record(_Provider(), SpawnInputs("kirocrew", ""), _A)

    assert asyncio.run(cs.config_stale_status(state, slot))["stale"] is None
    assert not slot._spawn_config.describes(respawned)


def test_a_respawn_keeps_the_fingerprint_a_warm_pool_claim_carries(monkeypatch, tmp_path):
    state = _make_state(tmp_path)
    claimed = _Provider()
    claimed.pool_spawn_config = (SpawnInputs("kirocrew", "/pool"), _A)
    mgr = _respawn_manager(monkeypatch, tmp_path, state, claimed)
    mgr.respawn_config_reader = AsyncMock(return_value=(SpawnInputs("kirocrew", ""), _CHANGED))

    asyncio.run(mgr._lifecycle_boundary()._eager_respawn("k"))

    mgr.respawn_config_reader.assert_awaited_once_with("k")
    assert claimed.pool_spawn_config == (SpawnInputs("kirocrew", "/pool"), _A)


def test_a_failing_respawn_reader_still_respawns_and_releases(monkeypatch, tmp_path):
    state = _make_state(tmp_path)
    respawned = _Provider()
    mgr = _respawn_manager(monkeypatch, tmp_path, state, respawned)
    mgr.respawn_config_reader = AsyncMock(side_effect=OSError("boom"))

    asyncio.run(mgr._lifecycle_boundary()._eager_respawn("k"))

    mgr.get_or_create.assert_awaited_once_with("k")
    mgr.release.assert_called_once_with("k")
    assert respawned.pool_spawn_config is None


def test_the_respawn_reader_is_none_for_a_key_no_recorded_chat_answers_to(tmp_path):
    state = _make_state(tmp_path)
    state.get_or_create_slot("s1")

    assert asyncio.run(cs.respawn_spawn_config(state, "nobody")) is None


def test_the_gateway_wires_the_respawn_reader(tmp_path):
    from kiro_crew.dashboard import server

    state = MagicMock()
    app = MagicMock()
    taken = AsyncMock(return_value=(SpawnInputs("kirocrew", ""), _A))
    with (
        patch.object(server, "start_config_stale_sweep", MagicMock()),
        patch.object(server, "subscribe_backend_changes", MagicMock()),
        patch.object(server, "respawn_spawn_config", taken),
    ):
        server._arm_config_stale_sweep(app, state)
        out = asyncio.run(state.sessions.respawn_config_reader("k"))

    assert out == (SpawnInputs("kirocrew", ""), _A)
    taken.assert_awaited_once_with(state, "k")


def _ordered_badge_turn(monkeypatch, *, stale: bool = False):
    """One harness turn whose badge steps, acquire and stream land in *order*."""
    from turn_harness import Do, TurnScript, run_turn

    from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK, AcpEvent, TurnUsage

    order: list[str] = []
    monkeypatch.setattr(chat_runner, "managed_spec_missing", lambda _a: False)

    def _fingerprint(*_a, **_k):
        order.append("fingerprint")
        return _A

    def _record(slot, provider, *_spawn):
        order.append("record")

    async def _refresh(_state, _slot):
        order.append("refresh")
        return {}

    monkeypatch.setattr(chat_runner, "spawn_config_fingerprint", _fingerprint)
    monkeypatch.setattr(chat_runner, "record_spawn_config", _record)
    monkeypatch.setattr(chat_runner, "refresh_config_stale", _refresh)

    def _setup(ctx):
        ctx.slot.config_stale = stale
        acquire = ctx.state.sessions.get_or_create.side_effect

        async def _acquire(*args, **kwargs):
            order.append("acquire")
            return await acquire(*args, **kwargs)

        ctx.state.sessions.get_or_create.side_effect = _acquire

    script = TurnScript(
        events=[
            Do(lambda _ctx: order.append("stream")),
            AcpEvent(kind=EVENT_TEXT_CHUNK, text="ok"),
            AcpEvent(kind=EVENT_COMPLETE, usage=TurnUsage(credits=1.0)),
        ],
        setup=_setup,
    )
    return order, asyncio.run(run_turn(script))


def test_a_turn_records_the_spawn_config_and_refreshes_the_badge_at_its_end(monkeypatch):
    """A turn fingerprints before the acquire, records after it, and refreshes
    the badge once its stream has ended."""
    order, record = _ordered_badge_turn(monkeypatch)

    assert record.rows("assistant"), "the turn did not land"
    assert order == ["fingerprint", "acquire", "record", "stream", "refresh"]


def test_nothing_relaunches_a_stale_session(monkeypatch):
    """Detection only: a turn on a badged slot keeps its process."""
    for gone in ("_consume_stale_config_reload", "_verify_reloaded_config", "approve_agent_reload"):
        assert not hasattr(chat_runner, gone)

    _order, record = _ordered_badge_turn(monkeypatch, stale=True)

    assert record.rows("assistant"), "the turn did not land"
    relaunches = {"reset", "destroy", "remove", "recycle_background"}
    assert not [call.name for call in record.session_calls if call.name in relaunches]
    assert [call.name for call in record.session_calls].count("get_or_create") == 1


def test_the_reload_action_clears_the_badge():
    """The badge is cleared in the ONE reload teardown, which the tab menu's
    Reload session route and the ``session_reload`` verb both call -- so either
    way of relaunching the process clears it (the verb's own behavioural pin is
    in test_session_control_reload.py)."""
    import inspect

    from kiro_crew.dashboard import chat_handlers
    from kiro_crew.dashboard import session_control as sc

    src = inspect.getsource(chat_handlers.reload_slot_session)
    assert "slot.config_stale = False" in src
    assert src.index("slot.config_stale = False") < src.rindex("state.push_slots_update()")
    assert "reload_slot_session(" in inspect.getsource(chat_handlers.api_chat_slot_reload)
    assert "reload_slot_session(" in inspect.getsource(sc.reload_target)


# ── session_config_status: verb, route, tool ─────────────────────────────────


@pytest.fixture
def _enabled(monkeypatch):
    monkeypatch.setattr(sc, "session_control_enabled", lambda: True)


@pytest.fixture
def verb_state(tmp_path, monkeypatch, kiro_home, _enabled):
    _write_spec(kiro_home, {"prompt": "a"})
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    provider = _Provider()
    state.sessions.get_provider = MagicMock(return_value=provider)
    state.push_slots_update = MagicMock()
    target._spawn_config = make_record(provider, SpawnInputs("kirocrew", ""), _fp())
    cfg = MagicMock()
    cfg.agent.member_acp_backend = ""
    cfg.agent.acp_backend = ""
    monkeypatch.setattr(cs.KiroCrewConfig, "load", MagicMock(return_value=cfg))
    return state, caller, target


def _status(state, caller, target: str) -> dict:
    return asyncio.run(
        sc.read_config_status(state, caller_session_key=slot_history_key(caller), target=target)
    )


def test_the_verb_reports_a_stale_target_and_sets_its_badge(verb_state, kiro_home):
    state, caller, target = verb_state
    assert _status(state, caller, "chat-2")["stale"] is False
    _write_spec(kiro_home, {"prompt": "b"})

    out = _status(state, caller, "chat-2")

    assert out["ok"] is True and out["target"] == "chat-2"
    assert out["stale"] is True and out["changed"][0].endswith("kirocrew.json")
    assert target.config_stale is True


def test_the_verb_reads_the_callers_own_chat(verb_state, kiro_home):
    """The documented step: an agent checks its OWN chat after editing its MCP servers."""
    state, caller, _ = verb_state
    provider = state.sessions.get_provider.return_value
    caller._spawn_config = make_record(provider, SpawnInputs("kirocrew", ""), _fp())
    _write_spec(kiro_home, {"prompt": "b"})

    out = _status(state, caller, "chat-1")

    assert out["ok"] is True and out["target"] == "chat-1"
    assert out["stale"] is True and caller.config_stale is True


def test_a_fenced_caller_reads_itself_but_not_a_peer_it_did_not_create(verb_state):
    """Allowing self widens nothing else: the ownership fence still holds for a peer."""
    state, caller, target = verb_state
    provider = state.sessions.get_provider.return_value
    caller._spawn_config = make_record(provider, SpawnInputs("kirocrew", ""), _fp())
    key = slot_history_key(caller)

    def fenced(name: str) -> dict:
        return asyncio.run(
            sc.read_config_status(state, caller_session_key=key, target=name, caller_fenced=True)
        )

    assert fenced("chat-1")["target"] == "chat-1"
    with pytest.raises(sc.SessionControlError) as refused:
        fenced("chat-2")
    assert refused.value.code == "not_creator"
    assert target.config_stale is False


def test_the_verb_reloads_nothing(verb_state, kiro_home):
    state, caller, target = verb_state
    state.sessions.reset = AsyncMock()
    _write_spec(kiro_home, {"prompt": "b"})

    _status(state, caller, "chat-2")

    state.sessions.reset.assert_not_awaited()


def test_the_verb_refuses_a_mirrored_target(verb_state):
    state, caller, target = verb_state
    state.sessions.set_mirror_link(slot_history_key(target), "C0FFEE", "1758.0001")

    with pytest.raises(sc.SessionControlError):
        _status(state, caller, "chat-2")
    assert target.config_stale is False


def test_the_verb_is_off_when_session_control_is_off(verb_state, monkeypatch):
    state, caller, _ = verb_state
    monkeypatch.setattr(sc, "session_control_enabled", lambda: False)

    with pytest.raises(sc.SessionControlError):
        _status(state, caller, "chat-2")


def test_the_route_is_registered_as_strict_internal():
    from kiro_crew.dashboard import server

    assert "/api/session-control/config-status" in server._STRICT_INTERNAL_API_PATHS


def test_the_route_refuses_what_the_verb_refuses(verb_state, monkeypatch):
    """The route maps a verb refusal onto its status, as every session-control route does."""
    state, caller, _ = verb_state
    request = MagicMock()
    request.app = {"state": state}
    request.query = {"target": "chat-2"}
    monkeypatch.setattr(handlers_sc, "_require_internal", AsyncMock(return_value=None))
    monkeypatch.setattr(handlers_sc, "_read_session_key", lambda _r: slot_history_key(caller))
    monkeypatch.setattr(handlers_sc, "_carried_fence", lambda _r: None)
    monkeypatch.setattr(sc, "session_control_enabled", lambda: False)

    resp = asyncio.run(handlers_sc.api_session_control_config_status(request))

    assert resp.status >= 400
    assert json.loads(resp.text)["code"]


_VERIFIED = "dashboard:chat-verified"


def _tool(resp: dict, args: dict) -> tuple[str, InMemoryDashboardClient]:
    dash = InMemoryDashboardClient({"GET /api/session-control/config-status": resp})
    out = TABLE.call("session_config_status", args, ToolContext(dash, Caller.strict(_VERIFIED)))
    return out, dash


def test_the_tool_reads_the_verb_route_with_the_verified_key():
    out, dash = _tool(
        {"ok": True, "target": "chat-2", "stale": True, "changed": ["~/.kiro/agents/k.json"]},
        {"target": "chat-2"},
    )
    (get,) = dash.requests
    assert get.path == "/api/session-control/config-status?target=chat-2"
    assert get.session_key == _VERIFIED
    assert "stale config (~/.kiro/agents/k.json changed" in out
    assert "Reload" in out


def test_the_tool_is_a_strict_session_control_row():
    """Gated on a verified caller like session_summary, so the channel block covers it."""
    assert "session_config_status" in SESSION_CONTROL_TOOLS


def test_the_tool_refuses_an_unverifiable_caller():
    dash = InMemoryDashboardClient({"GET /api/session-control/config-status": {}})
    out = TABLE.call(
        "session_config_status",
        {"target": "chat-2"},
        ToolContext(dash, Caller.unverified(_VERIFIED)),
    )
    assert out.startswith("Error")
    assert dash.requests == []


def test_the_tool_says_a_current_target_is_current():
    out, _ = _tool({"ok": True, "target": "chat-2", "stale": False}, {"target": "chat-2"})
    assert "runs on its current config" in out


def test_the_tool_says_an_unreadable_input_is_unknown():
    out, _ = _tool(
        {"ok": True, "target": "chat-2", "stale": None, "unreadable": [".kiro/settings/mcp.json"]},
        {"target": "chat-2"},
    )
    assert "unknown" in out and ".kiro/settings/mcp.json" in out


def test_the_tool_says_a_check_that_names_no_file_could_not_run():
    """A failed recompute or a reload mid-check names no file, so none is blamed."""
    out, _ = _tool(
        {"ok": True, "target": "chat-2", "stale": None, "unreadable": []}, {"target": "chat-2"}
    )
    assert "unknown" in out and "try again" in out
    assert "could not be read" not in out and "config file" not in out


def test_the_tool_does_not_suggest_a_retry_where_the_check_is_unavailable(monkeypatch):
    monkeypatch.setattr(stale_config, "WINDOWS_SCAN_DISABLED", True)
    out, _ = _tool(
        {"ok": True, "target": "chat-2", "stale": None, "unreadable": []}, {"target": "chat-2"}
    )
    assert "not available on Windows" in out
    assert "try again" not in out


def test_the_tool_surfaces_the_verb_refusal():
    out, _ = _tool({"error": "target session is mirrored", "code": "mirrored"}, {"target": "x"})
    assert out.startswith("Error:") and "mirrored" in out


def test_a_channel_agent_is_blocked_from_the_tool():
    from kiro_crew.channel import CHANNEL_AGENT_BLOCKED_TOOLS

    assert "session_config_status" in CHANNEL_AGENT_BLOCKED_TOOLS


# ── Baseline adoption and the carried-forward selection ──────────────────────


def test_an_input_unreadable_at_spawn_is_adopted_once_then_compared(
    kiro_home, tmp_path, monkeypatch
):
    """Readable later: adopted (not stale) and persisted, so a later edit is stale."""
    _write_spec(kiro_home, {"prompt": "a"})
    spec = kiro_home / "agents" / "kirocrew.json"
    real_read = stale_config._read_pinned_spec
    monkeypatch.setattr(
        stale_config,
        "_read_pinned_spec",
        lambda pinned, path, **kw: (
            ("unreadable", None) if path == spec else real_read(pinned, path, **kw)
        ),
    )
    recorded = _fp()
    assert "spec" in recorded.unreadable
    monkeypatch.setattr(stale_config, "_read_pinned_spec", real_read)
    state, slot = _status_slot(tmp_path, monkeypatch, recorded=recorded)

    assert asyncio.run(cs.config_stale_status(state, slot))["stale"] is False
    assert "spec" not in slot._spawn_config.fingerprint.unreadable

    _write_spec(kiro_home, {"prompt": "edited"})
    assert asyncio.run(cs.config_stale_status(state, slot))["stale"] is True


def test_an_unlistable_agents_dir_keeps_the_recorded_spec_selection(kiro_home, monkeypatch):
    """Unknown, not a switch to "no spec": the selection is carried forward too."""
    _write_spec(kiro_home, {"prompt": "a"})
    recorded = _fp()
    monkeypatch.setattr(
        stale_config, "_spec_scope_unlistable", lambda *_a: frozenset({"workspace", "user"})
    )
    monkeypatch.setattr(
        stale_config, "_agent_spec_files", lambda *_a: stale_config._SpecSelection()
    )
    current = _fp()
    assert "spec" in current.unreadable
    assert dict(current.parts)["spec_path"] == ""

    compared = stale_config.carry_forward(recorded, current)

    assert dict(compared.parts)["spec_path"] == dict(recorded.parts)["spec_path"]
    assert compared.spawn_only == recorded.spawn_only
    assert compared.reconcilable == recorded.reconcilable
    assert not is_stale(recorded, compared, hot_reloads=False)


def test_pool_spawn_config_is_declared_on_the_provider_contract():
    """H14: a capability the session layer reads is declared, never probed."""
    from kiro_crew.providers.base import LLMProvider

    assert isinstance(LLMProvider.__dict__["pool_spawn_config"], property)
    # Default: no receipt; a pool fill's set value is what is read back.
    import types

    holder = types.SimpleNamespace()
    prop = LLMProvider.__dict__["pool_spawn_config"]
    assert prop.fget(holder) is None
    prop.fset(holder, (SpawnInputs("kirocrew", ""), _A))
    assert prop.fget(holder) == (SpawnInputs("kirocrew", ""), _A)
    src = Path(cs.__file__).read_text(encoding="utf-8")
    assert 'getattr(provider, "pool_spawn_config"' not in src


def test_a_badged_chat_whose_session_expired_is_cleared(kiro_home, tmp_path, monkeypatch):
    _write_spec(kiro_home, {"prompt": "a"})
    state = _idle_chats(tmp_path, monkeypatch, kiro_home, keys=("gone",))
    slot = state._slots["gone"]
    slot.config_stale, slot.config_stale_inputs = True, "~/.kiro/agents/kirocrew.json"
    state.sessions.get_provider = MagicMock(return_value=None)  # idle-reaped

    asyncio.run(cs.refresh_all_config_stale(state, guarded=True))

    assert slot.config_stale is False and slot.config_stale_inputs == ""


def test_the_record_holds_fixed_size_digests_not_the_field_text(kiro_home):
    """A 5 MB spec costs a slot record a few 64-character digests, not 5 MB."""
    huge_ref = "@" + "s" * (1024 * 1024)
    _write_spec(
        kiro_home,
        {"prompt": "x" * (5 * 1024 * 1024), "mcpServers": _SERVER, "tools": [huge_ref, "@a"]},
    )
    fp = _fp()
    parts = dict(fp.parts)
    for key in ("spec_mcp", "spec_fields", "global_mcp"):
        assert len(parts[key]) == 64 and int(parts[key], 16) >= 0, key
    assert sum(len(v) for v in parts.values()) < 4096
    # The agent-controlled ``@server`` refs are held as digests too.
    assert len(fp.tool_refs) == 2
    assert all(len(r) == 64 and int(r, 16) >= 0 for r in fp.tool_refs)
    assert huge_ref not in fp.tool_refs


def _refs_spec(kiro_home, refs: list[str]) -> ConfigFingerprint:
    _write_spec(kiro_home, {"mcpServers": _SERVER, "tools": refs})
    return _fp()


def test_tool_refs_past_the_cap_fold_into_one_overflow_entry(kiro_home, monkeypatch):
    """Any number of refs costs a bounded set, and an edit past the cap still reads stale."""
    monkeypatch.setattr(stale_config, "FINGERPRINT_MAX_TOOL_REFS", 4)
    refs = [f"@srv{i}" for i in range(20)]
    before = _refs_spec(kiro_home, refs)
    assert len(before.tool_refs) == 5
    assert sum(1 for r in before.tool_refs if r.startswith("overflow:16:")) == 1
    # The order of the entries does not move it.
    assert _refs_spec(kiro_home, list(reversed(refs))) == before

    # Every single ref, kept or folded, is in the set: removing any one moves it
    # and reads stale on every provider, hot-reloading or not.
    for i in range(len(refs)):
        after = _refs_spec(kiro_home, refs[:i] + refs[i + 1 :])
        assert after.tool_refs != before.tool_refs, i
        assert is_stale(before, after, hot_reloads=True), i
    # So does renaming one, and adding one is never unchanged either.
    renamed = _refs_spec(kiro_home, refs[:-1] + ["@other"])
    assert renamed.tool_refs != before.tool_refs
    grown = _refs_spec(kiro_home, [*refs, "@srv20"])
    assert grown.tool_refs != before.tool_refs
    assert is_stale(before, grown, hot_reloads=False)


# ── Badge bookkeeping never breaks a turn ────────────────────────────────────

from test_fallback_row_attribution_8560 import (  # noqa: E402
    TestFallbackServedTurnAttribution as _TurnHarness,
)
from test_fallback_row_attribution_8560 import _deep_chain_client  # noqa: E402


class TestBadgeBookkeepingNeverBreaksATurn(_TurnHarness):
    """A raising fingerprint, inputs, record or refresh step: the turn still completes."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "target",
        [
            "spawn_config_fingerprint",
            "turn_spawn_inputs",
            "record_spawn_config",
            "refresh_config_stale",
        ],
    )
    async def test_a_raising_step_still_lets_the_turn_complete(self, tmp_path, monkeypatch, target):
        from kiro_crew.acp.types import TurnUsage
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.dashboard.handlers import usage as usage_mod
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        shard_dir = tmp_path / "usage" / "tokens"
        monkeypatch.setattr(usage_mod, "_TOKEN_USAGE_DIR", shard_dir)

        def _boom(*_a, **_k):
            raise ValueError("badge bookkeeping exploded")

        monkeypatch.setattr(chat_runner, target, _boom)

        async def _stream(msg):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok")
            yield LLMEvent(kind=EVENT_COMPLETE, usage=TurnUsage(credits=1.0))

        state = self._make_state(tmp_path, monkeypatch)
        client, _handle = _deep_chain_client(_stream)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True

        with patch("asyncio.sleep", new_callable=AsyncMock):  # flake-ok: skips retry backoff
            await _run_chat(state, slot, "hello")
            await self._drain_bg(state)

        assert len(self._rows(shard_dir)) == 1
        assert not [m for m in slot.messages if m.get("role") == "error"]

    @pytest.mark.asyncio
    async def test_a_turn_on_the_recorded_provider_takes_no_spawn_fingerprint(
        self, tmp_path, monkeypatch
    ):
        """The fingerprint is taken for a provider with no record, and only then.

        A later turn on the provider already recorded would compute it on the
        turn's critical path and drop it; a provider replaced while the turn
        acquires one is fingerprinted once it is acquired.
        """
        from kiro_crew.acp.types import TurnUsage
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.dashboard.handlers import usage as usage_mod
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        monkeypatch.setattr(usage_mod, "_TOKEN_USAGE_DIR", tmp_path / "usage" / "tokens")
        spy = MagicMock(return_value=_A)
        monkeypatch.setattr(chat_runner, "managed_spec_missing", lambda _a: False)
        monkeypatch.setattr(chat_runner, "spawn_config_fingerprint", spy)
        monkeypatch.setattr(chat_runner, "refresh_config_stale", AsyncMock(return_value={}))

        async def _stream(msg):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok")
            yield LLMEvent(kind=EVENT_COMPLETE, usage=TurnUsage(credits=1.0))

        state = self._make_state(tmp_path, monkeypatch)
        client, _handle = _deep_chain_client(_stream)
        replacement, _handle2 = _deep_chain_client(_stream)
        # The ``LLMProvider`` default: no warm-pool receipt.
        client.pool_spawn_config = replacement.pool_spawn_config = None
        self._wire_sessions(state, client)
        live = {"provider": client}
        state.sessions.get_provider = MagicMock(side_effect=lambda _key: live["provider"])
        slot = state.get_or_create_slot("s1")
        slot._titled = True

        async def _turn() -> None:
            with patch("asyncio.sleep", new_callable=AsyncMock):  # flake-ok: skips retry backoff
                await _run_chat(state, slot, "hello")
                await self._drain_bg(state)

        await _turn()
        assert spy.call_count == 1, spy.call_count
        assert slot._spawn_config.describes(client)

        await _turn()
        assert spy.call_count == 1

        async def _replace(*_a, **_k):
            live["provider"] = replacement
            return replacement, True, False

        state.sessions.get_or_create = AsyncMock(side_effect=_replace)
        await _turn()
        assert spy.call_count == 2 and slot._spawn_config.describes(replacement)


# ── Bounded signature and a reload during the check ──────────────────────────


def test_the_agents_dir_signature_is_capped_with_an_overflow_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(stale_config, "SIGNATURE_MAX_SPEC_ENTRIES", 3)
    agents = tmp_path / "agents"
    agents.mkdir()
    for i in range(5):
        (agents / f"a{i}.json").write_text("{}")

    signature = stale_config._stat_spec_dir(agents)

    # The directory's stat, three kept entries, one overflow marker.
    assert len(signature) == 1 + 3 + 1
    marker = signature[-1]
    assert marker[0] == "overflow" and marker[1] == 2 and len(marker[2]) == 64
    assert "a4.json" not in repr(signature[:-1])


def test_a_change_past_the_cap_still_changes_the_signature(tmp_path, monkeypatch):
    monkeypatch.setattr(stale_config, "SIGNATURE_MAX_SPEC_ENTRIES", 3)
    agents = tmp_path / "agents"
    agents.mkdir()
    for i in range(5):
        (agents / f"a{i}.json").write_text("{}")
    st = agents.stat()
    before = stale_config._stat_spec_dir(agents)

    # An edit to a file past the cap, no new name.
    (agents / "a4.json").write_text('{"prompt": "edited past the cap"}')
    os.utime(agents, ns=(st.st_atime_ns, st.st_mtime_ns))
    edited = stale_config._stat_spec_dir(agents)
    assert edited[1:-1] == before[1:-1], "the kept entries did not change"
    assert edited[-1] != before[-1]

    # A new file past the cap.
    (agents / "a5.json").write_text("{}")
    assert stale_config._stat_spec_dir(agents)[-1] != edited[-1]


def test_the_default_cap_is_a_shared_named_constant():
    assert stale_config.SIGNATURE_MAX_SPEC_ENTRIES == 256


@pytest.mark.parametrize("what", ["provider", "record", "session"])
def test_a_reload_during_the_check_drops_the_stale_answer(kiro_home, tmp_path, monkeypatch, what):
    """The old process's answer must not re-badge the fresh session."""
    _write_spec(kiro_home, {"prompt": "a"})
    state, slot = _status_slot(tmp_path, monkeypatch)
    _write_spec(kiro_home, {"prompt": "b"})  # stale against the live process
    real = cs.current_config_fingerprint
    live = state.sessions.get_provider.return_value

    def _reload_mid_read(*args):
        # A Reload (or a rebind) lands while the fingerprint is read off the loop.
        if what == "provider":
            state.sessions.get_provider = MagicMock(return_value=_Provider())
        elif what == "record":
            slot._spawn_config = make_record(live, slot._spawn_config.inputs, _A)
        else:
            monkeypatch.setattr(cs, "effective_session_key", lambda _s: "dashboard:rebound")
        slot.config_stale, slot.config_stale_inputs = False, ""
        return real(*args)

    monkeypatch.setattr(cs, "current_config_fingerprint", _reload_mid_read)

    out = asyncio.run(cs.refresh_config_stale(state, slot))

    assert out["stale"] is None
    assert slot.config_stale is False and slot.config_stale_inputs == ""
