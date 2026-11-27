"""Roots keep a server per chat only when no declared or saved fact disagrees.

Mixed cases retain the usual question and approval path across the dashboard
and rewriter. Batch sharing still uses its independent hazard verdict.
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
@pytest.mark.parametrize("surface", ["toggle", "row"])
async def test_a_stored_folder_answer_outranks_the_edition_until_answered_again(
    tmp_path, toggle_home, monkeypatch, surface
):
    from handlers_mcp_coverage_helpers import _payload, _request

    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    monkeypatch.setattr(mcp_mod, "kiro_agents_dir_path", lambda: toggle_home)
    monkeypatch.setattr(folder_use, "edition_folder_free", lambda: frozenset({"srv"}))
    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    expected = await _expected_launch()
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"mcp_gateway": {"cwd_use": {"srv": "uses_cwd"}}}))

    if surface == "toggle":
        status, body = await _post({"name": "srv", "stub": True, "expected_launch": expected})
        assert (status, body.get("code")) == (409, "cwd_unconfirmed")
    else:
        response = await mcp_mod.api_mcp_gateway_servers(_request(method="GET"))
        row = {r["name"]: r for r in _payload(response)["servers"]}["srv"]
        assert (row["cwd_use"], row["cwd_proven"]) == ("uses_cwd", False)
    assert json.loads(config.read_text())["mcp_gateway"]["cwd_use"] == {"srv": "uses_cwd"}
    assert _approval_module().load_approvals().approved == {}

    status, body = await _post(
        {"name": "srv", "stub": True, "expected_launch": expected, "cwd_use": "not_used"}
    )

    assert status == 200, body
    assert json.loads(config.read_text())["mcp_gateway"]["cwd_use"] == {"srv": "not_used"}
    stubs, target_env = _boot_rewrite(tmp_path, toggle_home)
    assert "srv" in stubs and _targets_running(target_env, _APPROVED_CMD)


@pytest.mark.asyncio
async def test_a_saved_not_used_answer_keeps_the_approval_path_after_roots(
    tmp_path, toggle_home, monkeypatch
):
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    monkeypatch.setattr(mcp_mod, "kiro_agents_dir_path", lambda: toggle_home)
    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    expected = await _expected_launch()
    (tmp_path / "config.json").write_text(
        json.dumps({"mcp_gateway": {"cwd_use": {"srv": "not_used"}}}), encoding="utf-8"
    )
    _saw_roots(_live_socket())

    status, body = await _post({"name": "srv", "stub": True, "expected_launch": expected})

    assert status == 200, body
    assert _approval_module().load_approvals().approved != {}


@pytest.mark.asyncio
@pytest.mark.parametrize("saw_roots", [True, False])
async def test_the_batch_share_skips_a_server_seen_asking_for_roots(
    tmp_path, toggle_home, monkeypatch, saw_roots
):
    # The recorded request is a hazard, so the eligibility verdict skips it. The
    # ``False`` case is the control: the same server with no request is stubbed.
    import kiro_crew.agent as agent_mod
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", toggle_home)
    meta = SimpleNamespace(
        status="ok",
        capabilities={"experimental": {"kirocrew.caller-identity": {}}},
        protocol_version="2024-11-05",
        tool_annotations=[],
        tools=[SimpleNamespace(name="t")],
    )
    monkeypatch.setattr(mcp_mod, "probe_metadata", lambda n: meta if n == "srv" else None)
    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    expected = await _expected_launch()
    # Sharing off, as the control needs: with no stored value it is now on.
    (tmp_path / "config.json").write_text(
        json.dumps({"mcp_gateway": {"enabled": False, "cwd_use": {"srv": "not_used"}}})
    )
    if saw_roots:
        _saw_roots(_live_socket())

    status, body = await _post(
        {"name": "srv", "stub": True, "expected_launch": expected, "resolve_eligibility": True}
    )

    assert status == 200, body
    if not saw_roots:
        assert (body["stubbed"], body["skipped"]) == (["srv"], [])
        return
    assert body["stubbed"] == []
    assert body["skipped"] == [{"name": "srv", "reason": "evidence_insufficient"}]
    assert _approval_module().load_approvals().approved == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["agent spec", "settings mcp.json"])
async def test_roots_exclusion_preserves_refresh_policy_without_wrapping(
    tmp_path, toggle_home, where
):
    from kiro_crew.mcp_gateway.overlay_refresh import rewrite_policy

    _declare(toggle_home, where, _APPROVED_CMD, [])
    expected = await _expected_launch()
    status, _body = await _post(
        {"name": "srv", "stub": True, "expected_launch": expected, "cwd_use": "not_used"}
    )
    assert status == 200
    config = tmp_path / "config.json"
    data = json.loads(config.read_text(encoding="utf-8"))
    data["mcp_gateway"].pop("cwd_use", None)
    data["mcp_gateway"].pop("cwd_use_launch", None)
    config.write_text(json.dumps(data), encoding="utf-8")
    _stubs, target_env = _boot_rewrite(tmp_path, toggle_home)
    assert _targets_running(target_env, _APPROVED_CMD)
    overlay_dir = tmp_path / "boot" / "mcp-gateway" / "agents"
    before = rewrite_policy(overlay_dir)
    assert before is not None and "srv" in before["stub_servers"]

    _saw_roots(tmp_path / "gw.sock")

    _stubs, target_env = _boot_rewrite(tmp_path, toggle_home)
    assert _targets_running(target_env, _APPROVED_CMD) == []
    assert rewrite_policy(overlay_dir) == before
    inputs = json.loads((overlay_dir / ".rewrite-fingerprint").read_text(encoding="utf-8"))[
        "inputs"
    ]
    assert "srv" in inputs["stub_servers"]
    assert inputs["roots_excluded"] == ["srv"]


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["agent spec", "settings mcp.json"])
async def test_roots_request_keeps_a_vault_reference_wrapped(tmp_path, toggle_home, where):
    from kiro_crew.agent_materialization.mcp_sources import _unrouted_secret_reference
    from kiro_crew.mcp_gateway.session_servers import injection_server_names

    _declare(toggle_home, where, _APPROVED_CMD, [])
    source = (
        toggle_home / "a.json"
        if where == "agent spec"
        else toggle_home.parent / "settings" / "mcp.json"
    )
    spec = json.loads(source.read_text(encoding="utf-8"))
    entry = spec["mcpServers"]["srv"]
    entry["env"] = {"API_TOKEN": "secret://test-token"}
    source.write_text(json.dumps(spec), encoding="utf-8")
    expected = await _expected_launch()
    status, body = await _post(
        {"name": "srv", "stub": True, "expected_launch": expected, "cwd_use": "not_used"}
    )
    assert status == 200, body
    _stubs, target_env = _boot_rewrite(tmp_path, toggle_home)
    assert _targets_running(target_env, _APPROVED_CMD)

    _saw_roots(tmp_path / "gw.sock")

    _stubs, target_env = _boot_rewrite(tmp_path, toggle_home)
    assert _targets_running(target_env, _APPROVED_CMD)
    overlay_dir = tmp_path / "boot" / "mcp-gateway" / "agents"
    served = injection_server_names(overlay_dir, "a", work_dir=None)
    assert "srv" in served
    assert not _unrouted_secret_reference("srv", entry, lambda: served, lambda: {})
    # A warm rewrite must retain the same stub and approved target too.
    _stubs, cached_targets = _boot_rewrite(tmp_path, toggle_home)
    assert cached_targets == target_env
    assert "srv" in injection_server_names(overlay_dir, "a", work_dir=None)


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
    assert (row["cwd_roots"], row["can_stub"], row["cwd_proven"]) == (False, True, False)


def _name_a_vault_secret(agents) -> None:
    """Give the agent spec's ``srv`` an env that names a vault secret."""
    source = agents / "a.json"
    spec = json.loads(source.read_text(encoding="utf-8"))
    spec["mcpServers"]["srv"]["env"] = {"API_TOKEN": "secret://test-token"}
    source.write_text(json.dumps(spec), encoding="utf-8")


