"""Who may use ``/api/memory/provider``, and what a switch requires.

The owner may do everything; an app reaches the routes only with
``permissions.memoryAdmin`` and never switches; an agent's internal secret opens
nothing here (the read-only conformance kit aside). A switch refuses a provider that
cannot do what the mode needs. While a provider replaces Crew's memory, Crew's own
memory refuses every editor write with 409.
"""

from __future__ import annotations

import json

import pytest
from member_memory_helpers import env  # noqa: F401 -- the fixture is requested by name
from member_memory_helpers import make_request
from memory_provider_helpers import FIXTURE_ID, READONLY_ID, fixture_provider, set_provider

from kiro_crew.config import loader
from kiro_crew.dashboard.handlers import memory_provider as handlers
from kiro_crew.dashboard.handlers import memory_provider_migrations as migrations

pytestmark = pytest.mark.xdist_group("memory_provider")


@pytest.fixture
def served(monkeypatch, tmp_path, env):  # noqa: F811 -- the imported fixture
    env.state.push_refresh = lambda *_a: None
    audits: list[dict] = []
    monkeypatch.setattr(handlers, "_audit", lambda *a, **k: audits.append({"args": a, **k}))
    with fixture_provider(monkeypatch, tmp_path / "provider.json") as fixture:
        fixture.env = env
        fixture.audits = audits
        yield fixture


def _app_request(served, path, *, method="GET", body=None, app="memory-fixture"):
    request = make_request(served.env.state, path, method=method, body=body, session="")
    request["user"] = "owner"
    request["app"] = app
    return request


def _owner(served, path, *, method="GET", body=None, query=None):
    return make_request(
        served.env.state, path, method=method, body=body, query=query, owner=True, session=""
    )


@pytest.mark.asyncio
async def test_an_app_without_memory_admin_is_refused(served, monkeypatch) -> None:
    monkeypatch.setattr(handlers, "_app_holds_memory_admin", lambda app: False)
    with pytest.raises(handlers.web.HTTPForbidden):
        await handlers.api_memory_provider_get(_app_request(served, "/api/memory/provider"))
    assert served.audits[-1]["args"][2] == "denied"


@pytest.mark.asyncio
async def test_an_app_with_memory_admin_reads_and_is_audited(served, monkeypatch) -> None:
    monkeypatch.setattr(handlers, "_app_holds_memory_admin", lambda app: True)
    response = await handlers.api_memory_provider_get(_app_request(served, "/api/memory/provider"))
    body = json.loads(response.text)
    assert {p["id"] for p in body["providers"]} == {FIXTURE_ID, READONLY_ID}
    assert served.audits[-1]["args"][:3] == ("app:memory-fixture", "get", "allowed")


@pytest.mark.asyncio
async def test_no_app_and_no_agent_can_switch(served, monkeypatch) -> None:
    monkeypatch.setattr(handlers, "_app_holds_memory_admin", lambda app: True)
    body = {"mode": "replace", "id": FIXTURE_ID}
    with pytest.raises(handlers.web.HTTPForbidden):
        await handlers.api_memory_provider_put(
            _app_request(served, "/api/memory/provider", method="PUT", body=body)
        )
    agent = make_request(
        served.env.state, "/api/memory/provider", method="PUT", body=body, internal=True
    )
    with pytest.raises(handlers.web.HTTPForbidden):
        await handlers.api_memory_provider_put(agent)
    assert loader.KiroCrewConfig.load().memory.provider.mode == "builtin"


@pytest.mark.asyncio
async def test_the_owner_switches_and_back(served) -> None:
    response = await handlers.api_memory_provider_put(
        _owner(
            served, "/api/memory/provider", method="PUT", body={"mode": "replace", "id": FIXTURE_ID}
        )
    )
    assert response.status == 200
    loader._invalidate_config_cache()
    provider = loader.KiroCrewConfig.load().memory.provider
    assert (provider.mode, provider.id) == ("replace", FIXTURE_ID) and provider.switched_at > 0
    response = await handlers.api_memory_provider_put(
        _owner(served, "/api/memory/provider", method="PUT", body={"mode": "builtin"})
    )
    loader._invalidate_config_cache()
    provider = loader.KiroCrewConfig.load().memory.provider
    # The id stays so chats that started on the provider stay on it.
    assert (provider.mode, provider.id) == ("builtin", FIXTURE_ID)


@pytest.mark.asyncio
async def test_a_read_only_provider_cannot_replace(served) -> None:
    response = await handlers.api_memory_provider_put(
        _owner(
            served,
            "/api/memory/provider",
            method="PUT",
            body={"mode": "replace", "id": READONLY_ID},
        )
    )
    body = json.loads(response.text)
    assert response.status == 409 and body["code"] == "replace_unsupported"
    assert "read_only" in body["detail"]["gaps"]


