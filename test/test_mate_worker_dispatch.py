# SPDX-License-Identifier: Apache-2.0
"""A crewmate on its own file chats on the conductor and hands work to its worker.

The chat half: a binding whose ``kiro_agent`` names a file found ONLY in the
crewmate folder runs its chat on ``kirocrew-mate-conductor``, so the file never
reaches the chat. The hand-off half: that chat's ``session_create`` with no
agent (or the worker named bare) opens ``kirocrew-mate-worker`` on the
crewmate's own workspace, store and member identity. Driven through the real
config, resolver and ``create_session``.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from chat_test_helpers import _make_state
from member_memory_helpers import forget_declared_stores, write_member_home

from kiro_crew import mate_worker
from kiro_crew.acp_backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_COPILOT,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKENDS_KNOWN,
    ACP_BACKENDS_MATE_WORKER_KIT,
)
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config import loader
from kiro_crew.crewmate_harness import harness_label
from kiro_crew.dashboard import create_rate_limit
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.mate_worker import MATE_CONDUCTOR_AGENT, MATE_WORKER_AGENT
from kiro_crew.members import DM_SLOT_KEY_PREFIX
from kiro_crew.testing.ids import seq_ids


class TestMemberChatAgent:
    def test_a_crewmate_owned_file_chats_on_the_conductor(self, monkeypatch):
        monkeypatch.setattr(loader, "_MATERIALIZED_AGENTS_READY", True)
        monkeypatch.setattr(loader, "_MATERIALIZED_MATE_ONLY", frozenset({"pixel"}))
        assert mate_worker.member_chat_agent("pixel") == MATE_CONDUCTOR_AGENT

    def test_any_other_binding_runs_as_it_is_named(self, monkeypatch):
        monkeypatch.setattr(loader, "_MATERIALIZED_AGENTS_READY", True)
        monkeypatch.setattr(loader, "_MATERIALIZED_MATE_ONLY", frozenset({"pixel"}))
        monkeypatch.setattr(loader, "_MATERIALIZED_AGENTS", frozenset({"reviewer"}))
        assert mate_worker.member_chat_agent("reviewer") == "reviewer"
        assert mate_worker.member_chat_agent("") == ""


def test_the_refresh_publishes_only_names_the_user_level_does_not_declare(tmp_path, monkeypatch):
    user, mates = tmp_path / "agents", tmp_path / "mates"
    user.mkdir()
    mates.mkdir()
    for directory, name in ((user, "shared"), (mates, "shared"), (mates, "pixel")):
        (directory / f"{name}.json").write_text(json.dumps({"name": name}), encoding="utf-8")
    monkeypatch.setattr(loader, "kiro_agents_dir", lambda: user)
    monkeypatch.setattr(loader, "mate_agents_dir", lambda: mates)
    monkeypatch.setattr(loader, "_MATERIALIZED_MATE_ONLY", frozenset())
    monkeypatch.setattr(loader, "_MATERIALIZED_AGENTS", frozenset())
    monkeypatch.setattr(loader, "_MATERIALIZED_AGENTS_READY", False)
    stems = dict(loader._MATERIALIZED_STEMS)
    try:
        loader.refresh_materialized_agents(heal_default=False)
        assert loader._MATERIALIZED_MATE_ONLY == frozenset({"pixel"})
        assert mate_worker.is_mate_owned_agent("pixel")
        assert not mate_worker.is_mate_owned_agent("shared")
    finally:
        loader._MATERIALIZED_STEMS.clear()
        loader._MATERIALIZED_STEMS.update(stems)


@pytest.fixture(autouse=True)
def _fresh_create_budget():
    create_rate_limit.reset_for_tests()
    yield
    create_rate_limit.reset_for_tests()


@pytest.fixture
def crewmate(tmp_path, monkeypatch):
    """``pixel`` bound to its own crewmate file, in its own workspace."""
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.execution_context import bind_session_execution, resolve_member_execution
    from kiro_crew.history import ConversationLog

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    config = write_member_home(tmp_path, "pixel", "ada")
    config["workspaces"] = {
        "default": {"dir": "workspace"},
        "member-pixel": {"dir": "workspace-member-pixel"},
        "member-ada": {"dir": "workspace-member-ada"},
    }
    config["agents"]["pixel"]["workspace"] = "member-pixel"
    config["agents"]["pixel"]["kiro_agent"] = "pixel"
    config["agents"]["ada"]["workspace"] = "member-ada"
    (tmp_path / "config.json").write_text(json.dumps(config), encoding="utf-8")
    for directory in ("workspace", "workspace-member-pixel", "workspace-member-ada"):
        (tmp_path / directory).mkdir(exist_ok=True)
    forget_declared_stores(monkeypatch)
    monkeypatch.setattr(
        loader,
        "_MATERIALIZED_AGENTS",
        frozenset({MATE_WORKER_AGENT, MATE_CONDUCTOR_AGENT, "pixel"}),
    )
    monkeypatch.setattr(loader, "_MATERIALIZED_MATE_ONLY", frozenset({"pixel"}))
    monkeypatch.setattr(loader, "_MATERIALIZED_AGENTS_READY", True)
    monkeypatch.setattr(sc, "session_control_enabled", lambda: True)
    monkeypatch.setattr(sc, "member_dispatch_enabled", lambda: True)

    state = _make_state(tmp_path)
    state.conversation_log = ConversationLog()
    caller = state.get_or_create_slot(DM_SLOT_KEY_PREFIX + "pixel", mode="member")
    caller.workspace = "member-pixel"
    caller.agent = "pixel"
    execution = resolve_member_execution(KiroCrewConfig.load(), "pixel")
    caller.memory_store = execution.store.legacy_name
    bind_session_execution(slot_history_key(caller), execution, vouch=True)
    return state, caller, execution


def _create(state, caller, **kwargs):
    return asyncio.run(
        sc.create_session(state, caller_session_key=slot_history_key(caller), **kwargs)
    )


def test_the_crewmates_own_chat_runs_on_the_conductor(crewmate):
    _state, _caller, execution = crewmate
    assert execution.template_id == MATE_CONDUCTOR_AGENT


@pytest.mark.parametrize(
    "named, kind",
    [
        ("", ""),
        (MATE_WORKER_AGENT, ""),
        (MATE_WORKER_AGENT, "template"),
        # The stock leaf worker the session_create schema names for every caller.
        ("kirocrew-worker", "template"),
        ("kirocrew-worker", ""),
        # Whatever else the model names: the gateway, not the prompt, decides.
        ("kirocrew", ""),
        ("some-agent-nobody-declared", ""),
        ("reviewer", "template"),
        # A template that shares a peer's name is still a template.
        ("ada", "template"),
        # Its own name is its own worker, not a second conductor chat.
        ("pixel", ""),
    ],
)
def test_the_conductor_hands_off_to_its_worker(crewmate, named, kind):
    from kiro_crew.execution_context import read_session_execution

    state, caller, execution = crewmate
    result = _create(state, caller, agent=named, agent_kind=kind)

    child = state.get_slot(result["target"])
    assert child is not None
    assert child.agent == MATE_WORKER_AGENT
    assert child.workspace == "member-pixel"
    actual = read_session_execution(slot_history_key(child), required=True)
    # The crewmate's own memory and identity, under the worker persona.
    assert actual.store == execution.store
    assert actual.member_id == "pixel"
    assert actual.selection_kind == "template"
    assert actual.template_id == MATE_WORKER_AGENT


def test_a_conductor_slot_with_no_record_opens_no_worker(crewmate, tmp_path):
    """A slot from before records names its crewmate by slot agent only, which
    is not an identity: it hands off to no worker."""
    from kiro_crew.execution_context import clear_session_execution, read_session_execution
    from kiro_crew.history import ConversationLog

    state, caller, _execution = crewmate
    # On the default workspace, so no workspace check refuses it first.
    path = tmp_path / "config.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    config["agents"]["pixel"].pop("workspace", None)
    path.write_text(json.dumps(config), encoding="utf-8")
    loader._invalidate_config_cache()
    caller.workspace = "default"
    before = set(state._slots)
    key = slot_history_key(caller)
    clear_session_execution(key)
    ConversationLog()._path(key).unlink()  # the transcript that holds the record
    caller.memory_store = ""
    assert read_session_execution(key) is None
    assert not sc_conducts(state, caller)

    with pytest.raises(sc.SessionControlError) as refused:
        _create(state, caller)

    # Not a conductor, so its unnamed child is the slot agent's own member
    # session, whose private store a member DM may not bind on its own.
    assert refused.value.code == "memory_delegation_denied"
    assert set(state._slots) == before  # nothing was allocated


def test_the_conductor_naming_another_crewmate_stays_refused(crewmate):
    state, caller, _execution = crewmate
    with pytest.raises(sc.SessionControlError) as refused:
        _create(state, caller, agent="ada")
    assert refused.value.code == "agent_workspace_mismatch"


def test_the_worker_does_not_hand_off_to_another_worker_by_default(crewmate):
    """A worker is not a conductor: its own unnamed child inherits the worker."""
    from kiro_crew.execution_context import read_session_execution

    state, caller, _execution = crewmate
    worker = state.get_slot(_create(state, caller)["target"])
    _start(state, worker)
    assert not sc_conducts(state, worker)
    _ = read_session_execution(slot_history_key(worker), required=True)


def sc_conducts(state, slot):
    """Whether *slot* dispatches its Bot's worker, from the kit its session started with."""
    from kiro_crew.dashboard.session_control_parts.session_create import _conducts_for_a_worker
    from kiro_crew.execution_context import read_session_execution
    from kiro_crew.mate_worker_conducts import holds_conducting_kit

    kit = holds_conducting_kit(state, slot)
    return _conducts_for_a_worker(read_session_execution(slot_history_key(slot)), kit)


