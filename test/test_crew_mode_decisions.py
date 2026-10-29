"""Crew Mode owner decisions 4, 5 and 6: one switch for every root, workers out of
History, and the server-side one-way lock.

Every case names the rule it guards and fails when that rule is removed.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web

from kiro_crew import crew_mode


@pytest.fixture(autouse=True)
def _clean(_floor_monkeypatch):
    _floor_monkeypatch.setattr(crew_mode, "_known_roots", set())
    _floor_monkeypatch.setattr(crew_mode, "persisted_parents", lambda: {})
    _floor_monkeypatch.setattr(crew_mode, "_live_cfg", lambda: None)
    _floor_monkeypatch.setattr(crew_mode, "_configured", crew_mode.DEFAULT_CREW_MODE_AGENT)
    yield


def _slot(key: str, agent: str = "kirocrew", **over: Any) -> SimpleNamespace:
    base = dict(
        key=key,
        agent=agent,
        agent_kind="template",
        crew_mode_agent="",
        mode="",
        memory_mode="persistent",
        _created_by="",
        _app="",
        instance_id="",
        running=False,
        turn_running=False,
        # A chat the person has written in: its Crew Mode is locked.
        messages=[{"role": "user", "content": "go"}],
        _disk_older_count=0,
    )
    base.update(over)
    return SimpleNamespace(**base)


# ── decision 6: the lock ─────────────────────────────────────────────────────


class _Sel:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def log_api_access(self, **kw: Any) -> None:
        self.calls.append(kw)


@pytest.fixture
def _sel(monkeypatch):
    from kiro_crew.dashboard import chat_handlers

    rec = _Sel()
    monkeypatch.setattr(chat_handlers, "sel", lambda: rec)
    return rec


def _refusal(slot: Any, agent: str, kind: str = "template", migration: bool = False):
    from kiro_crew.dashboard import chat_handlers

    caller = (
        chat_handlers.SlotAgentSwitchCaller.for_crew_mode_migration()
        if migration
        else chat_handlers.SlotAgentSwitchCaller(app="", remote="t", owner=True)
    )
    return chat_handlers._crew_mode_lock_refusal(slot, agent, kind, caller)


def test_a_crew_mode_root_refuses_another_agent_with_crew_mode_locked(_sel):
    resp = _refusal(_slot("r", "kirocrew-team-lead"), "kirocrew")
    assert isinstance(resp, web.Response) and resp.status == 409
    assert json.loads(resp.text)["code"] == "crew_mode_locked"
    assert _sel.calls and _sel.calls[0]["outcome"] == "denied"


def test_crew_mode_turns_off_before_the_first_user_message_and_locks_after(_sel):
    # No user message yet (the switch notice is not one): back to a plain agent passes.
    fresh = _slot(
        "r", "kirocrew-conductor", messages=[{"role": "assistant", "content": "Switched"}]
    )
    assert _refusal(fresh, "kirocrew") is None
    # Not onto another conductor, and not onto a crewmate.
    assert _refusal(fresh, "kirocrew-team-lead").status == 409
    assert _refusal(fresh, "kirocrew", kind="member").status == 409
    # From the first user message on, it is locked, in the window or trimmed to disk.
    sent = _slot("r", "kirocrew-conductor")
    assert _refusal(sent, "kirocrew").status == 409
    trimmed = _slot("r", "kirocrew-conductor", messages=[], _disk_older_count=3)
    assert _refusal(trimmed, "kirocrew").status == 409


def test_the_lock_lets_any_caller_move_a_root_onto_the_configured_conductor(_sel):
    # A root on another conductor (reopened after the migration finished, or stamped
    # with a conductor an update retired) is still Crew Mode; the one move the lock
    # allows is onto the conductor the config names, whoever asks.
    root = _slot("r", "kirocrew-team-lead")
    assert _refusal(root, "kirocrew-conductor") is None
    assert _refusal(root, "kirocrew-conductor", migration=True) is None
    # Not onto another conductor, not into the member namespace, not as a name-only
    # pick (a crewmate of the same name would resolve first), and the migration moves
    # only onto the configured conductor.
    assert _refusal(root, "kirocrew-security-conductor").status == 409
    assert _refusal(root, "kirocrew-conductor", kind="member").status == 409
    assert _refusal(root, "kirocrew-conductor", kind="").status == 409
    assert _refusal(root, "kirocrew", migration=True).status == 409
    # A name-only re-pick of the agent the root is on is not a re-pick either: the
    # name resolves to a crewmate of the same name first.
    assert _refusal(_slot("r", "kirocrew-conductor"), "kirocrew-conductor", kind="").status == 409


def test_a_reopened_worker_on_a_conductor_template_is_not_a_root(monkeypatch):
    """A reopen restores the creator onto the saved line only (``_saved_created_by``),
    never ``_created_by``; the root rule reads the saved creator like the slot list."""
    reopened = _slot("w", "kirocrew-conductor", _saved_created_by="r")
    assert not crew_mode.slot_is_crew_mode_root(reopened)
    assert _refusal(reopened, "kirocrew") is None
    # With settled lineage the session tree decides: a tracked node with no parent
    # edge is a session ``session_release`` let go, so it stands on its own.
    monkeypatch.setattr(crew_mode, "lineage_is_settled", lambda: True)
    monkeypatch.setattr(crew_mode, "persisted_tree_slots", lambda: frozenset({"w"}))
    assert crew_mode.slot_is_crew_mode_root(reopened)
    monkeypatch.setattr(crew_mode, "persisted_parents", lambda: {"w": "r"})
    assert not crew_mode.slot_is_crew_mode_root(reopened)
    # A slot the tree never saw falls back to its saved creator.
    monkeypatch.setattr(crew_mode, "persisted_tree_slots", lambda: frozenset({"other"}))
    assert not crew_mode.slot_is_crew_mode_root(reopened)
    assert crew_mode.slot_is_crew_mode_root(_slot("r", "kirocrew-conductor"))


def test_a_re_pick_of_the_same_agent_passes_and_a_plain_chat_is_never_locked(_sel):
    assert _refusal(_slot("r", "kirocrew-conductor"), "kirocrew-conductor") is None
    assert _refusal(_slot("p"), "kirocrew-conductor") is None
    # A worker (something dispatched it) is not a Crew Mode root, so it is not locked.
    assert _refusal(_slot("w", "kirocrew-conductor", _created_by="r"), "kirocrew") is None
    assert _sel.calls == []


def test_the_switch_loads_a_restored_window_off_the_loop_before_the_lock_reads_it():
    # The lock reads the transcript for a user message; a restored row never opened
    # still has its window on disk, and a read on the loop would load it inline.
    from kiro_crew.dashboard.chat_api import agent_switch

    source = open(agent_switch.__file__, encoding="utf-8").read()
    body = source.split("async def switch_slot_agent(", 1)[1]
    assert body.index("await load_window_off_loop(slot)") < body.index(
        "_crew_mode_lock_refusal(slot, agent_name, agent_kind, caller)"
    )


@pytest.mark.asyncio
async def test_the_provider_side_switch_is_vetoed_on_a_crew_mode_root(monkeypatch):
    from unittest.mock import MagicMock

    from kiro_crew.dashboard import chat_runner
    from kiro_crew.dashboard.chat_turn.turn_locals import _Flow

    assert chat_runner.crew_mode_is_root(_slot("r", "kirocrew-conductor"))
    assert not chat_runner.crew_mode_is_root(_slot("p"))
    audit = MagicMock()
    monkeypatch.setattr(chat_runner, "sel", lambda: audit)

    def _turn(slot):
        return SimpleNamespace(
            _turn_project=None,
            client=None,
            needs_session_reset=False,
            private_member=None,
            selected_binding=None,
            session_key="k",
            slot=slot,
            state=SimpleNamespace(),
            _produced_visible_output=False,
        )

    # The backend switching its own agent mid-turn is the one door the HTTP lock
    # cannot reach: the phase keeps the slot's agent, audits the denial, shows it,
    # and ends the turn so the session resets onto the conductor.
    root = _slot("r", "kirocrew-conductor")
    root.append = lambda role, text, cls: notices.append((role, text))
    notices: list = []
    turn = _turn(root)
    event = SimpleNamespace(text="kirocrew")
    assert await chat_runner._on_agent_switched(turn, event) is _Flow.BREAK
    assert root.agent == "kirocrew-conductor"
    assert turn.needs_session_reset and turn._produced_visible_output
    assert notices and notices[0][0] == "notice" and "Crew Mode" in notices[0][1]
    assert audit.log_api_access.call_args.kwargs["source"] == "crew_mode_locked"


# ── decision 4: one switch for every root ────────────────────────────────────


class _State:
    def __init__(self, *slots: Any) -> None:
        self._slots = {s.key: s for s in slots}
        self.pushes = 0
        self.conversation_log = None
        # The dashboard's start-up restore ran and finished.
        self._slot_list_rebuild_began = 1.0
        self.slot_list_rebuilding = False

    def push_slots_update(self) -> None:
        self.pushes += 1


@pytest.fixture
def _switch(monkeypatch, tmp_path):
    """The migration's switch, recorded; ``busy`` keys answer turn_in_flight."""
    from kiro_crew.config import paths
    from kiro_crew.dashboard import chat_handlers
    from kiro_crew.dashboard import crew_mode_migration as mig

    monkeypatch.setattr(paths, "data_home", lambda: tmp_path)
    monkeypatch.setattr(mig, "_pending", set())
    monkeypatch.setattr(mig, "_failed", {})
    monkeypatch.setattr(mig, "_lock", None)
    monkeypatch.setattr(mig, "_usable", lambda name: "")
    monkeypatch.setattr(crew_mode, "_unusable", {})
    audits: list[dict[str, Any]] = []
    monkeypatch.setattr(mig, "_audit", lambda *a, **k: audits.append({"args": a}))
    calls: list[tuple[str, str, Any]] = []

    async def fake_switch(state, slot, name, agent, kind, caller):
        calls.append((name, agent, caller.crew_mode_migration))
        slot.agent = agent
        slot.crew_mode_agent = agent
        return web.json_response({"ok": True, "agent": agent})

    monkeypatch.setattr(chat_handlers, "switch_slot_agent", fake_switch)
    return SimpleNamespace(calls=calls, audits=audits, mig=mig, home=tmp_path)


