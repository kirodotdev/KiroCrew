# SPDX-License-Identifier: Apache-2.0
"""A chat that keeps its record's agent after its Bot was set to another one says so.

A ``member`` record keeps the template it names (``test_mate_chat_agent_resolution``
pins that rule), so after ``kirocrew agent update <bot> --kiro-agent <new>`` an open
chat keeps running its old agent while a new chat runs ``<new>``. The chat tells the
user once per new agent: a notice row naming both agents, and in the Bot's own
thread, whose chat draws no notice row, one hidden turn in which the Bot says it.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_agent_selection_helpers import _finished, _turn_state
from chat_test_helpers import _make_app_with_agent_routes, drain_background_tasks
from dashboard_owner_helpers import as_owner

import kiro_crew.config.paths as paths_mod
from kiro_crew import members as members_mod
from kiro_crew.config import loader
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig, resolve_agent_bindings
from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard import gateway_notice as gn
from kiro_crew.dashboard import mate_thread_wakes as wakes
from kiro_crew.dashboard import member_rebind_notice as rebind
from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef, read_session_execution
from kiro_crew.history import ConversationLog
from kiro_crew.mate_worker import MATE_CONDUCTOR_AGENT
from kiro_crew.memory_stores import provision_member_memory
from kiro_crew.session_agent_selection import resolve_session_agent_bindings
from kiro_crew.testing.wait import default_timeout

_SHARED = ("alpha-agent", "beta-agent", "gamma-agent", "kirocrew")


@pytest.fixture
def home(monkeypatch, tmp_path):
    """A kiro home whose warm snapshot knows the shared agents and one Bot file."""
    kiro = tmp_path / "kiro"
    (kiro / "agents").mkdir(parents=True)
    (kiro / "mates" / "agents").mkdir(parents=True)
    for name in _SHARED[:-1]:
        (kiro / "agents" / f"{name}.json").write_text(
            json.dumps({"name": name, "prompt": f"I am {name}."}), encoding="utf-8"
        )
    (kiro / "mates" / "agents" / "atlas.json").write_text(
        json.dumps({"name": "atlas", "prompt": "Bot"}), encoding="utf-8"
    )
    monkeypatch.setattr(paths_mod, "kiro_home", lambda: kiro)
    monkeypatch.setattr(paths_mod, "_agents_dir_override", lambda: kiro / "agents")
    monkeypatch.setattr(paths_mod, "_mate_agents_dir_override", lambda: kiro / "mates" / "agents")
    monkeypatch.setattr(loader, "_MATERIALIZED_AGENTS", frozenset({*_SHARED, "atlas"}))
    monkeypatch.setattr(loader, "_MATERIALIZED_MATE_ONLY", frozenset({"atlas"}))
    monkeypatch.setattr(loader, "_MATERIALIZED_AGENTS_READY", True)
    monkeypatch.setattr(loader, "refresh_materialized_agents", lambda **_: None)
    monkeypatch.setattr(loader, "schedule_materialized_agents_refresh", lambda: None)
    return kiro


def _config(**bindings: str) -> KiroCrewConfig:
    cfg = KiroCrewConfig()
    cfg.agents = {
        name: KiroCrewAgentConfig(member_id=name, kiro_agent=kiro_agent)
        for name, kiro_agent in bindings.items()
    }
    return cfg


def _record(member_id: str, template_id: str, kind: str = "member") -> ExecutionContext:
    return ExecutionContext(
        member_id=member_id,
        store=MemoryStoreRef(f"member-{member_id}", member_id),
        selection_kind=kind,
        template_id=template_id,
    )


def _bound_session(session_key: str, record: ExecutionContext) -> None:
    from kiro_crew.execution_context import bind_session_execution

    bind_session_execution(session_key, record)


_KEYS = iter(range(1, 1000))


def _resolved(cfg: KiroCrewConfig, record: ExecutionContext, key: str = ""):
    """The bindings a turn resolves for a session bound to *record* under *cfg*.

    With no *key*, a session of its own, named apart from every key a test spells.
    """
    key = key or f"dashboard_resolved-{next(_KEYS)}"
    _bound_session(key, record)
    return resolve_session_agent_bindings(resolve_agent_bindings, cfg, key, None)


def test_a_chat_on_the_agent_its_bot_left_is_owed_the_new_one(home):
    bindings = _resolved(_config(fixer="beta-agent"), _record("fixer", "alpha-agent"))
    # The turn still runs the agent its record names; a new chat runs the binding.
    assert (bindings.kiro_agent, bindings.new_chat_agent) == ("alpha-agent", "beta-agent")
    assert rebind.rebound_agents(bindings) == ("beta-agent", "alpha-agent")


def test_a_conductor_thread_rebound_to_a_shared_agent_is_owed_it(home):
    """The reported case: a Bot moved onto its conductor, then set to a shared agent."""
    bindings = _resolved(_config(fixer="beta-agent"), _record("fixer", MATE_CONDUCTOR_AGENT))
    assert rebind.rebound_agents(bindings) == ("beta-agent", MATE_CONDUCTOR_AGENT)


def test_a_record_naming_the_bots_own_file_is_told_the_conductor_it_runs(home):
    """The agent told is the one the record runs, not the template id it stores."""
    bindings = _resolved(_config(atlas="beta-agent"), _record("atlas", "atlas"))
    assert rebind.rebound_agents(bindings) == ("beta-agent", MATE_CONDUCTOR_AGENT)


def test_a_chat_that_runs_what_a_new_chat_would_is_owed_nothing(home):
    shared = _resolved(_config(fixer="alpha-agent"), _record("fixer", "alpha-agent"))
    assert shared.new_chat_agent == "alpha-agent"
    assert rebind.rebound_agents(shared) == ("", "")
    # A binding that owns a Bot file chats on the conductor, the record's too.
    own = _resolved(_config(atlas="atlas"), _record("atlas", MATE_CONDUCTOR_AGENT))
    assert (own.kiro_agent, own.new_chat_agent) == (MATE_CONDUCTOR_AGENT, MATE_CONDUCTOR_AGENT)
    assert rebind.rebound_agents(own) == ("", "")


def test_a_template_record_or_a_member_the_config_lost_is_owed_nothing(home):
    worker = _resolved(_config(fixer="beta-agent"), _record("fixer", "alpha-agent", "template"))
    assert worker.new_chat_agent == ""
    gone = _resolved(_config(fixer="beta-agent"), _record("gone", "alpha-agent"))
    assert gone.new_chat_agent == ""
    assert rebind.rebound_agents(worker) == rebind.rebound_agents(gone) == ("", "")


def test_a_binding_the_agents_snapshot_does_not_know_is_rescanned_once(home, monkeypatch):
    """Reading what a new chat runs costs a turn no second rescan of the binding."""
    scans: list[bool] = []
    monkeypatch.setattr(loader, "refresh_materialized_agents", lambda **_: scans.append(True))
    bindings = _resolved(_config(fixer="unknown-agent"), _record("fixer", "alpha-agent"))
    assert rebind.rebound_agents(bindings) == ("unknown-agent", "alpha-agent")
    assert len(scans) == 1


class _Slot:
    def __init__(self, key: str, agent: str, mode: str = ""):
        self.key = key
        self.mode = mode
        self.agent = agent
        self.executor = ""
        self.task = None
        self._queue: list = []
        self._turn_admission_reserved = False

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()


class _State:
    def __init__(self, *slots: _Slot):
        self._slots = {s.key: s for s in slots}
        self._background_tasks: set = set()
        self.conversation_log = ConversationLog()

    def get_slot(self, key: str):
        return self._slots.get(key)


@pytest.fixture
def surfaced(monkeypatch):
    rows: list[tuple[str, str, str, dict]] = []

    def append_and_surface(state, slot, role, content, cls="", *, meta=None, **_):
        rows.append((slot.key, role, content, dict(meta or {})))
        return {}

    monkeypatch.setattr("kiro_crew.dashboard.state.append_and_surface", append_and_surface)
    return rows


@pytest.fixture
def dispatched(monkeypatch):
    calls: list[tuple[str, str]] = []

    def dispatch(state, slot, prompt, *, human_seen, heartbeat=False):
        calls.append((slot.key, prompt))

    monkeypatch.setattr("kiro_crew.dashboard.mate_welcome.dispatch_hidden_turn", dispatch)
    return calls


def _surface(state: _State, slot: _Slot, key: str, cfg, bindings) -> bool:
    return asyncio.run(rebind.surface_rebind_notice(state, slot, key, cfg, bindings))


def test_a_rebound_chat_is_told_once_per_new_agent(home, surfaced):
    key = "dashboard_chat-7"
    record = _record("fixer", "alpha-agent")
    slot = _Slot("chat-7", "fixer")
    state = _State(slot)
    cfg = _config(fixer="beta-agent")
    bindings = _resolved(cfg, record, key)

    for _ in range(2):  # a Reload, a resume or a restart reads the same line
        _surface(state, slot, key, cfg, bindings)

    assert len(surfaced) == 1
    _slot_key, role, content, meta = surfaced[0]
    assert role == "notice"
    assert "beta-agent" in content and "alpha-agent" in content and "fixer" in content
    # The dashboard words the row from its token; the kind stays beside it.
    assert meta == {
        "kind": rebind.MEMBER_REBIND_NOTICE_KIND,
        "notice": gn.GATEWAY_NOTICE_MEMBER_REBIND_KEPT_AGENT,
        "notice_args": {"bot": "fixer", "agent": "beta-agent", "recorded": "alpha-agent"},
    }
    meta_line = ConversationLog().get_metadata(key)
    assert meta_line[rebind.REBIND_NOTICE_FIELD] == "beta-agent"
    # The record is not rewritten: the chat still runs the agent it recorded.
    assert meta_line["execution_context"]["template_id"] == "alpha-agent"
    # An ordinary chat draws the row; only a Bot's own thread is owed a hidden turn.
    assert wakes._rebind_notices(state) == {}

    cfg = _config(fixer="gamma-agent")
    later = resolve_session_agent_bindings(resolve_agent_bindings, cfg, key, None)
    _surface(state, slot, key, cfg, later)
    assert [row[3]["notice_args"]["agent"] for row in surfaced] == ["beta-agent", "gamma-agent"]


def test_a_chat_that_follows_its_bot_is_told_nothing(home, surfaced):
    key = "dashboard_chat-8"
    slot = _Slot("chat-8", "fixer")
    cfg = _config(fixer="alpha-agent")
    bindings = _resolved(cfg, _record("fixer", "alpha-agent"), key)

    assert not _surface(_State(slot), slot, key, cfg, bindings)
    assert surfaced == []
    assert rebind.REBIND_NOTICE_FIELD not in ConversationLog().get_metadata(key)


def test_the_notice_names_the_bot_by_its_display_name(home, surfaced):
    key = "dashboard_chat-10"
    cfg = _config(fixer="beta-agent")
    cfg.agents["fixer"].display_name = "Fixer  Bot"
    slot = _Slot("chat-10", "fixer")
    bindings = _resolved(cfg, _record("fixer", "alpha-agent"), key)

    assert _surface(_State(slot), slot, key, cfg, bindings)
    assert surfaced[0][3]["notice_args"]["bot"] == "Fixer Bot"
    assert surfaced[0][2].startswith("ℹ Fixer Bot is now set to beta-agent")


def _bot_thread(record: ExecutionContext) -> tuple[_State, _Slot, str]:
    slot_key = members_mod.member_slot_key("fixer")
    members_mod.write_dm_binding("fixer", member="fixer", slot_key=slot_key)
    key = f"dashboard_{slot_key}"
    _bound_session(key, record)
    slot = _Slot(slot_key, "fixer", mode=members_mod.DM_SLOT_MODE)
    return _State(slot), slot, key


@pytest.mark.asyncio
async def test_a_bots_own_thread_has_the_bot_tell_its_user_after_the_turn(
    home, surfaced, dispatched
):
    """The notice is offered inside the thread's turn and runs once that turn ends."""
    state, slot, key = _bot_thread(_record("fixer", MATE_CONDUCTOR_AGENT))
    cfg = _config(fixer="beta-agent")
    bindings = await asyncio.to_thread(
        resolve_session_agent_bindings, resolve_agent_bindings, cfg, key, None
    )
    turn = asyncio.get_running_loop().create_future()
    slot.task = turn  # the turn that surfaced the notice is running

    await rebind.surface_rebind_notice(state, slot, key, cfg, bindings)
    # The turn's own end runs inside the turn, while the thread is still busy.
    wakes.note_turn_end(state, slot, "reply")
    # One loop step lets the flush run up to its wait on the busy turn.
    await asyncio.sleep(0)  # flake-ok: a single loop step, not a timing barrier
    assert dispatched == []
    assert not state._mate_wake_tasks[slot.key].done()

    turn.set_result(None)
    await _settle(state)

    assert [row[1] for row in surfaced] == ["notice"]
    assert len(dispatched) == 1
    thread, prompt = dispatched[0]
    assert thread == slot.key
    assert prompt.startswith(wakes.REBIND_NOTICE_PREFIX)
    assert "beta-agent" in prompt and MATE_CONDUCTOR_AGENT in prompt

    # A later turn end owes nothing more.
    wakes.note_turn_end(state, slot, "reply")
    await _settle(state)
    assert len(dispatched) == 1


