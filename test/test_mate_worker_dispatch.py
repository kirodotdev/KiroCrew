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
    ACP_BACKEND_KIRO,
    ACP_BACKENDS_KNOWN,
    ACP_BACKENDS_MATE_WORKER_KIT,
)
from kiro_crew.config import loader
from kiro_crew.dashboard import create_rate_limit
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.mate_worker import MATE_CONDUCTOR_AGENT, MATE_WORKER_AGENT
from kiro_crew.members import DM_SLOT_KEY_PREFIX


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
    """A slot from before records names its crewmate by slot agent only; the
    worker it would open has no crewmate and runs on the shared store."""
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

    with pytest.raises(sc.SessionControlError) as refused:
        _create(state, caller)

    assert refused.value.code == "conductor_record_missing"
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
    assert not sc_conducts(worker)
    _ = read_session_execution(slot_history_key(worker), required=True)


def sc_conducts(slot):
    from kiro_crew.dashboard.session_control_parts.session_create import _conducts_for_a_worker
    from kiro_crew.execution_context import read_session_execution

    return _conducts_for_a_worker(slot, read_session_execution(slot_history_key(slot)))


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
    assert "goose" in str(refused.value) and "kas" in str(refused.value)
    assert set(state._slots) == before  # nothing was allocated


def test_a_worker_that_lands_on_kiro_cli_is_refused(
    crewmate, tmp_path, no_known_workers, monkeypatch
):
    """kiro-cli loads member specs but reads ``--agent`` off disk, where the worker
    spec is not, so a worker that lands there (KAS not selectable) is refused."""
    _set_backends(tmp_path, default="kiro", member="kiro")
    monkeypatch.setattr(mate_worker, "crewmate_worker_backend", lambda _agent: ACP_BACKEND_KIRO)
    state, caller, _execution = crewmate
    before = set(state._slots)

    with pytest.raises(sc.SessionControlError) as refused:
        _create(state, caller)

    assert refused.value.code == "capability_harness_unsupported"
    assert "(kiro)" in str(refused.value) and "kas" in str(refused.value)
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
        assert refusal and "agent.member_acp_backend" in refusal
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