@pytest.mark.asyncio
async def test_a_first_start_records_the_conductor_and_moves_nothing(_switch):
    state = _State(_slot("r", "kirocrew-team-lead"))
    assert await _switch.mig.reconcile(state) is None
    assert _switch.mig.read_applied() == "kirocrew-conductor"
    assert _switch.calls == []
    assert state._slots["r"].agent == "kirocrew-team-lead"


@pytest.mark.asyncio
async def test_a_changed_conductor_moves_every_idle_root_and_audits_each(_switch, monkeypatch):
    _switch.mig.write_applied("kirocrew-conductor")
    monkeypatch.setattr(crew_mode, "_configured", "better-conductor")
    idle = _slot("r1", "kirocrew-conductor")
    old = _slot("r2", "kirocrew-team-lead")
    plain = _slot("p")
    worker = _slot("w", "kirocrew-conductor", _created_by="r1")
    state = _State(idle, old, plain, worker)
    out = await _switch.mig.reconcile(state)
    assert out == {"moved": ["r1", "r2"], "pending": []}
    assert idle.agent == old.agent == "better-conductor"
    assert plain.agent == "kirocrew" and worker.agent == "kirocrew-conductor"
    assert all(migration for _n, _a, migration in _switch.calls)
    assert len(_switch.audits) == 2
    assert _switch.mig.read_applied() == "better-conductor"