def _start(state, slot, *, session_id: str | None = None) -> _LiveSession:
    """(Re)start *slot*'s session: its live provider reports whether the composer
    mounted session control at that start (``member_dispatch_mounted``), decided by
    the predicate every composer reads, ``members.mounts_session_control``, at the
    moment of the start. A reload is another call. Each start reports a native session
    id of its own, as a real start does, unless *session_id* names one (``""`` for a
    session that reports none). Returns that provider."""
    from kiro_crew import members
    from kiro_crew.dashboard.chat_utils import effective_session_key

    live = getattr(state.sessions, "live_for_test", None)
    if not isinstance(live, dict):
        live = state.sessions.live_for_test = {}
        state.sessions.get_provider.side_effect = live.get
        state.sessions.start_ids_for_test = seq_ids("session")
    mounted = members.mounts_session_control(slot_history_key(slot))
    if session_id is None:
        session_id = next(state.sessions.start_ids_for_test)
    started = live[effective_session_key(slot)] = _LiveSession(mounted, session_id)
    return started


class _LiveSession:
    """A started session's provider, as far as these tests read it: whether its start
    mounted session control, and its native conversation id (``session_id``). A new
    start is a new object, as a new provider is."""

    def __init__(self, mounted: bool, session_id: str = "") -> None:
        self.member_dispatch_mounted = mounted
        self.session_id = session_id


# ── a conducting worker's children are the crewmate's own workers (worker_conducts) ──


def _write_config(path, config: dict) -> None:
    """Replace config.json whole, through a temp file in its folder, as the gateway's
    own save does. A load that races an in-place rewrite can read the file empty, and
    the loader then reports the whole config unreadable for the rest of the test, so
    the worker runs plain."""
    atomic_write(path, json.dumps(config))


def _switch(tmp_path, *, on: bool) -> None:
    """Set pixel's owner switch ``worker_conducts`` as the Profile's PUT would store it."""
    path = tmp_path / "config.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    config["agents"]["pixel"]["worker_conducts"] = on
    _write_config(path, config)
    loader._invalidate_config_cache()