@pytest.mark.asyncio
async def test_a_vault_server_seen_asking_for_roots_keeps_its_share_switch(
    tmp_path, toggle_home, monkeypatch
):
    from handlers_mcp_coverage_helpers import _payload, _request

    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    monkeypatch.setattr(mcp_mod, "kiro_agents_dir_path", lambda: toggle_home)
    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    _name_a_vault_secret(toggle_home)
    await asyncio.to_thread(_saw_roots, _live_socket())

    response = await mcp_mod.api_mcp_gateway_servers(_request(method="GET"))
    row = {r["name"]: r for r in _payload(response)["servers"]}["srv"]

    assert (row["cwd_roots"], row["can_stub"]) == (False, True)


@pytest.mark.asyncio
async def test_a_vault_server_seen_asking_for_roots_is_not_refused_by_the_toggle(
    tmp_path, toggle_home, monkeypatch
):
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    # The row scan the toggle reads, pointed at this home like the row test's.
    monkeypatch.setattr(mcp_mod, "kiro_agents_dir_path", lambda: toggle_home)
    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    _name_a_vault_secret(toggle_home)
    expected = await _expected_launch()
    _saw_roots(_live_socket())

    status, body = await _post(
        {"name": "srv", "stub": True, "expected_launch": expected, "cwd_use": "not_used"}
    )

    assert status == 200, body
    assert _approval_module().load_approvals().approved != {}


