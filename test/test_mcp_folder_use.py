"""Folder use is decided from facts, never asked (``mcp_gateway.folder_use``).

An edition-listed server shares without an answer, like Kiro Crew's own. A server
the gateway saw send ``roots/list`` stays in each chat whatever was answered: the
toggle refuses it, the batch share skips it, its row cannot share, and the next
rewrite does not wrap it.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from mcp_launch_approval_helpers import (  # noqa: F401 -- the fixture is requested by name
    _APPROVED_CMD,
    _approval_module,
    _declare,
    _targets_running,
    toggle_home_fixture,
)
from test_mcp_cwd_use import _boot_rewrite, _expected_launch, _post
from test_mcp_gateway_wedge_ping_gate import _make_backend

from kiro_crew.mcp_gateway import folder_use, hazards
from kiro_crew.mcp_gateway.rewriter_overlay import records_dir

#: Opts this module out of the conftest fixture that approves every gatewayd launch.
ENFORCE_LAUNCH_APPROVAL = True


def _saw_roots(socket_path, name: str = "srv") -> None:
    """Write the ledger gatewayd keeps, holding one ``roots/list`` by *name*."""
    ledger = hazards.HazardLedger(hazards.ledger_path(records_dir(socket_path)))
    ledger.record(name, hazards.HAZARD_ROOTS_REQUESTED, "launch")
    ledger.flush()


def _live_socket() -> str:
    from kiro_crew.config.loader import KiroCrewConfig

    return KiroCrewConfig.load().mcp_gateway.socket_path


@pytest.mark.asyncio
async def test_a_roots_request_is_routed_and_recorded_with_one_chat(monkeypatch):
    backend = _make_backend(refcount=1)
    backend.pool_key = SimpleNamespace(
        server_name="srv", command_args_hash="c", effective_env_hash="e", binary_version="v"
    )
    record = MagicMock(return_value=False)
    monkeypatch.setattr(hazards, "record_observed", record)
    frame = {"jsonrpc": "2.0", "id": 7, "method": "roots/list"}

    await backend._route_backend_line((json.dumps(frame) + "\n").encode())

    assert json.loads(backend._test_inbox.get_nowait())["method"] == "roots/list"
    record.assert_called_once_with(
        "srv", hazards.HAZARD_ROOTS_REQUESTED, hazards.launch_identity("c", "e", "v")
    )


def test_a_missing_ledger_names_no_roots_server(tmp_path):
    assert folder_use.roots_requesting(tmp_path / "gw.sock") == frozenset()
    _saw_roots(tmp_path / "gw.sock")
    assert folder_use.roots_requesting(tmp_path / "gw.sock") == {"srv"}


@pytest.mark.asyncio
async def test_an_edition_listed_server_routes_without_an_answer(
    tmp_path, toggle_home, monkeypatch
):
    monkeypatch.setattr(folder_use, "edition_folder_free", lambda: frozenset({"srv"}))
    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    expected = await _expected_launch()

    status, _body = await _post({"name": "srv", "stub": True, "expected_launch": expected})

    assert status == 200
    stubs, target_env = _boot_rewrite(tmp_path, toggle_home)
    assert "srv" in stubs and _targets_running(target_env, _APPROVED_CMD)


@pytest.mark.asyncio
async def test_a_server_seen_asking_for_roots_is_refused_even_when_answered(tmp_path, toggle_home):
    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    expected = await _expected_launch()
    _saw_roots(_live_socket())

    status, body = await _post(
        {"name": "srv", "stub": True, "expected_launch": expected, "cwd_use": "not_used"}
    )

    assert (status, body["code"]) == (409, "cwd_roots")
    assert _approval_module().load_approvals().approved == {}


@pytest.mark.asyncio
async def test_the_batch_share_skips_a_server_seen_asking_for_roots(
    tmp_path, toggle_home, monkeypatch
):
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    expected = await _expected_launch()
    (tmp_path / "config.json").write_text(
        json.dumps({"mcp_gateway": {"cwd_use": {"srv": "not_used"}}})
    )
    _saw_roots(_live_socket())
    monkeypatch.setattr(mcp_mod, "_stub_eligibility", lambda names, **_kw: (list(names), []))

    status, body = await _post(
        {"name": "srv", "stub": True, "expected_launch": expected, "resolve_eligibility": True}
    )

    assert status == 200, body
    assert body["stubbed"] == []
    assert body["skipped"] == [{"name": "srv", "reason": "cwd_roots"}]


@pytest.mark.asyncio
async def test_the_next_rewrite_stops_wrapping_a_server_seen_asking_for_roots(
    tmp_path, toggle_home
):
    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    expected = await _expected_launch()
    await _post({"name": "srv", "stub": True, "expected_launch": expected, "cwd_use": "not_used"})
    _stubs, target_env = _boot_rewrite(tmp_path, toggle_home)
    assert _targets_running(target_env, _APPROVED_CMD)

    _saw_roots(tmp_path / "gw.sock")

    _stubs, target_env = _boot_rewrite(tmp_path, toggle_home)
    assert _targets_running(target_env, _APPROVED_CMD) == []


@pytest.mark.asyncio
async def test_the_row_says_which_fact_decides(tmp_path, toggle_home, monkeypatch):
    from handlers_mcp_coverage_helpers import _payload, _request

    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    monkeypatch.setattr(mcp_mod, "kiro_agents_dir_path", lambda: toggle_home)
    monkeypatch.setattr(folder_use, "edition_folder_free", lambda: frozenset({"srv"}))
    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])

    async def _row():
        response = await mcp_mod.api_mcp_gateway_servers(_request(method="GET"))
        return {r["name"]: r for r in _payload(response)["servers"]}["srv"]

    row = await _row()
    assert (row["cwd_proven"], row["cwd_roots"], row["can_stub"]) == (True, False, True)

    await asyncio.to_thread(_saw_roots, _live_socket())
    row = await _row()
    assert (row["cwd_roots"], row["can_stub"], row["stub"]) == (True, False, False)