class TestAConductingWorkersChildren:
    """rfc-bot-workers-conduct.md section 5.4: with pixel's switch on, the worker its
    thread opened conducts, and its ``session_create`` opens pixel's own worker
    whatever ``agent`` it names, Profile-seeded, counted, filed in pixel's home.
    Its child is never marked, so one conducting level, by lineage. What it does
    per call follows the kit its session started with (section 5.2)."""

    @pytest.fixture
    def conducting_worker(self, crewmate, tmp_path):
        from kiro_crew import members

        state, caller, execution = crewmate
        _switch(tmp_path, on=True)
        worker = state.get_slot(_create(state, caller)["target"])
        key = slot_history_key(worker)
        # Marked at mint by the thread's create, known as pixel's worker, switch on,
        # and its session started with the session-control mount.
        assert members.is_thread_born_worker_session(key)
        assert members.conducting_worker_session(key)
        _start(state, worker)
        assert sc_conducts(state, worker)
        return state, worker, execution

    @pytest.mark.parametrize(
        "named, kind",
        [
            ("", ""),
            (MATE_WORKER_AGENT, "template"),
            ("kirocrew-worker", "template"),
            ("kirocrew", ""),
            ("reviewer", "template"),
            ("pixel", ""),
        ],
    )
    def test_its_child_is_the_crewmates_own_worker(self, conducting_worker, named, kind):
        from kiro_crew import members
        from kiro_crew.execution_context import read_session_execution

        state, worker, execution = conducting_worker
        child = state.get_slot(_create(state, worker, agent=named, agent_kind=kind)["target"])
        assert child.agent == MATE_WORKER_AGENT
        assert child.workspace == "member-pixel"
        actual = read_session_execution(slot_history_key(child), required=True)
        assert actual.store == execution.store
        assert actual.member_id == "pixel"
        assert actual.selection_kind == "template"
        assert actual.template_id == MATE_WORKER_AGENT
        # Counted toward pixel's Workers at once: the cap's registry knows it as
        # pixel's worker (``bot_worker_cap.own_worker_member``, untouched).
        assert members.crewmate_worker_member_id(slot_history_key(child)) == "pixel"
        # Profile-seeded like a worker the thread opens, not the worker's live posture.
        assert child._member_approval_seeded is True
        assert getattr(child, "_posture_inherited", False) is False
        # Filed in pixel's home, beside the worker that opened it.
        assert child.folder_id and child.folder_id == worker.folder_id
        # One conducting level, by lineage: the child's creator is a chat- key.
        assert child._created_by == worker.key
        assert not members.is_thread_born_worker_session(slot_history_key(child))
        assert not members.conducting_worker_session(slot_history_key(child))
        _start(state, child)
        assert not sc_conducts(state, child)

    def test_naming_another_crewmate_stays_refused(self, conducting_worker):
        state, worker, _execution = conducting_worker
        with pytest.raises(sc.SessionControlError) as refused:
            _create(state, worker, agent="ada")
        assert refused.value.code == "agent_workspace_mismatch"

    def test_the_child_counts_toward_workers_at_once(self, conducting_worker, tmp_path):
        """``max_workers`` is Bot-wide: the conducting worker and its child share it."""
        state, worker, _execution = conducting_worker
        path = tmp_path / "config.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        config["agents"]["pixel"]["max_workers"] = 1
        _write_config(path, config)
        loader._invalidate_config_cache()
        with pytest.raises(sc.SessionControlError) as refused:
            _create(state, worker)
        assert refused.value.code == "bot_worker_cap_reached"

    def test_the_switch_off_reaches_the_worker_at_its_next_start(self, conducting_worker, tmp_path):
        """Keyed on the kit its session started with, never the live switch: with the
        switch off, the running worker's child is still pixel's worker with the
        Profile seed; from its next start (the reload the owner's save brings) its
        create keeps today's resolution."""
        from kiro_crew import members
        from kiro_crew.dashboard.session_control_parts.session_create import (
            _hands_off_to_own_worker,
        )
        from kiro_crew.execution_context import read_session_execution
        from kiro_crew.mate_worker_conducts import holds_conducting_kit

        state, worker, _execution = conducting_worker
        key = slot_history_key(worker)
        execution = read_session_execution(key, required=True)
        _switch(tmp_path, on=False)
        assert members.is_thread_born_worker_session(key)
        assert not members.conducting_worker_session(key)  # its next start reads off
        assert holds_conducting_kit(state, worker)
        child = state.get_slot(
            _create(state, worker, agent="reviewer", agent_kind="template")["target"]
        )
        assert child.agent == MATE_WORKER_AGENT
        assert child._member_approval_seeded is True
        assert child.folder_id and child.folder_id == worker.folder_id
        _start(state, worker)
        kit = holds_conducting_kit(state, worker)
        assert not kit
        assert not sc_conducts(state, worker)
        assert not _hands_off_to_own_worker(execution, "", "", kit)
        assert not _hands_off_to_own_worker(execution, "reviewer", "template", kit)


def test_a_thread_born_worker_with_the_switch_off_is_marked_but_does_not_conduct(
    crewmate,
):
    """The mark is lineage; the switch decides. Off (the default), nothing changes."""
    from kiro_crew import members

    state, caller, _execution = crewmate
    worker = state.get_slot(_create(state, caller)["target"])
    key = slot_history_key(worker)
    _start(state, worker)
    assert members.is_thread_born_worker_session(key)
    assert not members.conducting_worker_session(key)
    assert not sc_conducts(state, worker)


def test_the_switch_on_reaches_a_running_plain_worker_at_its_next_start(crewmate, tmp_path):
    """A worker whose session started with the switch off stays plain until its next
    start: turning the switch on gives the running session no kit, so it dispatches
    nothing to pixel's worker; its next start conducts."""
    from kiro_crew import members
    from kiro_crew.mate_worker_conducts import holds_conducting_kit

    state, caller, _execution = crewmate
    worker = state.get_slot(_create(state, caller)["target"])
    key = slot_history_key(worker)
    _start(state, worker)
    _switch(tmp_path, on=True)
    assert members.conducting_worker_session(key)  # its next start would conduct
    assert not holds_conducting_kit(state, worker)
    assert not sc_conducts(state, worker)
    _start(state, worker)
    assert holds_conducting_kit(state, worker)
    assert sc_conducts(state, worker)


@pytest.mark.parametrize("verified", [True, False])
def test_a_revived_worker_is_marked_from_its_verified_creator(crewmate, monkeypatch, verified):
    """A revive restores the verified creator as a restart does, and the thread-born
    mark with it: an archived worker the thread opened is thread-born again after a
    gateway restart emptied the in-memory mark; a claim the creation records do not
    confirm marks nothing."""
    from kiro_crew import members

    state, caller, _execution = crewmate
    parent = caller.key if verified else "chat-not-its-creator"
    monkeypatch.setattr(sc, "_creation_parent", lambda key: parent)
    key = _create(state, caller)["target"]
    worker = state.get_slot(key)
    worker.messages.extend(
        [{"role": "user", "content": "m0"}, {"role": "assistant", "content": "m1"}]
    )
    worker._dirty = True
    asyncio.run(sc.close_target(state, caller_session_key=slot_history_key(caller), target=key))
    meta = state.conversation_log.get_metadata(f"dashboard:{key}")
    # The close is strictly before the revive (the revive helpers explain why).
    state.conversation_log.update_metadata(
        f"dashboard:{key}", {"closed_at": float(meta["closed_at"]) - 60.0}
    )
    members.note_worker_lineage(key, "")  # a gateway restart empties the mark
    assert not members.is_thread_born_worker_session(key)
    try:
        asyncio.run(
            sc.revive_session(state, caller_session_key=slot_history_key(caller), target=key)
        )
    except sc.SessionControlError:
        assert not verified  # an unconfirmed creator may also refuse the revive
    assert members.is_thread_born_worker_session(key) is verified


