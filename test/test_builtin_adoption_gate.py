"""The tool gate's own-server grant follows a former built-in adopted from its pin.

The ``app-own-server`` grant auto-approves an app's call to its OWN declared MCP
server for a first-party app. Adoption moves a built-in's record onto its own
repository (``origin=registry``), which the builtin-owned sets do not name, so
``builtin_adoption_gate.adopted_app_own_mcp_servers`` names the servers an adopted
record keeps and the boot warm adds them to the gate's sets. These tests pin who keeps
the grant (the adopted record of a pinned app, trusted at the pin's repository, whose
installed server is the one adoption carried) and who does not (a third-party record
with the same server name, a mismatched launch, a grant bound elsewhere), at the set
and at the gate, and end to end through a real STAGE and APPLY.
"""

from __future__ import annotations

import json
import os
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest
from builtin_adoption_helpers import (
    NAME,
    REPOSITORY,
    VERSION,
    build_world,
    install_pins,
    pin_row,
    repository_manifest,
    requires_git,
    seed_agent,
    shipped_manifest,
    write_builtin_record,
    write_tree,
)

from kiro_crew import hooks
from kiro_crew.apps import execution as exec_mod
from kiro_crew.apps import manager
from kiro_crew.apps.builtin_adoption_gate import adopted_app_own_mcp_servers
from kiro_crew.hooks import TOOL_AUTO_APPROVE, HookManager, HooksConfig, ToolCall

LAUNCHER = "backend/mcp_launch.py"
SERVER = "srv"
KEY = f"{NAME}:{SERVER}"
TOOLS = ["get_status", "list_items"]
OTHER_REPOSITORY = "https://github.com/SomeoneElse/kirocrew-app-fixture-former"

#: The wheel built-in's declaration: its server runs the wheel package by module.
WHEEL_SERVER = {
    "command": "python3",
    "args": ["-m", "kiro_crew.apps.builtins.fixture_former.backend.mcp_server"],
    "autoApprove": TOOLS,
}
#: The repository copy's: the same server, started from the app's own launcher.
TREE_SERVER = {"command": "python3", "args": [LAUNCHER], "autoApprove": TOOLS}
AGENT = "fixture-former-helper"


@pytest.fixture
def gate_sets(monkeypatch: pytest.MonkeyPatch):
    """Restore the gate's three boot-warmed registries after the test."""
    for name in ("_BUILTIN_APP_NAMES", "_BUILTIN_APP_MCP_SERVERS", "_BUILTIN_APP_AGENTS"):
        monkeypatch.setattr(hooks, name, getattr(hooks, name))


def _wheel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The wheel copy declaring ``srv`` and one agent, and a pin for it."""
    wheel_root = tmp_path / "wheel"
    write_tree(
        wheel_root / "fixture_former",
        shipped_manifest(mcpServers={SERVER: WHEEL_SERVER}, agents=["agents/helper.json"]),
        {"agents/helper.json": json.dumps({"name": AGENT})},
    )
    monkeypatch.setattr(exec_mod, "_builtin_manifest_sources", lambda: (wheel_root.resolve(),))
    install_pins(
        monkeypatch, tmp_path / "adoption-pins.json", [pin_row("a" * 40, "b" * 64, "c" * 64)]
    )


def _record(
    *,
    name: str = NAME,
    source_url: str = REPOSITORY,
    servers: dict | None = None,
    files: tuple[str, ...] = (LAUNCHER,),
) -> None:
    """An installed record for *name* from *source_url*, its tree declaring *servers*."""
    dest = manager.app_dir(name)
    manifest = repository_manifest(
        name=name, mcpServers={SERVER: TREE_SERVER} if servers is None else servers
    )
    write_tree(dest, manifest, {rel: "print('server')\n" for rel in files})
    manager._write_installed(
        name,
        manager.InstalledApp(
            name=name,
            version=VERSION,
            displayName="Fixture Former",
            source=f"registry:{name}",
            origin="registry",
            lifecycle="gateway",
            sourceUrl=source_url,
            sourceCommit="a" * 40,
        ),
    )


def _home() -> Path:
    return Path(os.environ["KIROCREW_HOME"])


def _trust(name: str = NAME, repository: str = REPOSITORY) -> None:
    seed_agent(_home(), {"apps_trusted": [name], "apps_trusted_repositories": {name: repository}})


@pytest.fixture
def adopted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The pinned app adopted from its repository, trusted at that repository."""
    _wheel(tmp_path, monkeypatch)
    _record()
    _trust()