@pytest.mark.asyncio
async def test_a_notice_its_flush_could_not_deliver_waits_for_the_next_turn_end(
    home, surfaced, dispatched
):
    """A thread busy with a queued message and no running turn gets the notice at that turn's end."""
    state, slot, key = _bot_thread(_record("fixer", MATE_CONDUCTOR_AGENT))
    cfg = _config(fixer="beta-agent")
    bindings = await asyncio.to_thread(
        resolve_session_agent_bindings, resolve_agent_bindings, cfg, key, None
    )
    slot._queue.append({"id": "q1"})  # a queued message, its turn not started

    assert await rebind.surface_rebind_notice(state, slot, key, cfg, bindings)
    await _settle(state)
    # The flush had no turn to wait on, so it ended with the notice still owed.
    assert dispatched == []
    assert slot.key in wakes._rebind_notices(state)

    slot._queue.clear()  # the queued turn ran and ends
    wakes.note_turn_end(state, slot, "reply")
    await _settle(state)
    assert len(dispatched) == 1
    assert dispatched[0][1].startswith(wakes.REBIND_NOTICE_PREFIX)
    assert wakes._rebind_notices(state) == {}


async def _settle(state: _State) -> None:
    """Await every flush and notice check the wake path started, each under the bound."""
    for task in list(state._background_tasks):
        await asyncio.wait_for(task, default_timeout())
    for task in list(getattr(state, "_mate_wake_tasks", {}).values()):
        await asyncio.wait_for(task, default_timeout())


