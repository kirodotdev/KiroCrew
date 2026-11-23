"""KAS approve-for-me: the engine switch, the per-session value, the audit row and the owner check."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew.acp import kas_approve_for_me as afm
from kiro_crew.acp import kas_pin

#: The real engine gate, before the autouse fixture stands in for it.
_REAL_GATE = kas_pin.engine_checks_auto_approvals


@pytest.fixture(autouse=True)
def _reset_owner_check(_floor_monkeypatch):
    """Each test starts with no owner leave, on an engine known to send its verdict."""
    afm.reset_owner_check()
    afm._ENGINE_SUPPORT[0] = True
    # The engine Crew runs is one that sends its verdict (the gate has its own tests).
    _floor_monkeypatch.setattr(
        "kiro_crew.acp.kas_pin.engine_checks_auto_approvals", lambda _v: True
    )
    _floor_monkeypatch.setattr("kiro_crew.acp.harness.kas._bundle_route_selected", lambda: True)
    yield
    afm.reset_owner_check()


def _option(value: str) -> list[dict[str, Any]]:
    return [{"id": "model", "currentValue": "auto"}, {"id": "approveForMe", "currentValue": value}]


class _Handle:
    def __init__(self, value: str | None, *, fail: bool = False, key: str = "dashboard:1"):
        self.config_options = [] if value is None else _option(value)
        self.writes: list[tuple[str, str]] = []
        self._fail = fail
        self._session_key = key

    async def set_config_option(self, config_id: str, value: str) -> None:
        if self._fail:
            raise RuntimeError("refused")
        self.writes.append((config_id, value))
        self.config_options = _option(value)


@pytest.mark.parametrize(
    ("current", "allowed", "target"),
    [
        (None, False, None),
        (None, True, None),
        ("off", False, None),
        ("off", True, "on"),
        ("on", True, None),
        ("on", False, "off"),
        ("files", True, "on"),
        ("files", False, "off"),
        ("", False, "off"),
    ],
)
def test_a_session_is_held_on_or_off_never_files(current, allowed, target):
    assert afm.settle_target(current, allowed=allowed) == target


def test_settle_writes_off_over_files_and_sends_nothing_without_the_option():
    files = _Handle("files")
    asyncio.run(afm.settle(files))
    assert files.writes == [("approveForMe", "off")]
    none = _Handle(None)
    asyncio.run(afm.settle(none))
    assert none.writes == []


def test_settle_writes_on_only_while_the_owner_allows_it():
    afm.register_owner_check(lambda key: key == "dashboard:1")
    opted = _Handle("files")
    asyncio.run(afm.settle(opted))
    assert opted.writes == [("approveForMe", "on")]
    other = _Handle("on", key="slack:1")
    asyncio.run(afm.settle(other))
    assert other.writes == [("approveForMe", "off")]


def test_an_owner_check_that_raises_holds_the_session_off():
    def boom(_key: str) -> bool:
        raise ValueError("x")

    afm.register_owner_check(boom)
    handle = _Handle("on")
    asyncio.run(afm.settle(handle))
    assert handle.writes == [("approveForMe", "off")]


def test_an_on_session_that_cannot_be_turned_off_refuses_the_turn():
    with pytest.raises(afm.ApproveForMeNotOff):
        asyncio.run(afm.settle(_Handle("on", fail=True)))
    # A failed write over ``files`` is logged, not raised.
    asyncio.run(afm.settle(_Handle("files", fail=True)))


def test_the_engine_switch_overrides_an_inherited_value():
    env = {afm.CLASSIFIER_MODE_ENV: "enforce"}
    afm.force_off(env)
    assert env[afm.CLASSIFIER_MODE_ENV] == "off"
    afm.enable(env)
    assert env[afm.CLASSIFIER_MODE_ENV] == "enforce"


def test_no_harness_inherits_the_engine_switch(monkeypatch):
    from kiro_crew.acp.client import _resolve_spawn_env
    from kiro_crew.acp.harness import harness_for
    from kiro_crew.acp.types import ACP_BACKEND_KAS, ACP_BACKEND_KIRO

    inherited = {afm.CLASSIFIER_MODE_ENV: "enforce"}
    kas_env = dict(inherited)
    harness_for(ACP_BACKEND_KAS).apply_spawn_env(kas_env)
    assert kas_env[afm.CLASSIFIER_MODE_ENV] == "off"
    kiro_env = dict(inherited)
    harness_for(ACP_BACKEND_KIRO).apply_spawn_env(kiro_env)
    assert kiro_env[afm.CLASSIFIER_MODE_ENV] == "off"
    client_env = _resolve_spawn_env(dict(inherited))
    assert client_env[afm.CLASSIFIER_MODE_ENV] == "off"


def test_only_an_opted_in_session_carries_enforce_in_its_own_env():
    afm.register_owner_check(lambda key: key == "dashboard:1")
    assert afm.session_env("dashboard:1", None) == {afm.CLASSIFIER_MODE_ENV: "enforce"}
    assert afm.session_env("dashboard:1", {"A": "1"}) == {
        "A": "1",
        afm.CLASSIFIER_MODE_ENV: "enforce",
    }
    # Not opted in: the env is left as the caller gave it, and a smuggled value is dropped.
    plain = {"A": "1"}
    assert afm.session_env("dashboard:2", plain) is plain
    assert afm.session_env("dashboard:2", None) is None
    assert afm.session_env("dashboard:2", {afm.CLASSIFIER_MODE_ENV: "enforce"}) is None


@pytest.mark.parametrize(
    ("backend", "own", "start", "want"),
    [
        ("kas", {afm.CLASSIFIER_MODE_ENV: "enforce"}, "off", "enforce"),
        ("kas", {}, "enforce", "off"),
        ("kas", None, "enforce", "off"),
        # kiro-cli's harness has already written ``off``; the runtime leaves it.
        ("", {afm.CLASSIFIER_MODE_ENV: "enforce"}, "off", "off"),
        # A backend that never reads the switch gets no value from Crew.
        ("claude", {afm.CLASSIFIER_MODE_ENV: "enforce"}, None, None),
        ("codex", {}, None, None),
    ],
)
def test_the_runtime_spawn_keeps_enforce_only_from_its_own_env(backend, own, start, want):
    # The gateway's or ``agent.env``'s value is already in *env*; it never decides.
    env = {} if start is None else {afm.CLASSIFIER_MODE_ENV: start}
    afm.apply_runtime_env(env, backend, own)
    assert env.get(afm.CLASSIFIER_MODE_ENV) == want


def test_kas_is_asked_to_list_the_option():
    from kiro_crew.acp.types import KAS_CLIENT_CAPABILITIES

    assert KAS_CLIENT_CAPABILITIES["_meta"]["kiro"]["approveForMeOption"] is True


class _Sel:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def log_tool_invocation(self, **kw: Any) -> None:
        self.rows.append(kw)


def _frame(call_id: str, approved: bool) -> dict[str, Any]:
    meta = {"kiro": {"trustClassifierApproved": True}} if approved else {"kiro": {}}
    return {
        "sessionUpdate": "tool_call",
        "toolCallId": call_id,
        "title": "Run: ls",
        "kind": "execute",
        "_meta": meta,
    }


def test_the_policy_denies_approve_for_me_unless_a_rule_lists_it(monkeypatch):
    from types import SimpleNamespace as NS

    from kiro_crew import approval_mode_policy as amp

    def verdict(control) -> bool:
        monkeypatch.setattr(amp, "_ceiling_control", lambda: control)
        monkeypatch.setattr(
            "kiro_crew.platform.governance_parts.evaluator._query_level",
            lambda c, _scope, item: NS(permitted=item in c.allow),
        )
        amp.reset_state()
        monkeypatch.setattr(amp, "_bootstrap", amp._resolve_now)
        return amp.session_mode_permitted("approve_for_me")

    # No rule, a rule allowing Trust, a deny-mode rule: refused, and hidden.
    assert verdict(None) is False
    assert verdict(NS(mode="allow", allow=("normal", "trust", "trust_reads"))) is False
    assert "approve_for_me" in amp.disabled_session_modes()
    assert verdict(NS(mode="deny", allow=(), deny=("yolo",))) is False
    # Listed by name: permitted, and Trust is unaffected either way.
    assert verdict(NS(mode="allow", allow=("normal", "trust", "approve_for_me"))) is True
    assert "approve_for_me" not in amp.disabled_session_modes()
    assert amp.clamp_session_mode("trust") == "trust"
    amp.reset_state()


def _state(slots: list[Any], *, yolo: bool = False, keys: set[str] | None = None) -> Any:
    state = SimpleNamespace(
        sessions=SimpleNamespace(_fold_key=lambda key: key),
        _slots={s.key: s for s in slots},
        is_yolo_active=lambda: yolo,
        yolo_reaches=lambda _slot: True,
    )
    if keys is not None:
        state._approve_for_me_keys = keys
    return state


def _slot(key: str, **flags: Any) -> Any:
    return SimpleNamespace(
        key=key,
        linked_session_key="",
        _trust=flags.get("trust", False),
        _trust_reads=flags.get("reads", False),
        _trust_scope="",
        running=flags.get("running", False),
        _turn_owner_sent=flags.get("owner_sent", True),
    )


@pytest.fixture
def _permitted(monkeypatch):
    from kiro_crew.dashboard import approve_for_me as dafm

    monkeypatch.setattr(dafm, "approve_for_me_permitted", lambda: True)
    monkeypatch.setattr(dafm, "effective_session_key", lambda slot: slot.key)
    monkeypatch.setattr(dafm, "profile_binds", lambda _key, _agent="": False)
    yield dafm


def test_the_owner_allows_only_a_session_a_person_turned_on(_permitted):
    dafm = _permitted
    slot = _slot("dashboard:1")
    assert dafm.owner_allows(_state([slot], keys={"dashboard:1"}), "dashboard:1") is True
    assert dafm.owner_allows(_state([slot]), "dashboard:1") is False
    # A key no live slot addresses is held off.
    assert dafm.owner_allows(_state([], keys={"dashboard:1"}), "dashboard:1") is False


@pytest.mark.parametrize("flags", [{"trust": True}, {"reads": True}])
def test_trust_or_reads_on_the_session_holds_it_off(_permitted, flags):
    state = _state([_slot("dashboard:1", **flags)], keys={"dashboard:1"})
    assert _permitted.owner_allows(state, "dashboard:1") is False


def test_a_reaching_yolo_grant_holds_it_off(_permitted):
    state = _state([_slot("dashboard:1")], yolo=True, keys={"dashboard:1"})
    assert _permitted.owner_allows(state, "dashboard:1") is False


def test_a_policy_deny_holds_it_off(_permitted, monkeypatch):
    monkeypatch.setattr(_permitted, "approve_for_me_permitted", lambda: False)
    state = _state([_slot("dashboard:1")], keys={"dashboard:1"})
    assert _permitted.owner_allows(state, "dashboard:1") is False


def test_a_dashboard_card_turn_keeps_its_human_approval_floor(_permitted, monkeypatch):
    from kiro_crew.dashboard import card_send

    state = _state([_slot("dashboard:1")], keys={"dashboard:1"})
    assert _permitted.owner_allows(state, "dashboard:1") is True
    with card_send.card_turn("dashboard:1"):
        assert card_send.card_turn_running("dashboard:1")
        assert _permitted.owner_allows(state, "dashboard:1") is False
    assert _permitted.owner_allows(state, "dashboard:1") is True


@pytest.mark.parametrize("mode", ["normal", "trust", "trust_reads"])
def test_any_mode_set_through_the_shared_mutation_ends_approve_for_me(tmp_path, monkeypatch, mode):
    """``apply_approval_mode`` is the one mutation every entry point calls."""
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.dashboard import chat_handlers
    from kiro_crew.dashboard.approve_for_me import approve_for_me_keys
    from kiro_crew.dashboard.state import DashboardState
    from kiro_crew.history import ConversationLog

    monkeypatch.setattr(chat_handlers, "session_mode_permitted", lambda _m: True)
    sessions = MagicMock(count=0)
    sessions.get_pid = MagicMock(return_value=None)
    sessions.get_provider = MagicMock(return_value=None)
    sessions.remove = AsyncMock()
    state = DashboardState(
        sessions=sessions,
        crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0.0,
        conversation_log=ConversationLog(base_dir=tmp_path),
    )
    slot = state.get_or_create_slot("s1")
    key = chat_handlers.effective_session_key(slot)
    approve_for_me_keys(state).add(key)
    answer = asyncio.run(
        chat_handlers.apply_approval_mode(
            state,
            mode=mode,
            slot=slot,
            slot_key=slot.key,
            request_app="",
            cron_creator="",
            audit_caller=lambda label: label,
        )
    )
    assert answer.status == 200
    assert key not in approve_for_me_keys(state)


@pytest.mark.parametrize("second", ["s1", "dashboard_s1", "dashboard:s1"])
def test_a_new_slot_under_a_deleted_slots_name_starts_with_approve_for_me_off(
    tmp_path, monkeypatch, second
):
    """The live slot keeps approve-for-me; a slot made again under its name does not inherit it."""
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.dashboard import approve_for_me as afm_dash
    from kiro_crew.dashboard.chat_utils_slots import effective_session_key
    from kiro_crew.dashboard.state import DashboardState
    from kiro_crew.dashboard.state_parts.slot_directory import SlotOrigin
    from kiro_crew.history import ConversationLog

    monkeypatch.setattr(afm_dash, "session_mode_permitted", lambda _m: True)
    monkeypatch.setattr(afm_dash, "profile_binds", lambda *_a, **_k: False)
    sessions = MagicMock(count=0)
    sessions.get_pid = MagicMock(return_value=None)
    sessions.get_provider = MagicMock(return_value=None)
    sessions.remove = AsyncMock()
    sessions._fold_key = lambda k: k
    state = DashboardState(
        sessions=sessions,
        crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0.0,
        conversation_log=ConversationLog(base_dir=tmp_path),
    )
    slot = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    key = effective_session_key(slot)
    afm_dash.approve_for_me_keys(state).add(key)
    assert afm_dash.owner_allows(state, key) is True
    # Getting the live slot again (a second tab, a later send) keeps it on.
    assert state.get_or_create_slot("s1") is slot
    assert afm_dash.owner_allows(state, key) is True
    state._slots.pop(slot.key)  # where close_slot ends
    again = state.get_or_create_slot(second, origin=SlotOrigin.USER)
    assert effective_session_key(again) == key
    assert afm_dash.owner_allows(state, key) is False


@pytest.mark.parametrize(
    "module",
    [
        "src/kiro_crew/dashboard/chat_api/slot_approval.py",  # dashboard picker (api_chat_mode)
        "src/kiro_crew/slack/code_channels/approval_picker.py",  # Slack code channel
        "src/kiro_crew/dashboard/crewmate_approval.py",  # crewmate live DM slot
        "src/kiro_crew/dashboard/agent_admin/crew_update.py",  # crewmate profile write
        "src/kiro_crew/dashboard/workflow_session_port.py",  # workflow session
    ],
)
def test_every_entry_point_sets_a_mode_through_the_shared_mutation(module):
    """Each one reaches the cleanup because each calls ``apply_approval_mode``."""
    import ast
    from pathlib import Path

    repo_root = Path(__file__).resolve().parent.parent
    tree = ast.parse((repo_root / module).read_text(encoding="utf-8"))
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "apply_approval_mode"
    ]
    assert calls, f"{module} sets an approval mode without apply_approval_mode"


@pytest.mark.parametrize(
    "flag, value",
    [
        ("linked_session_key", "slack:123.456"),
        ("_slack_linked", True),
        ("_slack_channel", "C123"),
        ("channel_origin", True),
        ("_channel_runtime_origin", True),
    ],
)
def test_a_session_tied_to_a_channel_holds_it_off(_permitted, flag, value):
    slot = _slot("dashboard:1")
    setattr(slot, flag, value)
    state = _state([slot], keys={"dashboard:1"})
    assert _permitted.owner_allows(state, "dashboard:1") is False


@pytest.mark.parametrize("flag", ["_turn_channel_origin", "_turn_channel_narrowed"])
def test_a_turn_from_a_channel_holds_it_off(_permitted, flag):
    slot = _slot("dashboard:1", running=True)
    state = _state([slot], keys={"dashboard:1"})
    assert _permitted.owner_allows(state, "dashboard:1") is True
    setattr(slot, flag, True)
    assert _permitted.owner_allows(state, "dashboard:1") is False


def _route_app(slot: Any) -> Any:
    from unittest.mock import MagicMock

    from aiohttp import web

    from kiro_crew.dashboard.chat_handlers import api_chat_slot_approve_for_me

    state = MagicMock()
    state._slots = {slot.key: slot}
    state.sessions.get_provider = MagicMock(return_value=None)
    state._approve_for_me_keys = set()
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/approve-for-me", api_chat_slot_approve_for_me)
    return app


@pytest.mark.parametrize("busy", [False, True])
def test_the_route_refuses_a_channel_slot_and_a_pending_first_turn(monkeypatch, busy):
    """A channel-tied slot is refused; a turn pending before any session exists is busy."""
    from unittest.mock import AsyncMock

    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.dashboard import chat_handlers
    from kiro_crew.dashboard.chat_api import slot_effort
    from kiro_crew.dashboard.state_parts.chat_slot import _ChatSlot

    monkeypatch.setattr(chat_handlers, "deny_session_approval_caller", AsyncMock(return_value=None))
    monkeypatch.setattr(chat_handlers, "approve_for_me_permitted", lambda: True)
    monkeypatch.setattr(
        slot_effort,
        "_configured_backend_for_slot",
        AsyncMock(return_value=next(iter(chat_handlers.ACP_BACKENDS_APPROVE_FOR_ME))),
    )
    seen: list[object] = []

    def _busy(_state, _slot, _key, provider):
        seen.append(provider)
        return busy

    monkeypatch.setattr(chat_handlers, "_switch_target_busy", _busy)
    slot = _ChatSlot("test")
    if not busy:
        slot.linked_session_key = "slack:123.456"
    app = _route_app(slot)

    async def _run() -> tuple[int, dict]:
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/test/approve-for-me", json={"enabled": True})
            return resp.status, await resp.json()

    status, body = asyncio.run(_run())
    assert status == 409
    if busy:
        assert body["code"] == "turn_in_flight"
        # The busy check ran though no session exists yet.
        assert seen == [None]
    else:
        assert body["code"] == "approve_for_me_not_owners"
    assert app["state"]._approve_for_me_keys == set()


def _capture(name: str) -> list[dict[str, Any]]:
    import json
    from pathlib import Path

    path = Path(__file__).resolve().parent / "fixtures" / "acp_frames" / "kas" / name
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()[1:]]


def _offered(frames: list[dict[str, Any]]) -> str | None:
    """The ``approveForMe`` value the capture's ``session/new`` answer lists."""
    answer = next(f["result"] for f in frames if "configOptions" in (f.get("result") or {}))
    return afm.option_value(answer["configOptions"])


