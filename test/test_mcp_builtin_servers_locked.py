"""Kiro Crew's own MCP servers cannot be switched off or removed.

The MCP Servers list locks the off switch on a built-in row, and every route
behind that list refuses the same thing: ``/api/mcp/toggle`` with
``enabled: false``, ``/api/mcp/remove``, ``DELETE /api/mcp/servers/{name}``,
``/api/capability/mcp/uninstall``, and an ``/api/mcp/apply`` change that turns the
Kiro Crew scope off or uninstalls. ``/api/mcp/toggle-all`` skips them when it
disables. Turning a built-in server back on is still accepted, so one
that is already off can be restored.

Every filesystem seam is redirected into ``tmp_path`` by the shared ``sandbox``
fixture; nothing touches the real ``~/.kiro``.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from handlers_mcp_coverage_helpers import sandbox_fixture  # noqa: F401
from handlers_mcp_coverage_helpers import _payload, _read_global, _request, _write_global

import kiro_crew.platform.context as platform_context
from kiro_crew.dashboard.handlers import agents as agents_handler
from kiro_crew.dashboard.handlers import mcp as mcp_mod

pytestmark = pytest.mark.asyncio

_BUILTIN = "kirocrew-core"


async def test_toggle_off_refuses_a_builtin_server_and_writes_nothing(
    sandbox: SimpleNamespace,
) -> None:
    _write_global(sandbox, {_BUILTIN: {"command": "x"}})
    resp = await mcp_mod.api_mcp_toggle(_request({"name": _BUILTIN, "enabled": False}))
    payload = _payload(resp)
    assert resp.status == 409, payload
    assert payload.get("code") == "mcp_server_builtin", payload
    assert "disabled" not in _read_global(sandbox)[_BUILTIN]
    assert sandbox.synced == []


async def test_toggle_on_still_restores_a_builtin_server(sandbox: SimpleNamespace) -> None:
    # A built-in that is already off can be switched back on.
    _write_global(sandbox, {_BUILTIN: {"command": "x", "disabled": True}})
    resp = await mcp_mod.api_mcp_toggle(_request({"name": _BUILTIN, "enabled": True}))
    assert resp.status == 200, _payload(resp)
    assert "disabled" not in _read_global(sandbox)[_BUILTIN]


async def test_toggle_all_off_leaves_builtin_servers_on(sandbox: SimpleNamespace) -> None:
    _write_global(sandbox, {_BUILTIN: {"command": "x"}, "srv": {"command": "y"}})
    resp = await mcp_mod.api_mcp_toggle_all(_request({"enabled": False}))
    assert resp.status == 200, _payload(resp)
    servers = _read_global(sandbox)
    assert servers["srv"].get("disabled") is True
    assert "disabled" not in servers[_BUILTIN]
    assert sandbox.batched == [(["srv"], False)]


async def test_remove_refuses_a_builtin_server(sandbox: SimpleNamespace) -> None:
    _write_global(sandbox, {_BUILTIN: {"command": "x"}})
    resp = await mcp_mod.api_mcp_remove(_request({"name": _BUILTIN}))
    payload = _payload(resp)
    assert resp.status == 409, payload
    assert payload.get("code") == "mcp_server_builtin", payload
    assert _BUILTIN in _read_global(sandbox)
    assert sandbox.synced == []


@pytest.mark.parametrize(
    "change",
    [
        {"name": _BUILTIN, "kirocrew": False, "kiroGlobal": False},
        {"name": _BUILTIN, "uninstall": True},
    ],
    ids=["kirocrew-off", "uninstall"],
)
async def test_apply_refuses_turning_off_a_builtin_server(
    sandbox: SimpleNamespace, change: dict
) -> None:
    _write_global(sandbox, {_BUILTIN: {"command": "x"}, "srv": {"command": "y"}})
    # The other change in the batch is not applied either: the refusal is the
    # whole request, before any write.
    other = {"name": "srv", "uninstall": True}
    resp = await mcp_mod.api_mcp_apply(_request({"changes": [other, change]}))
    payload = _payload(resp)
    assert resp.status == 409, payload
    assert payload.get("code") == "mcp_server_builtin", payload
    servers = _read_global(sandbox)
    assert servers[_BUILTIN] == {"command": "x"}
    assert "srv" in servers


async def test_stamped_rows_name_the_builtin_servers() -> None:
    builtin = frozenset({_BUILTIN})
    core: dict = {"name": _BUILTIN}
    other: dict = {"name": "srv"}
    mcp_mod._stamp_config_state(core, {}, {}, builtin)
    mcp_mod._stamp_config_state(other, {}, {}, builtin)
    assert core["builtin"] is True
    assert other["builtin"] is False


async def test_third_party_server_still_turns_off_and_is_removed(sandbox: SimpleNamespace) -> None:
    _write_global(sandbox, {"srv": {"command": "y"}})
    resp = await mcp_mod.api_mcp_toggle(_request({"name": "srv", "enabled": False}))
    assert resp.status == 200, _payload(resp)
    assert _read_global(sandbox)["srv"].get("disabled") is True
    resp = await mcp_mod.api_mcp_remove(_request({"name": "srv"}))
    assert resp.status == 200, _payload(resp)
    assert "srv" not in _read_global(sandbox)


async def test_toggle_all_on_lifts_a_builtin_servers_flag(sandbox: SimpleNamespace) -> None:
    _write_global(sandbox, {_BUILTIN: {"command": "x", "disabled": True}})
    resp = await mcp_mod.api_mcp_toggle_all(_request({"enabled": True}))
    assert resp.status == 200, _payload(resp)
    assert "disabled" not in _read_global(sandbox)[_BUILTIN]


async def test_delete_refuses_a_builtin_server(sandbox: SimpleNamespace) -> None:
    _write_global(sandbox, {_BUILTIN: {"command": "x"}})
    resp = await mcp_mod.api_mcp_server_detail(
        _request(None, match_info={"name": _BUILTIN}, method="DELETE")
    )
    payload = _payload(resp)
    assert resp.status == 409, payload
    assert payload.get("code") == "mcp_server_builtin", payload
    assert _read_global(sandbox)[_BUILTIN] == {"command": "x"}
    assert sandbox.synced == []


async def test_delete_still_removes_a_third_party_server(sandbox: SimpleNamespace) -> None:
    _write_global(sandbox, {"srv": {"command": "y"}})
    resp = await mcp_mod.api_mcp_server_detail(
        _request(None, match_info={"name": "srv"}, method="DELETE")
    )
    assert resp.status == 200, _payload(resp)
    assert "srv" not in _read_global(sandbox)


@pytest.mark.parametrize(
    "change",
    [
        {"name": _BUILTIN, "kirocrew": True, "kiroGlobal": True},
        {"name": _BUILTIN, "kirocrew": True, "kiroGlobal": True, "toolOverrides": {"wait": False}},
    ],
    ids=["kirocrew-on", "tool-override"],
)
async def test_apply_still_accepts_turning_on_or_a_tool_override(
    sandbox: SimpleNamespace, change: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The agent spec rebuild is not under test and must not write the host's.
    monkeypatch.setattr(mcp_mod, "rebuild_agent_config_reporting", lambda: (None, True))
    _write_global(sandbox, {_BUILTIN: {"command": "x"}})
    resp = await mcp_mod.api_mcp_apply(_request({"changes": [change]}))
    payload = _payload(resp)
    assert resp.status == 200, payload
    assert payload.get("code") != "mcp_server_builtin", payload


class _Manager:
    """Records every call, so a refusal is shown to land before the manager."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def available(self) -> bool:
        self.calls.append("available")
        return False


