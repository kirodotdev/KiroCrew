"""Member recall and explicit seed cross the real owner/session/store gates."""

from __future__ import annotations

import json
import os
from datetime import datetime
from types import SimpleNamespace
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from member_memory_helpers import DOCUMENT_CREDENTIAL, document_store
from member_memory_helpers import env as _member_env
from member_memory_helpers import request, seed_body

from kiro_crew import hooks, mcp_core, memory_schema, memory_stores
from kiro_crew.config import loader
from kiro_crew.dashboard.handlers import (
    cron,
    memory,
    memory_admin,
    memory_edit,
    memory_member,
    taskrunner,
)
from kiro_crew.mcp_tools import learn
from kiro_crew.memory import MemoryStore
from kiro_crew.vector_memory import VectorMemoryStore, open_member_database

pytestmark = pytest.mark.xdist_group("member_memory_api")


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["", "bob"])
async def test_spawn_inherits_member_or_uses_explicit_target(env, target):
    from kiro_crew.dashboard.handlers import messaging

    cfg = loader.KiroCrewConfig.load()
    cfg.agents["bob"].triggers = "review"
    cfg.save()
    env.state.subagents = SimpleNamespace(
        spawn=mock.Mock(return_value=SimpleNamespace(id="run-1", done=False))
    )
    response = await messaging.api_spawn(
        request(
            env,
            body={
                "task": "read your memory",
                "target_member": target,
                "parent_session": "dashboard:alice",
            },
            internal=True,
        )
    )
    assert response.status == 200
    admitted = env.state.subagents.spawn.call_args.kwargs["_execution_context"]
    assert admitted["member_id"] == (target or "alice")
    assert (
        env.state.subagents.spawn.call_args.kwargs["memory_store"] == f"member-{target or 'alice'}"
    )
    assert env.tiers["member-bob"].get_all_semantic() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "identity, parent_session",
    [("dashboard:alice", "dashboard:owner"), (None, "dashboard:alice")],
    ids=["private_forges_global_parent", "global_borrows_private_parent"],
)
async def test_internal_spawn_parent_must_match_caller_identity(
    env, monkeypatch, identity, parent_session
):
    from kiro_crew.dashboard.handlers import messaging

    env.state.subagents = SimpleNamespace(spawn=mock.Mock())
    response = await messaging.api_spawn(
        request(
            env,
            body={"task": "spawn", "parent_session": parent_session},
            internal=True,
            session=identity or "",
        )
    )
    assert response.status == 409
    assert json.loads(response.text)["code"] == "member_identity_unavailable"
    env.state.subagents.spawn.assert_not_called()


@pytest.mark.asyncio
async def test_private_taskrunner_start_forwards_protected_origin(env):
    from kiro_crew.execution_context import read_session_execution

    runner = SimpleNamespace(
        _work_dir=env.home / "tasks",
        _ctx=env.state.context_builder,
        start_background=mock.AsyncMock(return_value="run-1"),
        _capture_execution=read_session_execution,
    )
    env.state.task_runner = runner
    response = await taskrunner.api_taskrunner_start(
        request(
            env,
            body={"spec": "__inline__:# Task\nDo the work"},
            internal=True,
        )
    )
    assert response.status == 200
    assert runner.start_background.await_args.kwargs["session_key"] == "dashboard:alice"
    execution = runner.start_background.await_args.kwargs["execution_context"]
    assert execution.member_id == "alice"
    assert execution.store.legacy_name == "member-alice"


@pytest.mark.asyncio
async def test_taskrunner_status_uses_ordinary_internal_transport(env):
    for key, store in (
        ("taskrunner:alice-run:runtime", "member-alice"),
        ("taskrunner:bob-run:runtime", "member-bob"),
    ):
        env.bind_session(key, store)
    runner = SimpleNamespace(
        status=lambda: {
            "runs": [
                {"task_id": "alice-run", "source": "dashboard"},
                {"task_id": "bob-run", "source": "dashboard"},
                {"task_id": "global-run", "source": "dashboard"},
            ]
        },
        _workspace_dir=None,
        _work_dir=env.home / "tasks",
    )
    env.state.task_runner = runner
    response = await taskrunner.api_taskrunner_status(request(env, internal=True))
    assert response.status == 200
    assert [row["task_id"] for row in json.loads(response.text)["runs"]] == [
        "alice-run",
        "bob-run",
        "global-run",
    ]


@pytest.mark.asyncio
async def test_named_v1_continuation_retains_its_parent_store(env, monkeypatch):
    from kiro_crew.context import inherit_session_memory

    cfg = loader.KiroCrewConfig.load()
    cfg.memory_stores["legacy-team"] = loader.MemoryStoreConfig(memory_version=1)
    cfg.save()
    (env.home / "memory_stores" / "legacy-team").mkdir()
    memory_stores._DECLARED_MEMO = None
    env.metadata["dashboard:legacy"] = {"memory_store": "legacy-team"}
    env.state.conversation_log.update_metadata = lambda key, fields: env.metadata.setdefault(
        key, {}
    ).update(fields)
    monkeypatch.setattr("kiro_crew.context.prepare_store_vectors", mock.AsyncMock())

    inherited = await inherit_session_memory(
        env.state.context_builder, "dashboard:legacy", "taskrunner:legacy-child"
    )

    assert inherited == "legacy-team"
    from kiro_crew.execution_context import read_session_execution

    assert read_session_execution("taskrunner:legacy-child").store.legacy_name == "legacy-team"


def test_schedule_freezes_inherited_or_explicit_member_and_rejects_missing_identity(env):
    from kiro_crew.cron import CronJob, bind_cron_memory
    from kiro_crew.history import ConversationLog

    own = CronJob(id="own", name="own", message="work", session_key="dashboard:alice")
    bind_cron_memory(own)
    assert (own.member_id, own.memory_store) == ("alice", "member-alice")
    peer = CronJob(
        id="peer", name="peer", message="work", session_key="dashboard:alice", member_id="bob"
    )
    bind_cron_memory(peer)
    assert peer.execution_context["member_id"] == "bob"
    ConversationLog().update_metadata("slack:forged", {"memory_store": "member-bob"})
    forged = CronJob(id="forged", name="forged", message="work", session_key="slack:forged")
    with pytest.raises(ValueError, match="canonical member identity"):
        bind_cron_memory(forged)


# Re-export the shared synthetic fixture for API integration tests.
env = _member_env