@pytest.mark.parametrize("by_thread", [True, False])
def test_a_fork_is_marked_by_the_rule_a_create_follows(crewmate, tmp_path, monkeypatch, by_thread):
    """A fork is marked from its creator as a create is: the Bot's thread forking its
    worker opens a thread-born worker, and a conducting worker forking its own child
    opens a plain one. The answer is the same at the fork, after a gateway restart
    restores the fork, and after a revive, so no fork changes kind by restarting."""
    from kiro_crew import members
    from kiro_crew.dashboard.chat import restore_recent_sessions, save_all_slots_to_history

    state, caller, _execution = crewmate
    _switch(tmp_path, on=True)
    worker = state.get_slot(_create(state, caller)["target"])
    _start(state, worker)
    forker, source = caller, worker.key
    if not by_thread:
        forker = worker
        source = _create(state, worker, agent=MATE_WORKER_AGENT, agent_kind="template")["target"]
    state.get_slot(source).messages.extend(
        [{"role": "user", "content": "m0"}, {"role": "assistant", "content": "m1"}]
    )
    fork = asyncio.run(
        sc.fork_session(state, caller_session_key=slot_history_key(forker), source=source)
    )["target"]
    assert members.is_crewmate_worker_session(fork)
    assert members.is_thread_born_worker_session(fork) is by_thread

    # The crew log's creation edges, which ``_record_spawn`` wrote and which a restart
    # and a revive check the transcript's ``created_by`` against, are off here.
    parents = {worker.key: caller.key, fork: forker.key}
    if not by_thread:
        parents[source] = worker.key
    monkeypatch.setattr(sc, "_creation_parent", lambda key: parents.get(key, ""))

    # A gateway restart: the in-memory mark is gone, the open fork is restored.
    state.get_slot(fork)._dirty = True
    save_all_slots_to_history(state)
    members.note_worker_lineage(fork, "")
    restarted = _make_state(tmp_path)
    restarted.conversation_log = state.conversation_log
    restore_recent_sessions(restarted, window_minutes=9999)
    assert restarted.get_slot(fork)._created_by == forker.key
    assert members.is_thread_born_worker_session(fork) is by_thread

    # A revive of the archived fork, the mark emptied again by another restart.
    asyncio.run(sc.close_target(state, caller_session_key=slot_history_key(forker), target=fork))
    meta = state.conversation_log.get_metadata(f"dashboard:{fork}")
    # The close is strictly before the revive (the revive helpers explain why).
    state.conversation_log.update_metadata(
        f"dashboard:{fork}", {"closed_at": float(meta["closed_at"]) - 60.0}
    )
    members.note_worker_lineage(fork, "")
    asyncio.run(sc.revive_session(state, caller_session_key=slot_history_key(forker), target=fork))
    assert members.is_thread_born_worker_session(fork) is by_thread


@pytest.mark.parametrize(
    "on, profile, expected",
    [(True, "normal", "normal"), (True, "trust_reads", "trust_reads"), (False, "normal", "trust")],
)
def test_a_fork_that_conducts_takes_the_profile_posture(crewmate, tmp_path, on, profile, expected):
    """With the switch on, the thread's fork of its worker conducts, so it is seeded
    from the Bot's Profile once its record is bound, as a created worker is; a copy of
    the thread's trust would outlive the thread's later mode change, whose walk skips
    a Bot's workers. With the switch off the fork copies the thread's trust, as it
    does today."""
    from kiro_crew import members
    from kiro_crew.dashboard.approval_inheritance import held_posture

    state, caller, _execution = crewmate
    _switch(tmp_path, on=on)
    _profile_mode(tmp_path, profile)
    worker = state.get_slot(_create(state, caller)["target"])
    worker.messages.extend(
        [{"role": "user", "content": "m0"}, {"role": "assistant", "content": "m1"}]
    )
    caller._trust = caller._trust_reads = True  # a person set the thread to Trust
    fork = asyncio.run(
        sc.fork_session(state, caller_session_key=slot_history_key(caller), source=worker.key)
    )["target"]
    child = state.get_slot(fork)
    assert members.conducting_worker_session(slot_history_key(child)) is on
    assert held_posture(child) == expected
    assert child._trust_reads is (expected != "normal")
    # Seeded at birth, as a created worker is, not at its first turn.
    assert child._member_approval_seeded is on


def _profile_mode(tmp_path, mode: str) -> None:
    """Set pixel's Profile Permissions (``approval_mode``) as the Profile's PUT stores it."""
    path = tmp_path / "config.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    config["agents"]["pixel"]["approval_mode"] = mode
    _write_config(path, config)
    loader._invalidate_config_cache()


def _hold(slot, posture: str) -> None:
    """A mode a person set on *slot* (or a trust a fork copied), its Profile seed spent."""
    slot._member_approval_seeded = True
    slot._trust, slot._trust_reads = posture == "trust", posture == "trust_reads"


def _bot_file_switches_the_dashboard_off(monkeypatch, tmp_path) -> None:
    """The stock worker base and pixel's own file, in a crewmate folder of this test's
    own: the file mounts ``kirocrew-dashboard`` (Add abilities from a template that
    does) and its owner switched it off, so the spec built for pixel's worker has it
    off while the base and the global settings do not."""
    from kiro_crew.config import paths
    from kiro_crew.members import MEMBER_DISPATCH_SERVER

    user, mates = tmp_path / "kiro-agents", tmp_path / "kiro-mates"
    user.mkdir()
    mates.mkdir()
    monkeypatch.setattr(paths, "kiro_agents_dir", lambda: user)
    monkeypatch.setattr(paths, "mate_agents_dir", lambda: mates)
    base = {
        "name": MATE_WORKER_AGENT,
        "prompt": "You are the worker.",
        "tools": ["@kirocrew-core"],
        "mcpServers": {"kirocrew-core": {"command": "kirocrew", "args": ["mcp", "core"]}},
    }
    switched_off = {"command": "kirocrew", "args": ["mcp-dashboard"], "disabled": True}
    own = {"name": "pixel", "mcpServers": {MEMBER_DISPATCH_SERVER: switched_off}}
    (mates / f"{MATE_WORKER_AGENT}.json").write_text(json.dumps(base), encoding="utf-8")
    (mates / "pixel.json").write_text(json.dumps(own), encoding="utf-8")
    assert mate_worker.mate_file_for_member("pixel") == "pixel"