def test_a_session_with_no_transcript_claims_nothing(home, surfaced):
    slot = _Slot("chat-9", "fixer")
    state = _State(slot)
    bindings = _resolved(_config(fixer="beta-agent"), _record("fixer", "alpha-agent"))
    assert not _surface(state, slot, "dashboard_chat-9", _config(fixer="beta-agent"), bindings)
    assert surfaced == []
    assert not Path(state.conversation_log._path("dashboard_chat-9")).exists()


@pytest.mark.asyncio
async def test_turn_admission_appends_the_notice_naming_the_agent_the_record_runs(
    tmp_path, monkeypatch
):
    """End to end through ``_run_chat``: the turn after a rebind reuses the record and says so."""
    cfg = KiroCrewConfig.load()
    cfg.agents["fixer"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
    provision_member_memory(cfg, "fixer")
    cfg.save()
    state = _turn_state(tmp_path, monkeypatch)
    state.conversation_log = ConversationLog()
    slot = state.get_or_create_slot("rebound", agent="default")
    state.sessions.reset = AsyncMock(return_value=True)
    async with TestClient(TestServer(as_owner(_make_app_with_agent_routes(state)))) as client:
        response = await _finished(
            client.post("/api/chat/slots/rebound/agent", json={"agent": "fixer"}),
            "the agent pick",
        )
        assert response.status == 200, await response.text()

    async def turn(text: str) -> None:
        slot.append("user", text)
        await _finished(chat_runner._run_chat(state, slot, text), "the chat turn")
        await _finished(drain_background_tasks(state), "the background tasks draining")

    key = "dashboard:rebound"
    await turn("before the rebind")
    assert read_session_execution(key).template_id == "kirocrew"
    rebound = KiroCrewConfig.load()
    rebound.agents["fixer"].kiro_agent = "beta-agent"
    rebound.save()
    await turn("after the rebind")
    await turn("and once more")

    state.sessions.record_failure.assert_not_awaited()
    kinds = [(m.get("meta") or {}).get("kind") for m in slot.messages]
    told = [i for i, kind in enumerate(kinds) if kind == rebind.MEMBER_REBIND_NOTICE_KIND]
    assert len(told) == 1, [(m["role"], m["content"]) for m in slot.messages]
    row = slot.messages[told[0]]
    users = [m["content"] for m in slot.messages[: told[0]] if m["role"] == "user"]
    assert users[-1] == "after the rebind"
    assert row["role"] == "notice"
    assert row["meta"]["notice"] == gn.GATEWAY_NOTICE_MEMBER_REBIND_KEPT_AGENT
    # The agent named as kept is the one the record runs, never the new binding.
    assert row["meta"]["notice_args"] == {
        "bot": "fixer",
        "agent": "beta-agent",
        "recorded": "kirocrew",
    }
    assert read_session_execution(key).template_id == "kirocrew"


# A chat running a Bot on another harness than the Bot's own chat says so once.

_SELECTABLE = frozenset({"", "kas", "claude", "codex", "opencode"})
_BOT_ID = "atlas-0123456789ab"


@pytest.fixture
def harnesses(monkeypatch):
    """KAS, Kiro CLI, Claude Code, Codex and OpenCode selectable; a Kiro CLI pin runs on KAS."""
    from kiro_crew.agent_sdk import backends

    monkeypatch.setattr(backends, "selectable_backends", lambda: _SELECTABLE)
    monkeypatch.setattr(backends, "kiro_cli_pick_backend", lambda: "kas")
    monkeypatch.setattr(members_mod, "_CREWMATE_WORKER_SLOTS", set())
    monkeypatch.setattr(members_mod, "_CREWMATE_WORKER_MEMBERS", {})
    monkeypatch.setattr(members_mod, "_SESSION_BACKEND_PICKS", {})


def _harness_notice():
    from kiro_crew.dashboard import member_harness_notice

    return member_harness_notice


def _harness_config(pin: str = "", *, member_backend: str = "kas") -> KiroCrewConfig:
    cfg = KiroCrewConfig()
    cfg.agent.acp_backend = "kas"
    cfg.agent.member_acp_backend = member_backend
    cfg.agents = {
        "atlas": KiroCrewAgentConfig(member_id=_BOT_ID, kiro_agent="atlas", acp_backend=pin)
    }
    return cfg


def _bot_record(member_id: str = _BOT_ID, kind: str = "member") -> ExecutionContext:
    return _record(member_id, MATE_CONDUCTOR_AGENT, kind)


def test_a_chat_running_a_bot_pinned_elsewhere_is_owed_both_harnesses(home, harnesses):
    apart = _harness_notice().harnesses_apart(
        _harness_config("claude"), "dashboard:chat-1", _bot_record()
    )
    assert apart == ("claude", "kas")


def test_an_unpinned_bot_whose_own_chat_runs_the_bot_harness_is_owed_it(home, harnesses):
    cfg = _harness_config(member_backend="codex")
    assert _harness_notice().harnesses_apart(cfg, "dashboard:chat-1", _bot_record()) == (
        "codex",
        "kas",
    )


def test_a_bot_whose_own_chat_lands_on_the_chats_harness_is_owed_nothing(home, harnesses):
    notice = _harness_notice()
    # A pin Bots cannot run on lands on KAS; a stored Kiro CLI pin runs on KAS here.
    for pin in ("opencode", "kiro", "kas"):
        assert (
            notice.harnesses_apart(_harness_config(pin), "dashboard:chat-1", _bot_record()) is None
        )


def test_the_bots_own_thread_a_worker_and_an_unstartable_pin_are_owed_nothing(home, harnesses):
    notice = _harness_notice()
    cfg = _harness_config("claude")
    assert notice.harnesses_apart(cfg, f"dashboard:member-{_BOT_ID}", _bot_record()) is None
    members_mod._CREWMATE_WORKER_SLOTS.add("chat-7")
    members_mod._CREWMATE_WORKER_MEMBERS["chat-7"] = _BOT_ID
    assert notice.harnesses_apart(cfg, "dashboard:chat-7", _bot_record()) is None
    # The Bot's own chat refuses a pin this gateway cannot start, with its own message.
    unstartable = _harness_config("deepseek")
    assert notice.harnesses_apart(unstartable, "dashboard:chat-1", _bot_record()) is None


def test_a_template_record_or_a_bot_the_config_lost_is_owed_nothing(home, harnesses):
    notice = _harness_notice()
    cfg = _harness_config("claude")
    no_id = SimpleNamespace(selection_kind="member", member_id="")
    for record in (_bot_record(kind="template"), _bot_record("gone"), no_id):
        assert notice.harnesses_apart(cfg, "dashboard:chat-1", record) is None


def test_the_notice_names_each_harness_by_its_label(home, harnesses):
    notice = _harness_notice()
    assert (notice.harness_name("claude"), notice.harness_name("kas")) == (
        "Claude Code",
        "Kiro Agent",
    )
    assert notice.harness_notice_text("atlas", "Claude Code", "Kiro Agent") == (
        "ℹ atlas runs on Claude Code in its own chat, and this chat stays on Kiro Agent. "
        "Open atlas's own chat to use Claude Code."
    )
    meta = notice.harness_notice_meta("atlas", "Claude Code", "Kiro Agent")
    assert meta["kind"] == notice.MEMBER_HARNESS_NOTICE_KIND
    assert meta["notice"] == gn.GATEWAY_NOTICE_MEMBER_HARNESS_ELSEWHERE
    assert meta["notice_args"] == {
        "bot": "atlas",
        "bot_harness": "Claude Code",
        "chat_harness": "Kiro Agent",
    }


def _save_bot(pin: str) -> None:
    cfg = KiroCrewConfig.load()
    cfg.agent.acp_backend = "kas"
    cfg.agent.member_acp_backend = "kas"
    if "atlas" in cfg.agents:
        cfg.agents["atlas"].acp_backend = pin
    else:
        cfg.agents["atlas"] = KiroCrewAgentConfig(kiro_agent="kirocrew", acp_backend=pin)
    cfg.save()


async def _harness_chat(tmp_path, monkeypatch, slot_key: str, *, pick: bool = True):
    """A chat on a KAS default; *pick* switches it to atlas with the agent picker."""
    from unittest.mock import MagicMock

    _save_bot("claude")
    cfg = KiroCrewConfig.load()
    provision_member_memory(cfg, "atlas")
    cfg.save()
    state = _turn_state(tmp_path, monkeypatch)
    state.conversation_log = ConversationLog()
    state.notify = MagicMock()
    slot = state.get_or_create_slot(slot_key, agent="atlas" if not pick else "default")
    state.sessions.reset = AsyncMock(return_value=True)
    if pick:
        async with TestClient(TestServer(as_owner(_make_app_with_agent_routes(state)))) as client:
            response = await _finished(
                client.post(f"/api/chat/slots/{slot_key}/agent", json={"agent": "atlas"}),
                "the agent pick",
            )
            assert response.status == 200, await response.text()

    async def turn(text: str) -> None:
        slot.append("user", text)
        await _finished(chat_runner._run_chat(state, slot, text), "the chat turn")
        await _finished(drain_background_tasks(state), "the background tasks draining")

    return state, slot, turn


def _harness_rows(slot) -> list[dict]:
    return [m for m in slot.messages if (m.get("meta") or {}).get("kind") == "member_harness"]


@pytest.mark.asyncio
async def test_a_chat_switched_to_a_bot_pinned_elsewhere_says_so_once(
    tmp_path, monkeypatch, harnesses
):
    state, slot, turn = await _harness_chat(tmp_path, monkeypatch, "switched")
    await turn("first")
    told = _harness_rows(slot)
    assert len(told) == 1, [(m["role"], m["content"]) for m in slot.messages]
    row = told[0]
    assert row["role"] == "notice"
    assert row["meta"]["notice"] == "member_harness_elsewhere"
    assert row["meta"]["notice_args"] == {
        "bot": "atlas",
        "bot_harness": "Claude Code",
        "chat_harness": "Kiro Agent",
    }
    assert row["content"].startswith("ℹ atlas runs on Claude Code in its own chat")
    await turn("second")
    # A restart reads the claim back from the session's metadata line.
    state.conversation_log = ConversationLog()
    await turn("after a restart")
    assert len(_harness_rows(slot)) == 1
    state.notify.assert_not_called()
    state.sessions.record_failure.assert_not_awaited()
    # Nothing follows the pin: the chat still runs as atlas on its own record.
    record = read_session_execution("dashboard:switched")
    assert record.selection_kind == "member"
    assert record.member_id == KiroCrewConfig.load().agents["atlas"].member_id
    assert members_mod.session_backend_pick("dashboard:switched") is None


@pytest.mark.asyncio
async def test_a_later_change_of_the_bots_harness_is_told_again(tmp_path, monkeypatch, harnesses):
    _state, slot, turn = await _harness_chat(tmp_path, monkeypatch, "repinned")
    await turn("first")
    _save_bot("codex")
    await turn("after the pin moved")
    _save_bot("opencode")
    await turn("after the pin landed on this chat's harness")
    told = [row["meta"]["notice_args"]["bot_harness"] for row in _harness_rows(slot)]
    assert told == ["Claude Code", "OpenAI Codex"]


@pytest.mark.asyncio
async def test_a_chat_a_non_owner_created_on_a_bot_is_told_on_its_second_turn(
    tmp_path, monkeypatch, harnesses
):
    """A chat made on the Bot writes no record until its first turn, which writes it."""
    _state, slot, turn = await _harness_chat(tmp_path, monkeypatch, "created", pick=False)
    await turn("first")
    assert read_session_execution("dashboard:created").selection_kind == "member"
    assert _harness_rows(slot) == []
    await turn("second")
    await turn("third")
    assert len(_harness_rows(slot)) == 1


@pytest.mark.asyncio
async def test_a_regular_template_chat_gets_no_harness_notice(tmp_path, monkeypatch, harnesses):
    _save_bot("claude")
    state = _turn_state(tmp_path, monkeypatch)
    state.conversation_log = ConversationLog()
    slot = state.get_or_create_slot("plain", agent="default")
    for text in ("first", "second"):
        slot.append("user", text)
        await _finished(chat_runner._run_chat(state, slot, text), "the chat turn")
        await _finished(drain_background_tasks(state), "the background tasks draining")
    assert _harness_rows(slot) == []
