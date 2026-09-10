"""Member recall and explicit seed cross the real owner/session/store gates."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from member_memory_helpers import DOCUMENT_CREDENTIAL, document_store
from member_memory_helpers import env as _member_env
from member_memory_helpers import member_proof as _member_proof
from member_memory_helpers import patch_private_memory_supported, request, seed_body

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
from kiro_crew.vector_memory import VectorMemoryStore

pytestmark = pytest.mark.xdist_group("member_memory_api")


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["default", "bob"])
async def test_private_spawn_cannot_delegate_into_global_or_peer_memory(env, member_proof, target):
    from kiro_crew.config.loader import KiroCrewAgentConfig
    from kiro_crew.dashboard.handlers import messaging

    cfg = loader.KiroCrewConfig.load()
    cfg.agents["default"] = KiroCrewAgentConfig(kiro_agent="kirocrew", triggers="general")
    cfg.agents["bob"].triggers = "review"
    cfg.save()
    env.state.subagents = SimpleNamespace(spawn=mock.Mock())
    response = await messaging.api_spawn(
        request(
            env,
            body={"task": "read your memory", "crew": target, "parent_session": "dashboard:alice"},
            internal=True,
            proof=member_proof,
        )
    )
    assert response.status == 409
    assert json.loads(response.text)["code"] == "memory_unavailable"
    env.state.subagents.spawn.assert_not_called()


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
    monkeypatch.setattr(
        "kiro_crew.member_memory_auth.memory_request_identity",
        lambda request: (identity, True),
    )
    response = await messaging.api_spawn(
        request(env, body={"task": "spawn", "parent_session": parent_session}, internal=True)
    )
    assert response.status == 403
    env.state.subagents.spawn.assert_not_called()


@pytest.mark.asyncio
async def test_private_taskrunner_start_forwards_protected_origin(env, member_proof):
    runner = SimpleNamespace(
        _work_dir=env.home / "tasks",
        start_background=mock.AsyncMock(return_value="run-1"),
    )
    env.state.task_runner = runner
    response = await taskrunner.api_taskrunner_start(
        request(
            env,
            body={"spec": "__inline__:# Task\nDo the work"},
            internal=True,
            proof=member_proof,
        )
    )
    assert response.status == 200
    assert runner.start_background.await_args.kwargs["session_key"] == "dashboard:alice"


@pytest.mark.asyncio
async def test_private_taskrunner_status_hides_peer_and_global_runs(env, member_proof):
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
    response = await taskrunner.api_taskrunner_status(
        request(env, internal=True, proof=member_proof)
    )
    assert response.status == 200
    assert [row["task_id"] for row in json.loads(response.text)["runs"]] == ["alice-run"]


def test_private_run_boundary_is_enforced_without_http(env):
    from kiro_crew.context import require_memory_delegation

    require_memory_delegation(env.state.conversation_log, "dashboard:alice", "member-alice")
    require_memory_delegation(env.state.conversation_log, "dashboard:coordinator", "member-bob")
    for target in ("", "member-bob"):
        with pytest.raises(memory_stores.UnknownMemoryStore, match="tasks must retain"):
            require_memory_delegation(env.state.conversation_log, "dashboard:alice", target)


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
    assert env.metadata["taskrunner:legacy-child"]["memory_store"] == "legacy-team"


def test_private_schedule_cannot_retarget_a_peer_or_claim_unsigned_metadata(env):
    from kiro_crew.cron import CronJob, bind_cron_memory
    from kiro_crew.history import ConversationLog

    own = CronJob(id="own", name="own", message="work", session_key="dashboard:alice")
    bind_cron_memory(own)
    assert (own.member_id, own.memory_store) == ("alice", "member-alice")
    peer = CronJob(
        id="peer", name="peer", message="work", session_key="dashboard:alice", member_id="bob"
    )
    with pytest.raises(ValueError, match="only its own private memory"):
        bind_cron_memory(peer)
    ConversationLog().update_metadata("slack:forged", {"memory_store": "member-bob"})
    forged = CronJob(id="forged", name="forged", message="work", session_key="slack:forged")
    with pytest.raises(ValueError, match="trusted member assignment"):
        bind_cron_memory(forged)


# ``env`` / ``member_proof`` are rebound module attributes so pytest discovers them
# here and the modules that ``from test_member_memory_api import env`` keep working.
env = _member_env
member_proof = _member_proof


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
@pytest.mark.parametrize("store", ["", "member-alice"])
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
async def test_history_put_checks_today_when_the_bounded_v2_aggregate_omits_it(env, monkeypatch):
    memory_store = await document_store(env, "member-alice")
    today = memory_store._today_history_file()
    raw = f"retain {DOCUMENT_CREDENTIAL} exactly"
    today.write_text(raw, encoding="utf-8")
    (today.parent / "9999-12-31.md").write_text("future entry", encoding="utf-8")
    monkeypatch.setattr(MemoryStore, "_HISTORY_SNAPSHOT_MAX_ENTRIES", 1)
    baseline = memory_store.read_recent_history()
    assert DOCUMENT_CREDENTIAL not in baseline

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
    assert today.read_text(encoding="utf-8") == raw


@pytest.mark.asyncio
async def test_unsigned_transcript_cannot_claim_or_mint_private_memory(env, monkeypatch):
    from kiro_crew import member_memory_auth
    from kiro_crew.context import prepare_store_vectors, store_of_session

    forged_key = "slack:forged-private-assignment"
    env.metadata[forged_key] = {"memory_store": "member-bob"}
    with pytest.raises(memory_stores.UnknownMemoryStore, match="trusted member assignment"):
        store_of_session(env.state.conversation_log, forged_key)
    patch_private_memory_supported(monkeypatch)
    with pytest.raises(memory_stores.UnknownMemoryStore, match="trusted member assignment"):
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
    restored = VectorMemoryStore(db_path=directory / "memory.db")
    restored.init()
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
    tier = VectorMemoryStore(db_path=env.home / "memory_stores" / "member-alice" / "memory.db")
    tier.init()
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
    assert response.status == 403
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
async def test_internal_recall_uses_recorded_member_and_returns_bounded_evidence(env, member_proof):
    for name, marker in (
        ("", "GLOBALSECRET"),
        ("member-bob", "BOBSECRET"),
        ("member-alice", "ALICEFACT"),
    ):
        env.tiers[name].set_semantic(
            "project.database", f"PostgreSQL {marker}", 1.0, "user_explicit"
        )
    response = await memory_member.api_memory_recall(
        request(env, query={"q": "PostgreSQL database"}, internal=True, proof=member_proof)
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
    env.metadata["dashboard:alice"]["memory_store"] = "missing-member"
    env.tiers[""].set_semantic("project.database", "GLOBALSECRET", 1.0, "user_explicit")
    response = await memory_member.api_memory_recall(
        request(env, query={"q": "database"}, internal=True)
    )
    assert response.status == 503
    assert "GLOBALSECRET" not in response.text


@pytest.mark.asyncio
async def test_database_read_failure_returns_explicit_unavailability(
    env, monkeypatch, member_proof
):
    def unreadable(*args, **kwargs):
        raise OSError("disk unreadable")

    monkeypatch.setattr(env.tiers["member-alice"], "recall", unreadable)
    response = await memory_member.api_memory_recall(
        request(env, query={"q": "database"}, internal=True, proof=member_proof)
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
async def test_private_spawn_sibling_routes_refuse_foreign_runs(
    env, member_proof, handler_name, target_store
):
    from kiro_crew.dashboard.handlers import messaging

    # Fail at the scope boundary before status reads, provider work or mutations.
    env.state.subagents = SimpleNamespace(
        _inherited_memory_store=lambda run_id: target_store,
    )
    req = request(
        env,
        body={"task": "continue", "message": "steer", "parent_session": "dashboard:alice"},
        internal=True,
        proof=member_proof,
    )
    req.match_info["agent_id"] = "foreign-run"
    response = await getattr(messaging, handler_name)(req)
    assert response.status == 404
    assert json.loads(response.text)["code"] == "task_scope_denied"


@pytest.mark.asyncio
@pytest.mark.parametrize("claimed", ["", "dashboard:owner", "subagent:unknown"])
async def test_private_continue_authenticates_parent_before_cwd_read(env, member_proof, claimed):
    from kiro_crew.dashboard.handlers import messaging

    env.state.subagents = SimpleNamespace()
    req = request(
        env, body={"task": "continue", "parent_session": claimed}, internal=True, proof=member_proof
    )
    req.match_info["agent_id"] = "global-run"
    response = await messaging.api_spawn_continue(req)
    assert response.status == 403
    assert json.loads(response.text)["code"] == "member_session_unverified"


@pytest.mark.asyncio
async def test_private_spawn_list_and_clear_only_touch_own_runs(env, member_proof):
    from kiro_crew.dashboard.handlers import messaging

    def row(name, store):
        return SimpleNamespace(
            id=name,
            memory_store=store,
            task=name,
            done=True,
            parent_session_key="dashboard:" + name,
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

    rows = [row("alice-run", "member-alice"), row("bob-run", "member-bob"), row("global-run", "")]
    env.state.subagents = SimpleNamespace(
        all_agents=rows, _agents={r.id: r for r in rows}, _tasks={r.id: object() for r in rows}
    )
    response = await messaging.api_spawn_list(request(env, internal=True, proof=member_proof))
    assert [r["id"] for r in json.loads(response.text)["agents"]] == ["alice-run"]
    assert "result-bob" not in response.text and "result-global" not in response.text
    cleared = await messaging.api_spawn_clear(request(env, internal=True, proof=member_proof))
    assert json.loads(cleared.text)["cleared"] == 1
    assert set(env.state.subagents._agents) == {"bob-run", "global-run"}
    assert set(env.state.subagents._tasks) == {"bob-run", "global-run"}


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
async def test_private_caller_cannot_use_owner_cron_aggregate_routes(
    env, member_proof, handler_name
):
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
            proof=member_proof,
        )
    )
    assert response.status == 403
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
async def test_private_caller_cannot_read_owner_session_aggregate_routes(
    env, member_proof, handler_name
):
    from kiro_crew.dashboard.handlers import sessions

    response = await getattr(sessions, handler_name)(
        request(env, body={"keys": ["dashboard:owner"]}, internal=True, proof=member_proof)
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
    ],
)
def test_member_history_tools_cannot_cross_private_store(
    env, member_proof, monkeypatch, tool, arguments
):
    from kiro_crew.mcp_tools import sessions

    env.bind_session("dashboard:bob", "member-bob")
    for key, label in [
        ("dashboard:alice", "Alice"),
        ("dashboard:bob", "Bob"),
        ("dashboard:owner", "Global"),
    ]:
        env.history.append(key, "user", label + " confidential message")
    monkeypatch.setattr(
        mcp_core, "require_strict_session_key", lambda error: ("dashboard:alice", "")
    )
    monkeypatch.setattr(mcp_core, "_resolve_session_key", lambda: "dashboard:alice")
    result = sessions.HANDLERS[tool](tool, arguments)
    assert "Bob confidential" not in result and "Global confidential" not in result
    if tool == "get_chat_session":
        assert "Access denied" in result
    elif tool == "list_sessions":
        assert "dashboard_alice" in result
        assert "dashboard_bob" not in result and "dashboard_owner" not in result
    else:
        assert "Alice confidential" in result
        assert "dashboard:bob" not in result and "dashboard:owner" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "handler_name",
    [
        "api_session_control_create",
        "api_session_control_stop",
        "api_session_control_close",
        "api_session_control_send",
        "api_session_control_read",
    ],
)
async def test_member_cannot_bypass_spawn_via_unbound_session_control(
    env, member_proof, handler_name
):
    from kiro_crew.dashboard.handlers import session_control

    response = await getattr(session_control, handler_name)(
        request(
            env,
            body={"target": "dashboard:owner", "agent": "default"},
            internal=True,
            proof=member_proof,
        )
    )
    assert response.status == 403
    assert json.loads(response.text)["code"] == "member_scope_denied"


def test_forged_mcp_caller_without_proof_cannot_read_private_history(env, monkeypatch):
    from kiro_crew.mcp_caller import CallerContext
    from kiro_crew.mcp_tools import sessions

    env.history.append("dashboard:alice", "user", "Alice confidential message")
    monkeypatch.setattr(
        mcp_core, "require_strict_session_key", lambda error: ("dashboard:alice", "")
    )
    monkeypatch.setattr(
        "kiro_crew.mcp_caller.current_caller",
        lambda: CallerContext(session_key="dashboard:alice", from_gateway=True),
    )
    result = sessions.list_sessions("list_sessions", {"all_workspaces": True})
    assert result.startswith("Error:")
    assert "Alice confidential" not in result


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
async def test_private_chat_control_cannot_create_or_relabel_unbound_slots(
    env, member_proof, path, method
):
    from kiro_crew.dashboard.handlers._shared import private_chat_route_refusal

    app = web.Application()
    app["state"] = env.state
    req = make_mocked_request(
        method,
        path,
        app=app,
        headers={"X-Session-Key": "dashboard:alice", "X-Member-Session-Proof": member_proof},
    )
    req["internal_auth"] = True
    response = await private_chat_route_refusal(req)
    assert response.status == 403
    assert json.loads(response.text)["code"] == "member_scope_denied"


@pytest.mark.asyncio
async def test_private_followup_card_can_only_target_its_own_bound_tab(env, member_proof):
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
            headers={"X-Session-Key": "dashboard:alice", "X-Member-Session-Proof": member_proof},
        )
        req["internal_auth"] = True
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
async def test_member_current_session_callbacks_keep_verified_own_scope(
    env,
    member_proof,
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
            proof=member_proof,
            session=claimed,
            body={"tool": "loop", "raw_args": {"enabled": False}},
        )
    )
    if claimed != "dashboard:alice":
        assert response.status == 403
        provider.touch_activity.assert_not_called()
        read_policy.assert_not_called()
        publish.assert_not_called()
    else:
        assert response.status == 200
        if handler_name == "api_session_keepalive":
            provider.touch_activity.assert_called_once()
            env.state.sessions.get_provider.assert_called_once_with("dashboard:alice")
        elif handler_name == "api_session_tool_policy":
            assert json.loads(response.text) == {"exclude": ["unsafe_tool"]}
        else:
            assert publish.call_args.args[0] == "dashboard:alice"


@pytest.mark.asyncio
async def test_internal_chat_middleware_refuses_member_before_slot_creation(env, member_proof):
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
            "X-Member-Session-Proof": member_proof,
        },
    ).clone(remote="127.0.0.1")
    handler = mock.AsyncMock(return_value=web.json_response({"created": True}))
    middleware = token_auth_middleware(
        mixed_internal_paths=frozenset({"/api/chat"}),
        internal_secret="test-member-secret",
    )
    response = await middleware(req, handler)
    assert response.status == 403
    assert json.loads(response.text)["code"] == "member_scope_denied"
    handler.assert_not_awaited()
