"""``GET /v1/crew/capabilities`` against the Crew API contract."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_crew_api_app_credential as kit
import yaml
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from test_shared_crew_teammate_owner_access import TEAMMATES, _publish
from token_auth_helpers import clear_nonces  # noqa: F401 -- autouse fixture

from kiro_crew.dashboard import crew_api_auth
from kiro_crew.dashboard import crew_api_instance as instance
from kiro_crew.dashboard import crew_api_models
from kiro_crew.dashboard import crew_api_responses as responses
from kiro_crew.dashboard import local_authorization as la
from kiro_crew.dashboard.crew_api_auth import OWNER_ONLY_PUBLIC_ROUTES
from kiro_crew.dashboard.crew_api_scopes import CREDENTIAL_KEY, ROUTES
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

# The fixtures of the app credential tests, registered here under their own names.
audit, clock, store = kit.audit, kit.clock, kit.store

OWNER = "U_OWNER"
PATH = "/v1/crew/capabilities"
_SPEC = Path(__file__).resolve().parents[1] / "docs/request-for-change/assets/crew-api"


def _request(*, app_claim: str = "") -> web.Request:
    app = web.Application()
    app["state"] = SimpleNamespace(owner_id=OWNER)
    request = make_mocked_request("GET", PATH, app=app)
    request["app"] = app_claim
    request["user"] = OWNER
    return request


def _catalog(monkeypatch, ids: list[str]) -> None:
    async def _models() -> list[dict[str, str]]:
        return [{"id": model_id} for model_id in ids]

    monkeypatch.setattr(instance, "usable_models_off_loop", _models)


def _schema() -> dict:
    components = yaml.safe_load((_SPEC / "components.yaml").read_text(encoding="utf-8"))
    return components["components"]["schemas"]["Capabilities"]


@pytest.mark.asyncio
async def test_the_owner_gets_the_capabilities_this_gateway_serves(monkeypatch) -> None:
    _catalog(monkeypatch, ["scout", "writer"])
    resp = await instance.api_capabilities(_request())
    assert resp.status == 200
    assert resp.headers["X-Request-Id"].startswith("req_")
    assert resp.headers["Crew-API-Version"] == "1.0"
    agent = {"input_modalities": ["text"], "structured_output": False, "reasoning_summary": []}
    assert json.loads(resp.body) == {
        "api_version": {"current": "1.0"},
        "features": {name: False for name in instance.FEATURES},
        "agents": {"scout": agent, "writer": agent},
        "limits": {"max_input_bytes": responses._MAX_PROMPT_BYTES},
    }


@pytest.mark.asyncio
async def test_the_answer_holds_only_what_the_contract_names(monkeypatch) -> None:
    _catalog(monkeypatch, ["scout"])
    body = json.loads((await instance.api_capabilities(_request())).body)
    schema = _schema()
    assert set(schema["required"]) <= set(body)
    assert set(body) <= set(schema["properties"])
    assert set(body["features"]) == set(schema["properties"]["features"]["properties"])
    agent_fields = schema["properties"]["agents"]["additionalProperties"]["properties"]
    assert set(body["agents"]["scout"]) <= set(agent_fields)
    assert set(body["limits"]) <= set(schema["properties"]["limits"]["properties"])


def test_no_feature_is_reported_that_no_route_serves() -> None:
    # A feature joins SERVED_FEATURES in the change that serves it, and this pin
    # moves with it.
    assert instance.SERVED_FEATURES == frozenset()
    assert instance.SERVED_FEATURES <= set(instance.FEATURES)


@pytest.mark.asyncio
async def test_an_app_token_is_refused_and_nothing_is_read(monkeypatch) -> None:
    async def _no_read() -> list[dict[str, str]]:
        raise AssertionError("a refused caller must not reach the catalog")

    monkeypatch.setattr(instance, "usable_models_off_loop", _no_read)
    resp = await instance.api_capabilities(_request(app_claim="someapp"))
    error = json.loads(resp.body)["error"]
    assert resp.status == 403
    assert (error["type"], error["code"]) == ("permission_error", "owner_only")
    assert error["request_id"] == resp.headers["X-Request-Id"]


def test_the_route_is_owner_only_under_its_audit_operation() -> None:
    assert OWNER_ONLY_PUBLIC_ROUTES[PATH] == "crew_api.capabilities.read"


INSTANCE = "0b0e7f4e-7c1c-4c0a-9a55-6a1f3d6f2b10"


def _info_request(*, app_claim: str = "") -> web.Request:
    app = web.Application()
    app["state"] = SimpleNamespace(owner_id=OWNER)
    request = make_mocked_request("GET", instance.INFO_PATH, app=app)
    request["app"] = app_claim
    request["user"] = OWNER
    return request


@pytest.mark.asyncio
async def test_the_owner_reads_the_instance(monkeypatch, tmp_path) -> None:
    from kiro_crew.relay_client import lease

    seen: list[Path] = []

    def _instance_id(home: Path) -> str:
        seen.append(home)
        return INSTANCE

    monkeypatch.setattr(instance, "data_home", lambda: tmp_path)
    monkeypatch.setattr(lease, "crew_instance_id", _instance_id)
    resp = await instance.api_info(_info_request())
    assert resp.status == 200
    assert resp.headers["Crew-API-Version"] == "1.0"
    body = json.loads(resp.body)
    assert body == {"id": INSTANCE, "form": "local", "api_version": "1.0"}
    assert seen == [tmp_path]
    components = yaml.safe_load((_SPEC / "components.yaml").read_text(encoding="utf-8"))
    schema = components["components"]["schemas"]["InstanceInfo"]
    assert set(schema["required"]) <= set(body) <= set(schema["properties"])
    assert body["form"] in schema["properties"]["form"]["enum"]


@pytest.mark.asyncio
async def test_the_instance_id_is_the_one_the_relay_lease_keeps(monkeypatch, tmp_path) -> None:
    from kiro_crew.relay_client.lease import crew_instance_id

    monkeypatch.setattr(instance, "data_home", lambda: tmp_path)
    first = json.loads((await instance.api_info(_info_request())).body)["id"]
    second = json.loads((await instance.api_info(_info_request())).body)["id"]
    assert first == second == crew_instance_id(tmp_path)


@pytest.mark.asyncio
async def test_an_app_token_reads_no_instance(monkeypatch) -> None:
    def _no_read() -> str:
        raise AssertionError("a refused caller must not reach the instance id")

    monkeypatch.setattr(instance, "_instance_id", _no_read)
    resp = await instance.api_info(_info_request(app_claim="someapp"))
    error = json.loads(resp.body)["error"]
    assert resp.status == 403
    assert (error["type"], error["code"]) == ("permission_error", "owner_only")
    assert OWNER_ONLY_PUBLIC_ROUTES[instance.INFO_PATH] == "crew_api.info.resolve"


@pytest.mark.asyncio
@pytest.mark.parametrize("who", TEAMMATES)
@pytest.mark.parametrize("path", [PATH, instance.INFO_PATH])
async def test_a_person_the_owner_let_in_is_refused_and_nothing_is_read(
    monkeypatch, who: str, path: str
) -> None:
    def _no_read(*_args: object) -> None:
        raise AssertionError("a refused caller must not reach the catalog or the instance id")

    monkeypatch.setenv("KIROCREW_SHARED_CREW_PREVIEW", "1")
    monkeypatch.setattr(instance, "usable_models_off_loop", _no_read)
    monkeypatch.setattr(instance, "_instance_id", _no_read)
    request = _request() if path == PATH else _info_request()
    _publish(request, who)
    handler = instance.api_capabilities if path == PATH else instance.api_info
    resp = await handler(request)
    error = json.loads(resp.body)["error"]
    assert resp.status == 403
    assert (error["type"], error["code"]) == ("permission_error", "owner_only")


def test_importing_the_route_module_does_not_load_the_relay_lease(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "crew"))
    monkeypatch.setenv("KIRO_HOME", str(tmp_path / "kiro"))
    monkeypatch.setenv("PYTHONPATH", str(_SPEC.parents[3] / "src"))
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import kiro_crew.dashboard.routes.crew_api; "
            "print('kiro_crew.relay_client.lease' in sys.modules)",
        ],
        cwd=tmp_path,
        capture_output=True,
        encoding="utf-8",
        timeout=FALLBACK_TIMEOUT_SECS,
        check=True,
    )
    assert result.stdout.strip() == "False"


def test_the_instance_id_is_made_under_the_lock_two_first_readers_share(
    monkeypatch, tmp_path
) -> None:
    from kiro_crew.relay_client import lease

    held: list[bool] = []
    real = lease.uuid.uuid4

    def _made_under_the_lock():
        held.append(lease._INSTANCE_ID_LOCK.locked())
        return real()

    monkeypatch.setattr(lease.uuid, "uuid4", _made_under_the_lock)
    first = lease.crew_instance_id(tmp_path)
    assert held == [True]
    assert lease.crew_instance_id(tmp_path) == first
    assert held == [True]


def test_an_instance_id_that_cannot_be_saved_is_the_same_for_every_caller(
    monkeypatch, tmp_path
) -> None:
    import kiro_crew.atomic_write as atomic_write_mod
    from kiro_crew.relay_client import lease

    def _refuse(*args, **kwargs):
        raise OSError("read-only data home")

    monkeypatch.setattr(atomic_write_mod, "atomic_write", _refuse)
    monkeypatch.setattr(lease, "_UNSAVED_INSTANCE_IDS", {})
    first = lease.crew_instance_id(tmp_path)
    assert lease.crew_instance_id(tmp_path) == first
    assert not (tmp_path / lease.INSTANCE_ID_FILE).exists()


# ── an app credential: served on both routes, from its grant alone ───────────

BOTH = [PATH, instance.INFO_PATH]


def _as_app(request: web.Request, credential: la.LocalCredential) -> web.Request:
    """*request* as token auth publishes a verified app credential on it."""
    request["user"] = f"app:{credential.grant_id}"
    request["app"] = f"local-grant:{credential.grant_id}"
    request["is_dashboard_user"] = False
    request[CREDENTIAL_KEY] = credential
    return request


def _app_request(store: la.LocalAuthorization, clock, path: str) -> web.Request:
    found = store.verify(kit._grant(store, clock)["access_token"])
    assert found is not None
    return _as_app(_request() if path == PATH else _info_request(), found)


def _owner_catalog_is_never_read(monkeypatch) -> list[str]:
    """Stub every read of the owner's catalog and return the list of reads made."""
    reads: list[str] = []

    async def _off_loop() -> list[dict[str, str]]:
        reads.append("usable_models_off_loop")
        return [{"id": "the-owners-bot"}]

    def _sync() -> list[dict[str, str]]:
        reads.append("usable_models")
        return [{"id": "the-owners-bot"}]

    monkeypatch.setattr(instance, "usable_models_off_loop", _off_loop)
    monkeypatch.setattr(crew_api_models, "usable_models_off_loop", _off_loop)
    monkeypatch.setattr(crew_api_models, "usable_models", _sync)
    return reads