def test_an_adopted_first_party_record_keeps_its_declared_server(adopted):
    assert adopted_app_own_mcp_servers() == {KEY}
    assert KEY not in exec_mod.builtin_app_mcp_servers(), "an adopted record is not builtin-owned"


def test_a_third_party_record_with_the_same_server_name_is_not_trusted(tmp_path, monkeypatch):
    """Same app name, same server name, installed and trusted from another repository."""
    _wheel(tmp_path, monkeypatch)
    _record(source_url=OTHER_REPOSITORY)
    _trust(repository=OTHER_REPOSITORY)
    assert adopted_app_own_mcp_servers() == frozenset()


def test_a_third_party_app_declaring_the_same_server_is_not_named(adopted):
    """An app no pin names never contributes a key, whatever its server is called."""
    _record(name="someone-else", source_url=REPOSITORY)
    seed_agent(
        _home(),
        {
            "apps_trusted": [NAME, "someone-else"],
            "apps_trusted_repositories": {NAME: REPOSITORY, "someone-else": REPOSITORY},
        },
    )
    assert adopted_app_own_mcp_servers() == {KEY}


@pytest.mark.parametrize(
    "launch",
    [
        WHEEL_SERVER,
        {**TREE_SERVER, "args": ["../outside.py"]},
        {**TREE_SERVER, "command": "node"},
        {**TREE_SERVER, "args": ["backend/missing.py"]},
        {**TREE_SERVER, "autoApprove": [*TOOLS, "delete_everything"]},
        {**TREE_SERVER, "env": {"PYTHONPATH": "/elsewhere"}},
    ],
    ids=["wheel-module", "outside-the-tree", "node", "missing-file", "new-tool", "new-env"],
)
def test_a_mismatched_launch_is_not_trusted(tmp_path, monkeypatch, launch):
    _wheel(tmp_path, monkeypatch)
    (manager.apps_dir() / "outside.py").parent.mkdir(parents=True, exist_ok=True)
    (manager.apps_dir() / "outside.py").write_text("print('not the app')\n", encoding="utf-8")
    _record(servers={SERVER: launch})
    _trust()
    assert adopted_app_own_mcp_servers() == frozenset()


def test_a_server_the_wheel_never_declared_is_not_trusted(adopted):
    _record(servers={SERVER: TREE_SERVER, "extra": TREE_SERVER})
    assert adopted_app_own_mcp_servers() == {KEY}


@pytest.mark.parametrize("trust", ["none", "other-repository", "name-only"])
def test_the_grant_needs_execution_trust_bound_to_the_pin_repository(tmp_path, monkeypatch, trust):
    _wheel(tmp_path, monkeypatch)
    _record()
    if trust == "other-repository":
        _trust(repository=OTHER_REPOSITORY)
    elif trust == "name-only":
        seed_agent(_home(), {"apps_trusted": [NAME]})
    assert adopted_app_own_mcp_servers() == frozenset()


def test_a_builtin_owned_record_is_left_to_the_builtin_sets(tmp_path, monkeypatch):
    _wheel(tmp_path, monkeypatch)
    write_builtin_record(manifest=shipped_manifest(mcpServers={SERVER: WHEEL_SERVER}))
    _trust()
    assert adopted_app_own_mcp_servers() == frozenset()
    assert KEY in exec_mod.builtin_app_mcp_servers()