@pytest.mark.asyncio
async def test_a_roots_row_answered_no_by_itself_gets_its_share_switch_back(
    tmp_path, toggle_home, monkeypatch
):
    from handlers_mcp_coverage_helpers import _payload, _request

    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    monkeypatch.setattr(mcp_mod, "kiro_agents_dir_path", lambda: toggle_home)
    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    await asyncio.to_thread(_saw_roots, _live_socket())

    async def _row():
        response = await mcp_mod.api_mcp_gateway_servers(_request(method="GET"))
        return {r["name"]: r for r in _payload(response)["servers"]}["srv"]

    row = await _row()
    assert (row["cwd_roots"], row["can_stub"]) == (True, False)

    # The row's one answer: cwd_use alone, no stub, so nothing is shared or approved.
    status, body = await _post({"name": "srv", "cwd_use": "not_used"})

    assert status == 200, body
    assert _approval_module().load_approvals().approved == {}
    row = await _row()
    assert (row["cwd_roots"], row["can_stub"], row["cwd_use"]) == (False, True, "not_used")


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["row", "toggle", "rewrite"])
@pytest.mark.parametrize(
    "source", ["agents", "settings", "edition", "answer", "alias_vault", "alias_answer", "agree"]
)
async def test_roots_requires_every_source_to_agree(
    tmp_path, toggle_home, monkeypatch, source, surface
):
    from handlers_mcp_coverage_helpers import _payload, _request

    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.dashboard.handlers import mcp as mcp_mod
    from kiro_crew.mcp_gateway import launch_resolve, rewriter

    monkeypatch.setattr(mcp_mod, "kiro_agents_dir_path", lambda: toggle_home)
    name = "ns/srv" if source.startswith("alias_") else "srv"
    alias = "ns-srv" if source.startswith("alias_") else name
    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    spec = json.loads((toggle_home / "a.json").read_text(encoding="utf-8"))
    spec["mcpServers"][name] = spec["mcpServers"].pop("srv")
    (toggle_home / "a.json").write_text(json.dumps(spec), encoding="utf-8")
    if source in {"agents", "settings", "alias_vault"}:
        vault = {"command": _APPROVED_CMD, "args": [], "env": {"KEY": "secret://test-key"}}
        if source == "settings":
            settings = toggle_home.parent / "settings"
            settings.mkdir()
            (settings / "mcp.json").write_text(
                json.dumps({"mcpServers": {name: vault}}), encoding="utf-8"
            )
            other = {"name": "b", "tools": ["*"], "mcpServers": {}}
        else:
            other = {"name": "b", "tools": ["*"], "mcpServers": {alias: vault}}
        (toggle_home / "b.json").write_text(json.dumps(other), encoding="utf-8")
    monkeypatch.setattr(
        folder_use, "edition_folder_free", lambda: frozenset({alias} if source == "edition" else ())
    )
    expected = await _expected_launch(name)
    status, body = await _post(
        {"name": name, "stub": True, "expected_launch": expected, "cwd_use": "not_used"}
    )
    assert status == 200, body
    config = tmp_path / "config.json"
    data = json.loads(config.read_text(encoding="utf-8"))
    section = data["mcp_gateway"]
    if source == "alias_answer":
        section["cwd_use"] = {alias: section["cwd_use"][name]}
        section["cwd_use_launch"] = {alias: section["cwd_use_launch"][name]}
    elif source != "answer":
        section.pop("cwd_use", None)
        section.pop("cwd_use_launch", None)
    config.write_text(json.dumps(data), encoding="utf-8")
    _saw_roots(_live_socket(), alias)
    per_chat = source == "agree"

    if surface == "row":
        response = await mcp_mod.api_mcp_gateway_servers(_request(method="GET"))
        row = {r["name"]: r for r in _payload(response)["servers"]}[name]
        assert (row["cwd_roots"], row["can_stub"]) == (per_chat, not per_chat)
        if source == "edition":
            assert row["cwd_proven"] is False
    elif surface == "toggle":
        request = {"name": name, "stub": True, "expected_launch": expected}
        if source not in {"edition", "answer", "alias_answer", "agree"}:
            request["cwd_use"] = "not_used"
        status, body = await _post(request)
        if per_chat:
            assert (status, body.get("code")) == (409, "cwd_roots")
        elif source == "edition":
            assert (status, body.get("code")) == (409, "cwd_unconfirmed")
        else:
            assert status == 200, body
    else:
        cfg = KiroCrewConfig.load()
        kwargs = launch_resolve.rewrite_kwargs(cfg, frozenset(cfg.mcp_gateway.stub_servers))
        overlay = tmp_path / "boot" / "agents"
        kwargs.update(overlay_dir=overlay, work_dir=tmp_path / "work")
        _results, targets = rewriter.rewrite_agents(
            **kwargs, approvals=_approval_module().load_approvals()
        )
        rewritten = json.loads((overlay / "a.json").read_text(encoding="utf-8"))
        assert (
            bool(rewritten["mcpServers"][name].get("_kirocrew_mcp_gateway_wrapped")) is not per_chat
        )
        assert bool(_targets_running(targets, _APPROVED_CMD)) is not per_chat
        inputs = json.loads((overlay / ".rewrite-fingerprint").read_text(encoding="utf-8"))[
            "inputs"
        ]
        assert inputs["roots_excluded"] == ([name] if per_chat else [])