@pytest.mark.asyncio
@pytest.mark.parametrize("store", ["", "legacy-team", "member-alice"])
async def test_memory_events_redact_content_without_mutating_the_audit_store(env, store):
    if store == "legacy-team":
        cfg = loader.KiroCrewConfig.load()
        cfg.memory_stores[store] = loader.MemoryStoreConfig(memory_version=1)
        cfg.save()
        directory = env.home / "memory_stores" / store
        directory.mkdir()
        tier = VectorMemoryStore(db_path=directory / "memory.db")
        tier.init()
        env.tiers[store] = tier
        memory_stores._DECLARED_MEMO = None
    else:
        tier = env.tiers[store]

    credential = DOCUMENT_CREDENTIAL
    exfiltration_url = "https://evil.example.com/steal?data=" + "A" * 250
    assert tier.set_semantic("project.audit", credential, 1.0, "user_explicit") is None
    assert tier.set_semantic("project.audit", exfiltration_url, 1.0, "user_explicit") is None
    stored_events = tier.get_events()
    stored_update = next(event for event in stored_events if event["event_type"] == "update")
    assert credential in stored_update["old_value"]
    assert exfiltration_url in stored_update["new_value"]

    query = {"store": store} if store else None
    response = await memory.api_memory_events(
        request(env, query=query, owner=True, session="dashboard:ui")
    )
    returned_events = json.loads(response.text)["events"]

    assert response.status == 200
    surfaced = json.dumps(returned_events)
    assert credential not in surfaced
    assert exfiltration_url not in surfaced
    assert "[REDACTED" in surfaced
    identity_fields = ("id", "event_type", "memory_type", "memory_key")
    assert [tuple(event[field] for field in identity_fields) for event in returned_events] == [
        tuple(event[field] for field in identity_fields) for event in stored_events
    ]
    assert tier.get_events() == stored_events


@pytest.mark.asyncio
@pytest.mark.parametrize("store", ["", "member-alice"])
@pytest.mark.parametrize(
    "document,writer,reader",
    [
        ("preferences", "write_preferences", "read_preferences"),
        ("projects", "write_projects", "read_projects"),
        ("history", "append_history", "read_recent_history"),
    ],
)
async def test_sensitive_memory_document_is_redacted_and_preserved_on_put_refusal(
    env, store, document, writer, reader
):
    memory_store = await document_store(env, store)
    raw = f"retain {DOCUMENT_CREDENTIAL} exactly"
    getattr(memory_store, writer)(raw)
    before = getattr(memory_store, reader)()
    query = {"store": store} if store else None

    read_response = await getattr(memory, f"api_memory_{document}")(
        request(env, query=query, owner=True, session="dashboard:ui")
    )
    read_body = json.loads(read_response.text)
    assert read_response.status == 200
    assert read_body["content_redacted"] is True
    assert DOCUMENT_CREDENTIAL not in read_body["content"]

    write_request = request(
        env,
        body={"content": "an unrelated owner edit"},
        query=query,
        owner=True,
        session="dashboard:ui",
    ).clone(method="PUT")
    write_response = await getattr(memory, f"api_memory_{document}")(write_request)

    assert write_response.status == 409
    assert json.loads(write_response.text)["code"] == "memory_document_redacted"
    assert getattr(memory_store, reader)() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("store", ["", "member-alice"])
@pytest.mark.parametrize("document", ["preferences", "projects", "history"])
async def test_clean_memory_document_remains_editable(env, monkeypatch, store, document):
    # The essential-context validator has its own real-store tests. This matrix
    # isolates clean/redacted document admission across the V1 and V2 writers.
    monkeypatch.setattr(memory, "_validate_private_profile_update", lambda *_args: None)
    await document_store(env, store)
    query = {"store": store} if store else None
    replacement = f"clean {document} replacement"
    write_request = request(
        env,
        body={"content": replacement},
        query=query,
        owner=True,
        session="dashboard:ui",
    ).clone(method="PUT")

    write_response = await getattr(memory, f"api_memory_{document}")(write_request)
    read_response = await getattr(memory, f"api_memory_{document}")(
        request(env, query=query, owner=True, session="dashboard:ui")
    )

    assert write_response.status == 200
    body = json.loads(read_response.text)
    assert body["content_redacted"] is False
    assert replacement in body["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("document", ["preferences", "projects"])
async def test_profile_put_refuses_store_removed_during_final_validation(
    env, monkeypatch, document
):
    memory_store = await document_store(env, "member-alice")
    target = getattr(memory_store, f"_{document}_file")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("Original manual rules", encoding="utf-8")
    before = target.read_bytes()
    validate = memory._validate_private_profile_update

    def remove_store_then_validate(state, store, filename, content):
        cfg = loader.KiroCrewConfig.load()
        del cfg.memory_stores[store]
        del cfg.agents["alice"]
        cfg.save()
        return validate(state, store, filename, content)

    validation = mock.Mock(side_effect=remove_store_then_validate)
    monkeypatch.setattr(memory, "_validate_private_profile_update", validation)
    build_essentials = mock.Mock()
    env.state.context_builder._build_v2_essentials = build_essentials
    write = mock.Mock(wraps=memory_store._atomic_write_text)
    index = mock.Mock(wraps=memory_store._index_file)
    monkeypatch.setattr(memory_store, "_atomic_write_text", write)
    monkeypatch.setattr(memory_store, "_index_file", index)

    response = await getattr(memory, f"api_memory_{document}")(
        request(
            env,
            body={"content": "replacement after concurrent member removal"},
            query={"store": "member-alice"},
            owner=True,
            session="dashboard:ui",
        ).clone(method="PUT")
    )

    validation.assert_called_once()
    assert validation.call_args.args[1:3] == ("member-alice", f"{document}.md")
    assert response.status == 503
    assert json.loads(response.text)["code"] == "store_unavailable"
    assert target.read_bytes() == before
    write.assert_not_called()
    index.assert_not_called()
    build_essentials.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("store", [""])
@pytest.mark.parametrize("malformed", ["invalid_utf8", "oversized"])
async def test_history_put_preserves_an_unreadable_today_file(env, monkeypatch, store, malformed):
    memory_store = await document_store(env, store)
    today = memory_store._today_history_file()
    if malformed == "invalid_utf8":
        original = b"history before corruption \xff"
    else:
        monkeypatch.setattr(hooks, "MAX_FILE_BYTES", 128)
        monkeypatch.setattr(MemoryStore, "_HISTORY_SNAPSHOT_MAX_BYTES", 128)
        original = b"x" * 129
    today.write_bytes(original)
    query = {"store": store} if store else None

    response = await memory.api_memory_history(
        request(
            env,
            body={"content": "replacement"},
            query=query,
            owner=True,
            session="dashboard:ui",
        ).clone(method="PUT")
    )

    assert response.status == 503
    assert json.loads(response.text)["code"] == "store_unavailable"
    assert today.read_bytes() == original


@pytest.mark.asyncio
async def test_member_history_put_preserves_redacted_sqlite_authority(env):
    memory_store = await document_store(env, "member-alice")
    raw = f"retain {DOCUMENT_CREDENTIAL} exactly"
    memory_store.append_history(raw)
    baseline = memory_store.read_editable_history()

    response = await memory.api_memory_history(
        request(
            env,
            body={"content": "replacement"},
            query={"store": "member-alice"},
            owner=True,
            session="dashboard:ui",
        ).clone(method="PUT")
    )

    assert response.status == 409
    assert json.loads(response.text)["code"] == "memory_document_redacted"
    assert memory_store.read_editable_history() == baseline
    assert raw in baseline
    assert not memory_store._today_history_file().exists()


@pytest.mark.asyncio
async def test_unsigned_transcript_cannot_claim_or_mint_private_memory(env, monkeypatch):
    from kiro_crew import member_memory_auth
    from kiro_crew.context import prepare_store_vectors, store_of_session

    forged_key = "slack:forged-private-assignment"
    env.metadata[forged_key] = {"memory_store": "member-bob"}
    with pytest.raises(memory_stores.UnknownMemoryStore, match="execution identity is missing"):
        store_of_session(env.state.conversation_log, forged_key)
    with pytest.raises(
        memory_stores.UnknownMemoryStore, match="canonical member execution identity"
    ):
        await prepare_store_vectors(env.state.context_builder, "member-bob", session_key=forged_key)
    assert member_memory_auth.read_private_session_store(forged_key) is None
    assert store_of_session(env.state.conversation_log, "dashboard:alice") == "member-alice"


@pytest.mark.asyncio
async def test_legacy_import_honors_selected_member_store(env):
    response = await memory.api_memory_import(
        request(
            env,
            owner=True,
            session="dashboard:ui",
            query={"store": "member-alice"},
            body={
                "semantic": [
                    {
                        "key": "user.selected_import",
                        "value": "Alice only",
                        "source": "user_explicit",
                        "confidence": 1.0,
                    }
                ]
            },
        )
    )
    assert response.status == 200
    assert json.loads(response.text)["semantic"] == 1
    assert env.tiers["member-alice"].get_semantic("user.selected_import") is not None
    assert env.tiers[""].get_semantic("user.selected_import") is None
    assert env.tiers["member-bob"].get_semantic("user.selected_import") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "handler", [memory.api_memory_context_preview, memory.api_memory_observability]
)
async def test_legacy_preview_routes_read_only_the_selected_member(env, handler):
    env.tiers[""].set_semantic(
        "pref.scope_test", "Global confidential decision", 1.0, "user_explicit"
    )
    env.tiers["member-alice"].set_semantic(
        "pref.scope_test", "Alice confidential decision", 1.0, "user_explicit"
    )
    response = await handler(
        request(
            env,
            owner=True,
            session="dashboard:ui",
            query={"store": "member-alice", "q": "confidential decision"},
        )
    )
    assert response.status == 200
    assert "Alice confidential decision" in response.text
    assert "Global confidential decision" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("handler", [memory.api_memory_migrate, memory.api_memory_promote])
