"""A server is routed only after the operator says it does not use the working folder.

A routed server's backend runs in one fixed folder outside the project, shared by
every chat. So the toggle refuses to route a server the operator has not answered
``not_used`` for in ``mcp_gateway.cwd_use`` (Kiro Crew's own servers are not asked),
and the config resolver never routes one answered ``uses_cwd``. The answer is
recorded per server, so a renamed server is unanswered and its old approval does
not carry over, and an approval is bound to the launch it was given for.
"""

from __future__ import annotations

import json

import pytest
from body_stream_helpers import attach_body
from mcp_launch_approval_helpers import (  # noqa: F401 -- the fixture is requested by name
    _APPROVED_CMD,
    _approval_module,
    _declare,
    _targets_running,
    toggle_home_fixture,
)

from kiro_crew.config.integration_sections import _resolve_stub_servers
from kiro_crew.mcp_gateway import rewriter

#: Opts this module out of the conftest fixture that approves every gatewayd launch.
ENFORCE_LAUNCH_APPROVAL = True


def _raw_request(body: dict):
    """A stub request sent exactly as given: no answer is filled in for it."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from aiohttp import web
    from dashboard_owner_helpers import owner_claims

    request = MagicMock(spec=web.Request)
    request.app = {"state": SimpleNamespace(_mcp_gateway_apply_stub=None)}
    attach_body(request, body)
    return owner_claims(request)


async def _post(body: dict) -> tuple[int, dict]:
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    response = await mcp_mod.api_mcp_gateway_set_stub(_raw_request(body))
    return response.status, json.loads(response.body)


async def _expected_launch(name: str = "srv") -> str:
    import asyncio

    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    launches = await asyncio.to_thread(
        mcp_mod.launch_resolve.resolve_launches, [name], refresh_specs=False
    )
    return mcp_mod.display_launches(name, launches[name])["expected_launch"]


def _config(tmp_path) -> dict:
    return json.loads((tmp_path / "config.json").read_text())


def _boot_rewrite(tmp_path, agents):
    """The next gateway start: the routed set comes from config, as boot reads it."""
    from kiro_crew.config.loader import KiroCrewConfig

    stubs = frozenset(KiroCrewConfig.load().mcp_gateway.stub_servers)
    _results, target_env = rewriter.rewrite_agents(
        source_dir=agents,
        overlay_dir=tmp_path / "boot" / "mcp-gateway" / "agents",
        socket_path=tmp_path / "gw.sock",
        work_dir=tmp_path / "work",
        stub_servers=stubs,
        approvals=_approval_module().load_approvals(),
    )
    return stubs, target_env


@pytest.mark.parametrize(
    ("section", "expected"),
    [
        ({"stub_servers": ["a", "b"]}, ["a", "b"]),
        ({"stub_servers": ["a", "b"], "cwd_use": {"a": "uses_cwd"}}, ["b"]),
        ({"stub_overrides": {"a": True}, "cwd_use": {"a": "uses_cwd"}}, []),
        ({"stub_servers": ["a"], "cwd_use": {"a": "not_used"}}, ["a"]),
        # An unknown value reads as unanswered: it neither routes nor unroutes.
        ({"stub_servers": ["a"], "cwd_use": {"a": "yes", "b": 1}}, ["a"]),
        ({"stub_servers": ["a"], "cwd_use": "uses_cwd"}, ["a"]),
        ({"stub_servers": ["a"], "cwd_use": {"a": ["uses_cwd"], "b": {}}}, ["a"]),
    ],
)
def test_a_server_that_uses_the_folder_is_never_in_the_routed_set(section, expected):
    assert _resolve_stub_servers(section) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["agent spec", "settings mcp.json"])
async def test_routing_an_unanswered_server_is_refused_and_writes_nothing(
    tmp_path, toggle_home, where
):
    _declare(toggle_home, where, _APPROVED_CMD, [])
    expected = await _expected_launch()

    status, body = await _post({"name": "srv", "stub": True, "expected_launch": expected})

    assert (status, body["code"]) == (409, "cwd_unconfirmed")
    assert "mcp_gateway" not in _config(tmp_path)
    assert _approval_module().load_approvals().approved == {}
    stubs, target_env = _boot_rewrite(tmp_path, toggle_home)
    assert "srv" not in stubs and _targets_running(target_env, _APPROVED_CMD) == []


@pytest.mark.asyncio
async def test_a_server_answered_uses_cwd_cannot_be_routed(tmp_path, toggle_home):
    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    expected = await _expected_launch()

    status, body = await _post(
        {"name": "srv", "stub": True, "expected_launch": expected, "cwd_use": "uses_cwd"}
    )

    assert (status, body["code"]) == (409, "cwd_unconfirmed")
    assert _approval_module().load_approvals().approved == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["agent spec", "settings mcp.json"])
async def test_answering_not_used_routes_the_server_at_the_next_start(tmp_path, toggle_home, where):
    _declare(toggle_home, where, _APPROVED_CMD, [])
    expected = await _expected_launch()

    status, _body = await _post(
        {"name": "srv", "stub": True, "expected_launch": expected, "cwd_use": "not_used"}
    )

    assert status == 200
    assert _config(tmp_path)["mcp_gateway"]["cwd_use"] == {"srv": "not_used"}
    stubs, target_env = _boot_rewrite(tmp_path, toggle_home)
    assert "srv" in stubs and _targets_running(target_env, _APPROVED_CMD)


@pytest.mark.asyncio
async def test_answering_uses_cwd_later_unroutes_a_routed_server(tmp_path, toggle_home):
    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    expected = await _expected_launch()
    await _post({"name": "srv", "stub": True, "expected_launch": expected, "cwd_use": "not_used"})

    status, body = await _post({"name": "srv", "cwd_use": "uses_cwd"})

    assert status == 200 and body["stub"] is None
    assert _config(tmp_path)["mcp_gateway"]["cwd_use"] == {"srv": "uses_cwd"}
    stubs, target_env = _boot_rewrite(tmp_path, toggle_home)
    assert "srv" not in stubs and _targets_running(target_env, _APPROVED_CMD) == []


@pytest.mark.asyncio
async def test_a_routed_server_with_no_answer_keeps_running_but_reapproval_asks(
    tmp_path, toggle_home
):
    """Routed before the question existed: it keeps today's behaviour until answered."""
    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    expected = await _expected_launch()
    await _post({"name": "srv", "stub": True, "expected_launch": expected, "cwd_use": "not_used"})
    data = _config(tmp_path)
    del data["mcp_gateway"]["cwd_use"]
    (tmp_path / "config.json").write_text(json.dumps(data))

    stubs, target_env = _boot_rewrite(tmp_path, toggle_home)
    assert "srv" in stubs and _targets_running(target_env, _APPROVED_CMD)

    status, body = await _post({"name": "srv", "stub": True, "expected_launch": expected})
    assert (status, body["code"]) == (409, "cwd_unconfirmed")


