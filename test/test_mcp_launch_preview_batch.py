"""The batch launch read resolves every name in one pass and reports each name apart."""

from __future__ import annotations

import json

import pytest
from mcp_launch_approval_helpers import (  # noqa: F401 -- the fixture is requested by name
    _APPROVED_CMD,
    _ATTACK_CMD,
    _declare,
    _stub_request,
    _toggle_on,
    toggle_home_fixture,
)

ENFORCE_LAUNCH_APPROVAL = True


def _declare_servers(agents, names: list[str]) -> None:
    servers = {name: {"command": _ATTACK_CMD, "args": [name]} for name in names}
    (agents / "a.json").write_text(json.dumps({"name": "a", "mcpServers": servers}))


@pytest.mark.asyncio
async def test_batch_read_resolves_once_and_matches_single_reads(toggle_home, monkeypatch):
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    _declare_servers(toggle_home, ["one", "two", "three"])
    single = {}
    for name in ("one", "two", "three"):
        request = _stub_request({})
        request.query = {"name": name}
        response = await mcp_mod.api_mcp_gateway_server_launch(request)
        assert response.status == 200
        single[name] = json.loads(response.body)

    calls: list[list[str]] = []
    real = mcp_mod.launch_resolve.resolve_launches

    def counting(names, *args, **kwargs):
        calls.append(list(names))
        return real(names, *args, **kwargs)

    monkeypatch.setattr(mcp_mod.launch_resolve, "resolve_launches", counting)
    response = await mcp_mod.api_mcp_gateway_server_launches(
        _stub_request({"names": ["one", "two", "missing", "three", "one"]})
    )
    body = json.loads(response.body)

    assert response.status == 200
    assert calls == [["one", "two", "missing", "three"]]
    assert body["launches"] == single
    assert body["errors"] == {"missing": "launch_unresolved"}


@pytest.mark.asyncio
async def test_batch_read_reports_over_cap_per_name(toggle_home, monkeypatch):
    from kiro_crew.dashboard.handlers import mcp as mcp_mod
    from kiro_crew.mcp_gateway.launch_resolve import ResolvedLaunches

    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    monkeypatch.setattr(
        mcp_mod.launch_resolve,
        "resolve_launches",
        lambda names, **_kw: ResolvedLaunches(over_cap={"big"}),
    )
    response = await mcp_mod.api_mcp_gateway_server_launches(
        _stub_request({"names": ["big", "none"]})
    )

    assert response.status == 200
    assert json.loads(response.body) == {
        "launches": {},
        "errors": {"big": "launch_over_cap", "none": "launch_unresolved"},
    }


@pytest.mark.asyncio
async def test_batch_read_reports_an_invalid_name_without_failing_the_rest(
    toggle_home, monkeypatch
):
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    _declare_servers(toggle_home, ["one"])
    single_request = _stub_request({})
    single_request.query = {"name": "one"}
    single = json.loads((await mcp_mod.api_mcp_gateway_server_launch(single_request)).body)

    calls: list[list[str]] = []
    real = mcp_mod.launch_resolve.resolve_launches

    def counting(names, *args, **kwargs):
        calls.append(list(names))
        return real(names, *args, **kwargs)

    monkeypatch.setattr(mcp_mod.launch_resolve, "resolve_launches", counting)
    response = await mcp_mod.api_mcp_gateway_server_launches(
        _stub_request({"names": ["one", "my server"]})
    )
    body = json.loads(response.body)

    assert response.status == 200
    assert calls == [["one"]]
    assert body["launches"] == {"one": single}
    assert body["errors"] == {"my server": "invalid_server_name"}