def _tool_updates(frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        f["params"]["update"]
        for f in frames
        if f.get("method") == "session/update"
        and f["params"]["update"].get("sessionUpdate") in ("tool_call", "tool_call_update")
    ]


def test_the_live_off_capture_offers_nothing_and_asks():
    """KAS spawned with the switch off, as every unchosen Crew session: same as main."""
    frames = _capture("approve-for-me-off-live.jsonl")
    assert _offered(frames) is None
    assert afm.settle_target(None, allowed=True) is None
    assert [f for f in frames if f.get("method") == "session/request_permission"]
    assert not any(afm.classifier_approved(u) for u in _tool_updates(frames))


@pytest.mark.parametrize(
    "flag, value",
    [("_app", "some-app"), ("unattended", True), ("_origin", "cron"), ("_origin", "app")],
)
def test_a_slot_no_person_drives_holds_it_off(_permitted, flag, value):
    slot = _slot("dashboard:1")
    setattr(slot, flag, value)
    assert _permitted.owner_allows(_state([slot], keys={"dashboard:1"}), "dashboard:1") is False


def test_a_running_turn_the_owner_did_not_send_holds_it_off(_permitted):
    """Another token holder, a cron delivery, a loop wake or an app injection."""
    owner = _slot("dashboard:1", running=True, owner_sent=True)
    assert _permitted.owner_allows(_state([owner], keys={"dashboard:1"}), "dashboard:1") is True
    other = _slot("dashboard:1", running=True, owner_sent=False)
    assert _permitted.owner_allows(_state([other], keys={"dashboard:1"}), "dashboard:1") is False
    # Between turns the stale flag does not matter; the next turn is judged on its own.
    idle = _slot("dashboard:1", running=False, owner_sent=False)
    assert _permitted.owner_allows(_state([idle], keys={"dashboard:1"}), "dashboard:1") is True


