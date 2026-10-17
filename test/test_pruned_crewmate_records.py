"""Records bound to a crewmate the startup prune removed stay executable.

``crewmate_prune_migration`` deletes the ``config.agents`` rows an older agent
sync generated and leaves each agent installed. Chats, forks, subagent runs and
cron jobs that picked one of those crewmates still carry a ``member`` record
naming it, and a reader that trusts the kind refuses it as an unavailable
member. The record decoder re-reads exactly that shape as its template on
the shared store -- but only for a name the prune's own marker lists as
removed -- so every reader gets the same answer.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from kiro_crew import execution_context as ec
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.subagent_manager.admission.gate import _GateMixin


def _config(*names: str) -> KiroCrewConfig:
    cfg = KiroCrewConfig()
    cfg.agents = {name: KiroCrewAgentConfig(kiro_agent=name) for name in names}
    return cfg


def _record(
    *, name="synced-agent", template="synced-agent", store="default", member_id=None
) -> dict:
    execution = ec.ExecutionContext(
        member_id,
        ec.MemoryStoreRef(store, member_id),
        "member",
        template,
        selection_name=name,
    )
    return {ec.EXECUTION_CONTEXT_KEY: execution.to_record()}


@pytest.fixture
def config(monkeypatch):
    holder = {"cfg": _config("kirocrew"), "removed": frozenset({"synced-agent"})}
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls, *a, **k: holder["cfg"]))
    monkeypatch.setattr(
        "kiro_crew.crewmate_prune_migration.removed_crewmate_names", lambda: holder["removed"]
    )
    return holder


def test_decoder_reads_a_pruned_synced_crewmate_as_its_template(config):
    execution = ec.execution_from_record(_record())
    assert execution.selection_kind == "template"
    assert execution.template_id == "synced-agent"
    assert execution.selection_name == "synced-agent"
    assert execution.store.store_id == "default"
    assert execution.member_id is None


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"store": "private-store"}, id="own-store"),
        pytest.param({"store": "member-store", "member_id": "m-1"}, id="identity"),
        pytest.param({"name": "renamed", "template": "synced-agent"}, id="name-differs"),
    ],
)
def test_decoder_keeps_every_other_member_record(config, kwargs):
    execution = ec.execution_from_record(_record(**kwargs))
    assert execution.selection_kind == "member"


def test_decoder_keeps_a_member_whose_row_still_exists(config):
    config["cfg"] = _config("kirocrew", "synced-agent")
    assert ec.execution_from_record(_record()).selection_kind == "member"


def test_decoder_keeps_a_member_deleted_by_any_other_route(config):
    """A member row deleted by hand, not by the prune, keeps refusing."""
    config["removed"] = frozenset()
    assert ec.execution_from_record(_record()).selection_kind == "member"


def test_decoder_keeps_the_record_when_config_is_unreadable(config, monkeypatch):
    def boom(cls, *a, **k):
        raise OSError("unreadable")

    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(boom))
    assert ec.execution_from_record(_record()).selection_kind == "member"


def test_decoder_keeps_the_record_when_the_marker_is_unreadable(config, monkeypatch):
    def boom():
        raise OSError("unreadable")

    monkeypatch.setattr("kiro_crew.crewmate_prune_migration.removed_crewmate_names", boom)
    assert ec.execution_from_record(_record()).selection_kind == "member"


def test_continued_subagent_run_of_a_pruned_crewmate_is_admitted(config, monkeypatch):
    """``spawn_continue`` admits it rather than "selected member is unavailable"."""
    run = ec.execution_from_record(_record())
    gate = SimpleNamespace()
    execution = _GateMixin.resolve_spawn_execution(
        gate, conversation_key="subagent:run-1", _record=run
    )
    assert execution.selection_kind == "template"
    assert execution.template_id == "synced-agent"


def test_spawn_inheriting_a_pruned_crewmate_keeps_its_template(config):
    gate = SimpleNamespace()
    execution = _GateMixin.resolve_spawn_execution(
        gate,
        parent_session_key="dashboard:old-chat",
        _record=None,
        _inherited_selection=("member", "synced-agent"),
    )
    assert execution.selection_kind == "template"
    assert execution.template_id == "synced-agent"
    assert execution.store.store_id == "default"


def test_spawn_inheriting_a_member_the_prune_did_not_remove_still_refuses(config):
    config["removed"] = frozenset()
    with pytest.raises(ValueError, match="selected member is unavailable"):
        _GateMixin.resolve_spawn_execution(
            SimpleNamespace(),
            parent_session_key="dashboard:old-chat",
            _record=None,
            _inherited_selection=("member", "synced-agent"),
        )


def test_spawn_inheriting_a_missing_member_on_its_own_store_still_refuses(config, monkeypatch):
    def store_exists(store, *, memory_mode="persistent", app="", template_id=""):
        return ec.ExecutionContext(
            None, ec.MemoryStoreRef(store), "template", template_id, memory_mode, app
        )

    monkeypatch.setattr(ec, "execution_for_store", store_exists)
    gate = SimpleNamespace()
    with pytest.raises(ValueError, match="selected member is unavailable"):
        _GateMixin.resolve_spawn_execution(
            gate,
            parent_session_key="dashboard:old-chat",
            memory_store="private-store",
            _record=None,
            _inherited_selection=("member", "gone"),
        )


def test_prompt_builder_sees_no_member_for_a_pruned_crewmate(config):
    """``member_context_identity`` raised for a member name that is not configured."""
    from kiro_crew.member_essential_context import member_context_identity

    execution = ec.execution_from_record(_record())
    member = execution.member_id or (
        execution.selection_name if execution.selection_kind == "member" else ""
    )
    assert member == ""
    assert member_context_identity(member, member_is_id=False) == ("", "")


def test_run_reader_decodes_through_the_same_seam(config):
    from kiro_crew.subagent_persistence import read_run_execution

    state = dict(_record())
    execution = read_run_execution("run-1", state=state)
    assert execution == replace(execution, selection_kind="template")
    assert execution.selection_kind == "template"


def test_removed_names_come_from_every_prune_marker(tmp_path, monkeypatch):
    from kiro_crew import crewmate_prune_migration as mig

    monkeypatch.setattr(mig, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(mig, "_removed_cache", None)
    assert mig.removed_crewmate_names() == frozenset()
    (tmp_path / "crewmate_prune_v2_migrated.json").write_text('{"removed": ["old"]}')
    (tmp_path / mig.PRUNE_MARKER).write_text('{"removed": ["new", 3, ""], "kept": ["k"]}')
    (tmp_path / "crewmate_prune_migrated.json").write_text("not json")
    assert mig.removed_crewmate_names() == frozenset({"old", "new"})


# ── The first send of a resumed chat ──
#
# Resuming an old chat bound to a pruned crewmate sends through the
# ``created_in_send`` branch of ``api_chat``, which republishes the agent
# selection through ``bind_session_execution``. Its compare-and-set took the
# EXPECTED record from the decoder, which re-reads the pruned shape as its
# template, and compared it with the STORED bytes, which still say ``member``.
# The two never matched, so every such chat refused its first message with
# "session changed during admission"; a resend passed only because the slot
# then existed and the branch was skipped.


def _seed_pruned_chat(key: str, *, memory_mode: str = "persistent") -> dict:
    from kiro_crew.history import ConversationLog

    log = ConversationLog()
    log.append(key, "user", "hello from before the prune", agent="synced-agent")
    log.append(key, "assistant", "hi", agent="synced-agent")
    record = _record()
    record[ec.EXECUTION_CONTEXT_KEY]["memory_mode"] = memory_mode
    log.update_metadata(
        key,
        {
            **record,
            "agent": "synced-agent",
            "memory_store": "default",
            "memory_mode": memory_mode,
        },
    )
    return record[ec.EXECUTION_CONTEXT_KEY]


def _stored_carrier(key: str) -> dict:
    from kiro_crew.history import ConversationLog

    metadata, readable = ConversationLog().get_metadata_status(key)
    assert readable
    return metadata[ec.EXECUTION_CONTEXT_KEY]


@pytest.fixture
def pruned_installed(monkeypatch):
    """The prune removed ``synced-agent``; its agent file is still installed."""
    monkeypatch.setattr(
        "kiro_crew.crewmate_prune_migration.removed_crewmate_names",
        lambda: frozenset({"synced-agent"}),
    )
    monkeypatch.setattr(
        "kiro_crew.config.loader._materialized_kiro_agent",
        lambda name, project_dir=None: name if name in ("synced-agent", "kirocrew") else "",
    )
    cfg = KiroCrewConfig.load()
    assert "synced-agent" not in cfg.agents
    cfg.save()


@pytest.mark.asyncio
async def test_first_send_on_a_resumed_pruned_crewmate_chat_is_admitted(
    tmp_path, monkeypatch, pruned_installed
):
    """POST /api/chat on an old pruned-crewmate chat is admitted the first time."""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from chat_test_helpers import drain_background_tasks
    from dashboard_owner_helpers import as_owner
    from test_chat_agent_selection import _turn_state

    from kiro_crew.dashboard import chat_handlers

    key = "dashboard:old-chat"
    stored = _seed_pruned_chat(key)
    assert ec.read_session_execution(key).selection_kind == "template"
    state = _turn_state(tmp_path, monkeypatch)
    assert "old-chat" not in state._slots
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat", chat_handlers.api_chat)
    async with TestClient(TestServer(as_owner(app))) as client:
        response = await client.post(
            "/api/chat?ws=1", json={"slot": "old-chat", "message": "first send after the prune"}
        )
        body = await response.text()
        assert response.status == 200, body
        assert "session changed during admission" not in body
        await drain_background_tasks(state)
    # The turn reached the provider, and the selection the send recorded was
    # committed by the compare-and-set rather than refused by it.
    state.sessions.get_or_create.assert_awaited()
    published = _stored_carrier(key)
    assert published != stored
    assert published["selection_revision"]


def test_bind_admits_the_pruned_record_it_read(pruned_installed):
    """The CAS compares the decoded record with the decoded record it read."""
    key = "dashboard:old-chat"
    _seed_pruned_chat(key)
    prior = ec.read_session_execution(key)
    assert prior.selection_kind == "template"
    execution = replace(prior, selection_revision="next")
    ec.bind_session_execution(key, execution, replace_existing=True, expected=prior)
    assert _stored_carrier(key) == execution.to_record()


def test_bind_admits_a_restricted_pruned_record(pruned_installed):
    """The privacy-tightening CAS has the same shape and the same answer."""
    key = "dashboard:old-chat"
    _seed_pruned_chat(key)
    prior = ec.read_session_execution(key)
    ec.bind_session_execution(
        key, prior.with_mode("incognito"), replace_existing=True, expected=prior
    )
    assert _stored_carrier(key)["memory_mode"] == "incognito"


@pytest.mark.parametrize(
    "concurrent",
    [
        pytest.param({"selection_revision": "other-writer"}, id="revision"),
        pytest.param({"template_id": "kirocrew", "selection_name": "kirocrew"}, id="template"),
        pytest.param({"selection_kind": "template"}, id="adopted-by-another-writer-then-moved"),
    ],
)
def test_bind_still_refuses_a_real_concurrent_change(pruned_installed, monkeypatch, concurrent):
    """A record another writer changed between the read and the CAS is refused."""
    from kiro_crew.history import ConversationLog

    key = "dashboard:old-chat"
    _seed_pruned_chat(key)
    prior = ec.read_session_execution(key)
    original = ConversationLog.update_metadata_if
    raced = []

    def racing(self, session_key, fields, predicate, *args, **kwargs):
        if not raced:
            raced.append(True)
            carrier = dict(_stored_carrier(session_key))
            carrier.update(concurrent)
            if concurrent == {"selection_kind": "template"}:
                carrier["selection_revision"] = "moved"
            ConversationLog().update_metadata(session_key, {ec.EXECUTION_CONTEXT_KEY: carrier})
        return original(self, session_key, fields, predicate, *args, **kwargs)

    monkeypatch.setattr(ConversationLog, "update_metadata_if", racing)
    with pytest.raises(ValueError, match="session changed during admission"):
        ec.bind_session_execution(
            key, replace(prior, selection_revision="mine"), replace_existing=True, expected=prior
        )
    assert raced
    assert _stored_carrier(key)["selection_revision"] != "mine"


def test_restricted_bind_still_refuses_a_real_concurrent_change(pruned_installed, monkeypatch):
    from kiro_crew.history import ConversationLog

    key = "dashboard:old-chat"
    _seed_pruned_chat(key)
    prior = ec.read_session_execution(key)
    original = ConversationLog.update_metadata_if
    raced = []

    def racing(self, session_key, fields, predicate, *args, **kwargs):
        if not raced:
            raced.append(True)
            carrier = dict(_stored_carrier(session_key), selection_revision="other-writer")
            ConversationLog().update_metadata(session_key, {ec.EXECUTION_CONTEXT_KEY: carrier})
        return original(self, session_key, fields, predicate, *args, **kwargs)

    monkeypatch.setattr(ConversationLog, "update_metadata_if", racing)
    with pytest.raises(ValueError, match="session changed during privacy tightening"):
        ec.bind_session_execution(
            key, prior.with_mode("incognito"), replace_existing=True, expected=prior
        )
    assert raced
    assert _stored_carrier(key)["memory_mode"] == "persistent"