@pytest.mark.asyncio
async def test_batch_read_of_only_invalid_names_skips_resolution(toggle_home, monkeypatch):
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    def unexpected(*_a, **_kw):
        raise AssertionError("resolve_launches ran with no valid names")

    monkeypatch.setattr(mcp_mod.launch_resolve, "resolve_launches", unexpected)
    response = await mcp_mod.api_mcp_gateway_server_launches(
        _stub_request({"names": ["../x", "my server"]})
    )

    assert response.status == 200
    assert json.loads(response.body) == {
        "launches": {},
        "errors": {"../x": "invalid_server_name", "my server": "invalid_server_name"},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({}, "names_not_string_list"),
        ({"names": "srv"}, "names_not_string_list"),
        ({"names": [1]}, "names_not_string_list"),
        ({"names": ["  "]}, "names_required"),
        ({"names": [f"s{i}" for i in range(201)]}, "names_too_many"),
    ],
)
async def test_batch_read_refuses_a_malformed_body(toggle_home, monkeypatch, body, code):
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    def unexpected(*_a, **_kw):
        raise AssertionError("resolve_launches ran for a refused request")

    monkeypatch.setattr(mcp_mod.launch_resolve, "resolve_launches", unexpected)
    response = await mcp_mod.api_mcp_gateway_server_launches(_stub_request(body))

    assert response.status == 400
    assert json.loads(response.body)["code"] == code


@pytest.mark.asyncio
async def test_batch_read_failure_is_a_503(toggle_home, monkeypatch):
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    def broken(*_a, **_kw):
        raise RuntimeError("rewrite failed")

    monkeypatch.setattr(mcp_mod.launch_resolve, "resolve_launches", broken)
    response = await mcp_mod.api_mcp_gateway_server_launches(_stub_request({"names": ["srv"]}))

    assert response.status == 503
    assert json.loads(response.body)["code"] == "launch_resolve_failed"


@pytest.mark.asyncio
async def test_names_sharing_a_stem_resolve_as_single_reads_do(toggle_home):
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    _declare_servers(toggle_home, ["foo-mcp", "FOO_MCP"])
    single = {}
    for name in ("foo-mcp", "FOO_MCP"):
        request = _stub_request({})
        request.query = {"name": name}
        response = await mcp_mod.api_mcp_gateway_server_launch(request)
        assert response.status == 200
        single[name] = json.loads(response.body)

    response = await mcp_mod.api_mcp_gateway_server_launches(
        _stub_request({"names": ["foo-mcp", "FOO_MCP"]})
    )
    body = json.loads(response.body)

    assert body["errors"] == {}
    assert body["launches"] == single


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "resolved, code",
    [
        ({"over_cap": {"srv"}}, "launch_over_cap"),
        ({}, "launch_unresolved"),
    ],
)
async def test_a_single_read_and_a_batch_read_give_the_same_code(
    toggle_home, monkeypatch, resolved, code
):
    from kiro_crew.dashboard.handlers import mcp as mcp_mod
    from kiro_crew.mcp_gateway.launch_resolve import ResolvedLaunches

    monkeypatch.setattr(
        mcp_mod.launch_resolve,
        "resolve_launches",
        lambda names, **_kw: ResolvedLaunches(**resolved),
    )
    request = _stub_request({})
    request.query = {"name": "srv"}
    single = await mcp_mod.api_mcp_gateway_server_launch(request)
    batch = await mcp_mod.api_mcp_gateway_server_launches(_stub_request({"names": ["srv"]}))

    assert single.status == 409
    assert json.loads(single.body)["code"] == code
    assert json.loads(batch.body)["errors"] == {"srv": code}


@pytest.mark.asyncio
async def test_each_batch_preview_is_accepted_by_its_one_name_confirm(toggle_home):
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    names = [f"srv-{i}" for i in range(12)]
    _declare_servers(toggle_home, names)
    response = await mcp_mod.api_mcp_gateway_server_launches(_stub_request({"names": names}))
    body = json.loads(response.body)
    assert response.status == 200
    assert body["errors"] == {}

    for name in names:
        status, confirm = await _toggle_on(
            name, expected_launch=body["launches"][name]["expected_launch"]
        )
        assert status == 200, (name, confirm)
        assert confirm.get("code") != "launch_changed_since_display"