@pytest.mark.asyncio
async def test_a_remote_provider_needs_consent_at_the_switch(served, monkeypatch) -> None:
    caps = served.fixture.capabilities()
    remote = type(caps)(**{**caps.__dict__, "egress": "remote"})
    monkeypatch.setattr(served.fixture, "capabilities", lambda: remote)
    monkeypatch.setattr(served.fixture, "connect", lambda want_mode=None: remote)
    request = {"mode": "replace", "id": FIXTURE_ID}
    response = await handlers.api_memory_provider_put(
        _owner(served, "/api/memory/provider", method="PUT", body=request)
    )
    assert json.loads(response.text)["code"] == "egress_consent_required"
    response = await handlers.api_memory_provider_put(
        _owner(
            served, "/api/memory/provider", method="PUT", body={**request, "egress_consent": True}
        )
    )
    assert response.status == 200
    from kiro_crew.memory_provider import consent

    assert consent.has_egress_consent(served.fixture)


@pytest.mark.asyncio
async def test_crew_memory_refuses_editor_writes_while_replaced(served) -> None:
    set_provider("replace", FIXTURE_ID)
    refusal = await handlers.provider_active_refusal("")
    assert refusal is not None and refusal.status == 409
    assert json.loads(refusal.text)["code"] == "memory_provider_active"
    create = await handlers.api_memory_provider_items_create(
        _owner(
            served,
            "/api/memory/provider/items",
            method="POST",
            body={"item": {"kind": "fact", "text": "x"}},
            query={"provider": "builtin"},
        )
    )
    assert create.status == 409
    set_provider("builtin", FIXTURE_ID)
    assert await handlers.provider_active_refusal("") is None


@pytest.mark.asyncio
async def test_the_owner_browses_and_edits_the_active_provider(served) -> None:
    set_provider("replace", FIXTURE_ID)
    created = await handlers.api_memory_provider_items_create(
        _owner(
            served,
            "/api/memory/provider/items",
            method="POST",
            body={"item": {"kind": "fact", "text": "Ships on Tuesdays."}},
        )
    )
    item_id = json.loads(created.text)["id"]
    listed = json.loads(
        (
            await handlers.api_memory_provider_items(_owner(served, "/api/memory/provider/items"))
        ).text
    )
    assert [i["text"] for i in listed["items"]] == ["Ships on Tuesdays."]
    deleted = await handlers.api_memory_provider_item(
        make_request(
            served.env.state,
            f"/api/memory/provider/items/{item_id}",
            method="DELETE",
            owner=True,
            session="",
            match_info={"id": item_id},
        )
    )
    assert json.loads(deleted.text)["outcome"] == "deleted"


def test_memory_admin_admits_the_provider_routes_only(monkeypatch) -> None:
    from types import SimpleNamespace

    from kiro_crew.apps import host_exec_grants, manager
    from kiro_crew.dashboard import token_auth_app_scope as scope

    manifest = SimpleNamespace(permissions=SimpleNamespace(api=[], memoryAdmin=True))
    monkeypatch.setattr(host_exec_grants, "host_exec_granted", lambda app: True)
    monkeypatch.setattr(manager, "get_app_manifest", lambda app: manifest)
    monkeypatch.setattr(manager, "staged_app_grants", lambda app, family, read: read())
    scope._app_perms_cache.clear()
    allow = scope._app_api_allowlist("memory-fixture")
    assert allow == ("/api/memory/provider",)
    assert scope._api_pattern_matches(allow[0], "/api/memory/provider/items")
    assert not scope._api_pattern_matches(allow[0], "/api/memory/semantic")
    scope._app_perms_cache.clear()


def test_install_approval_carries_the_memory_admin_entry() -> None:
    from kiro_crew.apps.manager_records import _declared_grant_entries
    from kiro_crew.apps.point_grants import grant_source

    def api(permissions: dict) -> list[str]:
        return _declared_grant_entries(grant_source({"permissions": permissions}))["api"]

    assert api({"api": [], "memoryAdmin": True}) == ["/api/memory/provider"]
    assert api({"api": [], "memoryAdmin": "true"}) == []
    assert api({"api": ["/api/x"]}) == ["/api/x"]


@pytest.mark.asyncio
async def test_an_edit_runs_crews_lesson_screens_before_put(served) -> None:
    set_provider("replace", FIXTURE_ID)
    created = await handlers.api_memory_provider_items_create(
        _owner(
            served,
            "/api/memory/provider/items",
            method="POST",
            body={"item": {"kind": "rule", "text": "Run ruff before push."}},
        )
    )
    item_id = json.loads(created.text)["id"]
    edited = await handlers.api_memory_provider_item(
        make_request(
            served.env.state,
            f"/api/memory/provider/items/{item_id}",
            method="PATCH",
            body={"item": {"kind": "rule", "text": "The current model identity is gpt-5.6-sol."}},
            owner=True,
            session="",
            match_info={"id": item_id},
        )
    )
    assert json.loads(edited.text)["reason"] == "content_screen"
    assert [i["text"] for i in served.items("default")] == ["Run ruff before push."]