@pytest.mark.asyncio
@pytest.mark.parametrize("path", BOTH)
@pytest.mark.parametrize("scope", ["responses:write", "artifacts:read"])
async def test_token_auth_admits_any_app_credential_on_both_routes(
    store, clock, audit, path, scope
) -> None:
    tokens = kit._grant(store, clock, scope=scope)
    resp, request, ran = await kit._send(path=path, headers=kit._bearer(tokens["access_token"]))
    assert (resp.status, ran) == (200, True)
    row = audit.log_api_access.call_args.kwargs
    assert (row["caller"], row["outcome"], row["error"]) == (
        f"app:{tokens['grant_id']}",
        "allowed",
        "",
    )
    published = kit._published(request)
    assert published[CREDENTIAL_KEY].grant_id == tokens["grant_id"]
    assert published["is_dashboard_user"] is False


def test_both_routes_serve_an_app_and_need_no_scope() -> None:
    for key in (instance.CAPABILITIES_KEY, instance.INFO_KEY):
        assert key in crew_api_auth.APP_SERVED_ROUTES
        assert ROUTES[key].scopes == frozenset({"any"})
        assert key[1] not in crew_api_auth.app_owner_only_routes()


@pytest.mark.asyncio
async def test_an_apps_capabilities_come_from_its_grant_and_read_no_owner_catalog(
    monkeypatch, store, clock
) -> None:
    reads = _owner_catalog_is_never_read(monkeypatch)
    request = _app_request(store, clock, PATH)
    resp = await instance.api_capabilities(request)
    assert resp.status == 200
    assert resp.headers["Crew-API-Version"] == "1.0"
    body = json.loads(resp.body)
    assert body == {
        "api_version": {"current": "1.0"},
        "features": {name: False for name in instance.FEATURES},
        # A grant runs nothing today, so GET /v1/models gives it an empty list too.
        "agents": {},
        "limits": {"max_input_bytes": responses._MAX_PROMPT_BYTES},
    }
    assert "quota" not in body
    assert reads == []