@pytest.mark.asyncio
async def test_a_busy_root_waits_for_its_next_idle_point(_switch, monkeypatch):
    _switch.mig.write_applied("kirocrew-conductor")
    monkeypatch.setattr(crew_mode, "_configured", "better-conductor")
    busy = _slot("r", "kirocrew-conductor", turn_running=True)
    state = _State(busy)
    out = await _switch.mig.reconcile(state)
    assert out == {"moved": [], "pending": ["r"]}
    assert busy.agent == "kirocrew-conductor" and _switch.calls == []
    # Not advanced while a root is owed the move: a restart migrates it then.
    assert _switch.mig.read_applied() == "kirocrew-conductor"
    busy.turn_running = False
    out = await _switch.mig.migrate(state)
    assert out == {"moved": ["r"], "pending": []}
    assert _switch.mig.read_applied() == "better-conductor"


@pytest.mark.asyncio
async def test_an_update_shipping_a_new_default_moves_the_roots(_switch):
    _switch.mig.write_applied("kirocrew-old-default")
    root = _slot("r", "kirocrew-old-default", crew_mode_agent="kirocrew-old-default")
    state = _State(root)
    await _switch.mig.reconcile(state)
    assert root.agent == "kirocrew-conductor"


@pytest.mark.asyncio
async def test_the_start_pass_learns_closed_roots_and_writes_no_stamp(_switch):
    """A conductor chat from before Crew Mode had a switch is Crew Mode by its template
    alone; the stamp means it TURNED Crew Mode on and gates the folder rule and the
    History filter, so a restart must not backfill it."""

    class _Log:
        def __init__(self) -> None:
            self.updates: list[tuple[str, dict]] = []

        def update_metadata(self, key: str, fields: dict) -> None:
            self.updates.append((key, fields))

        def list_sessions(self) -> list[dict]:
            return [
                {"key": "dashboard_old-root", "agent": "kirocrew-team-lead"},
                {"key": "dashboard_old-worker", "agent": "kirocrew-team-lead"},
                {"key": "dashboard_on-root", "agent": "kirocrew-conductor"},
                {"key": "dashboard_plain", "agent": "kirocrew"},
            ]

        def get_metadata(self, key: str) -> dict:
            if key == "dashboard_old-worker":
                return {"agent": "kirocrew-team-lead", "created_by": "old-root"}
            if key == "dashboard_on-root":
                return {"agent": "kirocrew-conductor", "crew_mode_agent": "kirocrew-conductor"}
            return {"agent": "kirocrew-team-lead"}

    lead = _slot("lead", "kirocrew-team-lead")
    state = _State(lead)
    state.conversation_log = _Log()
    await _switch.mig.on_start(state)
    assert lead.crew_mode_agent == ""
    assert state.conversation_log.updates == []
    assert {"old-root", "on-root"} <= crew_mode.known_roots()
    assert "old-worker" not in crew_mode.known_roots()


