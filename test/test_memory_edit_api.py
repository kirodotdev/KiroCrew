"""Bulk routes cross the production owner and store resolution gates."""

import asyncio
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from member_memory_helpers import env as _member_env
from member_memory_helpers import request

from kiro_crew.dashboard.handlers import memory_edit

env = _member_env


@pytest.mark.asyncio
@pytest.mark.parametrize("internal", [False, True])
async def test_agent_and_nonowner_cannot_reach_bulk_even_without_store(env, internal):
    for handler in (
        memory_edit.api_memory_records,
        memory_edit.api_memory_records_refresh,
        memory_edit.api_memory_bulk_preview,
        memory_edit.api_memory_bulk_apply,
    ):
        response = await handler(
            request(
                env,
                body={} if handler != memory_edit.api_memory_records else None,
                internal=internal,
            )
        )
        assert response.status == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["default", "member-alice"])
async def test_owner_bulk_selects_only_requested_store_and_is_retryable(env, name):
    for tier in env.tiers.values():
        assert tier.set_semantic("user.email", "owner@old.example", 1.0, "user_explicit") is None
    for key in ("default", "member-alice", "member-bob"):
        response = await memory_edit.api_memory_records(
            request(env, query={"store": key, "q": "email"}, owner=True)
        )
        assert response.status == 200
        assert json.loads(response.text)["total"] == 1
    response = await memory_edit.api_memory_bulk_preview(
        request(
            env,
            owner=True,
            session="dashboard:ui",
            body={
                "store": name,
                "selection": {"query": {"q": "email"}},
                "operation": {"type": "replace_text", "find": "old", "replacement": "new"},
            },
        )
    )
    assert response.status == 200, response.text
    preview = json.loads(response.text)
    for _ in range(2):
        response = await memory_edit.api_memory_bulk_apply(
            request(
                env,
                owner=True,
                session="dashboard:ui",
                body={"store": name, "preview_id": preview["preview_id"]},
            )
        )
        assert response.status == 200, response.text
        assert json.loads(response.text)["changed_count"] == 1
    for key, tier in env.tiers.items():
        expected = "new" if key == ("" if name == "default" else name) else "old"
        assert (
            json.loads(tier.get_semantic("user.email")["value_json"]) == f"owner@{expected}.example"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["default", "member-alice"])
async def test_owner_record_listing_accepts_query_auth_token_without_widening_filters(env, name):
    tier = env.tiers["" if name == "default" else name]
    tier.set_semantic("user.contact", "selected contact", 1.0, "user_explicit")
    tier.set_semantic("user.language", "unrelated language", 1.0, "user_explicit")
    query = {"store": name, "q": "contact", "kind": "fact", "token": "fixture-link-token"}
    response = await memory_edit.api_memory_records(request(env, query=query, owner=True))
    assert response.status == 200, response.text
    payload = json.loads(response.text)
    assert payload["total"] == 1
    assert [entry["id"] for entry in payload["entries"]] == ["user.contact"]
    assert "fixture-link-token" not in response.text

    query["topic"] = "contact"
    invalid = await memory_edit.api_memory_records(request(env, query=query, owner=True))
    assert invalid.status == 400
    denied = await memory_edit.api_memory_records(request(env, query=query))
    assert denied.status == 403


@pytest.mark.asyncio
async def test_bad_store_and_invalid_filters_fail_explicitly(env):
    for query, status in [
        ({"store": "unknown"}, 404),
        ({"store": "default", "kind": "typo"}, 400),
        ({"store": "default", "limit": "-1"}, 400),
        ({"store": "default", "topic": "typo"}, 400),
        ({"store": "default", "topic": "email"}, 400),
    ]:
        response = await memory_edit.api_memory_records(request(env, query=query, owner=True))
        assert response.status == status


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["default", "member-alice"])
async def test_http_query_refresh_counts_current_membership_and_inactive_exclusions(env, name):
    tier = env.tiers["" if name == "default" else name]

    def seed():
        for candidate in env.tiers.values():
            for index in range(4):
                assert (
                    candidate.set_semantic(
                        f"user.contact{index}", "Straße user@example.org", 1.0, "user_explicit"
                    )
                    is None
                )

    await asyncio.to_thread(seed)

    @web.middleware
    async def owner_principal(request, handler):
        # Authentication is a fixture principal; the actual owner/store gates,
        # bounded JSON reader and selection service all run over HTTP below.
        request["user"] = "owner"
        request["app"] = ""
        return await handler(request)

    app = web.Application(middlewares=[owner_principal])
    app["state"] = env.state
    app.router.add_post("/api/memory/records/refresh", memory_edit.api_memory_records_refresh)
    selected = {
        "query": {"q": "STRASSE", "kind": "fact"},
        "exclude": [
            {"kind": "fact", "id": "user.contact0"},
            {"kind": "fact", "id": "user.contact1"},
        ],
    }
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/api/memory/records/refresh", json={"store": name, "selection": selected}
        )
        assert response.status == 200, await response.text()
        assert await response.json() == {"matched_count": 2}

        def change_membership():
            tier.delete_semantic("user.contact0", source="user_explicit")
            tier.set_semantic("user.contact1", "No matching phrase", 1.0, "user_explicit")
            tier.set_semantic(
                "user.contact4", "Ｓｔｒａｓｓｅ user@example.org", 1.0, "user_explicit"
            )

        await asyncio.to_thread(change_membership)
        before = {key: candidate.db.total_changes for key, candidate in env.tiers.items()}
        response = await client.post(
            "/api/memory/records/refresh", json={"store": name, "selection": selected}
        )
        assert response.status == 200, await response.text()
        assert await response.json() == {"matched_count": 3}
        for peer in {"default", "member-alice", "member-bob"} - {name}:
            response = await client.post(
                "/api/memory/records/refresh", json={"store": peer, "selection": selected}
            )
            assert response.status == 200, await response.text()
            assert await response.json() == {"matched_count": 2}
        response = await client.post(
            "/api/memory/records/refresh",
            json={"store": name, "items": selected["exclude"]},
        )
        explicit = await response.json()
        assert response.status == 200
        assert set(explicit) == {"entries", "missing"}
        assert [row["id"] for row in explicit["entries"]] == ["user.contact1"]
        assert explicit["missing"] == [{"kind": "fact", "id": "user.contact0"}]
        assert {key: candidate.db.total_changes for key, candidate in env.tiers.items()} == before


@pytest.mark.asyncio
async def test_private_store_unavailable_never_returns_global_records(env, monkeypatch):
    async def unavailable(state, name):
        return None

    monkeypatch.setattr(memory_edit, "vector_memory_for_store", unavailable)
    response = await memory_edit.api_memory_records(
        request(env, query={"store": "member-alice"}, owner=True)
    )
    assert response.status == 503
    assert json.loads(response.text)["code"] == "store_unavailable"