async def test_legacy_transformations_refuse_private_store_without_touching_global(env, handler):
    env.tiers[""].set_semantic("user.scope_test", "Global remains", 1.0, "user_explicit")
    before = env.tiers[""].get_semantic("user.scope_test")
    response = await handler(
        request(env, owner=True, session="dashboard:ui", query={"store": "member-alice"}, body={})
    )
    assert response.status == 400
    assert env.tiers[""].get_semantic("user.scope_test") == before
    assert env.tiers["member-alice"].get_semantic("user.scope_test") is None


@pytest.mark.asyncio
async def test_owner_can_stage_restore_of_lost_member_directory_and_see_pending_refusal(env):
    from kiro_crew import memory_backup

    tier = env.tiers["member-alice"]
    tier.set_semantic("project.database", "PostgreSQL", 1.0, "user_explicit")
    directory = env.home / "memory_stores" / "member-alice"
    backup = memory_backup.backup_store(directory / "memory.db")
    tier.close()
    directory.rename(env.home / "lost-alice")
    listing = await memory_admin.api_memory_backups(
        request(env, query={"store": "member-alice"}, owner=True)
    )
    assert listing.status == 200
    assert json.loads(listing.text)["backups"][0]["name"] == backup.name
    body = {"store": "member-alice", "name": backup.name}
    response = await memory_admin.api_memory_restore(request(env, body=body, owner=True))
    assert response.status == 200
    assert json.loads(response.text)["pending"] is True
    assert json.loads(response.text)["restart_required"] is True
    assert not directory.exists()
    refused = await memory_admin.api_memory_restore(request(env, body=body, owner=True))
    assert refused.status == 409
    assert json.loads(refused.text)["code"] == "restore_refused"
    assert "already pending" in json.loads(refused.text)["error"]
    with pytest.raises(memory_stores.UnknownMemoryStore):
        memory_stores.require_memory_store("member-alice")
    assert memory_backup.apply_pending_member_restores() == {"member-alice": ""}
    restored = open_member_database(
        directory / "memory.db", member_id="alice", store_id="member-alice"
    )
    env.tiers["member-alice"] = restored
    assert json.loads(restored.get_semantic("project.database")["value_json"]) == "PostgreSQL"


@pytest.mark.asyncio
async def test_selected_owner_seed_is_traceable_idempotent_and_independent(env):
    global_store = env.tiers[""]
    global_store.set_semantic("project.database", "PostgreSQL", 1.0, "user_explicit")
    global_store.set_semantic("user.private", "Do not copy this detail", 1.0, "user_explicit")
    body = seed_body({"kind": "fact", "id": "project.database"})
    response = await memory_member.api_memory_seed(request(env, body=body, owner=True))
    assert response.status == 200
    assert json.loads(response.text)["results"][0]["outcome"] == "imported"
    alice = env.tiers["member-alice"]
    assert alice.get_semantic("user.private") is None
    assert env.tiers["member-bob"].get_all_semantic() == []
    row = alice.list_by_facets(kind="fact")[0]
    assert json.loads(row["derived_from"])["store"] == "default"
    response = await memory_member.api_memory_seed(request(env, body=body, owner=True))
    assert json.loads(response.text)["results"][0]["outcome"] == "existing"
    alice.delete_semantic("project.database", "user_explicit")
    assert global_store.get_semantic("project.database") is not None