@pytest.mark.asyncio
async def test_an_import_body_is_read_to_the_end_across_chunks() -> None:
    from types import SimpleNamespace

    chunks = [b"a" * 10, b"b" * 10, b""]

    async def read(_n: int) -> bytes:
        return chunks.pop(0)

    request = SimpleNamespace(content=SimpleNamespace(read=read))
    assert await migrations._read_capped(request, 100) == b"a" * 10 + b"b" * 10
    chunks[:] = [b"a" * 10, b"b" * 10, b""]
    assert len(await migrations._read_capped(request, 15)) == 20


def test_the_app_proxy_refuses_a_declared_provider_route(monkeypatch) -> None:
    from types import SimpleNamespace

    from kiro_crew.apps import routes_proxy

    decl = SimpleNamespace(route="memory-provider")
    manifest = SimpleNamespace(
        contributes=SimpleNamespace(memoryProviders=SimpleNamespace(usable=lambda: [decl]))
    )
    monkeypatch.setattr(routes_proxy, "get_app_manifest", lambda name: manifest)
    assert routes_proxy._is_memory_provider_route("fixture", "memory-provider/v1/put")
    assert routes_proxy._is_memory_provider_route("fixture", "./memory-provider//v1/search")
    assert not routes_proxy._is_memory_provider_route("fixture", "memory-provider-ui/state")
    assert not routes_proxy._is_memory_provider_route("fixture", "settings")


@pytest.mark.asyncio
async def test_every_provider_string_in_a_response_is_redacted(served) -> None:
    from kiro_crew.memory_provider.contract import NewItem, ProviderScope

    token = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    set_provider("replace", FIXTURE_ID)
    scope = ProviderScope("default", None, "persistent", "dashboard")
    served.fixture.blocking_put(
        scope, NewItem(kind="fact", text="Uses zsh.", title=token, source=token), "t"
    )
    listed = await handlers.api_memory_provider_items(_owner(served, "/api/memory/provider/items"))
    assert token not in listed.text
    assert "Uses zsh." in listed.text


def test_the_cli_reaches_the_lesson_routes_with_a_recognized_session(monkeypatch) -> None:
    import contextlib
    import io

    from kiro_crew import cli_commands_gateway_api, cli_memory_provider, loopback_http

    sent = {}

    @contextlib.contextmanager
    def fake_open(request, timeout):
        sent["headers"] = dict(request.header_items())
        sent["url"] = request.full_url
        yield io.BytesIO(b'{"ok": true}')

    monkeypatch.setattr(loopback_http, "loopback_urlopen", fake_open)
    monkeypatch.setattr(cli_commands_gateway_api, "_client_port", lambda port: 5555)
    monkeypatch.setattr(cli_commands_gateway_api, "_internal_secret", lambda port: "s")
    assert cli_memory_provider._call("POST", "/api/lessons", {"rule": "x"}, None) == {"ok": True}
    assert sent["headers"]["X-session-key"] == "dashboard:ui"
    assert sent["url"] == "http://127.0.0.1:5555/api/lessons"


@pytest.mark.asyncio
async def test_meta_keys_are_redacted_and_the_export_stays_valid_jsonl(served) -> None:
    from kiro_crew.memory_provider import migration
    from kiro_crew.memory_provider.contract import NewItem, ProviderScope

    token = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    set_provider("replace", FIXTURE_ID)
    scope = ProviderScope("default", None, "persistent", "dashboard")
    for n, meta in enumerate(({token: "x"}, {"note": f"password={token}"})):
        served.fixture.blocking_put(
            scope, NewItem(kind="fact", text=f"Fact {n}.", meta=meta), f"m{n}"
        )
    listed = await handlers.api_memory_provider_items(_owner(served, "/api/memory/provider/items"))
    assert token not in listed.text
    from kiro_crew.dashboard.handlers._shared import _redact_memory_field

    lines = [
        line
        async for line in migration.export_lines(
            served.fixture,
            FIXTURE_ID,
            scope,
            scrub=lambda r: _redact_memory_field(handlers._scrub_meta_keys(r)),
        )
    ]
    assert all(line.endswith("\n") for line in lines)
    records = [json.loads(line) for line in lines]
    # The header, two items and the trailer.
    assert len(records) == 4 and token not in json.dumps(records)
    assert records[-1]["trailer"] is True and records[-1]["items"] == 2