@pytest.mark.parametrize(
    ("name", "observed_name", "matches"),
    [
        ("ns/srv", "ns/srv", True),
        ("ns/srv", "ns-srv", True),
        ("ns-srv", "ns/srv", True),
        ("npm:@ns/srv", "ns/srv", True),
        ("ns/srv", "other/srv", False),
    ],
)
def test_roots_observations_match_canonical_aliases(name, observed_name, matches):
    assert folder_use.roots_observed(name, {observed_name}) is matches
    assert (
        folder_use.asks_for_roots({observed_name: (hazards.HAZARD_ROOTS_REQUESTED,)}, name)
        is matches
    )
    assert not folder_use.asks_for_roots({observed_name: ("other_hazard",)}, name)


def _declare_slash_server(agents, where):
    _declare(agents, where, _APPROVED_CMD, [])
    source = agents / "a.json" if where == "agent spec" else agents.parent / "settings" / "mcp.json"
    spec = json.loads(source.read_text(encoding="utf-8"))
    spec["mcpServers"]["ns/srv"] = spec["mcpServers"].pop("srv")
    source.write_text(json.dumps(spec), encoding="utf-8")
    if where == "settings mcp.json":
        (agents / "a.json").write_text(
            json.dumps({"name": "a", "mcpServers": {}, "tools": ["@ns-srv"]}),
            encoding="utf-8",
        )


@pytest.mark.asyncio
async def test_alias_roots_excludes_raw_settings_name_without_changing_refresh_policy(
    tmp_path, toggle_home
):
    from kiro_crew.mcp_gateway.overlay_refresh import rewrite_policy

    _declare_slash_server(toggle_home, "settings mcp.json")
    expected = await _expected_launch("ns-srv")
    status, body = await _post(
        {"name": "ns-srv", "stub": True, "expected_launch": expected, "cwd_use": "not_used"}
    )
    assert status == 200, body
    config = tmp_path / "config.json"
    data = json.loads(config.read_text(encoding="utf-8"))
    data["mcp_gateway"]["stub_overrides"] = {"ns/srv": True}
    data["mcp_gateway"].pop("cwd_use", None)
    data["mcp_gateway"].pop("cwd_use_launch", None)
    config.write_text(json.dumps(data), encoding="utf-8")
    _stubs, targets = _boot_rewrite(tmp_path, toggle_home)
    assert _targets_running(targets, _APPROVED_CMD)
    overlay_dir = tmp_path / "boot" / "mcp-gateway" / "agents"
    before = rewrite_policy(overlay_dir)
    assert before is not None and "ns/srv" in before["stub_servers"]

    _saw_roots(tmp_path / "gw.sock", "ns-srv")

    _stubs, targets = _boot_rewrite(tmp_path, toggle_home)
    assert _targets_running(targets, _APPROVED_CMD) == []
    assert rewrite_policy(overlay_dir) == before
    inputs = json.loads((overlay_dir / ".rewrite-fingerprint").read_text(encoding="utf-8"))[
        "inputs"
    ]
    assert "ns/srv" in inputs["stub_servers"]
    assert inputs["roots_excluded"] == ["ns/srv"]
    _stubs, cached_targets = _boot_rewrite(tmp_path, toggle_home)
    assert cached_targets == targets


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["toggle", "row"])
async def test_alias_roots_decides_the_raw_name_on_dashboard(
    tmp_path, toggle_home, monkeypatch, surface
):
    from handlers_mcp_coverage_helpers import _payload, _request

    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    monkeypatch.setattr(mcp_mod, "kiro_agents_dir_path", lambda: toggle_home)
    _declare_slash_server(toggle_home, "agent spec")
    _saw_roots(_live_socket(), "ns-srv")

    if surface == "toggle":
        status, body = await _post({"name": "ns/srv", "stub": True})
        assert (status, body.get("code")) == (409, "cwd_roots")
        assert _approval_module().load_approvals().approved == {}
    else:
        response = await mcp_mod.api_mcp_gateway_servers(_request(method="GET"))
        row = {r["name"]: r for r in _payload(response)["servers"]}["ns/srv"]
        assert (row["cwd_roots"], row["can_stub"]) == (True, False)