def test_a_governance_profile_holds_it_off(_permitted, monkeypatch):
    monkeypatch.setattr(_permitted, "profile_binds", lambda _key, _agent="": True)
    state = _state([_slot("dashboard:1")], keys={"dashboard:1"})
    assert _permitted.owner_allows(state, "dashboard:1") is False


def test_re_picking_the_mode_it_already_holds_succeeds_during_a_turn(monkeypatch):
    """Nothing changes, so a running turn is no reason to answer 409."""
    from unittest.mock import AsyncMock

    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.dashboard import chat_handlers
    from kiro_crew.dashboard.chat_api import slot_effort
    from kiro_crew.dashboard.state_parts.chat_slot import _ChatSlot

    monkeypatch.setattr(chat_handlers, "deny_session_approval_caller", AsyncMock(return_value=None))
    monkeypatch.setattr(chat_handlers, "approve_for_me_permitted", lambda: True)
    monkeypatch.setattr(
        slot_effort,
        "_configured_backend_for_slot",
        AsyncMock(return_value=next(iter(chat_handlers.ACP_BACKENDS_APPROVE_FOR_ME))),
    )
    monkeypatch.setattr(chat_handlers, "_switch_target_busy", lambda *_a: True)
    slot = _ChatSlot("test")
    app = _route_app(slot)
    app["state"]._approve_for_me_keys = {chat_handlers.effective_session_key(slot)}

    async def _run() -> tuple[int, dict]:
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/test/approve-for-me", json={"enabled": True})
            return resp.status, await resp.json()

    assert asyncio.run(_run()) == (200, {"ok": True, "approve_for_me": True})