class TestTheFirstTurnOnTheKit:
    """rfc-bot-workers-conduct.md section 5.6: at the first turn a thread-born worker
    runs on the conducting kit, its posture becomes the stricter of what it holds and
    its Bot's Profile mode. It is never raised, and a mode a person sets while the
    worker conducts stands until its next start on the kit."""

    @staticmethod
    def _admit(state, slot, config=None) -> bool:
        """What turn admission runs before the provider is claimed and again after it:
        the Profile seed, then the tighten, on the turn's config load, with the start's
        own mount prediction for a session that is not live yet."""
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.dashboard.crewmate_approval import (
            predicted_start_mounts,
            seed_for_turn,
            tighten_for_conducting,
        )
        from kiro_crew.execution_context import read_session_execution

        config = config if config is not None else KiroCrewConfig.load()
        execution = read_session_execution(slot_history_key(slot))
        seed_for_turn(state, slot, config, execution)
        start_mounts = asyncio.run(predicted_start_mounts(state, slot, config))
        return tighten_for_conducting(state, slot, config, execution, start_mounts=start_mounts)

    @pytest.mark.parametrize(
        "profile, held, expected",
        [
            ("normal", "trust", "normal"),
            ("normal", "trust_reads", "normal"),
            ("trust_reads", "trust", "trust_reads"),
            ("trust", "normal", "normal"),
            ("trust", "trust_reads", "trust_reads"),
            ("trust_reads", "trust_reads", "trust_reads"),
        ],
    )
    def test_the_stricter_posture_wins(self, crewmate, tmp_path, profile, held, expected):
        from kiro_crew.dashboard.approval_inheritance import held_posture

        state, caller, _execution = crewmate
        _profile_mode(tmp_path, profile)
        worker = state.get_slot(_create(state, caller)["target"])
        _hold(worker, held)
        assert not self._admit(state, worker)  # plain: the switch is off
        assert held_posture(worker) == held
        _switch(tmp_path, on=True)  # its next start takes the kit
        assert self._admit(state, worker) is (expected != held)
        assert held_posture(worker) == expected
        if expected != held:
            # The Profile decides the lowered mode, not the creator's later changes.
            assert worker._posture_inherited is False

    def test_the_switch_on_reload_respawn_is_held_too(self, crewmate, tmp_path):
        """The reload a switch change brings starts the session before any turn; the
        first turn on that live session is the one that tightens it."""
        state, caller, _execution = crewmate
        _profile_mode(tmp_path, "normal")
        worker = state.get_slot(_create(state, caller)["target"])
        _hold(worker, "trust")
        _switch(tmp_path, on=True)
        _start(state, worker)  # the reload's respawn, with the kit
        assert sc_conducts(state, worker)
        assert self._admit(state, worker)
        assert not worker._trust
        # The live session's stored policy stops auto-approving as well.
        from kiro_crew.dashboard.chat_utils import effective_session_key

        state.sessions.set_approval_policy.assert_called_with(effective_session_key(worker), "")

    @pytest.mark.parametrize("turn_while_plain", [True, False])
    def test_a_mode_set_while_it_conducts_stands_until_its_next_start_on_the_kit(
        self, crewmate, tmp_path, turn_while_plain
    ):
        """Checked once per start on the kit. The switch-off and switch-on reloads
        each start the session again, with or without a turn between them."""
        state, caller, _execution = crewmate
        _profile_mode(tmp_path, "normal")
        _switch(tmp_path, on=True)
        worker = state.get_slot(_create(state, caller)["target"])
        _start(state, worker)
        assert not self._admit(state, worker)  # its first turn on the kit: nothing above normal
        worker._trust = True  # a person trusts the conducting worker
        assert not self._admit(state, worker)
        assert worker._trust
        _switch(tmp_path, on=False)
        _start(state, worker)  # the switch-off reload: a plain start
        if turn_while_plain:
            assert not self._admit(state, worker)
        assert worker._trust
        _switch(tmp_path, on=True)
        _start(state, worker)  # the switch-on reload: the next start on the kit
        assert self._admit(state, worker)
        assert not worker._trust

    def test_a_conversation_swapped_in_place_is_a_new_start_on_the_kit(self, crewmate, tmp_path):
        """Edit & Resend while sub-agent runs share the process swaps the native
        conversation on the provider it already runs (``swap_conversation``): the same
        provider object, a new native session id. The new conversation is a new start
        on the kit, so a mode a person set in the one it replaced is lowered again."""
        state, caller, _execution = crewmate
        _profile_mode(tmp_path, "normal")
        _switch(tmp_path, on=True)
        worker = state.get_slot(_create(state, caller)["target"])
        live = _start(state, worker, session_id="s1")
        _hold(worker, "trust")
        assert self._admit(state, worker)  # its first turn on the kit lowers it
        assert not worker._trust
        worker._trust = True  # a person trusts the conducting worker
        assert not self._admit(state, worker)  # the next turn claims the same conversation
        assert worker._trust
        live.session_id = "s2"  # the swap: the same provider, a new native conversation
        assert self._admit(state, worker)
        assert not worker._trust

    def test_a_session_that_reports_no_id_is_checked_at_each_turn(self, crewmate, tmp_path):
        """An empty native session id never matches the recorded start, so a session
        that reports none is checked again at its next turn: a mode a person set in
        between is lowered, and the check rests on nothing outside it."""
        state, caller, _execution = crewmate
        _profile_mode(tmp_path, "normal")
        _switch(tmp_path, on=True)
        worker = state.get_slot(_create(state, caller)["target"])
        _start(state, worker, session_id="")
        _hold(worker, "trust")
        assert self._admit(state, worker)
        assert not worker._trust
        worker._trust = True  # a person trusts the conducting worker
        assert self._admit(state, worker)  # still no id: checked again
        assert not worker._trust

    @pytest.mark.parametrize(
        ("backend", "dashboard_off", "mounts"),
        [
            pytest.param(ACP_BACKEND_KAS, False, True, id="kas-start-that-mounts"),
            pytest.param(ACP_BACKEND_KAS, True, False, id="kas-dashboard-switched-off"),
            pytest.param(ACP_BACKEND_CLAUDE, True, False, id="claude-dashboard-switched-off"),
            pytest.param(ACP_BACKEND_CLAUDE, "bot-file", False, id="claude-bot-file-off"),
            pytest.param(ACP_BACKEND_KAS, "bot-file", False, id="kas-bot-file-off"),
            pytest.param(ACP_BACKEND_COPILOT, False, False, id="backend-that-carries-no-mount"),
        ],
    )
    def test_a_cold_start_is_held_only_when_it_mounts(
        self, crewmate, tmp_path, monkeypatch, backend, dashboard_off, mounts
    ):
        """Before the claim, the start's own predicate decides: a worker whose start
        cannot mount session control (a backend that carries no mount, a switched-off
        ``kirocrew-dashboard``, in the global settings or in the Bot's own file) keeps
        the mode a person set, at the cold start and after the claim, while one whose
        start mounts is held to its Profile."""
        from kiro_crew.acp import session_mcp
        from kiro_crew.dashboard import chat_handlers
        from kiro_crew.members import MEMBER_DISPATCH_SERVER

        state, caller, _execution = crewmate
        _profile_mode(tmp_path, "normal")
        _switch(tmp_path, on=True)
        worker = state.get_slot(_create(state, caller)["target"])
        _hold(worker, "trust")
        # The facade is the import path and the patch surface.
        monkeypatch.setattr(
            chat_handlers, "configured_backend_for_session", lambda _key, _config: backend
        )
        off = {"mcpServers": {MEMBER_DISPATCH_SERVER: {"disabled": True}}}
        monkeypatch.setattr(
            session_mcp, "_global_settings", lambda **_kw: off if dashboard_off is True else {}
        )
        if dashboard_off == "bot-file":
            _bot_file_switches_the_dashboard_off(monkeypatch, tmp_path)
        assert self._admit(state, worker) is mounts  # the cold start, before the claim
        assert worker._trust is not mounts
        live = _start(state, worker)  # the claim's start mounts what the predicate said
        live.member_dispatch_mounted = mounts
        assert not self._admit(state, worker)
        assert worker._trust is not mounts

    def test_the_check_after_the_claim_holds_a_start_the_admission_did_not_predict(
        self, crewmate, tmp_path
    ):
        """The admission reads the switch off, a save turns it on, and the start then
        mounts the kit: the check after the claim, on the live start, lowers it."""
        from kiro_crew.config.loader import KiroCrewConfig

        state, caller, _execution = crewmate
        _profile_mode(tmp_path, "normal")
        worker = state.get_slot(_create(state, caller)["target"])
        _hold(worker, "trust")
        stale = KiroCrewConfig.load()  # the turn's load, the switch still off
        assert not self._admit(state, worker, stale)
        assert worker._trust
        _switch(tmp_path, on=True)
        _start(state, worker)  # the claim's start takes the kit
        assert self._admit(state, worker, stale)
        assert not worker._trust

    def test_a_pending_policy_verdict_leaves_the_check_to_the_next_turn(
        self, crewmate, tmp_path, monkeypatch
    ):
        """While a ceiling is being installed a trust reads as denied, so the check
        waits for a final answer rather than spend the start's one check on it."""
        from kiro_crew import approval_mode_policy

        state, caller, _execution = crewmate
        _profile_mode(tmp_path, "normal")
        _switch(tmp_path, on=True)
        worker = state.get_slot(_create(state, caller)["target"])
        _start(state, worker)
        _hold(worker, "trust")
        monkeypatch.setattr(approval_mode_policy, "verdict_pending", lambda: True)
        assert not self._admit(state, worker)
        monkeypatch.setattr(approval_mode_policy, "verdict_pending", lambda: False)
        assert self._admit(state, worker)
        assert not worker._trust

    def test_a_trust_a_policy_deny_hides_is_lowered_too(self, crewmate, tmp_path, monkeypatch):
        """The lowering reads what the worker HOLDS, not what the policy lets it use
        now, so a trust a deny hides cannot come back past the Profile when the deny
        lifts."""
        from kiro_crew.dashboard.state_parts import chat_slot

        state, caller, _execution = crewmate
        _profile_mode(tmp_path, "normal")
        _switch(tmp_path, on=True)
        worker = state.get_slot(_create(state, caller)["target"])
        _start(state, worker)
        _hold(worker, "trust")
        permitted = chat_slot.session_mode_permitted
        monkeypatch.setattr(chat_slot, "session_mode_permitted", lambda mode: mode == "normal")
        assert not worker._trust  # hidden by the deny, still held
        assert self._admit(state, worker)
        monkeypatch.setattr(chat_slot, "session_mode_permitted", permitted)  # the deny lifts
        assert not worker._trust

    def test_the_sessions_that_follow_the_worker_keep_following_it(self, crewmate, tmp_path):
        """A session the worker created while it was plain inherits its mode, follows
        the lowering, and stays inherited, so the worker's next change reaches it."""
        from kiro_crew.dashboard.approval_inheritance import held_posture

        state, caller, _execution = crewmate
        _profile_mode(tmp_path, "trust_reads")
        worker = state.get_slot(_create(state, caller)["target"])
        _hold(worker, "trust")
        child = state.get_or_create_slot("chat-11-1785397100")
        child.agent = "reviewer"
        child._created_by = worker.key
        child._posture_inherited = True
        child._trust = True
        _switch(tmp_path, on=True)
        _start(state, worker)
        assert self._admit(state, worker)
        assert held_posture(worker) == "trust_reads"
        assert held_posture(child) == "trust_reads"
        assert child._posture_inherited is True