@pytest.mark.parametrize("spelling", ["ns/srv", "ns-srv"])
@pytest.mark.parametrize(
    ("answer", "bound", "current", "expected"),
    [
        ("not_used", "old", "new", None),
        ("not_used", "same", "same", "not_used"),
        ("not_used", None, "new", "not_used"),
        ("uses_cwd", "old", "new", "uses_cwd"),
    ],
)
def test_alias_answers_keep_the_launch_binding(spelling, answer, bound, current, expected):
    other = "ns-srv" if spelling == "ns/srv" else "ns/srv"
    bindings = {other: bound} if bound is not None else {}
    assert folder_use.effective_answer(spelling, {other: answer}, bindings, current) == expected


@pytest.mark.parametrize("source", ["service", "member"])
def test_crewmate_declarations_join_the_same_name_wide_facts(
    tmp_path, toggle_home, monkeypatch, source
):
    from kiro_crew.agent_files import MATE_CONDUCTOR_AGENT_FILENAME
    from kiro_crew.mcp_gateway import mate_overlays

    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    mates = tmp_path / "mates"
    mates.mkdir()
    path = mates / (MATE_CONDUCTOR_AGENT_FILENAME if source == "service" else "member.json")
    path.write_text(
        json.dumps(
            {"mcpServers": {"srv": {"command": _APPROVED_CMD, "env": {"KEY": "secret://test-key"}}}}
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        mate_overlays,
        "list_mate_sources",
        lambda _path: mate_overlays.MateSources((path,) if source == "member" else ()),
    )
    facts = folder_use.read_declarations(toggle_home, mates)
    assert facts.complete and facts.vault == {"srv"}
    assert facts.per_chat({"srv"}, {"srv"}, frozenset(), {}, {}) == frozenset()


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["per_chat", "row", "toggle", "rewrite"])
async def test_raw_settings_preserve_the_saved_agent_folder_answer(
    tmp_path, toggle_home, monkeypatch, surface
):
    from handlers_mcp_coverage_helpers import _payload, _request

    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    monkeypatch.setattr(mcp_mod, "kiro_agents_dir_path", lambda: toggle_home)
    _declare(toggle_home, "settings mcp.json", "npx", ["foo"])
    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    for path in (toggle_home / "a.json", toggle_home.parent / "settings" / "mcp.json"):
        spec = json.loads(path.read_text(encoding="utf-8"))
        spec["mcpServers"]["foo"] = spec["mcpServers"].pop("srv")
        path.write_text(json.dumps(spec), encoding="utf-8")
    binding = mcp_mod.declared_launch(mcp_mod._collect_server_rows()["foo"])
    assert binding is not None
    expected = await _expected_launch("foo")
    status, body = await _post(
        {"name": "foo", "stub": True, "expected_launch": expected, "cwd_use": "not_used"}
    )
    assert status == 200, body
    config = tmp_path / "config.json"
    data = json.loads(config.read_text(encoding="utf-8"))
    data["mcp_gateway"]["cwd_use_launch"] = {"foo": binding}
    config.write_text(json.dumps(data), encoding="utf-8")
    _saw_roots(_live_socket(), "foo")
    _saw_roots(tmp_path / "gw.sock", "foo")

    if surface == "per_chat":
        facts = folder_use.read_declarations(toggle_home)
        assert "foo" not in facts.per_chat(
            {"foo"}, {"foo"}, frozenset(), {"foo": "not_used"}, {"foo": binding}
        )
    elif surface == "row":
        response = await mcp_mod.api_mcp_gateway_servers(_request(method="GET"))
        row = {r["name"]: r for r in _payload(response)["servers"]}["foo"]
        assert (row["cwd_roots"], row["can_stub"]) == (False, True)
    elif surface == "toggle":
        status, body = await _post({"name": "foo", "stub": True, "expected_launch": expected})
        assert status == 200, body
        status, body = await _post(
            {"name": "foo", "stub": True, "expected_launch": expected, "cwd_use": "not_used"}
        )
        assert status == 200, body
        assert json.loads(config.read_text())["mcp_gateway"]["cwd_use_launch"] == {"foo": binding}
    else:
        _stubs, targets = _boot_rewrite(tmp_path, toggle_home)
        assert _targets_running(targets, _APPROVED_CMD)
        overlay = tmp_path / "boot" / "mcp-gateway" / "agents"
        rewritten = json.loads((overlay / "a.json").read_text(encoding="utf-8"))
        assert rewritten["mcpServers"]["foo"]["_kirocrew_mcp_gateway_wrapped"]
        inputs = json.loads((overlay / ".rewrite-fingerprint").read_text(encoding="utf-8"))[
            "inputs"
        ]
        assert inputs["roots_excluded"] == []


