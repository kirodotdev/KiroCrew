"""``GET /v1/crew/capabilities`` against the Crew API contract."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from test_shared_crew_teammate_owner_access import TEAMMATES, _publish

from kiro_crew.dashboard import crew_api_instance as instance
from kiro_crew.dashboard import crew_api_responses as responses
from kiro_crew.dashboard.crew_api_auth import OWNER_ONLY_PUBLIC_ROUTES
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

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