@pytest.mark.asyncio
async def test_paginated_lists_keep_fact_and_episode_copy_provenance_after_reopen(env):
    source = env.tiers["member-bob"]
    source.set_semantic("project.database", "PostgreSQL", 1.0, "user_explicit")
    assert source.write_episodic(
        "Reviewed PostgreSQL database migration",
        conversation_id="database design",
        tags=["database"],
        importance=0.8,
        defer_embedding=True,
    )
    episode = source.get_episodic_list()[0]["id"]
    selections = [
        {"kind": "fact", "id": "project.database"},
        {"kind": "episode", "id": episode},
    ]
    response = await memory_member.api_memory_seed(
        request(env, body=seed_body(*selections, source="member-bob"), owner=True)
    )
    assert response.status == 200
    assert all(item["outcome"] == "imported" for item in json.loads(response.text)["results"])
    old_tier = env.tiers["member-alice"]
    old_tier.close()
    tier = open_member_database(
        env.home / "memory_stores" / "member-alice" / "memory.db",
        member_id="alice",
        store_id="member-alice",
    )
    env.tiers["member-alice"] = tier
    for handler, expected in (
        (memory.api_memory_semantic, selections[0]),
        (memory.api_memory_episodic_list, selections[1]),
    ):
        response = await handler(
            request(env, query={"store": "member-alice", "limit": "1", "offset": "0"}, owner=True)
        )
        assert response.status == 200
        entries = json.loads(response.text)["entries"]
        assert len(entries) == 1
        assert entries[0]["source"] == "user_seed"
        lineage = json.loads(entries[0]["derived_from"])
        assert lineage["store"] == "member-bob"
        assert lineage["item_id"] == expected["id"]
        assert lineage["kind"] == expected["kind"]
        assert lineage["copied_at"]
        response = await handler(
            request(env, query={"store": "member-alice", "limit": "1", "offset": "1"}, owner=True)
        )
        assert json.loads(response.text)["entries"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("internal", [False, True])
async def test_seed_cannot_be_authorized_by_agent_or_header(env, internal):
    env.tiers[""].set_semantic("project.database", "PostgreSQL", 1.0, "user_explicit")
    response = await memory_member.api_memory_seed(
        request(env, body=seed_body({"kind": "fact", "id": "project.database"}), internal=internal)
    )
    assert response.status == 403, response.text
    assert env.tiers["member-alice"].get_all_semantic() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source,target",
    [
        ("absent", "member-alice"),
        ("default", "absent"),
        ("member-bob", "default"),
        ("member-alice", "member-alice"),
    ],
)
async def test_invalid_source_or_destination_never_writes(env, source, target):
    response = await memory_member.api_memory_seed(
        request(
            env,
            body=seed_body(
                {"kind": "fact", "id": "project.database"}, source=source, target=target
            ),
            owner=True,
        )
    )
    assert response.status in {400, 404, 409}
    assert all(t.get_all_semantic() == [] for t in env.tiers.values())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "last",
    [
        {"kind": "fact", "id": "missing"},
        {"kind": "directive", "id": "project.database"},
        {"kind": "fact", "id": "project.database"},
    ],
)
async def test_stale_or_invalid_selection_is_checked_before_any_copy(env, last):
    env.tiers[""].set_semantic("project.database", "PostgreSQL", 1.0, "user_explicit")
    response = await memory_member.api_memory_seed(
        request(env, body=seed_body({"kind": "fact", "id": "project.database"}, last), owner=True)
    )
    assert response.status in {400, 409}
    assert env.tiers["member-alice"].get_all_semantic() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [[], {}])
async def test_unhashable_seed_kind_is_a_validation_error_without_writes(env, kind):
    env.tiers[""].set_semantic("project.database", "PostgreSQL", 1.0, "user_explicit")
    response = await memory_member.api_memory_seed(
        request(env, body=seed_body({"kind": kind, "id": "project.database"}), owner=True)
    )
    assert response.status == 400
    assert json.loads(response.text)["code"] == "invalid_seed_items"
    assert env.tiers["member-alice"].get_all_semantic() == []
    assert env.tiers["member-bob"].get_all_semantic() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("commit_before_error", [False, True])
async def test_seed_returns_truthful_partial_results_if_a_later_copy_fails(
    env, monkeypatch, commit_before_error
):
    source, destination = env.tiers[""], env.tiers["member-alice"]
    keys = ["project.database", "project.language", "project.region"]
    for key, value in zip(keys, ["PostgreSQL", "Python", "us-east"]):
        source.set_semantic(key, value, 1.0, "user_explicit")
    original = destination.seed_item_if_absent

    def fail_second(item, **kwargs):
        if kwargs["source_id"] == keys[1]:
            if commit_before_error:
                original(item, **kwargs)
            raise OSError("disk unavailable")
        return original(item, **kwargs)

    monkeypatch.setattr(destination, "seed_item_if_absent", fail_second)
    response = await memory_member.api_memory_seed(
        request(env, body=seed_body(*[{"kind": "fact", "id": key} for key in keys]), owner=True)
    )
    assert response.status == 200
    data = json.loads(response.text)
    assert data["partial"] is True
    assert [item["outcome"] for item in data["results"]] == [
        "imported",
        "unconfirmed",
        "not_attempted",
    ]
    assert [item["source_id"] for item in data["results"]] == keys
    assert "disk unavailable" in data["results"][1]["reason"]
    assert destination.get_semantic(keys[0]) is not None
    assert bool(destination.get_semantic(keys[1])) is commit_before_error
    assert destination.get_semantic(keys[2]) is None


@pytest.mark.asyncio
async def test_failed_copy_provenance_cannot_be_committed_by_a_later_write(env, monkeypatch):
    from kiro_crew import memory_schema

    source, destination = env.tiers[""], env.tiers["member-alice"]
    source.set_semantic("project.database", "PostgreSQL", 1.0, "user_explicit")

    def failed_stamp(*args):
        raise ValueError("cannot stamp source")

    monkeypatch.setattr(memory_schema, "facet_stamp_params", failed_stamp)
    response = await memory_member.api_memory_seed(
        request(env, body=seed_body({"kind": "fact", "id": "project.database"}), owner=True)
    )
    assert json.loads(response.text)["results"][0]["outcome"] == "unconfirmed"
    destination.set_semantic("project.language", "Python", 1.0, "user_explicit")
    assert destination.get_semantic("project.database") is None
    assert destination.get_semantic("project.language") is not None


@pytest.mark.asyncio
async def test_internal_recall_uses_recorded_member_and_returns_bounded_evidence(env):
    for name, marker in (
        ("", "GLOBALSECRET"),
        ("member-bob", "BOBSECRET"),
        ("member-alice", "ALICEFACT"),
    ):
        env.tiers[name].set_semantic(
            "project.database", f"PostgreSQL {marker}", 1.0, "user_explicit"
        )
    response = await memory_member.api_memory_recall(
        request(env, query={"q": "PostgreSQL database"}, internal=True)
    )
    assert response.status == 200
    result = json.loads(response.text)
    assert result["store"] == "member-alice"
    assert result["algorithm_version"] == "v2"
    assert result["total_chars"] <= 3000
    assert "ALICEFACT" in response.text
    assert "GLOBALSECRET" not in response.text and "BOBSECRET" not in response.text
    assert result["retrieval"]["facts"][0]["retrieval"]["reason"] == "keyword_match"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query,owner,internal",
    [
        ({"q": "database"}, False, False),
        ({"q": "database", "store": "member-bob"}, False, True),
        ({"q": "database", "store": "default"}, False, True),
    ],
)
async def test_caller_cannot_choose_another_members_recall(env, query, owner, internal):
    response = await memory_member.api_memory_recall(
        request(env, query=query, owner=owner, internal=internal)
    )
    assert response.status == 403


@pytest.mark.asyncio
async def test_owner_can_preview_a_selected_private_store(env):
    env.tiers["member-bob"].set_semantic("project.database", "PostgreSQL", 1.0, "user_explicit")
    response = await memory_member.api_memory_recall(
        request(env, query={"q": "database", "store": "member-bob"}, owner=True)
    )
    assert response.status == 200
    assert json.loads(response.text)["store"] == "member-bob"


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", [True, False])
async def test_global_v1_recall_uses_the_same_explicit_tool_route(env, monkeypatch, owner):
    from kiro_crew import member_memory_auth

    await document_store(env, "")
    env.tiers[""].set_semantic("project.database", "PostgreSQL V1FACT", 1.0, "user_explicit")
    env.tiers["member-alice"].set_semantic(
        "project.database", "PostgreSQL ALICEFACT", 1.0, "user_explicit"
    )
    env.state._slots["ui"] = SimpleNamespace(is_restricted=False, blocks_reads=False)
    env.metadata["dashboard:ui"] = {"memory_store": "default"}
    # A real host process without a protected member record is the legitimate
    # legacy caller. The authority check still runs when private stores exist.
    monkeypatch.setattr(member_memory_auth, "_request_peer_pid", lambda request: os.getpid())
    response = await memory_member.api_memory_recall(
        request(
            env,
            query={"q": "PostgreSQL database"},
            owner=owner,
            internal=not owner,
            session="dashboard:ui",
        )
    )
    assert response.status == 200
    result = json.loads(response.text)
    assert result["algorithm_version"] == "v1"
    assert result["store"] == ""
    assert "V1FACT" in response.text and "ALICEFACT" not in response.text
    assert result["total_chars"] <= 3000