def test_a_task_profile_bound_to_the_slots_agent_holds_it_off(_permitted, monkeypatch):
    monkeypatch.setattr(_permitted, "profile_binds", lambda _key, agent="": agent == "researcher")
    slot = _slot("dashboard:1")
    assert _permitted.owner_allows(_state([slot], keys={"dashboard:1"}), "dashboard:1") is True
    slot.agent = "researcher"
    assert _permitted.owner_allows(_state([slot], keys={"dashboard:1"}), "dashboard:1") is False


def test_profile_binds_asks_the_agents_task_bind(monkeypatch):
    from kiro_crew.dashboard import approve_for_me as dafm

    seen = []

    def _resolve(key, *, agent=""):
        seen.append((key, agent))
        return object() if agent == "researcher" else None

    monkeypatch.setattr("kiro_crew.platform.governance_profiles.resolve_active_scope", _resolve)
    assert dafm.profile_binds("dashboard:1") is False
    assert dafm.profile_binds("dashboard:1", "researcher") is True
    assert seen == [("dashboard:1", ""), ("dashboard:1", "researcher")]

    def _boom(key, *, agent=""):
        raise RuntimeError("x")

    monkeypatch.setattr("kiro_crew.platform.governance_profiles.resolve_active_scope", _boom)
    assert dafm.profile_binds("dashboard:1") is True


