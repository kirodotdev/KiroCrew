"""A deleted wheel copy's limit inputs, frozen into its pin row, answer as the copy did.

Rows 1 to 4 and 9 of app-kit-platform.md § 33 ("A pin row with no wheel copy") are
limits whose input only a wheel copy supplied: the stdio server ceiling (E10) and the
tool gate's own-server grant, the ``agentTools`` ceiling with the gate's
``app-own-tools`` row, and the built-in leg of the credential masks and agent read-only
directories. The release that deletes a copy freezes those inputs into its row
(``builtin_adoption_baseline.freeze_deleted_copy``, printed by ``adoption_pin.py
freeze``). Each test asks a reader with the fixture app's copy shipping, then with the
copy deleted and its row frozen, and pins the same answer; with no frozen value it pins
the fail-closed answer; past the frozen value it pins that nothing more is granted.
Rows 2 and 4 are auto-approval grants, so those are checked at the gate's verdict.
Only the fixture app is touched. Owning spec: app-kit-platform.md § 33.
"""

from __future__ import annotations

import itertools
import json
import os
import shutil
import types
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from builtin_adoption_helpers import (
    NAME,
    PACKAGE,
    REPOSITORY,
    VERSION,
    build_world,
    drop_wheel_copy,
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
from kiro_crew.apps import agent_readonly_dirs, app_agent_tools, builtin_adoption_gate
from kiro_crew.apps import builtin_adoption_pins as pins_mod
from kiro_crew.apps import credential_masks
from kiro_crew.apps import execution as exec_mod
from kiro_crew.apps import manager
from kiro_crew.apps.builtin_adoption_baseline import (
    freeze_deleted_copy,
    freeze_grant_inputs,
    no_copy_limit_input,
)
from kiro_crew.apps.builtin_adoption_gate import adopted_app_own_mcp_servers
from kiro_crew.dashboard.handlers import app_agent_tools as handler_mod
from kiro_crew.hooks import TOOL_AUTO_APPROVE, HookManager, HooksConfig, ToolCall
from kiro_crew.mcp_tools import app_tools

LAUNCHER = "backend/mcp_launch.py"
SERVER = "srv"
KEY = f"{NAME}:{SERVER}"
TOOLS = ["get_status", "list_items"]
AGENT = "fixture-former-helper"
#: The wheel copy's server runs the wheel package by module; the adopted tree's, its own file.
WHEEL_SERVER = {
    "command": "python3",
    "args": ["-m", f"kiro_crew.apps.builtins.{PACKAGE}.backend.mcp_server"],
    "autoApprove": TOOLS,
}
TREE_SERVER = {"command": "python3", "args": [LAUNCHER], "autoApprove": TOOLS}
TOOL = "fixture_former_note"
_SCHEMA = {
    "type": "object",
    "properties": {"item_id": {"type": "string", "maxLength": 64}},
    "required": ["item_id"],
}
BLOCK: dict[str, Any] = {
    "route": "agent-tools",
    "tools": [{"name": TOOL, "description": "Post one note to an item.", "inputSchema": _SCHEMA}],
}
#: A tool an adopted tree declares that the copy never did.
EXTRA_TOOL = {
    "name": "fixture_former_extra",
    "description": "Not in the copy.",
    "inputSchema": _SCHEMA,
}
BACKEND = {"entryPoint": "server.py", "port": "auto"}
#: The wheel copy: every limit input rows 1 to 4 and 9 read.
WHEEL = shipped_manifest(
    mcpServers={SERVER: WHEEL_SERVER},
    agents=["agents/helper.json"],
    agentTools=BLOCK,
    backend=BACKEND,
    credentialMasks=["state/"],
    agentReadOnlyDirs=["notes"],
)
WHEEL_FILES = {"agents/helper.json": json.dumps({"name": AGENT})}


def _clear_process_reads() -> None:
    """Empty every per-process read these tests fill: the wheel scan, pins, tools and masks."""
    app_agent_tools.shipped_tools.cache_clear()
    credential_masks.clear_cache()
    credential_masks._pin_table_names.cache_clear()
    pins_mod.load_adoption_pins.cache_clear()
    exec_mod.invalidate_shipped_builtin_apps()
    manager.invalidate_orphan_cache()


@pytest.fixture(autouse=True)
def _fresh_reads(_floor_monkeypatch: pytest.MonkeyPatch):
    """Each test reads its own wheel, pins and home, and leaves those reads empty.

    Its teardown runs before the shared ``monkeypatch`` is undone, so a test never puts a
    stand-in over a name this clears through ``monkeypatch``; it scopes one with
    ``patch.object`` instead.
    """
    _floor_monkeypatch.setattr(app_agent_tools, "_cache", None)
    _clear_process_reads()
    yield
    _clear_process_reads()


@pytest.fixture(autouse=True)
def _quiet_sel(_floor_monkeypatch: pytest.MonkeyPatch):
    import kiro_crew.sel as sel_mod

    quiet = types.SimpleNamespace(log_api_access=lambda **_kw: None)
    _floor_monkeypatch.setattr(handler_mod, "sel", lambda: quiet)
    _floor_monkeypatch.setattr(sel_mod, "sel", lambda: quiet)


@pytest.fixture
def gate_sets(monkeypatch: pytest.MonkeyPatch):
    """Restore the gate's three boot-warmed registries, and the warm mark, after the test."""
    for name in ("_BUILTIN_APP_NAMES", "_BUILTIN_APP_MCP_SERVERS", "_BUILTIN_APP_AGENTS"):
        monkeypatch.setattr(hooks, name, getattr(hooks, name))
    monkeypatch.setattr(builtin_adoption_gate, "_warmed", builtin_adoption_gate._warmed)


def _ship(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, manifest: dict = WHEEL) -> Path:
    """The fixture app's wheel copy declaring *manifest*, seen by every reader, and its pin."""
    wheel_root = tmp_path / "wheel"
    wheel = write_tree(wheel_root / PACKAGE, manifest, WHEEL_FILES)
    monkeypatch.setattr(exec_mod, "_builtin_manifest_sources", lambda: (wheel_root.resolve(),))
    monkeypatch.setattr(credential_masks, "_builtins_root", lambda: wheel_root)
    exec_mod.invalidate_shipped_builtin_apps()
    install_pins(
        monkeypatch, tmp_path / "adoption-pins.json", [pin_row("a" * 40, "b" * 64, "c" * 64)]
    )
    return wheel


def _set_frozen(value: dict | None) -> None:
    """Give the fixture row *value* as its ``deletedCopy``, or none."""
    document = json.loads(pins_mod._PINS_PATH.read_text(encoding="utf-8"))
    (row,) = document["pins"]
    row.pop(pins_mod.DELETED_COPY_KEY, None)
    if value is not None:
        row[pins_mod.DELETED_COPY_KEY] = value
    pins_mod._PINS_PATH.write_text(json.dumps(document), encoding="utf-8")
    pins_mod.load_adoption_pins.cache_clear()


def _delete(wheel: Path, frozen: str = "copy") -> None:
    """What a release that deletes the copy ships: no wheel package, the pin row kept.

    *frozen* is what the row carries: ``"copy"`` the whole ``deletedCopy`` that
    ``adoption_pin.py freeze`` prints, ``"grants"`` only E9's half of it, ``"none"`` none.
    """
    value = None
    if frozen == "copy":
        value = freeze_deleted_copy(wheel)
    elif frozen == "grants":
        value = freeze_grant_inputs(json.loads((wheel / "app.json").read_text(encoding="utf-8")))
    _set_frozen(value)
    shutil.rmtree(wheel)
    exec_mod.invalidate_shipped_builtin_apps()
    # Per-process reads: a release that deletes a copy starts a new process.
    app_agent_tools.shipped_tools.cache_clear()
    app_agent_tools._cache = None
    credential_masks.clear_cache()


def _adopted(*, servers: dict | None = None, block: dict | None = None) -> None:
    """The fixture app's record adopted from its repository, and trusted there."""
    manifest = repository_manifest(
        mcpServers={SERVER: TREE_SERVER} if servers is None else servers,
        agentTools=BLOCK if block is None else block,
        backend=BACKEND,
    )
    write_tree(manager.app_dir(NAME), manifest, {LAUNCHER: "print('server')\n"})
    manager._write_installed(
        NAME,
        manager.InstalledApp(
            name=NAME,
            version=VERSION,
            displayName="Fixture Former",
            source=f"registry:{NAME}",
            origin="registry",
            lifecycle="gateway",
            sourceUrl=REPOSITORY,
            sourceCommit="a" * 40,
        ),
    )
    seed_agent(
        Path(os.environ["KIROCREW_HOME"]),
        {"apps_trusted": [NAME], "apps_trusted_repositories": {NAME: REPOSITORY}},
    )


_ABSENT = pytest.mark.parametrize("frozen", ["none", "grants"], ids=["no-row", "e9-only"])


# --------------------------------------------------------------------------- #
# The freeze: each input as its reader reads it, and only what the copy declared
# --------------------------------------------------------------------------- #


def test_a_frozen_copy_carries_each_limit_input_as_its_reader_reads_it(tmp_path, monkeypatch):
    wheel = _ship(tmp_path, monkeypatch)
    frozen = freeze_deleted_copy(wheel)
    assert frozen == {
        **freeze_grant_inputs(WHEEL),
        "mcpServers": {SERVER: WHEEL_SERVER},
        "agentNames": [AGENT],
        "agentTools": BLOCK,
        "credentialMasks": ["state/"],
        "agentReadOnlyDirs": ["notes"],
    }
    row = pin_row("a" * 40, "b" * 64, "c" * 64, deletedCopy=frozen)
    assert NAME in pins_mod.parse_adoption_pins({"schemaVersion": 1, "pins": [row]})


def test_a_copy_that_supplied_no_limit_freezes_only_its_e9_input(tmp_path, monkeypatch):
    """An agent's name reaches no limit without a server its app keeps, so it is not frozen.

    The rows frozen before these inputs existed (copies with agents and no server among
    them) therefore re-derive unchanged.
    """
    plain = shipped_manifest(agents=["agents/helper.json"])
    wheel = _ship(tmp_path, monkeypatch, plain)
    assert freeze_deleted_copy(wheel) == freeze_grant_inputs(plain)


@pytest.mark.parametrize(
    "limits",
    [
        {"mcpServers": {}},
        {"mcpServers": ["srv"]},
        {"agentNames": []},
        {"agentNames": [""]},
        {"agentTools": "agent-tools"},
        {"credentialMasks": "state/"},
        {"agentReadOnlyDirs": [1]},
        {"seams": ["anything"]},
    ],
)
def test_a_row_whose_frozen_limit_input_is_malformed_is_dropped(limits):
    def table(frozen):
        row = pin_row("a" * 40, "b" * 64, "c" * 64, deletedCopy=frozen)
        return pins_mod.parse_adoption_pins({"schemaVersion": 1, "pins": [row]})

    assert NAME in table({"permissions": {}, "grants": {}})
    assert table({"permissions": {}, "grants": {}, **limits}) == {}


def test_a_frozen_value_beside_a_shipped_copy_is_never_read(tmp_path, monkeypatch):
    """One source of truth: while the copy ships, every reader reads the copy."""
    _ship(tmp_path, monkeypatch)
    _adopted()
    _set_frozen(
        {
            "permissions": {},
            "grants": {},
            "mcpServers": {"other": TREE_SERVER},
            "agentTools": {**BLOCK, "route": "elsewhere"},
            "credentialMasks": ["other.json"],
        }
    )
    pin = pins_mod.adoption_pin(NAME)
    assert pin is not None and pin.deleted_copy
    assert no_copy_limit_input(pin, "mcpServers") is None
    assert adopted_app_own_mcp_servers() == {KEY}
    assert [(t.name, t.route) for t in app_agent_tools.declared_tools()] == [(TOOL, "agent-tools")]
    assert credential_masks.declared_masks(installed=False) == {NAME: ("state/",)}


# --------------------------------------------------------------------------- #
# Row 1: E10's stdio server ceiling
# --------------------------------------------------------------------------- #


def test_e10_holds_a_pinned_tree_to_the_frozen_servers(tmp_path):
    from kiro_crew.apps.builtin_adoption_checks import pinned_tree_refusal
    from kiro_crew.apps.manifest import AppManifest

    pin = pins_mod.parse_adoption_pins(
        {"schemaVersion": 1, "pins": [pin_row("a" * 40, "b" * 64, "c" * 64)]}
    )[NAME]
    wheel = AppManifest.from_dict(shipped_manifest(mcpServers={SERVER: WHEEL_SERVER}))
    trees = itertools.count()

    def refusal(server, **ceiling):
        tree = write_tree(
            tmp_path / f"tree-{next(trees)}",
            repository_manifest(mcpServers={SERVER: server}),
            {LAUNCHER: "print('server')\n"},
        )
        manifest = AppManifest.from_json_file(tree / "app.json")
        return pinned_tree_refusal(pin, manifest, tree, **ceiling)

    frozen = {SERVER: WHEEL_SERVER}
    assert refusal(TREE_SERVER, shipped_manifest=wheel) is None
    assert refusal(TREE_SERVER, frozen_servers=frozen) is None
    wider = {**TREE_SERVER, "autoApprove": [*TOOLS, "delete_everything"]}
    assert refusal(wider, shipped_manifest=wheel) == "pinned_feature_unsupported"
    assert refusal(wider, frozen_servers=frozen) == "pinned_feature_unsupported"
    assert refusal(TREE_SERVER) == "pinned_feature_unsupported", "neither: fail-closed"
    bare = AppManifest.from_dict(shipped_manifest())
    assert (
        refusal(TREE_SERVER, shipped_manifest=bare, frozen_servers=frozen)
        == "pinned_feature_unsupported"
    ), "a frozen ceiling is never read beside a wheel manifest"


async def _warm() -> None:
    from kiro_crew.dashboard import server

    await server._warm_builtin_app_names()


def _granted(tier: str, call: ToolCall, **surface: Any) -> bool:
    result = HookManager(HooksConfig()).judge(call, **surface)
    return result.tier == tier and result.action == TOOL_AUTO_APPROVE


def _server_call(**fields: Any) -> ToolCall:
    return ToolCall(
        title="Get the status",
        mcp_server=KEY,
        mcp_tool="get_status",
        identity_trusted=True,
        **fields,
    )


@requires_git
@pytest.mark.asyncio
@pytest.mark.parametrize("frozen", ["copy", "none"])
async def test_a_never_adopted_host_carries_the_copys_server_once_the_copy_is_deleted(
    tmp_path, monkeypatch, gate_sets, frozen
):
    """STAGE and APPLY with the copy gone: the frozen servers are E10's ceiling.

    With them the host adopts and the own-server grant holds as it did for the built-in;
    without them every server is refused for this release, as before this freeze.
    """
    from kiro_crew.apps import builtin_adoption as adoption
    from kiro_crew.apps.builtin_adoption_state import read_ledger
    from kiro_crew.apps.point_grants import grant_source
    from kiro_crew.config.loader import _invalidate_config_cache

    wheel = shipped_manifest(mcpServers={SERVER: WHEEL_SERVER})
    world = build_world(
        tmp_path,
        monkeypatch,
        wheel_manifest=wheel,
        tree_files={LAUNCHER: "print('server')\n"},
        mcpServers={SERVER: TREE_SERVER},
    )
    monkeypatch.setattr(manager, "discover_builtin_apps", lambda: [])
    write_builtin_record(manifest=wheel)
    manager._write_approved_grants(NAME, manager.builtin_approved_grants(grant_source(wheel)))
    _invalidate_config_cache()
    await _warm()
    assert _granted("app-own-server", _server_call(), app=NAME), "the built-in's grant"

    drop_wheel_copy(world, freeze=frozen == "copy")
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    staged = await adoption.run_staging_pass(now=lambda: now)
    if frozen == "none":
        assert staged == {NAME: "blocked"}
        assert read_ledger(NAME).reason == "pinned_feature_unsupported"
        return
    assert staged == {NAME: "staged"}
    assert await adoption.apply_builtin_adoptions() == {NAME: "applied"}
    await _warm()
    assert _granted("app-own-server", _server_call(), app=NAME)


# --------------------------------------------------------------------------- #
# Rows 2 and 4: the own-server grant and the app-own-tools row, at the gate
# --------------------------------------------------------------------------- #


def _gate_answers() -> dict[str, bool]:
    """What the warmed gate auto-approves for the fixture app, each under its own row."""

    def tool_call(tool: str, agent: str = AGENT) -> ToolCall:
        return ToolCall(
            title=f"Running: @kirocrew-core/{tool}",
            mcp_server="kirocrew-core",
            mcp_tool=tool,
            identity_trusted=True,
            resolved_agent=agent,
        )

    surface = {"app": NAME, "session_key": "dashboard:s1"}
    return {
        "own server": _granted("app-own-server", _server_call(), app=NAME),
        "own server, by agent": _granted("app-own-server", _server_call(resolved_agent=AGENT)),
        "own tool": _granted("app-own-tools", tool_call(TOOL), **surface),
        "a tool the copy never declared": _granted(
            "app-own-tools", tool_call(EXTRA_TOOL["name"]), **surface
        ),
        "an agent the copy never declared": _granted(
            "app-own-tools", tool_call(TOOL, "fixture-former-other"), **surface
        ),
    }


_TODAY = {
    "own server": True,
    "own server, by agent": True,
    "own tool": True,
    "a tool the copy never declared": False,
    "an agent the copy never declared": False,
}


@pytest.mark.asyncio
async def test_the_gate_keeps_the_built_in_auto_approvals_and_no_more(
    tmp_path, monkeypatch, gate_sets
):
    """Deleting the copy and freezing it leaves both grants exactly as the copy gave them."""
    wheel = _ship(tmp_path, monkeypatch)
    _adopted(block={**BLOCK, "tools": [*BLOCK["tools"], EXTRA_TOOL]})
    await _warm()
    assert _gate_answers() == _TODAY

    _delete(wheel)
    await _warm()
    assert _gate_answers() == _TODAY


@_ABSENT
@pytest.mark.asyncio
async def test_with_no_frozen_input_the_gate_grants_neither(
    tmp_path, monkeypatch, gate_sets, frozen
):
    wheel = _ship(tmp_path, monkeypatch)
    _adopted()
    _delete(wheel, frozen)
    await _warm()
    assert not any(_gate_answers().values())


def test_the_own_server_grant_and_agent_map_read_the_same_once_the_copy_is_deleted(
    tmp_path, monkeypatch
):
    wheel = _ship(tmp_path, monkeypatch)
    _adopted()

    def answer():
        return adopted_app_own_mcp_servers(), exec_mod.builtin_app_agents(frozenset({NAME}))

    shipped = answer()
    assert shipped == ({KEY}, {AGENT: NAME, f"{NAME}--{AGENT}": NAME})
    _delete(wheel)
    assert answer() == shipped


@_ABSENT
def test_with_no_frozen_servers_the_grant_and_agent_map_name_nothing(tmp_path, monkeypatch, frozen):
    wheel = _ship(tmp_path, monkeypatch)
    _adopted()
    _delete(wheel, frozen)
    assert adopted_app_own_mcp_servers() == frozenset()
    assert exec_mod.builtin_app_agents(frozenset({NAME})) == {}


@pytest.mark.parametrize(
    ("servers", "kept"),
    [
        ({SERVER: {**TREE_SERVER, "autoApprove": [*TOOLS, "delete_everything"]}}, set()),
        ({SERVER: {**TREE_SERVER, "env": {"PYTHONPATH": "/elsewhere"}}}, set()),
        ({SERVER: {**TREE_SERVER, "args": ["-m", "elsewhere"]}}, set()),
        ({SERVER: TREE_SERVER, "extra": TREE_SERVER}, {KEY}),
    ],
    ids=["new-tool", "new-env", "module-launch", "new-server"],
)
def test_the_frozen_servers_grant_nothing_the_copy_did_not(tmp_path, monkeypatch, servers, kept):
    wheel = _ship(tmp_path, monkeypatch)
    _adopted(servers=servers)
    _delete(wheel)
    assert adopted_app_own_mcp_servers() == kept


# --------------------------------------------------------------------------- #
# Row 3: the agentTools ceiling and deny universe
# --------------------------------------------------------------------------- #


def _listed() -> list[str]:
    return [t.name for t in app_agent_tools.declared_tools()]


def _universe() -> list[str]:
    return sorted(t["name"] for t in app_tools.schemas())


def test_an_adopted_record_lists_and_withholds_the_same_tools_once_the_copy_is_deleted(
    tmp_path, monkeypatch
):
    wheel = _ship(tmp_path, monkeypatch)
    _adopted()
    assert (_listed(), _universe()) == ([TOOL], [TOOL])
    _delete(wheel)
    assert (_listed(), _universe()) == ([TOOL], [TOOL])


@_ABSENT
def test_with_no_frozen_block_an_adopted_record_lists_nothing(tmp_path, monkeypatch, frozen):
    wheel = _ship(tmp_path, monkeypatch)
    _adopted()
    _delete(wheel, frozen)
    assert (_listed(), _universe()) == ([], [])


@pytest.mark.parametrize(
    ("block", "listed"),
    [
        ({**BLOCK, "tools": [*BLOCK["tools"], EXTRA_TOOL]}, [TOOL]),
        ({**BLOCK, "tools": [{**BLOCK["tools"][0], "description": "Post everything."}]}, []),
        ({**BLOCK, "route": "elsewhere"}, []),
    ],
    ids=["new-tool", "drifted-description", "drifted-route"],
)
def test_the_frozen_block_lists_nothing_the_copy_did_not(tmp_path, monkeypatch, block, listed):
    wheel = _ship(tmp_path, monkeypatch)
    _adopted(block=block)
    _delete(wheel)
    assert _listed() == listed
    assert _universe() == [TOOL]


def test_a_deleted_copys_frozen_tools_stay_in_the_deny_universe(tmp_path, monkeypatch):
    """Named with no adopted record at all, as the copy's were, so every scoped agent is denied."""
    wheel = _ship(tmp_path, monkeypatch)
    _delete(wheel)
    assert _listed() == []
    assert _universe() == [TOOL]


@pytest.mark.asyncio
async def test_a_call_reaches_the_adopted_apps_route_once_the_copy_is_deleted(
    tmp_path, monkeypatch
):
    from kiro_crew.apps import routes as app_routes

    wheel = _ship(tmp_path, monkeypatch)
    _adopted()
    _delete(wheel)
    seen: list[tuple[str, str]] = []

    async def _proxy(request: web.Request, *, read_body: bytes | None = None) -> web.Response:
        seen.append((request.match_info["name"], request.match_info["path"]))
        return web.json_response({"proxied": True})

    monkeypatch.setattr(app_routes, "handle_app_api_proxy", _proxy)
    req = make_mocked_request("POST", app_tools._CALL_PATH)
    req["internal_auth"] = True
    req._read_bytes = json.dumps({"tool": TOOL, "arguments": {"item_id": "i-1"}}).encode()
    resp = await handler_mod.api_app_agent_tool_call(req)
    assert (resp.status, json.loads(resp.body)) == (200, {"proxied": True})
    assert seen == [(NAME, "agent-tools")]


# --------------------------------------------------------------------------- #
# Row 9: the built-in leg of the credential masks and agent read-only directories
# --------------------------------------------------------------------------- #


def _built_in_leg() -> tuple[Any, ...]:
    return (
        credential_masks.declared_masks(installed=False),
        agent_readonly_dirs.declared_dirs(installed=False),
        credential_masks.declared_crew_leaves(installed=False),
        credential_masks.declared_crew_leaves(installed=False, kind=agent_readonly_dirs.KIND),
    )


def test_the_built_in_leg_keeps_a_deleted_copys_masks_and_read_only_dirs(tmp_path, monkeypatch):
    wheel = _ship(tmp_path, monkeypatch)
    shipped = _built_in_leg()
    assert shipped[:2] == ({NAME: ("state/",)}, {NAME: ("notes",)})
    assert f"apps/{NAME}/data/state" in shipped[2]
    _delete(wheel)
    assert _built_in_leg() == shipped


def test_a_deleted_copys_frozen_declaration_lifts_nothing(tmp_path, monkeypatch):
    """The backend's carve-out reads the code it runs, and no code of the deleted copy runs."""
    wheel = _ship(tmp_path, monkeypatch)
    assert credential_masks.declared_crew_leaves_for(NAME, builtin=True)
    _delete(wheel)
    assert credential_masks.declared_crew_leaves_for(NAME, builtin=True) == ()
    assert credential_masks.declared_crew_dir_leaves_for(NAME, builtin=True) == frozenset()
    assert agent_readonly_dirs.declared_crew_leaves_for(NAME, builtin=True) == ()


@_ABSENT
def test_with_no_frozen_declaration_the_built_in_leg_declares_nothing(
    tmp_path, monkeypatch, frozen
):
    wheel = _ship(tmp_path, monkeypatch)
    _delete(wheel, frozen)
    assert _built_in_leg() == ({}, {}, (), ())


# --------------------------------------------------------------------------- #
# A table that froze nothing costs a stdio server's listing no module
# --------------------------------------------------------------------------- #


def test_the_pin_table_gate_reads_the_loaders_own_file():
    """The raw read and the loader name one table, one override and one module."""
    assert credential_masks._PIN_TABLE == pins_mod._PINS_PATH
    assert credential_masks._HARNESS_PIN_TABLE_ENV == pins_mod.HARNESS_PINS_ENV
    assert credential_masks._PIN_LOADER == pins_mod.__name__


def test_a_table_that_names_no_frozen_key_is_never_loaded(tmp_path, monkeypatch):
    """Before the loader is in the process, only a table whose text names the key loads it.

    ``test_mcp_listing_import_budget.py`` holds the module count this keeps; here the
    gate's answer is pinned on two harness tables, with the loader taken as absent. The
    loader's stand-in records each load rather than raising, since the mask leg logs and
    swallows a failure, and the table that names the key shows it does record one. It is
    held by ``patch.object`` for these calls only: ``_fresh_reads`` clears the real
    loader's cache at teardown, before the shared ``monkeypatch`` is undone
    (``test/conftest.py``'s ``_a_shared_monkeypatch_first``).
    """
    monkeypatch.setattr(credential_masks, "_PIN_LOADER", "kiro_crew.apps._absent_for_this_test")
    plain, freezing = tmp_path / "plain.json", tmp_path / "freezing.json"
    row = pin_row("a" * 40, "b" * 64, "c" * 64)
    plain.write_text(json.dumps({"schemaVersion": 1, "pins": [row]}), encoding="utf-8")
    frozen = {"permissions": {}, "grants": {}, "credentialMasks": ["state/"]}
    freezing.write_text(
        json.dumps({"schemaVersion": 1, "pins": [{**row, "deletedCopy": frozen}]}),
        encoding="utf-8",
    )
    loads: list[str] = []

    def loader() -> dict:
        loads.append("loaded")
        return {}

    masks = credential_masks.CREDENTIAL_MASKS
    with patch.object(pins_mod, "load_adoption_pins", loader):
        monkeypatch.setenv(pins_mod.HARNESS_PINS_ENV, str(plain))
        assert not credential_masks.pin_table_may_freeze("credentialMasks")
        assert credential_masks._frozen_builtin_declarations(masks) == []
        assert app_agent_tools._frozen_copy_manifests(()) == {}
        assert loads == []

        monkeypatch.setenv(pins_mod.HARNESS_PINS_ENV, str(freezing))
        assert credential_masks.pin_table_may_freeze("credentialMasks")
        assert not credential_masks.pin_table_may_freeze("agentTools")
        assert credential_masks._frozen_builtin_declarations(masks) == []
        assert loads == ["loaded"]