@pytest.mark.asyncio
async def test_an_apps_agents_are_the_rows_its_model_list_gives(monkeypatch, store, clock) -> None:
    reads = _owner_catalog_is_never_read(monkeypatch)
    request = _app_request(store, clock, PATH)
    asked: list[la.LocalCredential] = []

    def _for_grant(credential: la.LocalCredential) -> list[dict[str, str]]:
        asked.append(credential)
        return [{"id": "helper"}]

    monkeypatch.setattr(instance, "models_for_grant", _for_grant)
    body = json.loads((await instance.api_capabilities(request)).body)
    assert list(body["agents"]) == ["helper"]
    assert asked == [request[CREDENTIAL_KEY]]
    assert reads == []


@pytest.mark.asyncio
async def test_an_app_and_the_owner_read_the_same_instance_id(
    monkeypatch, tmp_path, store, clock
) -> None:
    from kiro_crew.relay_client import lease

    home = tmp_path / "data-home"
    home.mkdir()
    monkeypatch.setattr(instance, "data_home", lambda: home)
    # The app calls first, so its call is the one that makes and keeps the id.
    assert not (home / lease.INSTANCE_ID_FILE).exists()
    app_resp = await instance.api_info(_app_request(store, clock, instance.INFO_PATH))
    assert app_resp.status == 200
    assert (home / lease.INSTANCE_ID_FILE).exists()
    owner_resp = await instance.api_info(_info_request())
    assert json.loads(app_resp.body) == json.loads(owner_resp.body)
    assert json.loads(app_resp.body)["id"] == lease.crew_instance_id(home)


