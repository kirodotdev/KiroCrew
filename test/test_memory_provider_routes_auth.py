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
from memory_provider_helpers import (  # noqa: F401
    FIXTURE_ID,
    READONLY_ID,
    fixture_provider,
    memory_provider_preview_on,
    set_provider,
)

from kiro_crew.config import loader
from kiro_crew.dashboard.handlers import memory_provider as handlers
from kiro_crew.dashboard.handlers import memory_provider_migrations as migrations
from kiro_crew.testing.synthetic_credentials import github_classic

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


def test_the_catalog_row_discloses_memory_admin_before_install() -> None:
    # The install prompt reads the catalog row; install approves grant_source's prefix.
    from kiro_crew.apps.official_catalog import _disclosed_permissions_manifest

    assert _disclosed_permissions_manifest({"permissions": {"memoryAdmin": True}}) == {
        "permissions": {"memoryAdmin": True}
    }
    assert _disclosed_permissions_manifest({"permissions": {"memoryAdmin": "true"}}) is None


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

    from kiro_crew.apps import routes, routes_proxy

    decl = SimpleNamespace(route="memory-provider")
    manifest = SimpleNamespace(
        contributes=SimpleNamespace(memoryProviders=SimpleNamespace(usable=lambda: [decl]))
    )
    # Patched through the facade: it is the one place every holder of the name follows.
    monkeypatch.setattr(routes, "get_app_manifest", lambda name: manifest)
    assert routes_proxy._is_memory_provider_route("fixture", "memory-provider/v1/put")
    assert routes_proxy._is_memory_provider_route("fixture", "./memory-provider//v1/search")
    assert not routes_proxy._is_memory_provider_route("fixture", "memory-provider-ui/state")
    assert not routes_proxy._is_memory_provider_route("fixture", "settings")


@pytest.mark.asyncio
async def test_every_provider_string_in_a_response_is_redacted(served) -> None:
    from kiro_crew.memory_provider.contract import NewItem, ProviderScope

    token = github_classic(seed=13)
    set_provider("replace", FIXTURE_ID)
    scope = ProviderScope("default", None, "persistent", "dashboard")
    served.fixture.blocking_put(
        scope, NewItem(kind="fact", text="Uses zsh.", title=token, source=token), "t"
    )
    listed = await handlers.api_memory_provider_items(_owner(served, "/api/memory/provider/items"))
    assert token not in listed.text
    assert "Uses zsh." in listed.text


@pytest.mark.asyncio
async def test_an_app_cannot_replace_an_item_it_only_saw_redacted(served, monkeypatch) -> None:
    from kiro_crew.memory_provider.contract import NewItem, ProviderScope

    monkeypatch.setattr(handlers, "_app_holds_memory_admin", lambda app: True)
    token = "glpat-" + "A1b2C3d4E5f6G7h8I9j0"
    set_provider("replace", FIXTURE_ID)
    scope = ProviderScope("default", None, "persistent", "dashboard")
    served.fixture.blocking_put(scope, NewItem(kind="fact", text="Uses zsh.", title=token), "t1")
    served.fixture.blocking_put(scope, NewItem(kind="fact", text="Uses vim."), "t2")
    served.fixture.blocking_put(
        scope, NewItem(kind="fact", text="Uses tmux.", meta={token: "clean value"}), "t3"
    )
    listed = await handlers.api_memory_provider_items(
        _app_request(served, "/api/memory/provider/items")
    )
    assert token not in listed.text
    ids = {i["text"]: i["id"] for i in json.loads(listed.text)["items"]}

    async def patch(item_id: str) -> handlers.web.Response:
        request = _app_request(
            served,
            f"/api/memory/provider/items/{item_id}",
            method="PATCH",
            body={"item": {"kind": "fact", "text": "Uses bash."}},
        )
        request.match_info.update({"id": item_id})
        return await handlers.api_memory_provider_item(request)

    held = await patch(ids["Uses zsh."])
    assert held.status == 409 and json.loads(held.text)["code"] == "document_holds_secret"
    held = await patch(ids["Uses tmux."])
    assert held.status == 409 and json.loads(held.text)["code"] == "document_holds_secret"
    stored = await served.fixture.get(scope, ids["Uses tmux."])
    assert stored is not None and stored.meta[token] == "clean value"
    assert stored.text == "Uses tmux."
    assert (await patch(ids["Uses vim."])).status == 200


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