def test_the_route_refuses_to_turn_it_on_where_a_profile_binds(monkeypatch):
    from unittest.mock import AsyncMock

    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.dashboard import chat_handlers
    from kiro_crew.dashboard.chat_api import slot_effort
    from kiro_crew.dashboard.state_parts.chat_slot import _ChatSlot

    monkeypatch.setattr(chat_handlers, "deny_session_approval_caller", AsyncMock(return_value=None))
    monkeypatch.setattr(chat_handlers, "approve_for_me_permitted", lambda: True)
    monkeypatch.setattr(chat_handlers, "approve_for_me_profile_binds", lambda _k, _a="": True)
    monkeypatch.setattr(
        slot_effort,
        "_configured_backend_for_slot",
        AsyncMock(return_value=next(iter(chat_handlers.ACP_BACKENDS_APPROVE_FOR_ME))),
    )
    app = _route_app(_ChatSlot("test"))

    async def _run() -> tuple[int, dict]:
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/test/approve-for-me", json={"enabled": True})
            return resp.status, await resp.json()

    status, body = asyncio.run(_run())
    assert status == 403
    assert body["code"] == "mode_disabled_by_policy"
    assert app["state"]._approve_for_me_keys == set()


def test_a_busy_session_is_never_reset_by_a_mode_change(monkeypatch):
    """A turn waiting on a card or a question keeps them; the restart waits for idle."""
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.dashboard import chat_handlers
    from kiro_crew.dashboard.chat_api import slot_effort

    reset = AsyncMock(return_value=True)
    monkeypatch.setattr(chat_handlers, "_reset_slot_session", reset)
    monkeypatch.setattr(chat_handlers, "_switch_target_busy", lambda *_a, **_k: True)
    state = MagicMock()
    state.sessions.get_provider = MagicMock(return_value=object())
    slot = SimpleNamespace(key="s1")
    assert (
        asyncio.run(slot_effort._restart_for_approve_for_me(state, slot, "dashboard:s1")) is False
    )
    reset.assert_not_awaited()
    monkeypatch.setattr(chat_handlers, "_switch_target_busy", lambda *_a, **_k: False)
    assert asyncio.run(slot_effort._restart_for_approve_for_me(state, slot, "dashboard:s1")) is True
    reset.assert_awaited_once()


# ── The engine's verdict on a permission request ────────────────────────────


@pytest.mark.parametrize(
    "auto, by",
    [
        ({"decision": "allow", "by": "classifier"}, "classifier"),
        ({"decision": "allow", "by": "preset"}, "preset"),
        ({"decision": "prompt", "by": "classifier"}, ""),
        ({"decision": "allow", "by": "other"}, ""),
        ({"decision": "allow"}, ""),
        ("allow", ""),
        (None, ""),
    ],
)
def test_only_the_documented_verdict_shape_counts(auto, by):
    params = {"_meta": {"kiro": {"autoApproval": auto}}} if auto is not None else {}
    assert afm.auto_approval_by(params) == by