@pytest.mark.parametrize("source", ["service", "member"])
def test_folder_bindings_match_agent_rows_with_settings_and_crewmates(
    tmp_path, toggle_home, monkeypatch, source
):
    from kiro_crew.agent_files import MATE_CONDUCTOR_AGENT_FILENAME
    from kiro_crew.dashboard.handlers import mcp as mcp_mod
    from kiro_crew.mcp_gateway import mate_overlays

    monkeypatch.setattr(mcp_mod, "kiro_agents_dir_path", lambda: toggle_home)
    _declare(toggle_home, "settings mcp.json", "npx", ["srv"])
    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    (toggle_home / "b.json").write_text(
        json.dumps({"mcpServers": {"ns/srv": {"command": _APPROVED_CMD, "args": ["one"]}}}),
        encoding="utf-8",
    )
    (toggle_home / "c.json").write_text(
        json.dumps({"mcpServers": {"ns-srv": {"command": _APPROVED_CMD, "args": ["two"]}}}),
        encoding="utf-8",
    )
    mates = tmp_path / "mates"
    mates.mkdir()
    path = mates / (MATE_CONDUCTOR_AGENT_FILENAME if source == "service" else "member.json")
    path.write_text(
        json.dumps({"mcpServers": {"srv": {"command": _APPROVED_CMD, "args": ["mate"]}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        mate_overlays,
        "list_mate_sources",
        lambda _path: mate_overlays.MateSources((path,) if source == "member" else ()),
    )
    facts = folder_use.read_declarations(toggle_home, mates)
    rows = mcp_mod._collect_server_rows()
    for name in ("srv", "ns/srv", "ns-srv"):
        assert facts.launch(name) == mcp_mod.declared_launch(rows[name])
    assert facts.launch("missing") is None
    assert facts.launch("ns/srv") != facts.launch("ns-srv")
    facts.complete = False
    assert facts.launch("srv") == mcp_mod.declared_launch(rows["srv"])


def test_a_builtin_alias_does_not_clear_the_folder_question():
    assert "kirocrew-guide" in folder_use.KIROCREW_BIN_MCP_SERVERS
    assert "kirocrew/guide" not in folder_use.KIROCREW_BIN_MCP_SERVERS
    assert folder_use.folder_free_source("kirocrew/guide", frozenset()) is None
    assert folder_use.folder_free_source("kirocrew-guide", frozenset()) == folder_use.SOURCE_CREW


def test_incomplete_declarations_still_invalidate_a_stale_folder_answer():
    facts = folder_use.FolderDeclarations(launches={"srv": {"current"}}, complete=False)
    current = facts.launch("srv")
    assert current == "current"
    assert folder_use.effective_answer("srv", {"srv": "not_used"}, {"srv": "old"}, current) is None
    assert facts.per_chat({"srv"}, {"srv"}, frozenset(), {}, {}) == frozenset()


@pytest.mark.parametrize("spelling", ["ns/srv", "ns-srv"])
@pytest.mark.parametrize(
    ("answer", "bound", "expected"),
    [
        ("uses_cwd", None, "uses_cwd"),
        ("not_used", "current", "not_used"),
        ("not_used", "old", "uses_cwd"),
        ("invalid", None, "uses_cwd"),
    ],
)
def test_exact_answer_wins_before_alias_fallback(spelling, answer, bound, expected):
    other = "ns-srv" if spelling == "ns/srv" else "ns/srv"
    alias_answer = "not_used" if answer == "uses_cwd" else "uses_cwd"
    assert (
        folder_use.effective_answer(
            spelling, {spelling: answer, other: alias_answer}, {spelling: bound}, "current"
        )
        == expected
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["row", "toggle", "per_chat"])
@pytest.mark.parametrize("saw_roots", [False, True])
async def test_exact_folder_answer_outranks_conflicting_alias(
    tmp_path, toggle_home, monkeypatch, surface, saw_roots
):
    from handlers_mcp_coverage_helpers import _payload, _request

    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    monkeypatch.setattr(mcp_mod, "kiro_agents_dir_path", lambda: toggle_home)
    _declare_slash_server(toggle_home, "agent spec")
    answers = {"ns/srv": "uses_cwd", "ns-srv": "not_used"}
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"mcp_gateway": {"cwd_use": answers}}), encoding="utf-8")
    if saw_roots:
        _saw_roots(_live_socket(), "ns-srv")

    if surface == "row":
        response = await mcp_mod.api_mcp_gateway_servers(_request(method="GET"))
        row = {r["name"]: r for r in _payload(response)["servers"]}["ns/srv"]
        assert row["cwd_use"] == "uses_cwd"
        assert row["cwd_roots"] is saw_roots
    elif surface == "per_chat":
        facts = folder_use.read_declarations(toggle_home)
        assert facts.per_chat(
            {"ns/srv"}, {"ns-srv"} if saw_roots else set(), frozenset(), answers, {}
        ) == (frozenset({"ns/srv"}) if saw_roots else frozenset())
    else:
        expected = await _expected_launch("ns/srv")
        status, body = await _post({"name": "ns/srv", "stub": True, "expected_launch": expected})
        assert (status, body.get("code")) == (409, "cwd_roots" if saw_roots else "cwd_unconfirmed")
        assert _approval_module().load_approvals().approved == {}
        assert json.loads(config.read_text())["mcp_gateway"]["cwd_use"] == answers
        status, body = await _post({"name": "ns/srv", "cwd_use": "not_used"})
        assert status == 200, body
        assert json.loads(config.read_text())["mcp_gateway"]["cwd_use"] == {
            "ns/srv": "not_used",
            "ns-srv": "not_used",
        }
        status, body = await _post({"name": "ns/srv", "stub": True, "expected_launch": expected})
        assert status == 200, body
        stubs, targets = _boot_rewrite(tmp_path, toggle_home)
        assert "ns/srv" in stubs and _targets_running(targets, _APPROVED_CMD)