@pytest.mark.asyncio
async def test_a_real_turn_on_the_kit_claims_its_session_at_the_profiles_posture(
    crewmate, tmp_path, monkeypatch
):
    """Through turn admission: a worker holding a trust it copied while the switch was
    off claims its session at its Profile's posture once the switch is on."""
    from chat_agent_selection_helpers import _finished, _turn_state

    from kiro_crew.dashboard import chat_runner

    state, caller, _execution = crewmate
    await asyncio.to_thread(_profile_mode, tmp_path, "normal")
    worker = state.get_slot((await asyncio.to_thread(_create, state, caller))["target"])
    await asyncio.to_thread(_switch, tmp_path, on=True)
    turn_state = _turn_state(tmp_path, monkeypatch)
    turn_state.conversation_log = state.conversation_log
    slot = turn_state.get_or_create_slot(worker.key)
    slot.agent = worker.agent
    slot.agent_kind = worker.agent_kind
    slot.workspace = worker.workspace
    slot.memory_store = worker.memory_store
    slot.project = worker.project
    _hold(slot, "trust")
    seen = {}
    claim = turn_state.sessions.get_or_create

    async def _claim(*args, **kwargs):
        seen["trust"] = slot._trust
        return await claim(*args, **kwargs)

    turn_state.sessions.get_or_create = _claim
    await _finished(chat_runner._run_chat(turn_state, slot, "go"), "the first turn on the kit")
    assert seen.get("trust") is False, "the worker claimed its session in the copied trust"


@pytest.mark.asyncio
async def test_a_real_turn_holds_a_start_its_admission_did_not_predict(
    crewmate, tmp_path, monkeypatch
):
    """Through turn admission: the turn reads the switch off, a save turns it on, and
    the session its claim starts takes the kit. The check after the claim lowers the
    worker before the session's policy is stored."""
    from chat_agent_selection_helpers import _finished, _turn_state

    from kiro_crew.dashboard import chat_runner
    from kiro_crew.dashboard.chat_utils import effective_session_key

    state, caller, _execution = crewmate
    await asyncio.to_thread(_profile_mode, tmp_path, "normal")
    worker = state.get_slot((await asyncio.to_thread(_create, state, caller))["target"])
    turn_state = _turn_state(tmp_path, monkeypatch)
    turn_state.conversation_log = state.conversation_log
    slot = turn_state.get_or_create_slot(worker.key)
    slot.agent = worker.agent
    slot.agent_kind = worker.agent_kind
    slot.workspace = worker.workspace
    slot.memory_store = worker.memory_store
    slot.project = worker.project
    _hold(slot, "trust")
    key = effective_session_key(slot)
    live: dict = {}
    turn_state.sessions.get_provider.side_effect = live.get
    claim = turn_state.sessions.get_or_create

    async def _claim(*args, **kwargs):
        await asyncio.to_thread(_switch, tmp_path, on=True)  # lands after the turn's load
        provider, is_new, resumed = await claim(*args, **kwargs)
        provider.member_dispatch_mounted = True  # its start mounted the kit
        live[key] = provider
        return provider, is_new, resumed

    turn_state.sessions.get_or_create = _claim
    await _finished(chat_runner._run_chat(turn_state, slot, "go"), "the turn")
    assert slot._trust is False
    calls = turn_state.sessions.set_approval_policy.call_args_list
    stored = [c.args[1] for c in calls if c.args[0] == key]
    assert stored and stored[-1] == "", "the session kept an auto-approving policy"