@pytest.mark.parametrize(
    ("backend", "own", "declared"),
    [
        ("kas", {afm.CLASSIFIER_MODE_ENV: "enforce"}, True),
        ("kas", {}, False),
        ("kas", None, False),
        ("", {afm.CLASSIFIER_MODE_ENV: "enforce"}, False),
    ],
)
def test_crew_declares_it_checks_auto_approvals_only_on_an_opted_in_process(backend, own, declared):
    from kiro_crew.acp.types import KAS_CLIENT_CAPABILITIES

    before = repr(KAS_CLIENT_CAPABILITIES)
    caps = afm.with_checks_auto_approvals(KAS_CLIENT_CAPABILITIES, backend, own)
    assert ("checksAutoApprovals" in caps["_meta"]["kiro"]) is declared
    assert caps["_meta"]["kiro"]["approveForMeOption"] is True
    assert repr(KAS_CLIENT_CAPABILITIES) == before
    assert "checksAutoApprovals" not in KAS_CLIENT_CAPABILITIES["_meta"]["kiro"]


def test_the_runtime_handshake_adds_the_declaration_from_its_own_env():
    import inspect

    from kiro_crew.acp import runtime_spawn_launch

    src = inspect.getsource(runtime_spawn_launch._handshake_client_capabilities)
    assert "with_checks_auto_approvals(" in src
    assert 'getattr(self, "_extra_env", None)' in src


@pytest.mark.parametrize("kas", [True, False])
def test_the_verdict_is_read_only_off_a_kas_request(kas):
    from kiro_crew.acp._dispatch_parts.permission_requests import build_permission_event
    from kiro_crew.acp.types import JsonRpcMessage

    msg = JsonRpcMessage(
        id=7,
        method="session/request_permission",
        params={
            "sessionId": "s1",
            "toolCall": {"toolCallId": "t1", "title": "ls", "kind": "execute"},
            "options": [
                {"optionId": "a", "kind": "allow_once", "name": "Allow"},
                {"optionId": "r", "kind": "reject_once", "name": "Reject"},
            ],
            "_meta": {"kiro": {"autoApproval": {"decision": "allow", "by": "classifier"}}},
        },
    )
    event, _ = build_permission_event(msg, kas_consent_meta=kas)
    assert event is not None
    assert event.kas_auto_approval == ("classifier" if kas else "")


def test_no_released_engine_is_known_to_check_auto_approvals():
    assert kas_pin.KAS_CHECKS_AUTO_APPROVALS_MIN_ENGINE_VERSION is None
    assert _REAL_GATE("9.9.9") is False


def test_the_engine_gate_reads_the_floor_and_an_unknown_version_as_no(monkeypatch):
    monkeypatch.setattr(kas_pin, "KAS_CHECKS_AUTO_APPROVALS_MIN_ENGINE_VERSION", (0, 66, 120))
    assert _REAL_GATE("0.66.120") is True
    assert _REAL_GATE("0.66.119") is False
    assert _REAL_GATE(None) is False
    assert _REAL_GATE("garbage") is False


@pytest.mark.parametrize(
    "bundle, engine, want", [(True, True, True), (False, True, False), (True, False, False)]
)
def test_only_crews_own_engine_at_a_checking_version_supports_it(monkeypatch, bundle, engine, want):
    monkeypatch.setattr("kiro_crew.acp.harness.kas._bundle_route_selected", lambda: bundle)
    monkeypatch.setattr("kiro_crew.acp.kas_pin.engine_checks_auto_approvals", lambda _v: engine)
    assert afm.refresh_engine_support() is want
    assert afm.engine_supported() is want


def test_an_engine_without_support_holds_every_session_off():
    afm.register_owner_check(lambda _key: True)
    afm._ENGINE_SUPPORT[0] = False
    assert afm.may_stay_on("dashboard:1") is False
    assert afm.session_env("dashboard:1", None) is None
    handle = _Handle("on")
    asyncio.run(afm.settle(handle))  # the fixture's engine reads as supported again
    afm._ENGINE_SUPPORT[0] = None
    assert afm.may_stay_on("dashboard:1") is False


def test_an_engine_seen_approving_on_its_own_is_marked_and_the_turn_cancelled(monkeypatch):
    """With the capability declared the engine never runs a call on its own verdict."""
    sink = _Sel()
    monkeypatch.setattr("kiro_crew.sel.sel", lambda: sink)
    afm.register_owner_check(lambda _key: True)
    cancels: list[int] = []

    class _Live:
        _session_key = "dashboard:1"
        _session_id = "s1"
        active_agent = "kirocrew"

        def __init__(self) -> None:
            self._audit_tasks: set[Any] = set()

        async def cancel(self) -> None:
            cancels.append(1)

    async def run() -> None:
        handle = _Live()
        # The first frame names the call; the marked update carries no title or kind.
        afm.audit_classifier_approved(
            handle,
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "t1",
                "title": "Run: ls",
                "kind": "execute",
            },
        )
        marked = {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "t1",
            "_meta": {"kiro": {"trustClassifierApproved": True}},
        }
        afm.audit_classifier_approved(handle, marked)
        afm.audit_classifier_approved(handle, marked)
        while handle._audit_tasks:
            await asyncio.gather(*list(handle._audit_tasks))

    asyncio.run(run())
    assert cancels == [1]
    assert [(r["tool_name"], r["tool_kind"], r["request_id"]) for r in sink.rows] == [
        ("Run: ls", "execute", "t1")
    ]
    assert afm.engine_supported() is False
    assert afm.refresh_engine_support() is False  # stays off for this gateway's life
    assert afm.may_stay_on("dashboard:1") is False