@pytest.mark.asyncio
async def test_an_id_that_cannot_be_saved_is_answered_alike_to_an_app_and_the_owner(
    monkeypatch, tmp_path, store, clock
) -> None:
    import kiro_crew.atomic_write as atomic_write_mod
    from kiro_crew.relay_client import lease

    def _refuse(*args, **kwargs):
        raise OSError(f"read-only data home {tmp_path}")

    home = tmp_path / "data-home"
    home.mkdir()
    monkeypatch.setattr(instance, "data_home", lambda: home)
    monkeypatch.setattr(atomic_write_mod, "atomic_write", _refuse)
    monkeypatch.setattr(lease, "_UNSAVED_INSTANCE_IDS", {})
    app_resp = await instance.api_info(_app_request(store, clock, instance.INFO_PATH))
    owner_resp = await instance.api_info(_info_request())
    assert (app_resp.status, owner_resp.status) == (200, 200)
    assert json.loads(app_resp.body) == json.loads(owner_resp.body)
    assert str(tmp_path) not in app_resp.body.decode("utf-8")


@pytest.mark.asyncio
@pytest.mark.parametrize("path", BOTH)
async def test_a_failing_read_is_the_same_error_for_an_app_and_the_owner(
    monkeypatch, tmp_path, store, clock, path
) -> None:
    def _fail(*_args: object) -> None:
        raise web.HTTPInternalServerError()

    async def _fail_off_loop() -> None:
        raise web.HTTPInternalServerError()

    monkeypatch.setattr(instance, "_instance_id", _fail)
    monkeypatch.setattr(instance, "usable_models_off_loop", _fail_off_loop)
    monkeypatch.setattr(instance, "models_for_grant", _fail)
    handler = instance.api_capabilities if path == PATH else instance.api_info
    app_resp = await handler(_app_request(store, clock, path))
    owner_resp = await handler(_request() if path == PATH else _info_request())
    assert (app_resp.status, owner_resp.status) == (500, 500)
    app_error, owner_error = (json.loads(r.body)["error"] for r in (app_resp, owner_resp))
    app_error.pop("request_id"), owner_error.pop("request_id")
    assert app_error == owner_error
    assert str(tmp_path) not in app_resp.body.decode("utf-8")