@pytest.mark.asyncio
async def test_a_mode_a_person_sets_while_it_conducts_stands_across_its_turns(
    crewmate, tmp_path, monkeypatch
):
    """Through two real turns on one running session: a person trusts the conducting
    worker after its first turn on the kit, and its next turn keeps that trust. Each
    claim moves the manager's session generation, as ``get_or_create`` does (twice),
    so only the session itself can say the start is the same one."""
    from chat_agent_selection_helpers import _finished, _turn_state

    from kiro_crew.dashboard import chat_runner
    from kiro_crew.dashboard.chat_utils import effective_session_key

    state, caller, _execution = crewmate
    await asyncio.to_thread(_profile_mode, tmp_path, "normal")
    await asyncio.to_thread(_switch, tmp_path, on=True)
    worker = state.get_slot((await asyncio.to_thread(_create, state, caller))["target"])
    turn_state = _turn_state(tmp_path, monkeypatch)
    turn_state.conversation_log = state.conversation_log
    slot = turn_state.get_or_create_slot(worker.key)
    slot.agent = worker.agent
    slot.agent_kind = worker.agent_kind
    slot.workspace = worker.workspace
    slot.memory_store = worker.memory_store
    slot.project = worker.project
    key = effective_session_key(slot)
    live: dict = {}
    generation = {"now": 0}
    turn_state.sessions.get_provider.side_effect = live.get
    turn_state.sessions.session_generation.side_effect = lambda _key: generation["now"]
    claim = turn_state.sessions.get_or_create

    async def _claim(*args, **kwargs):
        generation["now"] += 2
        provider, is_new, resumed = await claim(*args, **kwargs)
        provider.member_dispatch_mounted = True
        live[key] = provider
        return provider, is_new, resumed

    turn_state.sessions.get_or_create = _claim
    await _finished(chat_runner._run_chat(turn_state, slot, "go"), "the first turn on the kit")
    slot._trust = True  # a person trusts the conducting worker
    await _finished(chat_runner._run_chat(turn_state, slot, "again"), "its next turn")
    assert slot._trust is True, "the worker's next turn took back the trust a person set"


def test_a_history_resume_reopens_a_worker_plain(crewmate, tmp_path):
    """A History resume restores no creator (a revive alone restores a corroborated
    one), so the worker it reopens is plain even when this process still holds the
    mark from before the close: the same answer a restart in between gives."""
    from kiro_crew import members
    from kiro_crew.dashboard.chat_handlers import resume_slot_from_history

    state, caller, _execution = crewmate
    _switch(tmp_path, on=True)
    key = _create(state, caller)["target"]
    worker = state.get_slot(key)
    worker.messages.extend(
        [{"role": "user", "content": "m0"}, {"role": "assistant", "content": "m1"}]
    )
    worker._dirty = True
    asyncio.run(sc.close_target(state, caller_session_key=slot_history_key(caller), target=key))
    meta = state.conversation_log.get_metadata(f"dashboard:{key}")
    # The close is strictly before the resume (the revive helpers explain why).
    state.conversation_log.update_metadata(
        f"dashboard:{key}", {"closed_at": float(meta["closed_at"]) - 60.0}
    )
    assert members.is_thread_born_worker_session(key)  # no restart since the close
    asyncio.run(resume_slot_from_history(state, name=key))
    assert key in state._slots
    assert not members.is_thread_born_worker_session(key)
    assert not members.conducting_worker_session(slot_history_key(state.get_slot(key)))


# ── the worker runs on its crewmate's backend, with its kit, or not at all ──


def _set_backends(tmp_path, *, default: str, member: str) -> None:
    """Store ``agent.acp_backend`` / ``agent.member_acp_backend`` as a user would."""
    path = tmp_path / "config.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    config.setdefault("agent", {}).update({"acp_backend": default, "member_acp_backend": member})
    path.write_text(json.dumps(config), encoding="utf-8")
    loader._invalidate_config_cache()


@pytest.fixture
def no_known_workers(monkeypatch):
    from kiro_crew import members

    monkeypatch.setattr(members, "_CREWMATE_WORKER_SLOTS", set())
    return members


def _selected(key: str) -> str:
    from kiro_crew.members import select_provider_backend

    agent = loader.KiroCrewConfig.load().agent
    return select_provider_backend(key, agent.member_acp_backend, agent.acp_backend)


@pytest.mark.parametrize("member", ["kas", "kiro"])
def test_the_worker_runs_on_its_crewmates_backend_not_the_default(
    crewmate, tmp_path, no_known_workers, member
):
    """Ray's live shape: ``acp_backend`` Kiro, crewmates on KAS (a stored ``kiro``
    member backend reads as KAS too). The worker follows its crewmate."""
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS, ACP_BACKEND_KIRO

    _set_backends(tmp_path, default="kiro", member=member)
    state, caller, _execution = crewmate
    worker = state.get_slot(_create(state, caller)["target"])
    key = slot_history_key(worker)

    assert _selected(key) == ACP_BACKEND_KAS
    # Any other session on the default keeps the default.
    assert _selected("dashboard:chat-unrelated") == ACP_BACKEND_KIRO


def test_the_worker_keeps_its_backend_after_a_restart(crewmate, tmp_path, no_known_workers):
    """A restart forgets the in-memory set; the record read every turn makes
    before allocating (``resolve_session_agent_bindings``) puts the worker back."""
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS, ACP_BACKEND_KIRO
    from kiro_crew.session_agent_selection import resolve_session_agent_bindings

    _set_backends(tmp_path, default="kiro", member="kas")
    state, caller, _execution = crewmate
    key = slot_history_key(state.get_slot(_create(state, caller)["target"]))
    no_known_workers._CREWMATE_WORKER_SLOTS.clear()
    assert _selected(key) == ACP_BACKEND_KIRO  # the gap the read below closes

    def resolver(config, selected, *project, **kwargs):
        return SimpleNamespace(kiro_agent=MATE_WORKER_AGENT, memory_store_name="")

    resolve_session_agent_bindings(resolver, loader.KiroCrewConfig.load(), key, MATE_WORKER_AGENT)

    assert _selected(key) == ACP_BACKEND_KAS


def test_a_worker_switched_to_another_template_leaves_its_crewmates_backend(
    crewmate, tmp_path, no_known_workers
):
    """Its record now names another template, so the next read before allocating
    takes the key off the crewmate's backend."""
    from dataclasses import replace

    from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS, ACP_BACKEND_KIRO
    from kiro_crew.execution_context import bind_session_execution, read_session_execution
    from kiro_crew.session_agent_selection import resolve_session_agent_bindings

    _set_backends(tmp_path, default="kiro", member="kas")
    state, caller, _execution = crewmate
    key = slot_history_key(state.get_slot(_create(state, caller)["target"]))
    assert _selected(key) == ACP_BACKEND_KAS
    worker = read_session_execution(key, required=True)
    switched = replace(worker, template_id="kirocrew", selection_name="kirocrew")
    bind_session_execution(key, switched, replace_existing=True, expected=worker)

    def resolver(config, selected, *project, **kwargs):
        return SimpleNamespace(kiro_agent="kirocrew", memory_store_name="")

    resolve_session_agent_bindings(resolver, loader.KiroCrewConfig.load(), key, "kirocrew")

    assert _selected(key) == ACP_BACKEND_KIRO