def _denied(call_id: str, effect: str = "evaluate") -> dict[str, Any]:
    denial = {"effect": "deny", "matchedRule": {"effect": effect}, "capability": "shell"}
    return {
        "sessionUpdate": "tool_call_update",
        "toolCallId": call_id,
        "status": "failed",
        "content": [{"type": "content", "content": {"type": "text", "text": "refused"}}],
        "_meta": {"kiro": {"policyDenial": denial}},
    }


def test_a_call_kas_refused_itself_is_marked_and_recorded_once(monkeypatch, caplog):
    """KAS's classifier refuses without asking Crew: the row says so, one SEL row, one warning."""
    sink = _Sel()
    monkeypatch.setattr("kiro_crew.sel.sel", lambda: sink)
    handle = SimpleNamespace(_session_key="dashboard:1", _session_id="s1", active_agent="kirocrew")
    afm.audit_classifier_approved(handle, _frame("t1", False))
    first, again = _denied("t1"), _denied("t1")
    with caplog.at_level("WARNING", logger=afm.__name__):
        afm.audit_classifier_approved(handle, first)
        afm.audit_classifier_approved(handle, again)
    assert first["content"][0]["content"]["text"] == afm.KAS_BLOCKED_LINE
    assert first["content"][1]["content"]["text"] == "refused"
    assert [(r["tool_name"], r["outcome"], r["metadata"]["reason"]) for r in sink.rows] == [
        ("Run: ls", "denied", "approve_for_me_kas_denied")
    ]
    assert len([r for r in caplog.records if "refused a tool call" in r.getMessage()]) == 1
    # A refusal is not a call the engine ran on its own: the option stays offered.
    assert afm.engine_supported() is True


def test_only_a_classifier_refusal_counts_as_kas_blocking():
    assert afm.classifier_denied(_denied("t1")) is True
    assert afm.classifier_denied(_denied("t1", effect="deny")) is False
    assert afm.classifier_denied(_frame("t1", False)) is False
    assert afm.classifier_denied({"_meta": {"kiro": {"policyDenial": "x"}}}) is False


def test_the_tool_row_of_a_kas_refusal_starts_with_the_blocked_line():
    """Through the shared parser: the row's output carries the marked line first."""
    from kiro_crew.acp._dispatch_parts.tool_result_events import _build_tool_result_event

    update = _denied("t2")
    afm.audit_classifier_approved(
        SimpleNamespace(_session_key="dashboard:1", _session_id="s1", active_agent="k"), update
    )
    event = _build_tool_result_event(update)
    assert event is not None and event.tool_output.startswith(afm.KAS_BLOCKED_LINE)


def test_a_long_tool_call_id_is_cut_before_it_is_kept(monkeypatch):
    sink = _Sel()
    monkeypatch.setattr("kiro_crew.sel.sel", lambda: sink)
    handle = SimpleNamespace(_session_key="dashboard:1", _session_id="s1", active_agent="kirocrew")
    afm.audit_classifier_approved(handle, _frame("x" * 10_000, True))
    assert len(sink.rows[0]["request_id"]) == afm.CALL_ID_MAX_CHARS
    assert all(len(k) <= afm.CALL_ID_MAX_CHARS for k in handle._kas_classifier_audited)


# ── Crew's permission path answers the verdict ──────────────────────────────


def _rung_state(confirm: tuple[str, ...] = ()) -> Any:
    hooks = SimpleNamespace(
        _config=SimpleNamespace(confirm_tools=list(confirm), confirm_tools_unknown=False)
    )
    return SimpleNamespace(context_builder=SimpleNamespace(hooks=hooks))


def _rung_event(server: str = "", tool: str = "execute_bash") -> Any:
    return SimpleNamespace(
        mcp_server_name=server, tool_name=tool, title=f"@{server}/{tool}" if server else "ls"
    )


@pytest.mark.parametrize(
    "engine, owner, confirm, server, want",
    [
        (True, True, (), "", True),
        (False, True, (), "", False),
        (True, False, (), "", False),
        (True, True, ("@srv/*",), "srv", False),
        (True, True, ("@other/*",), "srv", True),
        # A native call is matched on its title and its tool name too.
        (True, True, ("ls",), "", False),
        (True, True, ("execute_bash",), "", False),
        (True, True, ("@srv/*",), "", True),
    ],
)
def test_the_verdict_approves_only_after_the_owner_and_confirm_checks(
    monkeypatch, engine, owner, confirm, server, want
):
    from kiro_crew.dashboard import approve_for_me as dafm
    from kiro_crew.dashboard import chat_runner  # noqa: F401 - composes tool_approval's names
    from kiro_crew.dashboard.chat_turn import tool_approval

    monkeypatch.setattr("kiro_crew.platform.governance.confirm_tools_unknown_now", lambda: False)
    afm._ENGINE_SUPPORT[0] = engine
    monkeypatch.setattr(dafm, "owner_allows", lambda _s, _k: owner)
    got = tool_approval._approve_for_me_allows(
        _rung_state(confirm), "dashboard:1", _rung_event(server)
    )
    assert got is want