@pytest.mark.asyncio
async def test_temporary_session_cannot_recall(env):
    env.state._slots["alice"].blocks_reads = True
    response = await memory_member.api_memory_recall(
        request(env, query={"q": "database"}, internal=True)
    )
    assert response.status == 403
    assert json.loads(response.text)["code"] == "memory_reads_disabled"


@pytest.mark.asyncio
async def test_unknown_recorded_binding_fails_without_reading_global(env):
    cfg = loader.KiroCrewConfig.load()
    del cfg.memory_stores["member-alice"]
    cfg.save()
    env.tiers[""].set_semantic("project.database", "GLOBALSECRET", 1.0, "user_explicit")
    response = await memory_member.api_memory_recall(
        request(env, query={"q": "database"}, internal=True)
    )
    assert response.status == 503
    assert "GLOBALSECRET" not in response.text


@pytest.mark.asyncio
async def test_database_read_failure_returns_explicit_unavailability(env, monkeypatch):
    def unreadable(*args, **kwargs):
        raise OSError("disk unreadable")

    monkeypatch.setattr(env.tiers["member-alice"], "recall", unreadable)
    response = await memory_member.api_memory_recall(
        request(env, query={"q": "database"}, internal=True)
    )
    assert response.status == 503
    assert json.loads(response.text)["code"] == "store_unavailable"


def test_mcp_recall_forwards_strict_identity_and_encoded_query(monkeypatch):
    monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "subagent:alice-run")
    get = mock.Mock(return_value={"store": "member-alice", "semantic_context": "known fact"})
    monkeypatch.setattr(mcp_core, "_get", get)
    result = json.loads(learn.memory_recall("memory_recall", {"query": "数据库 & PostgreSQL?"}))
    assert result["store"] == "member-alice"
    path = get.call_args.args[0]
    assert parse_qs(urlsplit(path).query) == {"q": ["数据库 & PostgreSQL?"]}
    assert get.call_args.kwargs == {"session_key": "subagent:alice-run"}


@pytest.mark.parametrize("query", [None, "", " ", "x" * 2001])
def test_invalid_mcp_recall_never_calls_gateway(monkeypatch, query):
    get = mock.Mock()
    monkeypatch.setattr(mcp_core, "_get", get)
    assert learn.memory_recall("memory_recall", {"query": query}).startswith("Error:")
    get.assert_not_called()


def test_unresolved_mcp_identity_never_falls_back_to_default_session(monkeypatch):
    monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "")
    get = mock.Mock()
    monkeypatch.setattr(mcp_core, "_get", get)
    assert "established session" in learn.memory_recall("memory_recall", {"query": "database"})
    get.assert_not_called()


async def episode_edit_body(env, store, mem_id, operation):
    response = await memory_edit.api_memory_records(
        request(env, query={"store": store, "kind": "episode"}, owner=True)
    )
    assert response.status == 200
    record = next(row for row in json.loads(response.text)["entries"] if row["id"] == mem_id)
    return {
        "store": store,
        "selection": {"items": [{key: record[key] for key in ("kind", "id", "revision")}]},
        "operation": operation,
    }


@pytest.mark.asyncio
async def test_episode_bulk_correction_preserves_identity_provenance_and_retry(env):
    tier = env.tiers["member-alice"]
    tier.write_episodic(
        "Postgres listens on port 5432 locally",
        tags=["database"],
        importance=0.9,
        defer_embedding=True,
        facets=memory_schema.MemoryFacets(derived_from="explicit-source", surface="owner_seed"),
    )
    row = tier.get_episodic_list()[0]
    body = await episode_edit_body(
        env,
        "member-alice",
        row["id"],
        {"type": "set", "text": "Postgres listens on port 6432 locally"},
    )
    preview = await memory_edit.api_memory_bulk_preview(request(env, body=body, owner=True))
    assert preview.status == 200 and json.loads(preview.text)["changed_count"] == 1
    assert tier.get_episodic_list()[0] == row
    apply_body = {"store": "member-alice", "preview_id": json.loads(preview.text)["preview_id"]}
    response = await memory_edit.api_memory_bulk_apply(request(env, body=apply_body, owner=True))
    assert response.status == 200 and json.loads(response.text)["changed_count"] == 1
    updated = tier.get_episodic_list()[0]
    assert updated["id"] == row["id"] and updated["created_at"] == row["created_at"]
    assert updated["derived_from"] == "explicit-source" and updated["source"] == "user_explicit"
    assert updated["text"] == body["operation"]["text"]
    assert updated["tags"] == row["tags"] and updated["importance"] == row["importance"]
    events = tier.get_events()
    assert sum(event["event_type"] == "correct" for event in events) == 1
    retry = await memory_edit.api_memory_bulk_apply(request(env, body=apply_body, owner=True))
    assert retry.status == 200 and json.loads(retry.text) == json.loads(response.text)
    assert tier.get_episodic_list()[0] == updated and tier.get_events() == events
    foreign = await memory_edit.api_memory_bulk_apply(
        request(env, body={**apply_body, "store": "member-bob"}, owner=True)
    )
    assert foreign.status == 400 and json.loads(foreign.text)["code"] == "invalid_memory_preview"
    assert env.tiers["member-bob"].get_episodic_list() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change,status",
    [
        ({"text": "short"}, 422),
        ({"text": []}, 422),
        ({"tags": [1]}, 400),
        ({"tags": "tag"}, 400),
        ({"importance": True}, 400),
        ({"text": float("nan")}, 400),
    ],
)
async def test_episode_bulk_correction_invalid_input_never_mutates(env, change, status):
    tier = env.tiers["member-alice"]
    tier.write_episodic("Postgres listens on port 5432 locally", defer_embedding=True)
    row = tier.get_episodic_list()[0]
    events = tier.get_events()
    body = await episode_edit_body(
        env,
        "member-alice",
        row["id"],
        {"type": "set", "text": "A valid corrected episode", **change},
    )
    response = await memory_edit.api_memory_bulk_preview(request(env, body=body, owner=True))
    assert response.status == status
    assert tier.get_episodic_list()[0] == row and tier.get_events() == events