def test_a_worker_slot_made_again_on_an_ordinary_template_leaves_at_the_commit(
    crewmate, tmp_path, no_known_workers, monkeypatch
):
    """The explicit selection's commit takes the key out, before any turn, so the
    model picker and the eager start read the configured backend too."""
    from aiohttp.test_utils import TestClient, TestServer
    from chat_test_helpers import _make_app_with_agent_routes
    from dashboard_owner_helpers import as_owner

    from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS, ACP_BACKEND_KIRO

    _set_backends(tmp_path, default="kiro", member="kas")
    state, caller, _execution = crewmate
    worker = state.get_slot(_create(state, caller)["target"])
    key = slot_history_key(worker)
    assert _selected(key) == ACP_BACKEND_KAS
    monkeypatch.setattr(
        loader, "_MATERIALIZED_AGENTS", frozenset({*loader._MATERIALIZED_AGENTS, "kirocrew"})
    )
    state._slots.pop(worker.key)

    async def remake():
        async with TestClient(TestServer(as_owner(_make_app_with_agent_routes(state)))) as client:
            response = await client.post(
                "/api/chat/slots",
                json={"name": worker.key, "agent": "kirocrew", "agent_kind": "template"},
            )
            assert response.status == 200, await response.text()

    asyncio.run(remake())

    assert state._slots[worker.key].agent == "kirocrew"
    assert _selected(key) == ACP_BACKEND_KIRO


def test_a_worker_on_a_backend_that_loads_no_member_spec_is_refused(
    crewmate, tmp_path, no_known_workers
):
    """The member chat's refusal and code: goose loads no saved member spec."""
    _set_backends(tmp_path, default="kiro", member="goose")
    state, caller, _execution = crewmate
    before = set(state._slots)

    with pytest.raises(sc.SessionControlError) as refused:
        _create(state, caller)

    assert refused.value.code == "capability_harness_unsupported"
    assert "goose" in str(refused.value) and harness_label("kas") in str(refused.value)
    assert set(state._slots) == before  # nothing was allocated


def test_a_worker_that_lands_on_kiro_cli_is_refused(
    crewmate, tmp_path, no_known_workers, monkeypatch
):
    """kiro-cli loads member specs but reads ``--agent`` off disk, where the worker
    spec is not, so a worker that lands there (KAS not selectable) is refused."""
    _set_backends(tmp_path, default="kiro", member="kiro")
    monkeypatch.setattr(mate_worker, "crewmate_worker_backend", lambda *_a: ACP_BACKEND_KIRO)
    state, caller, _execution = crewmate
    before = set(state._slots)

    with pytest.raises(sc.SessionControlError) as refused:
        _create(state, caller)

    assert refused.value.code == "capability_harness_unsupported"
    assert "(kiro)" in str(refused.value) and harness_label("kas") in str(refused.value)
    assert set(state._slots) == before  # nothing was allocated


def test_a_worker_its_pin_cannot_carry_is_refused_naming_the_pin(
    crewmate, tmp_path, no_known_workers
):
    """The pin outranks ``agent.member_acp_backend``, so a hand-off its ``kiro`` pin
    put on the kiro-cli v2 launch is refused naming the pin, never the setting that
    cannot move it. (A pin to a harness Bots cannot run on, such as deepseek, runs
    on KAS instead and is not refused.)"""
    _set_backends(tmp_path, default="kiro", member="kas")
    path = tmp_path / "config.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    config["agents"]["pixel"]["acp_backend"] = "kiro"
    path.write_text(json.dumps(config), encoding="utf-8")
    loader._invalidate_config_cache()
    state, caller, _execution = crewmate
    before = set(state._slots)

    with pytest.raises(sc.SessionControlError) as refused:
        _create(state, caller)

    assert refused.value.code == "capability_harness_unsupported"
    assert "(kiro)" in str(refused.value)
    assert "pixel is pinned to" in str(refused.value)
    assert "agent.member_acp_backend" not in str(refused.value)
    assert set(state._slots) == before  # nothing was allocated


def test_only_the_crewmates_worker_record_routes_to_its_backend(no_known_workers):
    from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef

    def record(kind, template, member="pixel"):
        return ExecutionContext(member, MemoryStoreRef("member-pixel-1", member), kind, template)

    assert mate_worker.note_worker_session(
        "dashboard:chat-1", record("template", MATE_WORKER_AGENT)
    )
    for key, other in (
        ("dashboard:chat-2", record("member", MATE_CONDUCTOR_AGENT)),
        ("dashboard:chat-3", record("template", "kirocrew-worker")),
        ("dashboard:chat-4", record("member", MATE_WORKER_AGENT)),
        ("dashboard:chat-5", None),
    ):
        assert not mate_worker.note_worker_session(key, other), key
    assert no_known_workers._CREWMATE_WORKER_SLOTS == {"chat-1"}
    # Every spelling of the key answers the same.
    assert no_known_workers.is_crewmate_worker_session("dashboard_chat-1")


@pytest.mark.parametrize("backend", sorted(ACP_BACKENDS_KNOWN))
def test_every_backend_carries_the_kit_or_refuses_the_worker(backend, monkeypatch, tmp_path):
    """No backend runs a crewmate's worker on the bare base: either it carries the
    per-session spec, and that spec carries the crewmate's switched-off tool, or the
    worker is refused with a remedy."""
    refusal = mate_worker.worker_kit_backend_refusal(backend)
    if backend not in ACP_BACKENDS_MATE_WORKER_KIT:
        assert refusal and "change Settings → Agent Harness" in refusal
        return
    assert refusal is None
    from kiro_crew.config import paths as paths_mod

    mates = tmp_path / "mates"
    mates.mkdir()
    (mates / "pixel.json").write_text(
        json.dumps({"name": "pixel", "prompt": "", "excludedTools": ["@jira/delete_issue"]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(paths_mod, "mate_agents_dir", lambda: mates)
    monkeypatch.setattr(mate_worker, "mate_file_for_member", lambda member, config=None: "pixel")
    monkeypatch.setattr(mate_worker, "globally_disabled_refs", lambda names: [])
    monkeypatch.setattr(
        "kiro_crew.execution_context.read_session_execution",
        lambda key: SimpleNamespace(member_id="pixel"),
    )
    base = {"name": MATE_WORKER_AGENT, "prompt": "base", "tools": ["fs_read"], "mcpServers": {}}

    spec = mate_worker.worker_session_spec(base, "dashboard:chat-9")

    assert "@jira/delete_issue" in spec.get("excludedTools", [])


def test_unknown_harness_pin_refuses_worker_creation_without_echoing_it(crewmate, caplog):
    from kiro_crew.config.loader import KiroCrewConfig

    secret = "sk-test-not-a-real-secret-0000"
    cfg = KiroCrewConfig.load()
    cfg.agents["pixel"].acp_backend = secret
    cfg.save()
    state, caller, _execution = crewmate
    before = set(state._slots)
    with pytest.raises(sc.SessionControlError) as raised:
        _create(state, caller)
    assert raised.value.status == 409
    assert raised.value.code == "crewmate_harness_unavailable"
    assert "an unknown harness" in raised.value.message
    assert secret not in str(raised.value) + caplog.text
    assert set(state._slots) == before
    assert KiroCrewConfig.load().agents["pixel"].acp_backend == secret