def _afm_turn(
    monkeypatch,
    *,
    owner_ok: bool,
    title: str = "ls",
    tool_kind: str = "execute",
    confirm: tuple[str, ...] = (),
    tool_name: str = "",
):
    import asyncio

    from turn_harness import UNANSWERED, Do, SlotSpec, TurnScript, run_turn

    from kiro_crew.acp.types import (
        EVENT_COMPLETE,
        EVENT_PERMISSION_REQUEST,
        STOP_REASON_END_TURN,
        AcpEvent,
    )

    monkeypatch.setattr("kiro_crew.dashboard.approve_for_me.owner_allows", lambda *_a: owner_ok)
    monkeypatch.setattr("kiro_crew.platform.governance.confirm_tools_unknown_now", lambda: False)

    def _hooks(ctx: Any) -> None:
        # The operator's hooks config, as the context builder holds it, put in
        # place once the prompt is built (the harness runs with no builder).
        from kiro_crew.hooks import HookManager, HooksConfig

        hooks = HookManager(HooksConfig(confirm_tools=list(confirm)))
        ctx.state.context_builder = SimpleNamespace(hooks=hooks)

    prompt = AcpEvent(
        kind=EVENT_PERMISSION_REQUEST,
        request_id="req-afm-1",
        title=title,
        tool_kind=tool_kind,
        tool_name=tool_name,
        kas_auto_approval="classifier",
    )
    done = AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)
    return asyncio.run(
        run_turn(
            TurnScript(events=[Do(_hooks), prompt, done], answers={"req-afm-1": UNANSWERED}),
            slot=SlotSpec(),
        )
    )


def test_the_verdict_approves_a_call_after_crews_checks(monkeypatch):
    """An owner-checked verdict answers the call with no card and an SEL row naming by=."""
    record = _afm_turn(monkeypatch, owner_ok=True)
    assert record.approval_cards == []
    rows = record.audits(tool_name="ls")
    assert any((r.get("metadata") or {}).get("reason") == "approve_for_me" for r in rows), rows
    assert all(
        (r.get("metadata") or {}).get("by") == "classifier"
        for r in rows
        if (r.get("metadata") or {}).get("reason") == "approve_for_me"
    )


def test_the_verdict_asks_when_the_owner_check_fails(monkeypatch):
    """Without the owner check the verdict counts for nothing: the normal card shows."""
    record = _afm_turn(monkeypatch, owner_ok=False)
    assert len(record.approval_cards) == 1


def test_a_kas_shell_call_is_named_by_its_kiro_cli_alias(monkeypatch):
    """KAS stamps a shell call ``run_command`` with a model-written title; the
    operator's ``execute_bash`` confirm pattern still names it, so a card shows."""
    record = _afm_turn(
        monkeypatch,
        owner_ok=True,
        title="Print probe string",
        tool_name="run_command",
        confirm=("execute_bash",),
    )
    assert len(record.approval_cards) == 1
    assert not [
        r for r in record.audits() if (r.get("metadata") or {}).get("reason") == "approve_for_me"
    ]


@pytest.mark.parametrize("pattern", ["ls", "l*"])
def test_the_verdict_asks_for_a_native_call_confirm_tools_names(monkeypatch, pattern):
    """A native shell call the operator's ``hooks.confirm_tools`` names still shows a card."""
    record = _afm_turn(monkeypatch, owner_ok=True, confirm=(pattern,))
    assert len(record.approval_cards) == 1
    assert not [
        r for r in record.audits() if (r.get("metadata") or {}).get("reason") == "approve_for_me"
    ]


def test_a_kept_label_is_bounded():
    """An engine-written title or kind is cut before it is kept per call."""
    handle = SimpleNamespace()
    title, kind = afm._note_label(handle, "c1", {"title": "t" * 100_000, "kind": "k" * 1000})
    assert (len(title), len(kind)) == (afm._LABEL_TITLE_MAX, afm._LABEL_KIND_MAX)
    assert handle._kas_call_labels["c1"] == (title, kind)


def test_the_picker_and_route_hide_it_on_an_engine_without_support(monkeypatch):
    from unittest.mock import AsyncMock

    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.dashboard import chat_handlers
    from kiro_crew.dashboard.chat_api import slot_effort
    from kiro_crew.dashboard.state_parts.chat_slot import _ChatSlot

    backend = next(iter(chat_handlers.ACP_BACKENDS_APPROVE_FOR_ME))
    slot = _ChatSlot("test")
    afm._ENGINE_SUPPORT[0] = False
    assert slot_effort._approve_for_me_field(SimpleNamespace(), slot, backend) == {}
    monkeypatch.setattr(chat_handlers, "deny_session_approval_caller", AsyncMock(return_value=None))
    monkeypatch.setattr(chat_handlers, "approve_for_me_permitted", lambda: True)
    monkeypatch.setattr(chat_handlers, "approve_for_me_refresh_engine_support", lambda: False)
    monkeypatch.setattr(
        slot_effort, "_configured_backend_for_slot", AsyncMock(return_value=backend)
    )
    app = _route_app(slot)

    async def _run() -> tuple[int, dict]:
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/test/approve-for-me", json={"enabled": True})
            return resp.status, await resp.json()

    status, body = asyncio.run(_run())
    assert (status, body["code"]) == (409, "approve_for_me_not_offered")
    assert app["state"]._approve_for_me_keys == set()