@pytest.mark.asyncio
@pytest.mark.parametrize("store", ["default", "member-alice"])
async def test_episode_bulk_correction_owner_gate_and_stale_selection(env, store):
    tier = env.tiers["" if store == "default" else store]
    tier.write_episodic("Postgres listens on port 5432 locally", defer_embedding=True)
    row = tier.get_episodic_list()[0]
    body = await episode_edit_body(
        env, store, row["id"], {"type": "set", "text": "Postgres listens on port 6432 locally"}
    )
    denied = await memory_edit.api_memory_bulk_preview(request(env, body=body, internal=True))
    assert denied.status == 403 and tier.get_episodic_list()[0] == row
    preview = await memory_edit.api_memory_bulk_preview(request(env, body=body, owner=True))
    assert preview.status == 200
    apply_body = {"store": store, "preview_id": json.loads(preview.text)["preview_id"]}
    denied = await memory_edit.api_memory_bulk_apply(request(env, body=apply_body, internal=True))
    assert denied.status == 403 and tier.get_episodic_list()[0] == row
    applied = await memory_edit.api_memory_bulk_apply(request(env, body=apply_body, owner=True))
    assert applied.status == 200 and json.loads(applied.text)["changed_count"] == 1
    stale = await memory_edit.api_memory_bulk_preview(request(env, body=body, owner=True))
    assert stale.status == 409 and json.loads(stale.text)["code"] == "stale_memory_preview"
    assert tier.get_episodic_list()[0]["text"] == body["operation"]["text"]


@pytest.mark.asyncio
async def test_pending_restore_status_survives_refresh_and_owner_can_cancel(env):
    from kiro_crew import memory_backup

    tier = env.tiers["member-alice"]
    tier.set_semantic("project.database", "PostgreSQL", 1.0, "user_explicit")
    path = env.home / "memory_stores" / "member-alice" / "memory.db"
    backup = memory_backup.backup_store(path)
    tier.set_semantic("project.database", "SQLite", 1.0, "user_explicit")
    response = await memory_admin.api_memory_restore(
        request(env, body={"store": "member-alice", "name": backup.name}, owner=True)
    )
    assert response.status == 200
    # Staging is deferred to the next start: the live tier keeps its post-backup value.
    assert json.loads(tier.get_semantic("project.database")["value_json"]) == "SQLite"
    listing = await memory_admin.api_memory_backups(
        request(env, query={"store": "member-alice"}, owner=True)
    )
    status = json.loads(listing.text)
    assert status["pending"] and status["pending_restore"]["backup_name"] == backup.name
    denied = await memory_admin.api_memory_restore_cancel(
        request(env, body={"store": "member-alice"}, internal=True)
    )
    assert denied.status == 403
    result = await memory_admin.api_memory_restore_cancel(
        request(env, body={"store": "member-alice"}, owner=True)
    )
    assert result.status == 200 and json.loads(result.text)["cancelled"] is True
    listing = await memory_admin.api_memory_backups(
        request(env, query={"store": "member-alice"}, owner=True)
    )
    assert json.loads(listing.text)["pending"] is False
    assert json.loads(listing.text)["pending_restore"] is None
    assert backup.exists()


@pytest.mark.asyncio
async def test_episode_copy_retry_after_correction_and_forgetting_does_not_resurrect(env):
    source = env.tiers[""]
    source.write_episodic("Postgres listens on port 5432 locally", defer_embedding=True)
    source_id = source.get_episodic_list()[0]["id"]
    body = seed_body({"kind": "episode", "id": source_id})
    first = await memory_member.api_memory_seed(request(env, body=body, owner=True))
    assert first.status == 200
    target = env.tiers["member-alice"]
    copied_id = target.get_episodic_list()[0]["id"]
    edit_body = await episode_edit_body(
        env,
        "member-alice",
        copied_id,
        {"type": "set", "text": "Postgres listens on port 6432 locally"},
    )
    preview = await memory_edit.api_memory_bulk_preview(request(env, body=edit_body, owner=True))
    assert preview.status == 200
    applied = await memory_edit.api_memory_bulk_apply(
        request(
            env,
            body={"store": "member-alice", "preview_id": json.loads(preview.text)["preview_id"]},
            owner=True,
        )
    )
    assert applied.status == 200 and json.loads(applied.text)["changed_count"] == 1
    retry = await memory_member.api_memory_seed(request(env, body=body, owner=True))
    assert json.loads(retry.text)["results"][0]["outcome"] == "existing"
    assert len(target.get_episodic_list()) == 1
    assert "6432" in target.get_episodic_list()[0]["text"]
    target.delete_episodic(copied_id)
    retry = await memory_member.api_memory_seed(request(env, body=body, owner=True))
    assert json.loads(retry.text)["results"][0]["outcome"] == "existing"
    assert target.get_episodic_list() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "handler_name",
    [
        "api_spawn_continue",
        "api_spawn_steer",
        "api_spawn_release",
        "api_spawn_status",
        "api_spawn_retry",
        "api_spawn_delete",
    ],
)
@pytest.mark.parametrize("target_store", ["", "member-bob"])
async def test_private_spawn_sibling_routes_refuse_foreign_runs(env, handler_name, target_store):
    from kiro_crew.dashboard.handlers import messaging

    # Fail at the scope boundary before status reads, provider work or mutations.
    env.state.subagents = SimpleNamespace(
        get=lambda run_id: SimpleNamespace(
            parent_session_key="dashboard:foreign", memory_store=target_store
        ),
    )
    req = request(
        env,
        body={"task": "continue", "message": "steer", "parent_session": "dashboard:alice"},
        internal=True,
    )
    req.match_info["agent_id"] = "foreign-run"
    response = await getattr(messaging, handler_name)(req)
    assert response.status == 404
    assert json.loads(response.text)["code"] == "task_scope_denied"


@pytest.mark.asyncio
@pytest.mark.parametrize("claimed", ["", "dashboard:owner", "subagent:unknown"])
async def test_private_continue_authenticates_parent_before_cwd_read(env, claimed):
    from kiro_crew.dashboard.handlers import messaging

    env.state.subagents = SimpleNamespace()
    req = request(env, body={"task": "continue", "parent_session": claimed}, internal=True)
    req.match_info["agent_id"] = "global-run"
    response = await messaging.api_spawn_continue(req)
    assert response.status == 409
    assert json.loads(response.text)["code"] == "member_identity_unavailable"