def test_an_unreadable_agent_spec_is_skipped_and_launches_still_bind(
    tmp_path, toggle_home, monkeypatch
):
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    monkeypatch.setattr(mcp_mod, "kiro_agents_dir_path", lambda: toggle_home)
    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    (toggle_home / "dangling.json").mkdir()

    facts = folder_use.read_declarations(toggle_home)

    assert facts.complete
    current = mcp_mod.declared_launch(mcp_mod._collect_server_rows()["srv"])
    assert current is not None and facts.launch("srv") == current
    # A not_used answer bound to an older launch is not an answer for this one.
    assert folder_use.effective_answer("srv", {"srv": "not_used"}, {"srv": "old"}, current) is None


@pytest.mark.asyncio
async def test_an_unreadable_agent_spec_still_binds_a_new_answer(
    tmp_path, toggle_home, monkeypatch
):
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    monkeypatch.setattr(mcp_mod, "kiro_agents_dir_path", lambda: toggle_home)
    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    (toggle_home / "dangling.json").mkdir()

    status, body = await _post({"name": "srv", "cwd_use": "not_used"})

    assert status == 200, body
    current = mcp_mod.declared_launch(mcp_mod._collect_server_rows()["srv"])
    config = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert config["mcp_gateway"]["cwd_use_launch"] == {"srv": current}


def _answer_request(body: dict, apply):
    from aiohttp import web
    from body_stream_helpers import attach_body
    from dashboard_owner_helpers import owner_claims

    request = MagicMock(spec=web.Request)
    request.app = {"state": SimpleNamespace(_mcp_gateway_apply_stub=apply)}
    attach_body(request, body)
    return owner_claims(request)


async def _answer_with_broker(body: dict) -> tuple[int, dict, list[bool]]:
    """Post *body* to the stub route with a broker apply that records each call."""
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    calls: list[bool] = []

    async def apply(*, launch_approved: bool = False) -> dict:
        calls.append(launch_approved)
        return {"applied": False, "restart_required": True, "stub_servers": ["srv"]}

    response = await mcp_mod.api_mcp_gateway_set_stub(_answer_request(body, apply))
    return response.status, json.loads(response.body), calls