@pytest.mark.asyncio
async def test_a_hand_written_routing_entry_runs_nothing_routed(tmp_path, toggle_home):
    """A name put in ``stub_servers`` by hand has no approval, so it stays in the chat."""
    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    (tmp_path / "config.json").write_text(json.dumps({"mcp_gateway": {"stub_servers": ["srv"]}}))

    stubs, target_env = _boot_rewrite(tmp_path, toggle_home)

    assert "srv" in stubs and _targets_running(target_env, _APPROVED_CMD) == []


@pytest.mark.asyncio
async def test_a_renamed_server_is_unanswered_and_unapproved(tmp_path, toggle_home):
    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    expected = await _expected_launch()
    await _post({"name": "srv", "stub": True, "expected_launch": expected, "cwd_use": "not_used"})
    spec = json.loads((toggle_home / "a.json").read_text())
    spec["mcpServers"] = {"srv2": spec["mcpServers"]["srv"]}
    (toggle_home / "a.json").write_text(json.dumps(spec))
    data = _config(tmp_path)
    data["mcp_gateway"]["stub_overrides"]["srv2"] = True
    (tmp_path / "config.json").write_text(json.dumps(data))

    _stubs, target_env = _boot_rewrite(tmp_path, toggle_home)
    assert _targets_running(target_env, _APPROVED_CMD) == []

    status, body = await _post(
        {"name": "srv2", "stub": True, "expected_launch": await _expected_launch("srv2")}
    )
    assert (status, body["code"]) == (409, "cwd_unconfirmed")