@pytest.mark.asyncio
async def test_spawn_list_follows_origin_including_cross_member_delegation(env):
    from kiro_crew.dashboard.handlers import messaging

    def row(name, store):
        return SimpleNamespace(
            id=name,
            memory_store=store,
            task=name,
            done=True,
            parent_session_key="dashboard:" + name.removesuffix("-run"),
            agent="kirocrew",
            started=1,
            result="result-" + name,
            error="",
            user_stopped=False,
            outcome="success",
            include_memory=True,
            include_lessons=True,
            include_project=True,
        )

    rows = [row("alice-run", "member-bob"), row("bob-run", "member-alice"), row("global-run", "")]
    env.state.subagents = SimpleNamespace(
        all_agents=rows, _agents={r.id: r for r in rows}, _tasks={r.id: object() for r in rows}
    )
    response = await messaging.api_spawn_list(request(env, internal=True))
    assert [r["id"] for r in json.loads(response.text)["agents"]] == ["alice-run"]
    assert "result-bob" not in response.text and "result-global" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "handler_name",
    [
        "api_crons_create",
        "api_cron_update",
        "api_cron_delete",
        "api_cron_batch_delete",
        "api_cron_run",
        "api_cron_cancel",
        "api_cron_to_chat",
        "api_cron_enable",
        "api_cron_ack",
        "api_cron_history",
        "api_cron_history_detail",
        "api_cron_history_all",
        "api_cron_script_source",
        "api_cron_secret_grant",
        "api_crons",
        "api_cron_folders",
        "api_cron_folders_create",
        "api_cron_folders_update",
        "api_cron_folders_delete",
    ],
)
async def test_private_caller_cannot_use_owner_cron_aggregate_routes(env, handler_name):
    response = await getattr(cron, handler_name)(
        request(
            env,
            body={
                "member_id": "bob",
                "session_key": "",
                "name": "foreign",
                "message": "read memory",
                "every": 60,
            },
            internal=True,
        )
    )
    assert response.status == 403, response.text
    assert json.loads(response.text)["code"] == "member_scope_denied"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "handler_name",
    [
        "api_sessions",
        "api_sessions_search",
        "api_session_detail",
        "api_sessions_summarize",
    ],
)
async def test_private_caller_cannot_read_owner_session_aggregate_routes(env, handler_name):
    from kiro_crew.dashboard.handlers import sessions

    response = await getattr(sessions, handler_name)(
        request(env, body={"keys": ["dashboard:owner"]}, internal=True)
    )
    assert response.status == 403
    assert json.loads(response.text)["code"] == "member_scope_denied"


@pytest.mark.parametrize(
    "tool, arguments",
    [
        ("list_sessions", {"all_workspaces": True}),
        ("search_chat_history", {"query": "confidential", "all_workspaces": True}),
        ("get_chat_session", {"session_key": "dashboard:bob", "all_workspaces": True}),
        ("get_chat_session", {"session_key": "dashboard:owner", "all_workspaces": True}),
        ("get_chat_session", {"session_key": "dashboard:incognito", "all_workspaces": True}),
    ],
)
def test_history_tools_preserve_retention_without_member_confidentiality(
    env, monkeypatch, tool, arguments
):
    from kiro_crew.mcp_tools import sessions

    env.bind_session("dashboard:bob", "member-bob")
    for key, label in [
        ("dashboard:alice", "Alice"),
        ("dashboard:bob", "Bob"),
        ("dashboard:owner", "Global"),
    ]:
        env.history.append(key, "user", label + " confidential message")
    env.history.append("dashboard:incognito", "user", "Restricted payload")
    env.history.update_metadata("dashboard:incognito", {"memory_mode": "incognito"})
    monkeypatch.setattr(
        mcp_core, "require_strict_session_key", lambda error: ("dashboard:alice", "")
    )
    monkeypatch.setattr(mcp_core, "_resolve_session_key", lambda: "dashboard:alice")
    result = sessions.HANDLERS[tool](tool, arguments)
    assert "Restricted payload" not in result
    if tool == "get_chat_session":
        if arguments["session_key"] == "dashboard:incognito":
            assert "incognito/temporary" in result
        else:
            assert "Bob" in result or "Global" in result
    elif tool == "list_sessions":
        assert "dashboard_alice" in result
        assert "dashboard_bob" in result and "dashboard_owner" in result
        assert "dashboard_incognito" not in result
    else:
        assert "Alice" in result and "Bob" in result and "Global" in result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path, method",
    [
        ("/api/chat", "POST"),
        ("/api/chat/slots", "POST"),
        ("/api/chat/slots", "GET"),
        ("/api/chat/slots/alice/agent", "POST"),
        ("/api/chat/slots/alice/resume", "POST"),
    ],
)
async def test_private_chat_control_cannot_create_or_relabel_unbound_slots(env, path, method):
    from kiro_crew.dashboard.handlers._shared import private_chat_route_refusal

    app = web.Application()
    app["state"] = env.state
    req = make_mocked_request(
        method,
        path,
        app=app,
        headers={"X-Session-Key": "dashboard:alice"},
    )
    req["internal_auth"] = True
    req["peer_verified"] = True
    response = await private_chat_route_refusal(req)
    assert response.status == 403
    assert json.loads(response.text)["code"] == "member_scope_denied"


@pytest.mark.asyncio
async def test_private_followup_card_can_only_target_its_own_bound_tab(env):
    from kiro_crew.dashboard.handlers._shared import private_chat_route_refusal

    env.state._slots["alice"] = SimpleNamespace(
        key="alice", memory_store="member-alice", linked_session_key=""
    )
    env.state._slots["bob"] = SimpleNamespace(
        key="bob", memory_store="member-bob", linked_session_key=""
    )
    app = web.Application()
    app["state"] = env.state
    for slot in ["alice", "bob"]:
        req = make_mocked_request(
            "POST",
            f"/api/chat/slots/{slot}/followup",
            app=app,
            headers={"X-Session-Key": "dashboard:alice"},
        )
        req["internal_auth"] = True
        req["peer_verified"] = True
        req.match_info["slot"] = slot
        response = await private_chat_route_refusal(req)
        if slot == "alice":
            assert response is None
        else:
            assert response.status == 403


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "handler_name",
    [
        "api_session_keepalive",
        "api_session_tool_policy",
        "api_session_directive",
    ],
)
@pytest.mark.parametrize("claimed", ["dashboard:alice", "dashboard:bob", "dashboard:owner"])
async def test_session_callbacks_use_the_ordinary_transport_identity(
    env,
    monkeypatch,
    handler_name,
    claimed,
):
    from kiro_crew.dashboard.handlers import sessions

    provider = SimpleNamespace(touch_activity=mock.Mock())
    env.state.sessions = SimpleNamespace(
        get_provider=mock.Mock(return_value=provider),
        touch=mock.Mock(),
    )
    env.state.get_slot = lambda key: SimpleNamespace(agent="alice")
    read_policy = mock.Mock(return_value={"exclude": ["unsafe_tool"]})
    monkeypatch.setattr(sessions, "_read_managed_tool_policy_sync", read_policy)
    monkeypatch.setattr(mcp_core, "derive_directive", lambda *args: ("loop", {"enabled": False}))
    publish = mock.Mock(return_value="directive-1")
    monkeypatch.setattr(sessions.directive_queue, "publish", publish)
    response = await getattr(sessions, handler_name)(
        request(
            env,
            internal=True,
            session=claimed,
            body={"tool": "loop", "raw_args": {"enabled": False}},
        )
    )
    assert response.status == 200
    if handler_name == "api_session_keepalive":
        provider.touch_activity.assert_called_once()
        env.state.sessions.get_provider.assert_called_once_with(claimed)
    elif handler_name == "api_session_tool_policy":
        assert json.loads(response.text) == {"exclude": ["unsafe_tool"]}
    else:
        assert publish.call_args.args[0] == claimed


