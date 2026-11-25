"""A tool toggle on a raw-key MCP server's row reaches the one server mounted for it.

A server stored as ``npm:@playwright/mcp`` is mounted, and listed, as
``playwright-mcp``. Each case drives ``POST /api/mcp/apply`` the way the MCP tab
sends it, through the real non-clean rebuild, then reads the store, the rendered
``kirocrew.json`` and ``GET /api/mcp``. The edited raw entry must reach the one
``playwright-mcp`` mount rather than mount again as ``playwright-mcp-2``, and with a
Kiro global that declares ``playwright-mcp`` itself the toggle must reach that
declared mount rather than the raw key's ``playwright-mcp-2``.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from body_stream_helpers import attach_body
from dashboard_owner_helpers import owner_claims
from mcp_merge_helpers import bundled_defaults

from kiro_crew import agent as agent_mod
from kiro_crew.agent_materialization import mcp_aliases
from kiro_crew.config.paths import kiro_agents_dir
from kiro_crew.dashboard.handlers import mcp as mcp_mod
from kiro_crew.kiro_cli import SPEC_PERMISSIONS_MIN_VERSION
from kiro_crew.mcp_discovery import SCOPE_KIRO_GLOBAL, SCOPE_KIROCREW

RAW = "npm:@playwright/mcp"
ALIAS = "playwright-mcp"
TOOL = "browser_install"


class _NoLock:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *args: object) -> None:
        return None


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """An isolated store, Kiro global and agents dir that the rebuild, the apply
    writers and the listing all read."""
    config_dir = bundled_defaults(tmp_path)
    store = mcp_mod._kirocrew_mcp_json()
    assert store == Path(agent_mod._user_dir()) / "mcp.json"
    kiro_global = tmp_path / "kiro-global-mcp.json"
    mc_config = tmp_path / "config.json"
    mc_config.write_text(json.dumps({"agent": {"kiro_hooks_autoimport": False}}), encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    commands = {}
    for label in ("raw", "global"):
        command = bin_dir / f"{label}-server"
        command.write_text("#!/bin/sh\n", encoding="utf-8")
        command.chmod(0o755)
        commands[label] = str(command)

    monkeypatch.setattr(
        "kiro_crew.kiro_cli.installed_kiro_cli_version", lambda: SPEC_PERMISSIONS_MIN_VERSION
    )
    monkeypatch.setattr(agent_mod, "_BUNDLED_CFG_DIR", config_dir)
    monkeypatch.setattr(agent_mod, "_KIRO_MCP_JSON", kiro_global)
    monkeypatch.setattr(agent_mod, "_MANAGED_MCP_SERVERS", {})
    monkeypatch.setattr(agent_mod, "_prompt_path", lambda: config_dir / "prompt.md")
    monkeypatch.setattr(agent_mod, "_shipped_defaults", lambda: config_dir / "defaults.json")
    monkeypatch.setattr(agent_mod, "_project_dir", lambda: None)
    monkeypatch.setattr(agent_mod, "_aim_skill_paths", list)
    monkeypatch.setattr(agent_mod, "_mc_config_path", lambda: mc_config)
    monkeypatch.setattr(agent_mod, "_extra_mcp_scope_globals", list)
    monkeypatch.setattr(mcp_mod, "_GLOBAL_MCP_JSON", kiro_global)
    monkeypatch.setattr(mcp_mod, "_extra_mcp_scopes", list)
    monkeypatch.setattr(mcp_mod, "_get_mcp_lock", _NoLock)
    monkeypatch.setattr(mcp_mod, "sel", MagicMock)
    monkeypatch.setattr(
        "kiro_crew.mcp_discovery._MCP_SOURCES",
        ((store, SCOPE_KIROCREW), (kiro_global, SCOPE_KIRO_GLOBAL)),
    )
    monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (store, kiro_global))
    # The listing would arm a background re-probe that spawns every server.
    monkeypatch.setattr(mcp_mod, "_mcp_probe_in_progress", True)
    monkeypatch.setattr(mcp_mod, "_bg_mcp_probe", AsyncMock(return_value=None))
    monkeypatch.setattr(mcp_mod, "_mcp_probe_cache", [])
    return SimpleNamespace(store=store, kiro_global=kiro_global, commands=commands)


def _write(path: Path, servers: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


def _apply_request(change: dict) -> MagicMock:
    state = MagicMock()
    state._background_tasks = set()
    state.owner_id = ""
    request = MagicMock(spec=web.Request)
    request.app = {"state": state}
    attach_body(request, {"changes": [change]})
    return owner_claims(request)


def _list_request() -> dict:
    class _Req(dict):
        def __init__(self) -> None:
            super().__init__(user="local-app", app="")
            self.app = {
                "state": SimpleNamespace(
                    _background_tasks=set(),
                    sessions=SimpleNamespace(final_drain_started=False),
                )
            }

    return _Req()


def _mounted() -> dict:
    spec = json.loads((kiro_agents_dir() / "kirocrew.json").read_text(encoding="utf-8"))
    return {k: v for k, v in spec["mcpServers"].items() if mcp_aliases._is_alias_family(k, ALIAS)}


async def _toggle(*, enabled: bool, kiro_global: bool) -> dict:
    change = {
        "name": ALIAS,
        "kirocrew": True,
        "kiroGlobal": kiro_global,
        "toolOverrides": {TOOL: enabled},
    }
    response = await mcp_mod.api_mcp_apply(_apply_request(change))
    assert response.status == 200
    body = json.loads(response.body)
    assert body["rebuild"]["ok"] is True
    return body


async def _listed_rows() -> list[str]:
    response = await mcp_mod.api_mcp_servers(_list_request())
    assert response.status == 200
    return [row["name"] for row in json.loads(response.body) if ALIAS in row["name"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False], ids=["re-enable", "disable"])
async def test_a_raw_key_tool_toggle_mounts_the_server_once(
    home: SimpleNamespace, enabled: bool
) -> None:
    """Layout A (re-enable a tool the raw entry holds off) and its disable twin."""
    raw = {"command": home.commands["raw"]}
    before = dict(raw, disabledTools=[TOOL]) if enabled else raw
    after = raw if enabled else dict(raw, disabledTools=[TOOL])
    _write(home.store, {RAW: before})
    agent_mod.rebuild_agent_config(refresh_forks=False)
    assert set(_mounted()) == {ALIAS}

    body = await _toggle(enabled=enabled, kiro_global=False)

    assert body["results"][0]["actions"]["tools"] == [TOOL]
    assert json.loads(home.store.read_text(encoding="utf-8"))["mcpServers"] == {RAW: after}
    mounted = _mounted()
    assert set(mounted) == {ALIAS}
    assert mounted[ALIAS].get("disabledTools", []) == after.get("disabledTools", [])
    assert await _listed_rows() == [ALIAS]


@pytest.mark.asyncio
async def test_a_tool_toggle_reaches_an_alias_the_kiro_global_declares(
    home: SimpleNamespace,
) -> None:
    """The Kiro global declares ``playwright-mcp`` with its own program, so the raw
    store key mounts beside it as ``playwright-mcp-2``. Switching a tool off from the
    ``playwright-mcp`` row switches it off on that mount and leaves the raw entry,
    which is the other server, as it was."""
    raw = {"command": home.commands["raw"]}
    _write(home.kiro_global, {ALIAS: {"command": home.commands["global"]}})
    _write(home.store, {RAW: raw})
    agent_mod.rebuild_agent_config(refresh_forks=False)
    assert set(_mounted()) == {ALIAS, f"{ALIAS}-2"}

    await _toggle(enabled=False, kiro_global=True)

    stored = json.loads(home.store.read_text(encoding="utf-8"))["mcpServers"]
    assert stored[RAW] == raw
    assert stored[ALIAS]["disabledTools"] == [TOOL]
    mounted = _mounted()
    assert set(mounted) == {ALIAS, f"{ALIAS}-2"}
    assert mounted[ALIAS]["command"] == home.commands["global"]
    assert mounted[ALIAS]["disabledTools"] == [TOOL]
    assert mounted[f"{ALIAS}-2"]["command"] == home.commands["raw"]
    assert "disabledTools" not in mounted[f"{ALIAS}-2"]


@pytest.mark.asyncio
async def test_a_toggle_seeds_a_newly_declared_alias_from_its_declaration(
    home: SimpleNamespace,
) -> None:
    """The Kiro global starts declaring ``playwright-mcp`` after the raw key was
    rendered under that name, and a tool is switched off before any rebuild. The
    alias-keyed store entry takes the Kiro global's program, not the raw key's
    program the rendered spec still holds under the alias."""
    raw = {"command": home.commands["raw"]}
    _write(home.store, {RAW: raw})
    agent_mod.rebuild_agent_config(refresh_forks=False)
    assert set(_mounted()) == {ALIAS}
    _write(home.kiro_global, {ALIAS: {"command": home.commands["global"]}})

    await _toggle(enabled=False, kiro_global=True)

    stored = json.loads(home.store.read_text(encoding="utf-8"))["mcpServers"]
    assert stored[RAW] == raw
    assert stored[ALIAS] == {"command": home.commands["global"], "disabledTools": [TOOL]}
    mounted = _mounted()
    assert set(mounted) == {ALIAS, f"{ALIAS}-2"}
    assert mounted[ALIAS]["command"] == home.commands["global"]
    assert mounted[ALIAS]["disabledTools"] == [TOOL]
    assert mounted[f"{ALIAS}-2"]["command"] == home.commands["raw"]
    assert "disabledTools" not in mounted[f"{ALIAS}-2"]


@pytest.mark.parametrize("scope", ["store", "kiro-global"])
def test_a_list_added_to_a_raw_key_by_hand_updates_its_one_mount(
    home: SimpleNamespace, scope: str
) -> None:
    """The raw entry gains ``disabledTools`` by hand after its first rebuild, in the
    store or in the Kiro global, and the next non-clean rebuild carries it."""
    path = home.store if scope == "store" else home.kiro_global
    raw = {"command": home.commands["raw"]}
    _write(path, {RAW: raw})
    agent_mod.rebuild_agent_config(refresh_forks=False)
    assert set(_mounted()) == {ALIAS}

    _write(path, {RAW: dict(raw, disabledTools=[TOOL])})
    agent_mod.rebuild_agent_config(refresh_forks=False)

    mounted = _mounted()
    assert set(mounted) == {ALIAS}
    assert mounted[ALIAS]["disabledTools"] == [TOOL]


def test_a_kiro_global_guard_survives_a_store_entry_that_omits_the_list(
    home: SimpleNamespace,
) -> None:
    """The alias takes the MERGED raw entry's list, so a guard the Kiro global sets on
    the raw key is kept when the store's copy of that key has no list."""
    raw = {"command": home.commands["raw"]}
    _write(home.kiro_global, {RAW: dict(raw, disabledTools=[TOOL])})
    _write(home.store, {RAW: raw})
    agent_mod.rebuild_agent_config(refresh_forks=False)
    agent_mod.rebuild_agent_config(refresh_forks=False)

    mounted = _mounted()
    assert set(mounted) == {ALIAS}
    assert mounted[ALIAS]["disabledTools"] == [TOOL]


