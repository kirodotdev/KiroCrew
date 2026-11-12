"""E10 for ``mcpServers``: the stdio servers a former built-in carries through adoption.

A pinned tree may declare a stdio MCP server only when it starts that server from a
``.py`` file inside the tree and asks for nothing the wheel copy did not declare.
The accepted shape is the one registration anchors to the installed app root, so
these tests pin both halves: what the check accepts and refuses, that a refused
launch is one registration would not resolve inside the root either, and, end to
end, that an adopted record registers its server from the adopted tree.
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
    PACKAGE,
    build_world,
    requires_git,
    shipped_manifest,
    write_builtin_record,
    write_tree,
)

from kiro_crew.apps.builtin_adoption_mcp import adopted_mcp_servers_refusal, tree_owned_launch
from kiro_crew.apps.manifest import AppManifest

LAUNCHER = "backend/mcp_launch.py"
SERVER = "srv"
TOOLS = ["get_status", "list_items"]

#: The wheel built-in's declaration: its server runs the wheel package by module.
WHEEL_SERVER = {
    "command": "python3",
    "args": ["-m", f"kiro_crew.apps.builtins.{PACKAGE}.backend.mcp_server"],
    "autoApprove": TOOLS,
}
#: The repository copy's: the same server, started from the app's own launcher.
TREE_SERVER = {"command": "python3", "args": [LAUNCHER], "autoApprove": TOOLS}


def _wheel(**server_overrides):
    return types.SimpleNamespace(mcpServers={SERVER: {**WHEEL_SERVER, **server_overrides}})


def _tree(tmp_path, servers, files=(LAUNCHER,)):
    root = tmp_path / "tree"
    for rel in files:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("print('server')\n", encoding="utf-8")
    root.mkdir(parents=True, exist_ok=True)
    return types.SimpleNamespace(mcpServers=servers), root


def _refusal(tmp_path, server, wheel=None, files=(LAUNCHER,)):
    manifest, root = _tree(tmp_path, {SERVER: server}, files)
    return adopted_mcp_servers_refusal(manifest, root, _wheel() if wheel is None else wheel)


def test_a_tree_with_no_server_needs_no_wheel_declaration(tmp_path):
    manifest, root = _tree(tmp_path, {})
    assert adopted_mcp_servers_refusal(manifest, root, None) is None


def test_the_tree_launcher_of_a_declared_server_is_carried(tmp_path):
    assert _refusal(tmp_path, TREE_SERVER) is None


@pytest.mark.parametrize(
    "server",
    [
        {**TREE_SERVER, "autoApprove": TOOLS[:1]},
        {**TREE_SERVER, "autoApprove": []},
        {"command": "python3", "args": [LAUNCHER]},
        {**TREE_SERVER, "disabled": True},
        {**TREE_SERVER, "command": "python"},
        {**TREE_SERVER, "command": "py.exe"},
        {**TREE_SERVER, "args": ["-u", LAUNCHER, "--verbose"]},
    ],
    ids=["fewer-tools", "no-tools", "no-auto-approve", "disabled", "python", "py-exe", "options"],
)
def test_a_narrower_server_is_carried(tmp_path, server):
    assert _refusal(tmp_path, server) is None


@pytest.mark.parametrize(
    "server",
    [
        {**TREE_SERVER, "autoApprove": [*TOOLS, "delete_everything"]},
        {**TREE_SERVER, "autoApprove": ["*"]},
        {**TREE_SERVER, "autoApprove": "get_status"},
        {**TREE_SERVER, "env": {"PYTHONPATH": "/elsewhere"}},
        {**TREE_SERVER, "env": ["PYTHONPATH"]},
        {**TREE_SERVER, "cwd": "/"},
        {**TREE_SERVER, "url": "http://127.0.0.1:9100/mcp"},
        "python3 backend/mcp_launch.py",
    ],
    ids=[
        "new-tool",
        "wildcard",
        "tools-not-a-list",
        "new-env",
        "env-not-a-map",
        "cwd",
        "url",
        "str",
    ],
)
def test_a_server_asking_beyond_the_wheel_declaration_is_refused(tmp_path, server):
    assert _refusal(tmp_path, server) == "pinned_feature_unsupported"


def test_a_server_the_wheel_shipped_disabled_stays_disabled(tmp_path):
    wheel = _wheel(disabled=True)
    assert _refusal(tmp_path, {**TREE_SERVER, "disabled": True}, wheel) is None
    assert _refusal(tmp_path / "b", TREE_SERVER, wheel) == "pinned_feature_unsupported"


def test_an_env_entry_the_wheel_set_may_be_kept_or_dropped_but_not_changed(tmp_path):
    wheel = _wheel(env={"LOG_LEVEL": "info", "PYTHONPATH": "backend"})
    assert _refusal(tmp_path, {**TREE_SERVER, "env": {"LOG_LEVEL": "info"}}, wheel) is None
    assert _refusal(tmp_path / "b", TREE_SERVER, wheel) is None
    changed = {"LOG_LEVEL": "info", "PYTHONPATH": "/elsewhere"}
    assert _refusal(tmp_path / "c", {**TREE_SERVER, "env": changed}, wheel) == (
        "pinned_feature_unsupported"
    )
    assert _refusal(tmp_path / "d", {**TREE_SERVER, "env": {"LOG_LEVEL": 1}}, wheel) == (
        "pinned_feature_unsupported"
    )


def test_a_server_the_wheel_never_declared_is_refused(tmp_path):
    manifest, root = _tree(tmp_path, {SERVER: TREE_SERVER, "extra": TREE_SERVER})
    assert adopted_mcp_servers_refusal(manifest, root, _wheel()) == "pinned_feature_unsupported"


@pytest.mark.parametrize(
    "wheel",
    [None, types.SimpleNamespace(mcpServers={}), types.SimpleNamespace()],
    ids=["no-wheel-manifest", "wheel-declares-none", "no-field"],
)
def test_without_a_wheel_declaration_every_server_is_refused(tmp_path, wheel):
    manifest, root = _tree(tmp_path, {SERVER: TREE_SERVER})
    assert adopted_mcp_servers_refusal(manifest, root, wheel) == "pinned_feature_unsupported"


#: Launches that run code the pinned tree does not hold, each refused.
_FOREIGN_LAUNCHES = [
    {"command": "python3", "args": ["-m", "kiro_crew.apps.builtins.x.mcp_server"]},
    {"command": "python3", "args": ["-mserver"]},
    {"command": "python3", "args": ["-c", "import server"]},
    {"command": "python3", "args": ["../outside.py"]},
    {"command": "python3", "args": ["backend/missing.py"]},
    {"command": "python3", "args": ["backend"]},
    {"command": "python3", "args": ["backend/mcp_launch.sh"]},
    {"command": "python3", "args": []},
    {"command": "node", "args": [LAUNCHER]},
    {"command": "kirocrew", "args": ["mcp-server"]},
    {"command": "./bin/python3", "args": [LAUNCHER]},
    {"command": "python3", "args": "backend/mcp_launch.py"},
]
_FOREIGN_IDS = [
    "wheel-module",
    "attached-module",
    "inline-code",
    "outside-the-tree",
    "missing-file",
    "directory",
    "not-py",
    "no-target",
    "node",
    "host-cli",
    "path-command",
    "args-not-a-list",
]


@pytest.mark.parametrize("launch", _FOREIGN_LAUNCHES, ids=_FOREIGN_IDS)
def test_a_launch_outside_the_tree_is_refused(tmp_path, launch):
    files = (LAUNCHER, "backend/mcp_launch.sh")
    (tmp_path / "outside.py").write_text("print('not the app')\n", encoding="utf-8")
    server = {**launch, "autoApprove": TOOLS}
    assert _refusal(tmp_path, server, files=files) == "pinned_feature_unsupported"


def test_an_absolute_launcher_inside_the_tree_is_refused(tmp_path):
    """Absolute is how a manifest names a file the pin may not cover; never anchored."""
    _manifest, root = _tree(tmp_path, {})
    server = {**TREE_SERVER, "args": [str(root / LAUNCHER)]}
    assert _refusal(tmp_path, server) == "pinned_feature_unsupported"


@pytest.mark.parametrize(
    ("launch", "accepted"),
    [(TREE_SERVER, True)] + [(launch, False) for launch in _FOREIGN_LAUNCHES],
    ids=["tree-launcher", *_FOREIGN_IDS],
)
def test_the_check_accepts_exactly_what_registration_anchors_in_the_root(
    tmp_path, launch, accepted
):
    """One predicate: an accepted launch registers inside the root, a refused one does not."""
    from kiro_crew.apps.bridges import resolve_stdio_command

    _manifest, root = _tree(tmp_path, {}, files=(LAUNCHER, "backend/mcp_launch.sh"))
    (tmp_path / "outside.py").write_text("print('not the app')\n", encoding="utf-8")
    assert tree_owned_launch(launch, root) is accepted
    if not isinstance(launch.get("args"), list):
        return
    resolved = resolve_stdio_command(json.loads(json.dumps(launch)), app_root=root)
    real_root = Path(os.path.realpath(root))
    anchored = [
        arg
        for arg in resolved.get("args") or []
        if isinstance(arg, str)
        and os.path.isabs(arg)
        and Path(arg).is_relative_to(real_root)
        and Path(arg).is_file()
    ]
    assert bool(anchored) is accepted, resolved


def test_pinned_tree_refusal_reads_the_wheel_declaration(tmp_path):
    """E10 is wired through the tree check STAGE and APPLY both run."""
    from builtin_adoption_helpers import pin_row, repository_manifest

    from kiro_crew.apps.builtin_adoption_checks import pinned_tree_refusal
    from kiro_crew.apps.builtin_adoption_pins import parse_adoption_pins

    pin = parse_adoption_pins(
        {"schemaVersion": 1, "pins": [pin_row("a" * 40, "b" * 64, "c" * 64)]}
    )[NAME]
    tree = write_tree(
        tmp_path / "tree",
        repository_manifest(mcpServers={SERVER: TREE_SERVER}),
        {LAUNCHER: "print('server')\n"},
    )
    manifest = AppManifest.from_json_file(tree / "app.json")
    wheel = AppManifest.from_dict(shipped_manifest(mcpServers={SERVER: WHEEL_SERVER}))
    assert pinned_tree_refusal(pin, manifest, tree, shipped_manifest=wheel) is None
    bare = AppManifest.from_dict(shipped_manifest())
    assert (
        pinned_tree_refusal(pin, manifest, tree, shipped_manifest=bare)
        == "pinned_feature_unsupported"
    )
    assert pinned_tree_refusal(pin, manifest, tree) == "pinned_feature_unsupported"


@requires_git
@pytest.mark.asyncio
async def test_an_adopted_record_registers_its_server_from_the_adopted_tree(tmp_path, monkeypatch):
    import kiro_crew.apps.bridges as bridges
    import kiro_crew.sel as sel_mod
    from kiro_crew.apps import builtin_adoption as adoption
    from kiro_crew.apps import manager
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
    mcp_path = tmp_path / "kirocrew.json"
    monkeypatch.setattr(bridges, "_mcp_json_path", lambda: mcp_path)

    key = f"{NAME}:{SERVER}"

    def registered() -> dict:
        assert bridges.reregister_app_mcp_servers(NAME) == [key]
        return json.loads(mcp_path.read_text(encoding="utf-8"))["mcpServers"][key]

    before = registered()
    assert f"kiro_crew.apps.builtins.{PACKAGE}.backend.mcp_server" in before["args"], before

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert await adoption.run_staging_pass(now=lambda: now) == {NAME: "staged"}
    assert await adoption.apply_builtin_adoptions() == {NAME: "applied"}

    after = registered()
    launcher = os.path.realpath(manager.app_dir(NAME) / LAUNCHER)
    assert launcher in after["args"], after
    assert not any("kiro_crew.apps.builtins" in str(arg) for arg in after["args"]), after
    assert after["command"] == before["command"], "the gateway's own interpreter, as before"
    assert after.get("autoApprove") == before.get("autoApprove")
    assert after.get("env") == before.get("env")