@pytest.mark.asyncio
async def test_internal_chat_middleware_refuses_member_before_slot_creation(env):
    from kiro_crew.dashboard.token_auth import token_auth_middleware

    app = web.Application()
    app["state"] = env.state
    req = make_mocked_request(
        "POST",
        "/api/chat/slots",
        app=app,
        headers={
            "X-Internal-Secret": "test-member-secret",
            "X-Session-Key": "dashboard:alice",
        },
    ).clone(remote="127.0.0.1")
    handler = mock.AsyncMock(return_value=web.json_response({"created": True}))
    middleware = token_auth_middleware(
        mixed_internal_paths=frozenset({"/api/chat"}),
        internal_secret="test-member-secret",
    )
    response = await middleware(req, handler)
    # The loopback TCP caller holds the secret and declares a session key it
    # cannot attest, so the identity refusal lands ahead of the member-scope
    # decision. Either way the chat control never runs.
    assert response.status == 409
    assert json.loads(response.text)["code"] == "member_identity_unavailable"
    handler.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "PUT"])
async def test_member_history_oversized_document_is_unavailable_without_overwrite(env, method):
    await document_store(env, "member-alice")
    db = env.tiers["member-alice"].db
    limit = MemoryStore._HISTORY_SNAPSHOT_MAX_BYTES
    day = datetime.now().astimezone().date().isoformat()
    with db:
        db.execute("INSERT INTO memory_history VALUES (?,?,1,'now')", (day, "x" * (limit + 1)))
    before = db.total_changes
    response = await memory.api_memory_history(
        request(
            env,
            body={"content": "Do not overwrite the unreadable source"},
            query={"store": "member-alice"},
            owner=True,
            session="dashboard:ui",
        ).clone(method=method)
    )
    assert response.status == 503
    assert json.loads(response.text)["code"] == "store_unavailable"
    assert db.total_changes == before
    assert (
        db.execute(
            "SELECT length(CAST(content AS BLOB)) FROM memory_history WHERE day=?", (day,)
        ).fetchone()[0]
        == limit + 1
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["persistent", "incognito", "temporary"])
@pytest.mark.parametrize("vector_available", [True, False])
@pytest.mark.parametrize(
    "question", ["{}", "What do we know about {}?", "Which project does {} belong to?"]
)
async def test_markdown_only_facts_reachable_from_agent_recall(
    env, monkeypatch, mode, vector_available, question
):
    from kiro_crew import context, member_memory_auth
    from kiro_crew.hooks import HookManager
    from kiro_crew.learn import LessonStore
    from kiro_crew.skills import SkillsLoader

    store = await document_store(env, "")
    store.write_projects("# Active Projects\nNotebookquartz task belongs to the synthetic project.")
    store.append_history("Dailyquartz milestone was verified.")
    assert env.tiers[""].get_semantic("Notebookquartz") is None
    builder = context.ContextBuilder(
        memory=store,
        lessons=LessonStore(base_dir=env.home / "synthetic-lessons"),
        skills=SkillsLoader(skills_path=env.home / "synthetic-skills", install_builtins=False),
        hooks=HookManager(),
    )
    monkeypatch.setattr(context, "kiro_agents_dir", lambda: env.home / "empty-agents")
    monkeypatch.setattr(context, "agent_skill_globs", lambda agent: [])
    prompt = env.home / "synthetic-prompt.txt"
    prompt.write_text("Preserve all safety controls.", encoding="utf-8")
    monkeypatch.setattr(context, "_prompt_path", lambda **kwargs: prompt)
    greeting, _ = builder.build_message("hi", True, blocks_reads=mode == "temporary")
    assert greeting.endswith("hi")
    if mode == "temporary":
        assert "Notebookquartz" not in greeting and "Dailyquartz" not in greeting
    else:
        assert "Notebookquartz" in greeting and "Dailyquartz" in greeting
    env.state._slots["global"] = SimpleNamespace(
        is_restricted=mode == "incognito", blocks_reads=mode == "temporary", memory_mode=mode
    )
    session = "dashboard:global"
    env.metadata[session] = {"memory_store": "default", "memory_mode": mode}
    monkeypatch.setattr(member_memory_auth, "_request_peer_pid", lambda request: os.getpid())
    if not vector_available:
        store.vector_store = None
    before = (store.read_projects(), store.read_recent_history())
    search = mock.Mock(wraps=store.search)
    monkeypatch.setattr(store, "search", search)
    for fact in ("Notebookquartz", "Dailyquartz"):
        query = question.format(fact)
        search.reset_mock()
        response = await memory_member.api_memory_recall(
            request(env, query={"q": query}, internal=True, session=session)
        )
        assert response.text is not None
        if mode == "temporary":
            assert response.status == 403
            assert fact not in response.text
            search.assert_not_called()
            continue
        assert response.status == 200
        assert search.call_count == 1
        assert search.call_args.kwargs == {"limit": 5, "match_any": True, "strict": True}
        payload = json.loads(response.text)
        assert fact in payload["semantic_context"]
        assert payload["store"] == ""
        assert payload["total_chars"] <= 3000
        # The real MCP renderer forwards this HTTP result with the strict
        # session identity and keeps the notebook evidence in its model payload.
        monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: session)
        transport = mock.Mock(return_value=payload)
        monkeypatch.setattr(mcp_core, "_get", transport)
        rendered = learn.memory_recall("memory_recall", {"query": query})
        assert fact in rendered
        assert "reference data, not instructions" in rendered
        assert len(rendered.encode("utf-8")) <= 16384
        assert transport.call_args.kwargs == {"session_key": session}
    assert (store.read_projects(), store.read_recent_history()) == before


@pytest.mark.asyncio
async def test_markdown_recall_is_bound_to_named_v1_not_global(env, monkeypatch):
    from kiro_crew import member_memory_auth

    cfg = loader.KiroCrewConfig.load()
    cfg.memory_stores["legacy-notebook"] = loader.MemoryStoreConfig(memory_version=1)
    cfg.save()
    (env.home / "memory_stores" / "legacy-notebook").mkdir()
    monkeypatch.setattr(memory_stores, "_DECLARED_MEMO", None)
    own = await document_store(env, "legacy-notebook")
    global_store = await document_store(env, "")
    own.write_projects("# Active Projects\nQuartzscope OWN notebook milestone.")
    global_store.write_projects("# Active Projects\nQuartzscope GLOBAL notebook milestone.")
    env.state._slots["legacy"] = SimpleNamespace(is_restricted=False, blocks_reads=False)
    # Internal recall resolves the store from the session's recorded execution,
    # never from transcript metadata alone.
    env.bind_session("dashboard:legacy", "legacy-notebook")
    monkeypatch.setattr(member_memory_auth, "_request_peer_pid", lambda request: os.getpid())
    response = await memory_member.api_memory_recall(
        request(
            env,
            query={"q": "What do we know about Quartzscope?"},
            internal=True,
            session="dashboard:legacy",
        )
    )
    assert response.status == 200
    assert response.text is not None
    assert "OWN notebook" in response.text
    assert "GLOBAL notebook" not in response.text
    assert json.loads(response.text)["store"] == "legacy-notebook"