@pytest.mark.asyncio
async def test_changed_args_after_the_answer_stop_routing_until_approved(tmp_path, toggle_home):
    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    expected = await _expected_launch()
    await _post({"name": "srv", "stub": True, "expected_launch": expected, "cwd_use": "not_used"})

    _declare(toggle_home, "agent spec", _APPROVED_CMD, ["-m", "srv_in_this_folder"])

    _stubs, target_env = _boot_rewrite(tmp_path, toggle_home)
    assert _targets_running(target_env, _APPROVED_CMD) == []


@pytest.mark.asyncio
async def test_the_batch_share_skips_an_unanswered_server(tmp_path, toggle_home, monkeypatch):
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    expected = await _expected_launch()
    monkeypatch.setattr(mcp_mod, "_stub_eligibility", lambda names, **_kw: (list(names), []))

    status, body = await _post(
        {"name": "srv", "stub": True, "expected_launch": expected, "resolve_eligibility": True}
    )

    assert status == 200
    assert body["stubbed"] == []
    assert body["skipped"] == [{"name": "srv", "reason": "cwd_unconfirmed"}]
    assert _approval_module().load_approvals().approved == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"name": "srv", "cwd_use": "maybe"}, "cwd_use_invalid"),
        ({"names": ["srv"], "stub": False, "cwd_use": "uses_cwd"}, "cwd_use_invalid"),
        ({"name": "kirocrew-core", "cwd_use": "uses_cwd"}, "cwd_use_not_asked"),
        ({"name": "srv", "cwd_use": []}, "cwd_use_invalid"),
    ],
)
async def test_a_bad_answer_is_refused(tmp_path, toggle_home, body, code):
    status, answer = await _post(body)

    assert (status, answer.get("code")) == (400, code)
    assert _config(tmp_path) == {}


@pytest.mark.asyncio
async def test_answering_uses_cwd_forgets_the_approval(tmp_path, toggle_home):
    """Losing the answer later by hand cannot bring the server back routed."""
    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    expected = await _expected_launch()
    await _post({"name": "srv", "stub": True, "expected_launch": expected, "cwd_use": "not_used"})

    await _post({"name": "srv", "cwd_use": "uses_cwd"})
    data = _config(tmp_path)
    del data["mcp_gateway"]["cwd_use"]
    (tmp_path / "config.json").write_text(json.dumps(data))

    stubs, target_env = _boot_rewrite(tmp_path, toggle_home)
    assert "srv" in stubs and _targets_running(target_env, _APPROVED_CMD) == []


@pytest.mark.asyncio
async def test_an_overlay_answer_for_another_server_keeps_the_base_answer(
    tmp_path, toggle_home, monkeypatch
):
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    expected = await _expected_launch()
    await _post({"name": "srv", "cwd_use": "not_used"})
    monkeypatch.setattr(
        mcp_mod, "_local_overlay_section", lambda: {"cwd_use": {"other": "uses_cwd"}}
    )

    status, _body = await _post({"name": "srv", "stub": True, "expected_launch": expected})

    assert status == 200


@pytest.mark.asyncio
async def test_a_bad_overlay_answer_hides_the_base_answer(tmp_path, toggle_home, monkeypatch):
    """The overlay's value wins whole, as the loader merges it, even when unreadable."""
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    expected = await _expected_launch()
    await _post({"name": "srv", "cwd_use": "not_used"})
    monkeypatch.setattr(
        mcp_mod, "_local_overlay_section", lambda: {"cwd_use": {"srv": "uses_cwds"}}
    )

    status, body = await _post({"name": "srv", "stub": True, "expected_launch": expected})

    assert (status, body["code"]) == (409, "cwd_unconfirmed")