def test_an_agent_shell_learn_add_is_origin_gated_on_the_provider_path(monkeypatch) -> None:
    """The CLI's provider branch demotes an agent's own standing rule, as the builtin one does."""
    import argparse

    from kiro_crew import cli_commands_learn, cli_memory_provider
    from kiro_crew.cloud import aws
    from kiro_crew.config import loader as cfg_loader

    cfg = cfg_loader.KiroCrewConfig.load()
    cfg.memory.lesson_origin_gate = "enforce"
    monkeypatch.setattr(cli_commands_learn.KiroCrewConfig, "load", classmethod(lambda c: cfg))
    monkeypatch.setattr(cli_memory_provider, "provider_replaces", lambda: True)
    monkeypatch.setattr(aws, "_in_agent_session", lambda: True)
    sent: dict = {}
    monkeypatch.setattr(
        cli_memory_provider, "learn", lambda action, add, query, port, scope: sent.update(add)
    )
    args = argparse.Namespace(
        learn_action="add", rule="Always use tabs.", category="tool", applies="always"
    )
    cli_commands_learn._learn(args)
    assert sent["rule"] == "Always use tabs." and sent["applies"] == "on_topic"


@pytest.mark.asyncio
async def test_meta_keys_are_redacted_and_the_export_stays_valid_jsonl(served) -> None:
    from kiro_crew.memory_provider import migration
    from kiro_crew.memory_provider.contract import NewItem, ProviderScope

    token = github_classic(seed=13)
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


def test_the_generic_config_editor_cannot_switch_memory() -> None:
    from kiro_crew.dashboard.handlers.config_all_settings import READONLY_REASONS

    for key in ("mode", "id", "switched_at", "stop_learning_ack"):
        assert f"memory.provider.{key}" in READONLY_REASONS


@pytest.mark.asyncio
async def test_an_item_deleted_and_added_again_is_stored_again(served) -> None:
    set_provider("replace", FIXTURE_ID)
    body = {"item": {"kind": "fact", "text": "Uses zsh."}}
    first = await handlers.api_memory_provider_items_create(
        _owner(served, "/api/memory/provider/items", method="POST", body=body)
    )
    item_id = json.loads(first.text)["id"]
    await handlers.api_memory_provider_item(
        make_request(
            served.env.state,
            f"/api/memory/provider/items/{item_id}",
            method="DELETE",
            owner=True,
            session="",
            match_info={"id": item_id},
        )
    )
    assert served.items("default") == []
    await handlers.api_memory_provider_items_create(
        _owner(served, "/api/memory/provider/items", method="POST", body=body)
    )
    assert [i["text"] for i in served.items("default")] == ["Uses zsh."]


def test_memory_admin_needs_its_own_approved_grant(monkeypatch) -> None:
    from types import SimpleNamespace

    from kiro_crew.apps import manager

    manifest = SimpleNamespace(permissions=SimpleNamespace(memoryAdmin=True))
    monkeypatch.setattr(manager, "get_app_manifest", lambda app: manifest)
    monkeypatch.setattr(manager, "is_app_enabled", lambda app: True)
    # An approved parent such as /api/memory does not stand in for the exact grant.
    monkeypatch.setattr(manager, "staged_app_grants", lambda app, family, declared: [])
    assert not handlers._app_holds_memory_admin("memory-fixture")
    monkeypatch.setattr(
        manager, "staged_app_grants", lambda app, family, declared: list(declared())
    )
    assert handlers._app_holds_memory_admin("memory-fixture")


# ── provider benchmarks ──────────────────────────────────────────────────────


def _bench_body(**extra):
    return {"suite": "isolation", "providers": ["baseline:recency"], **extra}


@pytest.mark.asyncio
async def test_an_app_plans_a_benchmark_but_never_runs_one(served, monkeypatch) -> None:
    from kiro_crew.dashboard.handlers import memory_provider_bench as bench

    monkeypatch.setattr(handlers, "_app_holds_memory_admin", lambda app: True)
    planned = await bench.api_bench_runs(
        _app_request(served, "/api/memory/provider/bench/runs", method="POST", body=_bench_body())
    )
    body = json.loads(planned.text)
    assert planned.status == 200 and body["state"] == "planned"
    refused = await bench.api_bench_runs(
        _app_request(
            served,
            "/api/memory/provider/bench/runs",
            method="POST",
            body=_bench_body(dry_run=False),
        )
    )
    assert refused.status == 403 and json.loads(refused.text)["code"] == "owner_only"
    for call, method in (
        (bench.api_bench_run_resume, "POST"),
        (bench.api_bench_run, "DELETE"),
    ):
        request = _app_request(
            served, f"/api/memory/provider/bench/runs/{body['run_id']}", method=method
        )
        request.match_info.update({"id": body["run_id"]})
        with pytest.raises(handlers.web.HTTPForbidden):
            await call(request)
    with pytest.raises(handlers.web.HTTPForbidden):
        await bench.api_bench_evalsets(
            _app_request(served, "/api/memory/provider/bench/evalsets", method="POST", body={})
        )
    # The app sees its own plan; another app sees nothing of it.
    listed = await bench.api_bench_runs(_app_request(served, "/api/memory/provider/bench/runs"))
    assert [r["id"] for r in json.loads(listed.text)["runs"]] == [body["run_id"]]
    other = await bench.api_bench_runs(
        _app_request(served, "/api/memory/provider/bench/runs", app="another-app")
    )
    assert json.loads(other.text)["runs"] == []


