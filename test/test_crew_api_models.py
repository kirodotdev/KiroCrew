"""``GET /v1/models`` against the Crew API contract: the ids a turn can name."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.agent_discovery_parts.spec_fields import AgentInfo
from kiro_crew.dashboard import crew_api_models as models
from kiro_crew.dashboard.handlers.core import _SENSITIVE_MASK
from kiro_crew.testing.synthetic_credentials import github_classic

OWNER = "U_OWNER"
SECRET = github_classic(seed=13)


def _request(*, app_claim: str = "") -> web.Request:
    app = web.Application()
    app["state"] = SimpleNamespace(owner_id=OWNER)
    request = make_mocked_request("GET", "/v1/models", app=app)
    request["app"] = app_claim
    request["user"] = OWNER
    return request


def _catalog(monkeypatch, bots: list[str], templates: list[tuple[str, str]]) -> None:
    cfg = SimpleNamespace(agents={name: object() for name in bots})
    monkeypatch.setattr(models.KiroCrewConfig, "load", lambda: cfg)
    monkeypatch.setattr(
        models,
        "list_agents",
        lambda: [
            AgentInfo(name=name, filename=f"{name}.json", description=text, model="auto")
            for name, text in templates
        ],
    )


@pytest.mark.asyncio
async def test_the_owner_gets_the_list_in_the_openai_shape(monkeypatch) -> None:
    import json

    _catalog(monkeypatch, [], [("writer", "writes")])
    resp = await models.api_models(_request())
    assert resp.status == 200
    assert resp.headers["X-Request-Id"].startswith("req_")
    assert resp.headers["Crew-API-Version"] == "1.0"
    assert json.loads(resp.body) == {
        "object": "list",
        "data": [
            {
                "id": "writer",
                "object": "model",
                "owned_by": "crew",
                "crew": {"kind": "agent", "name": "writer", "description": "writes"},
            }
        ],
    }


@pytest.mark.asyncio
async def test_an_app_token_is_refused_and_nothing_is_read(monkeypatch) -> None:
    import json

    def _no_read():
        raise AssertionError("a refused caller must not reach the catalog")

    monkeypatch.setattr(models, "usable_models", _no_read)
    resp = await models.api_models(_request(app_claim="someapp"))
    error = json.loads(resp.body)["error"]
    assert resp.status == 403
    assert (error["type"], error["code"]) == ("permission_error", "owner_only")
    assert error["request_id"] == resp.headers["X-Request-Id"]


@pytest.mark.asyncio
async def test_a_catalog_error_is_a_contract_error(monkeypatch) -> None:
    import json

    def _unavailable():
        raise web.HTTPServiceUnavailable(reason="catalog unavailable")

    monkeypatch.setattr(models, "usable_models", _unavailable)
    resp = await models.api_models(_request())
    error = json.loads(resp.body)["error"]
    assert resp.status == 503
    assert error["type"] == "server_error"
    assert error["request_id"] == resp.headers["X-Request-Id"]
    assert resp.headers["Crew-API-Version"] == "1.0"


def test_a_bot_shadows_a_template_of_its_name_and_an_unsafe_name_is_left_out(monkeypatch) -> None:
    _catalog(monkeypatch, ["scout", "bad\x1bname"], [("scout", "template"), ("writer", "")])
    assert [(row["id"], row["crew"]) for row in models.usable_models()] == [
        ("scout", {"kind": "bot", "name": "scout"}),
        ("writer", {"kind": "agent", "name": "writer"}),
    ]


def test_a_credential_shaped_template_description_is_masked(monkeypatch) -> None:
    _catalog(monkeypatch, [], [("atlas", f"key {SECRET}")])
    assert [row["crew"] for row in models.usable_models()] == [
        {"kind": "agent", "name": "atlas", "description": _SENSITIVE_MASK}
    ]


def test_a_template_whose_name_would_be_masked_is_left_out(monkeypatch) -> None:
    _catalog(monkeypatch, [], [(SECRET, "benign"), ("reviewer", "benign")])
    assert [row["id"] for row in models.usable_models()] == ["reviewer"]


def test_a_bot_whose_name_would_be_masked_is_left_out(monkeypatch) -> None:
    _catalog(monkeypatch, [SECRET, "scout"], [])
    assert [row["id"] for row in models.usable_models()] == ["scout"]


def test_an_ordinary_template_row_is_unchanged(monkeypatch) -> None:
    _catalog(monkeypatch, [], [("reviewer", "reviews pull requests")])
    assert models.usable_models() == [
        {
            "id": "reviewer",
            "object": "model",
            "owned_by": "crew",
            "crew": {"kind": "agent", "name": "reviewer", "description": "reviews pull requests"},
        }
    ]