class _RootLog:
    def list_sessions(self) -> list[dict]:
        return [{"key": "dashboard_closed-root", "agent": "kirocrew-conductor"}]

    def get_metadata(self, key: str) -> dict:
        return {"agent": "kirocrew-conductor"}

    def update_metadata(self, key: str, fields: dict) -> None:
        pass


@pytest.mark.asyncio
async def test_G5_a_live_worker_of_a_closed_root_is_hidden_from_the_first_frame(_switch):
    """The start pass learns closed roots off the boot path and says when one holds a
    live slot, so the slot frame that follows stamps the worker."""
    worker = _slot("w", "kirocrew-worker", _created_by="closed-root")
    state = _State(worker)
    state.conversation_log = _RootLog()
    assert "closed-root" not in crew_mode.known_roots()
    # Learned by the start pass, and it says a slot moved.
    assert await _switch.mig.learn_closed_roots(state) is True
    assert "closed-root" in crew_mode.known_roots()
    stamps = crew_mode.live_stamps(state._slots)
    assert stamps["w"][0] == "closed-root"
    # Already known: nothing new, no frame owed.
    assert await _switch.mig.learn_closed_roots(state) is False


@pytest.mark.asyncio
async def test_G5_the_start_pass_pushes_a_frame_when_it_learns_a_holding_root(_switch):
    worker = _slot("w", "kirocrew-worker", _created_by="closed-root")
    state = _State(worker)
    state.conversation_log = _RootLog()
    await _switch.mig.on_start(state)
    assert state.pushes >= 1


def test_a_history_stem_is_its_slot_key():
    from kiro_crew.dashboard.crew_mode_migration import history_slot_key

    assert history_slot_key("dashboard_chat-3-1792") == "chat-3-1792"
    assert history_slot_key("dashboard_dashboard_chat-3-1792") == "chat-3-1792"
    assert history_slot_key("dashboard:chat-3") == "chat-3"
    assert history_slot_key("slack_123") == "slack_123"


def test_the_config_key_is_watched_live_and_the_start_pass_follows_the_restore():
    from kiro_crew.dashboard import server
    from kiro_crew.dashboard.server_runtime import config_watch

    source = open(config_watch.__file__, encoding="utf-8").read()
    assert '"agent.crew_mode_agent"' in source
    # The start pass reads the slot set, so the dashboard starts it right after the
    # restore has rebuilt it; a headless gateway (no restore) never starts it.
    assert "crew_mode_migration.run(state)" not in source
    body = open(server.__file__, encoding="utf-8").read()
    assert body.index("await _restore_dashboard_sessions(state, cfg)") < body.index(
        "crew_mode_migration.run(state)"
    )


def test_the_session_catalog_carries_the_crew_fields(tmp_path):
    # ``list_sessions`` rows name the creator, the namespace and the Crew Mode stamp off
    # the metadata line, so History can classify a row without opening its file.
    from kiro_crew.history import ConversationLog

    log = ConversationLog(base_dir=tmp_path)
    log.append("dashboard_root", "user", "go")
    log.update_metadata("dashboard_root", {"agent": "kirocrew-conductor", "agent_kind": "template"})
    log.append("dashboard_w", "user", "go")
    log.update_metadata(
        "dashboard_w",
        {"agent": "kirocrew-conductor", "created_by": "root", "crew_mode_agent": ""},
    )
    by_key = {row["key"]: row for row in log.list_sessions()}
    assert by_key["dashboard_root"]["agent_kind"] == "template"
    assert "created_by" not in by_key["dashboard_root"]
    assert by_key["dashboard_w"]["created_by"] == "root"
    assert "crew_mode_agent" not in by_key["dashboard_w"]
    # The same rows from the metadata cache a second listing answers from.
    again = {row["key"]: row for row in log.list_sessions()}
    assert again["dashboard_w"]["created_by"] == "root"