def test_an_app_reads_a_run_the_owner_started_but_not_another_apps_plan() -> None:
    # The owner starts every run an app asks for, from the host sheet.
    from types import SimpleNamespace

    from kiro_crew.dashboard.handlers.memory_provider_bench import _own

    started = SimpleNamespace(caller="owner")
    planned = SimpleNamespace(caller="app:memory")
    assert _own(started, "app:memory") and _own(started, "app:other")
    assert _own(planned, "app:memory") and not _own(planned, "app:other")
    assert _own(planned, "owner")


@pytest.mark.asyncio
async def test_an_agent_token_opens_no_benchmark_route(served) -> None:
    from kiro_crew.dashboard.handlers import memory_provider_bench as bench

    agent = make_request(
        served.env.state,
        "/api/memory/provider/bench/runs",
        method="POST",
        body=_bench_body(),
        internal=True,
    )
    with pytest.raises(handlers.web.HTTPForbidden):
        await bench.api_bench_runs(agent)


@pytest.mark.asyncio
async def test_own_memory_needs_a_local_provider_or_an_exact_acknowledgement(served) -> None:
    from kiro_crew.dashboard.handlers import memory_provider_bench as bench
    from kiro_crew.eval.bench import provider_run
    from kiro_crew.eval.bench.own_memory import OWN_SUITE

    options = provider_run.RunOptions.from_request(
        {"suite": "own:e_000000000000", "providers": [FIXTURE_ID]}
    )
    plan = {
        "items": 12,
        "providers": [
            {
                "id": FIXTURE_ID,
                "egress": "remote",
                "cleanup": "reset",
                # The provider's own version differs; the acknowledgement names the app's.
                "version": "0.9",
                "app_version": "1.0.0",
            }
        ],
    }
    assert "exact item count (12)" in bench._egress_refusal(options, OWN_SUITE, plan)
    options.egress_ack = {FIXTURE_ID: {"app_version": "1.0.0", "items": 11}}
    assert bench._egress_refusal(options, OWN_SUITE, plan)
    options.egress_ack = {FIXTURE_ID: {"app_version": "1.0.0", "items": 12}}
    assert bench._egress_refusal(options, OWN_SUITE, plan) == ""
    plan["providers"][0]["cleanup"] = "delete"
    assert "cannot reset" in bench._egress_refusal(options, OWN_SUITE, plan)
    plan["providers"][0].update({"egress": "local", "cleanup": "reset"})
    assert (
        bench._egress_refusal(
            provider_run.RunOptions.from_request(
                {"suite": "own:e_000000000000", "providers": [FIXTURE_ID]}
            ),
            OWN_SUITE,
            plan,
        )
        == ""
    )


@pytest.mark.asyncio
async def test_conformance_refuses_crews_own_memory(served) -> None:
    response = await handlers.api_memory_provider_conformance(
        _owner(
            served, "/api/memory/provider/conformance", method="POST", body={"provider": "builtin"}
        )
    )
    assert response.status == 400
    assert json.loads(response.body)["code"] == "not_an_app_provider"


def test_a_single_store_provider_is_refused_a_benchmark(served, monkeypatch) -> None:
    import dataclasses

    from kiro_crew.dashboard.handlers import memory_provider_bench as bench
    from kiro_crew.eval.bench import provider_run

    caps = served.fixture.capabilities()
    single = dataclasses.replace(
        caps, multi_store=False, bench=dataclasses.replace(caps.bench, scratch=False)
    )
    monkeypatch.setattr(served.fixture, "capabilities", lambda: single)
    options = provider_run.RunOptions.from_request(
        {"suite": "isolation", "providers": [FIXTURE_ID]}
    )
    targets, refusal = bench._targets("r1", options, "owner", "isolation")
    assert targets == {} and "keeps one memory" in refusal