def test_an_unpinned_app_is_never_named(adopted, monkeypatch, tmp_path):
    install_pins(monkeypatch, tmp_path / "no-pins.json", [])
    assert adopted_app_own_mcp_servers() == frozenset()


def test_an_unreadable_installed_manifest_fails_closed(adopted):
    (manager.app_dir(NAME) / "app.json").write_text("{not json", encoding="utf-8")
    assert adopted_app_own_mcp_servers() == frozenset()


# -- at the gate, after the boot warm ---------------------------------------------


async def _warm() -> None:
    from kiro_crew.dashboard import server

    await server._warm_builtin_app_names()


def _own_server_call(server: str = KEY, **fields) -> ToolCall:
    return ToolCall(
        title="Get the status",
        mcp_server=server,
        mcp_tool="get_status",
        identity_trusted=True,
        **fields,
    )


def _granted(call: ToolCall, **surface) -> bool:
    """Whether the own-server grant decided *call* (the read-only tier may approve it too)."""
    result = HookManager(HooksConfig()).judge(call, **surface)
    return result.tier == "app-own-server" and result.action == TOOL_AUTO_APPROVE


@pytest.mark.asyncio
async def test_the_warmed_gate_auto_approves_the_adopted_apps_own_server(adopted, gate_sets):
    await _warm()
    assert _granted(_own_server_call(), app=NAME)
    # A builtin whose slot carries no app identity recovers it from its agent, and so
    # does the adopted record: the agent names come from the wheel's own manifest.
    assert _granted(_own_server_call(resolved_agent=AGENT))


@pytest.mark.asyncio
async def test_the_warmed_gate_asks_for_a_third_party_record_with_the_same_server(
    tmp_path, monkeypatch, gate_sets
):
    _wheel(tmp_path, monkeypatch)
    _record(source_url=OTHER_REPOSITORY)
    _trust(repository=OTHER_REPOSITORY)
    await _warm()
    assert not _granted(_own_server_call(), app=NAME)
    assert not _granted(_own_server_call(resolved_agent=AGENT))


@pytest.mark.asyncio
async def test_the_warmed_gate_asks_for_a_mismatched_launch(tmp_path, monkeypatch, gate_sets):
    _wheel(tmp_path, monkeypatch)
    _record(servers={SERVER: WHEEL_SERVER})
    _trust()
    await _warm()
    assert not _granted(_own_server_call(), app=NAME)


# -- end to end: a real STAGE and APPLY -------------------------------------------


@requires_git
@pytest.mark.asyncio
async def test_adoption_leaves_the_own_server_grant_as_it_was(tmp_path, monkeypatch, gate_sets):
    """The wheel copy's grant before adoption is the adopted record's grant after it."""
    import kiro_crew.sel as sel_mod
    from kiro_crew.apps import builtin_adoption as adoption
    from kiro_crew.config.loader import _invalidate_config_cache

    monkeypatch.setattr(
        sel_mod, "sel", lambda: types.SimpleNamespace(log_api_access=lambda **_kw: None)
    )
    wheel = shipped_manifest(mcpServers={SERVER: WHEEL_SERVER})
    build_world(
        tmp_path,
        monkeypatch,
        wheel_manifest=wheel,
        tree_files={LAUNCHER: "print('server')\n"},
        mcpServers={SERVER: TREE_SERVER},
    )
    write_builtin_record(manifest=wheel)
    _invalidate_config_cache()

    await _warm()
    assert _granted(_own_server_call(), app=NAME)

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert await adoption.run_staging_pass(now=lambda: now) == {NAME: "staged"}
    assert await adoption.apply_builtin_adoptions() == {NAME: "applied"}
    assert KEY not in exec_mod.builtin_app_mcp_servers()

    await _warm()
    assert _granted(_own_server_call(), app=NAME)