class _CapabilityRequest:
    """A request ``read_bounded_json(max_bytes=None)`` accepts."""

    content_type = "application/json"
    can_read_body = True

    def __init__(self, body: Any) -> None:
        self._body = body
        self.app = {"state": None}

    async def json(self) -> Any:
        return self._body


async def test_capability_uninstall_refuses_a_builtin_server(
    _floor_monkeypatch: pytest.MonkeyPatch, monkeypatch: pytest.MonkeyPatch
) -> None:
    _floor_monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: True,
    )
    mgr = _Manager()

    class _Ctx:
        capability_manager = mgr

    monkeypatch.setattr(platform_context, "current_context", lambda: _Ctx())
    monkeypatch.setattr("kiro_crew.dashboard.handlers.mcp._agent_home_not_owned", lambda _n: None)
    resp = await agents_handler.api_capability_mcp_uninstall(
        _CapabilityRequest({"server_id": _BUILTIN})
    )
    assert resp.status == 409
    assert json.loads(resp.body.decode())["code"] == "mcp_server_builtin"
    assert mgr.calls == []


async def test_apply_accepts_edits_on_a_builtin_the_store_already_mutes(
    sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The table sends the row's current presence (`kirocrew: false`) with a
    # tool edit on a built-in that is already off: that is no turn-off.
    monkeypatch.setattr(mcp_mod, "rebuild_agent_config_reporting", lambda: (None, True))
    store = mcp_mod._kirocrew_mcp_json()
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(
        json.dumps({"mcpServers": {_BUILTIN: {"command": "x", "disabled": True}}}),
        encoding="utf-8",
    )
    _write_global(sandbox, {_BUILTIN: {"command": "x"}})
    change = {
        "name": _BUILTIN,
        "kirocrew": False,
        "kiroGlobal": True,
        "toolOverrides": {"wait": False},
    }
    resp = await mcp_mod.api_mcp_apply(_request({"changes": [change]}))
    payload = _payload(resp)
    assert resp.status == 200, payload


async def test_custom_add_refuses_a_disabled_entry_under_a_builtin_name(
    sandbox: SimpleNamespace,
) -> None:
    from kiro_crew.dashboard.handlers import mcp_custom

    body = {"servers": {_BUILTIN: {"command": "x"}}, "enable": False}
    resp = await mcp_custom.api_mcp_custom_add(_request(body))
    payload = _payload(resp)
    assert resp.status == 409, payload
    assert payload.get("code") == "mcp_server_builtin", payload
    assert not mcp_mod._kirocrew_mcp_json().exists()


async def test_discover_install_refuses_a_builtin_name(
    sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.dashboard.handlers import mcp_discover

    async def _plan(_server_id: str) -> tuple[str, dict, str, list[str]]:
        return _BUILTIN, {"command": "x"}, "npm", []

    monkeypatch.setattr(mcp_discover, "official_install_plan", _plan)
    resp = await mcp_discover.api_mcp_discover_install(
        _request({"provider": "official", "id": "io.example/kirocrew-core"})
    )
    payload = _payload(resp)
    assert resp.status == 409, payload
    assert payload.get("code") == "mcp_server_builtin", payload
    assert not mcp_mod._kirocrew_mcp_json().exists()


async def test_custom_add_refuses_a_disabled_entry_under_a_builtin_alias(
    sandbox: SimpleNamespace,
) -> None:
    # `@kirocrew/core` is listed as its alias `kirocrew-core`.
    from kiro_crew.dashboard.handlers import mcp_custom

    body = {"servers": {"@kirocrew/core": {"command": "x"}}, "enable": False}
    resp = await mcp_custom.api_mcp_custom_add(_request(body))
    payload = _payload(resp)
    assert resp.status == 409, payload
    assert payload.get("code") == "mcp_server_builtin", payload
    assert not mcp_mod._kirocrew_mcp_json().exists()


async def test_toggle_off_refuses_a_builtin_alias(sandbox: SimpleNamespace) -> None:
    _write_global(sandbox, {"@kirocrew/core": {"command": "x"}})
    resp = await mcp_mod.api_mcp_toggle(_request({"name": "@kirocrew/core", "enabled": False}))
    payload = _payload(resp)
    assert resp.status == 409, payload
    assert payload.get("code") == "mcp_server_builtin", payload
    assert "disabled" not in _read_global(sandbox)["@kirocrew/core"]
    assert sandbox.synced == []


@pytest.mark.parametrize("opt_in", ["kirocrew-dashboard", "kirocrew-secrets"])
async def test_a_granted_opt_in_server_is_not_locked(sandbox: SimpleNamespace, opt_in: str) -> None:
    # An `opt_in` set is a grant the operator made by hand: it switches off and is
    # removed exactly as on main, and its row is not stamped built-in.
    assert opt_in not in mcp_mod._builtin_mcp_server_names()
    _write_global(sandbox, {opt_in: {"command": "x"}})
    resp = await mcp_mod.api_mcp_toggle(_request({"name": opt_in, "enabled": False}))
    assert resp.status == 200, _payload(resp)
    assert _read_global(sandbox)[opt_in].get("disabled") is True
    resp = await mcp_mod.api_mcp_remove(_request({"name": opt_in}))
    assert resp.status == 200, _payload(resp)
    assert opt_in not in _read_global(sandbox)
    row: dict = {"name": opt_in}
    mcp_mod._stamp_config_state(row, {}, {}, mcp_mod._builtin_mcp_server_names())
    assert row["builtin"] is False


async def test_apply_checks_every_change_after_a_built_in_lookup_answers_none(
    sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The first change is reported built-in by the name test but its refusal lookup
    # answers None (a fallback read); the later built-in uninstall is still refused.
    from kiro_crew.dashboard.handlers import mcp_agent_sync

    real = mcp_agent_sync._builtin_server_refusal

    async def _refusal(name: str):
        return None if name == "edition-srv" else await real(name)

    async def _is_builtin(name: str) -> bool:
        return name in {"edition-srv", _BUILTIN}

    monkeypatch.setattr(mcp_mod, "_builtin_server_refusal", _refusal)
    monkeypatch.setattr(mcp_mod, "_is_builtin_mcp_server", _is_builtin)
    _write_global(sandbox, {_BUILTIN: {"command": "x"}, "edition-srv": {"command": "y"}})
    changes = [{"name": "edition-srv", "uninstall": True}, {"name": _BUILTIN, "uninstall": True}]
    resp = await mcp_mod.api_mcp_apply(_request({"changes": changes}))
    payload = _payload(resp)
    assert resp.status == 409, payload
    assert payload.get("code") == "mcp_server_builtin", payload
    servers = _read_global(sandbox)
    assert _BUILTIN in servers and "edition-srv" in servers


async def test_computer_with_its_gate_closed_is_not_locked(
    sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The locked set is what a fresh spec build emits: a closed gate withholds
    # kirocrew-computer, so it switches off and is removed as on main.
    import kiro_crew.agent as agent_mod

    spec = {**agent_mod._MANAGED_MCP_SERVERS["kirocrew-computer"], "spec_gate": lambda: False}
    monkeypatch.setitem(agent_mod._MANAGED_MCP_SERVERS, "kirocrew-computer", spec)
    assert "kirocrew-computer" not in mcp_mod._builtin_mcp_server_names()
    _write_global(sandbox, {"kirocrew-computer": {"command": "x"}})
    resp = await mcp_mod.api_mcp_toggle(_request({"name": "kirocrew-computer", "enabled": False}))
    assert resp.status == 200, _payload(resp)
    assert _read_global(sandbox)["kirocrew-computer"].get("disabled") is True
    resp = await mcp_mod.api_mcp_remove(_request({"name": "kirocrew-computer"}))
    assert resp.status == 200, _payload(resp)
    row: dict = {"name": "kirocrew-computer"}
    mcp_mod._stamp_config_state(row, {}, {}, mcp_mod._builtin_mcp_server_names())
    assert row["builtin"] is False


async def test_an_unreadable_server_set_locks_nothing(
    sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The set read fails: no name is refused, and the route answers as on main.
    # Patched through the facade, which reaches every family member holding the name.
    def _raise() -> frozenset[str]:
        raise RuntimeError("platform context did not compose")

    monkeypatch.setattr(mcp_mod, "emission_eligible_mcp_servers", _raise)
    assert mcp_mod._builtin_mcp_server_names() == frozenset()
    _write_global(sandbox, {_BUILTIN: {"command": "x"}})
    resp = await mcp_mod.api_mcp_toggle(_request({"name": _BUILTIN, "enabled": False}))
    assert resp.status == 200, _payload(resp)
    assert _read_global(sandbox)[_BUILTIN].get("disabled") is True
    row: dict = {"name": _BUILTIN}
    mcp_mod._stamp_config_state(row, {}, {}, mcp_mod._builtin_mcp_server_names())
    assert row["builtin"] is False