async def _approve_current_launch() -> None:
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    launches = await asyncio.to_thread(
        mcp_mod.launch_resolve.resolve_launches, ["srv"], refresh_specs=False
    )
    await asyncio.to_thread(_approval_module().approve, {"srv": launches["srv"]})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("section", "approved", "routes"),
    [
        # r12: an exact uses_cwd answer took a rostered server off routing.
        ({"stub_servers": ["srv"], "cwd_use": {"srv": "uses_cwd"}}, True, True),
        # r11: a rostered server with no answer, kept per chat by roots alone.
        ({"stub_servers": ["srv"]}, True, True),
        # Not in the roster: saving the answer routes nothing.
        ({}, True, False),
        # Declined: the operator's off decision outranks the roster.
        ({"stub_servers": ["srv"], "stub_overrides": {"srv": False}}, True, False),
    ],
    ids=["exact-uses-cwd", "unanswered", "not-rostered", "declined"],
)
async def test_an_answer_that_routes_a_server_runs_the_broker_apply(
    tmp_path, toggle_home, monkeypatch, section, approved, routes
):
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    monkeypatch.setattr(mcp_mod, "kiro_agents_dir_path", lambda: toggle_home)
    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    (tmp_path / "config.json").write_text(json.dumps({"mcp_gateway": section}), encoding="utf-8")
    if approved:
        await _approve_current_launch()
    await asyncio.to_thread(_saw_roots, _live_socket())

    status, body, calls = await _answer_with_broker({"name": "srv", "cwd_use": "not_used"})

    assert status == 200, body
    config = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert config["mcp_gateway"]["cwd_use"] == {"srv": "not_used"}
    if routes:
        assert calls == [True]
        assert (body["applied"], body["restart_required"], body["routing_changed"]) == (
            False,
            True,
            True,
        )
    else:
        assert calls == []
        assert (body["applied"], body["restart_required"], body["routing_changed"]) == (
            True,
            False,
            False,
        )


@pytest.mark.asyncio
async def test_an_answer_that_routes_an_unapproved_launch_asks_for_the_launch(
    tmp_path, toggle_home, monkeypatch
):
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    monkeypatch.setattr(mcp_mod, "kiro_agents_dir_path", lambda: toggle_home)
    _declare(toggle_home, "agent spec", _APPROVED_CMD, [])
    section = {"stub_servers": ["srv"], "cwd_use": {"srv": "uses_cwd"}}
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"mcp_gateway": section}), encoding="utf-8")
    await asyncio.to_thread(_saw_roots, _live_socket())

    status, body, calls = await _answer_with_broker({"name": "srv", "cwd_use": "not_used"})

    assert (status, body["code"], calls) == (409, "expected_launch_required", [])
    assert json.loads(config.read_text(encoding="utf-8"))["mcp_gateway"] == section

    # The page's preview then sends the launch with the answer in one request.
    expected = await _expected_launch()
    status, body, calls = await _answer_with_broker(
        {"name": "srv", "stub": True, "expected_launch": expected, "cwd_use": "not_used"}
    )

    assert status == 200, body
    assert calls == [True]
    assert json.loads(config.read_text(encoding="utf-8"))["mcp_gateway"]["cwd_use"] == {
        "srv": "not_used"
    }
    assert _approval_module().load_approvals().approved != {}


@pytest.mark.asyncio
@pytest.mark.parametrize("share", [False, True], ids=["roots-answer", "share-answer"])
async def test_a_folder_answer_binds_the_launch_after_specs_rebuild(
    tmp_path, toggle_home, monkeypatch, share
):
    from handlers_mcp_coverage_helpers import _payload, _request

    from kiro_crew import agent
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    monkeypatch.setattr(mcp_mod, "kiro_agents_dir_path", lambda: toggle_home)
    _declare(toggle_home, "settings mcp.json", _APPROVED_CMD, ["new"])
    expected = await _expected_launch()
    _declare(toggle_home, "agent spec", _APPROVED_CMD, ["old"])
    before = mcp_mod._folder_declarations().launch("srv")
    settings = toggle_home.parent / "settings" / "mcp.json"
    source = toggle_home / "a.json"

    def rebuild(*args, **kwargs):
        spec = json.loads(source.read_text(encoding="utf-8"))
        spec["mcpServers"] = json.loads(settings.read_text(encoding="utf-8"))["mcpServers"]
        source.write_text(json.dumps(spec), encoding="utf-8")

    monkeypatch.setattr(agent, "rebuild_agent_config", rebuild)
    _saw_roots(_live_socket())
    request = {"name": "srv", "cwd_use": "not_used"}
    if share:
        request.update(stub=True, expected_launch=expected)

    status, body = await _post(request)

    assert status == 200, body
    current = mcp_mod._folder_declarations().launch("srv")
    assert current is not None and current != before
    config = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert config["mcp_gateway"]["cwd_use_launch"] == {"srv": current}
    response = await mcp_mod.api_mcp_gateway_servers(_request(method="GET"))
    row = {r["name"]: r for r in _payload(response)["servers"]}["srv"]
    assert (row["cwd_roots"], row["can_stub"], row["cwd_use"]) == (False, True, "not_used")