def test_an_alias_the_kiro_global_declares_keeps_its_own_list(home: SimpleNamespace) -> None:
    """A ``playwright-mcp`` entry a source declares is not the raw key's rendering, so
    the raw key's list is not projected onto it even when the programs match."""
    raw = {"command": home.commands["raw"]}
    _write(home.kiro_global, {ALIAS: dict(raw, disabledTools=[TOOL])})
    _write(home.store, {RAW: raw})
    agent_mod.rebuild_agent_config(refresh_forks=False)
    agent_mod.rebuild_agent_config(refresh_forks=False)

    mounted = _mounted()
    assert mounted[ALIAS]["disabledTools"] == [TOOL]
    assert "disabledTools" not in mounted[f"{ALIAS}-2"]


def test_a_spec_already_holding_both_mounts_folds_back_to_one(
    home: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A spec a rebuild without the reconcile and the alias adoption left with
    ``playwright-mcp`` (stale list) beside ``playwright-mcp-2`` heals on the next
    rebuild: one mount, the source's list, and no ref left naming the second mount."""
    raw = {"command": home.commands["raw"]}
    _write(home.store, {RAW: dict(raw, disabledTools=[TOOL])})
    agent_mod.rebuild_agent_config(refresh_forks=False)
    _write(home.store, {RAW: raw})
    with monkeypatch.context() as patched:
        patched.setattr(
            "kiro_crew.agent_materialization.mcp_sources.reconcile_raw_key_tool_overrides",
            lambda config, sources: None,
        )
        patched.setattr(
            "kiro_crew.agent_materialization.mcp_sources._adopt_stale_alias_rendering",
            lambda *args, **kwargs: None,
        )
        agent_mod.rebuild_agent_config(refresh_forks=False)
    assert set(_mounted()) == {ALIAS, f"{ALIAS}-2"}

    agent_mod.rebuild_agent_config(refresh_forks=False)

    spec = json.loads((kiro_agents_dir() / "kirocrew.json").read_text(encoding="utf-8"))
    assert set(_mounted()) == {ALIAS}
    assert "disabledTools" not in spec["mcpServers"][ALIAS]
    refs = [*spec.get("tools", []), *spec.get("allowedTools", [])]
    assert not [ref for ref in refs if isinstance(ref, str) and f"{ALIAS}-2" in ref]