@pytest.mark.asyncio
@pytest.mark.parametrize("path", BOTH)
async def test_a_revoked_grants_token_is_401_before_either_handler(store, clock, path) -> None:
    tokens = kit._grant(store, clock)
    assert store.revoke_grant(tokens["grant_id"], caller="U_HOST_OWNER") is True
    resp, _, ran = await kit._send(path=path, headers=kit._bearer(tokens["access_token"]))
    kit._assert_refused(resp, ran, "token_revoked", "The grant was revoked.")


@pytest.mark.asyncio
@pytest.mark.parametrize("path", BOTH)
async def test_an_expired_token_is_401_before_either_handler(store, clock, path) -> None:
    tokens = kit._grant(store, clock)
    clock.now += la.ACCESS_TTL_SECS + 1
    resp, _, ran = await kit._send(path=path, headers=kit._bearer(tokens["access_token"]))
    kit._assert_401(resp, ran)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", BOTH)
async def test_an_app_credential_through_a_forwarder_is_401_on_both_routes(
    store, clock, path
) -> None:
    tokens = kit._grant(store, clock)
    headers = {**kit._bearer(tokens["access_token"]), "X-Forwarded-For": "203.0.113.7"}
    resp, request, ran = await kit._send(path=path, headers=headers)
    kit._assert_401(resp, ran)
    assert CREDENTIAL_KEY not in kit._published(request)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", BOTH)
async def test_no_credential_is_401_on_both_routes(store, path) -> None:
    resp, _, ran = await kit._send(path=path)
    kit._assert_401(resp, ran)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", BOTH)
async def test_a_request_marked_as_an_app_without_a_verified_credential_is_refused(
    monkeypatch, path
) -> None:
    """Only the credential token auth verified skips the owner check, never a claim."""

    def _no_read(*_args: object) -> None:
        raise AssertionError("a refused caller must not reach the catalog or the instance id")

    monkeypatch.setattr(instance, "usable_models_off_loop", _no_read)
    monkeypatch.setattr(instance, "models_for_grant", _no_read)
    monkeypatch.setattr(instance, "_instance_id", _no_read)
    request = _request() if path == PATH else _info_request()
    request["user"] = "app:grt_made_up"
    request["app"] = "local-grant:grt_made_up"
    request["is_dashboard_user"] = False
    handler = instance.api_capabilities if path == PATH else instance.api_info
    resp = await handler(request)
    error = json.loads(resp.body)["error"]
    assert resp.status == 403
    assert (error["type"], error["code"]) == ("permission_error", "owner_only")
