"""What a released wheel copy stops doing, and the ``/api/apps`` adoption row.

Matrix rows 1, 3, 4, 13, 14 (N+k), 18, 19 and 25 of the adoption contract, plus
the ``mcp-<pkg>`` refusal and the config key's validation.
"""

from __future__ import annotations

import ast
import json
import sys
import types
from pathlib import Path

import pytest
from aiohttp import web
from builtin_adoption_helpers import (
    NAME,
    PACKAGE,
    REPOSITORY,
    host_with_seams,
    install_pins,
    pin_row,
    shipped_manifest,
    write_builtin_record,
    write_tree,
)

import kiro_crew
from kiro_crew.apps import builtin_adoption as adoption
from kiro_crew.apps import builtin_adoption_pins as pins_mod
from kiro_crew.apps import execution as exec_mod
from kiro_crew.apps import manager
from kiro_crew.apps.builtin_adoption_state import write_ledger

_SRC = Path(kiro_crew.__file__).resolve().parent
COMMIT = "a" * 40


@pytest.fixture
def wheel(tmp_path, monkeypatch):
    """The fixture app shipped in a wheel package, pinned, with no record yet."""
    root = tmp_path / "wheel"
    package = write_tree(root / PACKAGE, shipped_manifest())
    monkeypatch.setattr(exec_mod, "_builtin_manifest_sources", lambda: (root.resolve(),))
    install_pins(monkeypatch, tmp_path / "pins.json", [pin_row(COMMIT, "b" * 64, "c" * 64)])
    yield package.resolve()
    pins_mod.load_adoption_pins.cache_clear()


def _adopt_record(**fields) -> None:
    """The record a successful APPLY leaves."""
    write_builtin_record()
    record = manager._read_installed(NAME)
    assert record is not None
    record.source, record.origin, record.lifecycle = f"registry:{NAME}", "registry", "gateway"
    record.sourceUrl, record.sourceCommit = REPOSITORY, COMMIT
    for key, value in fields.items():
        setattr(record, key, value)
    manager._write_installed(NAME, record)


def _unpin(monkeypatch, tmp_path) -> None:
    install_pins(monkeypatch, tmp_path / "none.json", [])


# --------------------------------------------------------------------------- #
# The predicate
# --------------------------------------------------------------------------- #


def test_a_builtin_owned_record_is_not_released(wheel):
    write_builtin_record()
    assert not adoption.builtin_released(NAME)
    assert exec_mod.shipped_builtin_app_root(NAME) == wheel


def test_an_adopted_record_is_released(wheel):
    _adopt_record()
    assert adoption.builtin_released(NAME)
    assert exec_mod.shipped_builtin_app_root(NAME) is None
    assert adoption.builtin_package_released(PACKAGE)


def test_a_fresh_install_of_a_pinned_name_keeps_the_wheel_copy(wheel):
    """Row 18: no record yet; the built-in registers as today and adopts later."""
    assert not adoption.builtin_released(NAME)
    assert exec_mod.shipped_builtin_app_root(NAME) == wheel
    assert not adoption.builtin_package_released(PACKAGE)


def test_an_unpinned_name_with_no_record_keeps_todays_behaviour(wheel, monkeypatch, tmp_path):
    _unpin(monkeypatch, tmp_path)
    assert not adoption.builtin_released(NAME)
    assert exec_mod.shipped_builtin_app_root(NAME) == wheel


def test_an_adopted_marker_keeps_an_unpinned_name_released(wheel, monkeypatch, tmp_path):
    from builtin_adoption_helpers import seed_agent

    _unpin(monkeypatch, tmp_path)
    marker = {
        "state": "adopted",
        "repository": REPOSITORY,
        "commit": COMMIT,
        "addedName": True,
        "setRepository": True,
        "removedLocal": False,
    }
    seed_agent(manager.config_dir(), {"apps_builtin_adoption": {NAME: marker}})
    assert adoption.builtin_released(NAME)


def test_a_mid_swap_absence_keeps_todays_behaviour(wheel):
    manager._update_marker_dir(NAME).mkdir(parents=True)
    assert not adoption.builtin_released(NAME)


def test_an_unreadable_record_path_keeps_todays_behaviour(wheel):
    manager.apps_dir().mkdir(parents=True, exist_ok=True)
    manager.app_dir(NAME).write_text("not a directory", encoding="utf-8")
    assert not adoption.builtin_released(NAME)


def test_a_raising_predicate_keeps_the_shipped_root(wheel, monkeypatch):
    write_builtin_record()

    def boom(_name):
        raise RuntimeError("unanswerable")

    monkeypatch.setattr(adoption, "builtin_released", boom)
    assert exec_mod.shipped_builtin_app_root(NAME) == wheel


# --------------------------------------------------------------------------- #
# Row 1: the BUILTIN_NAMES route loop; row 4: on_disable
# --------------------------------------------------------------------------- #


def _fake_package(monkeypatch):
    module = types.ModuleType(f"kiro_crew.apps.builtins.{PACKAGE}")
    seen: dict[str, object] = {"register": 0, "disable": 0}

    async def wheel_startup(_app):
        return None

    seen["startup"] = wheel_startup

    def register_routes(app):
        seen["register"] += 1
        app.router.add_get(f"/api/apps/{NAME}/wheel", lambda r: web.Response())
        app.on_startup.append(wheel_startup)

    def on_disable(app):
        seen["disable"] += 1

    module.register_routes = register_routes  # type: ignore[attr-defined]
    module.on_disable = on_disable  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module.__name__, module)
    from kiro_crew.dashboard.routes import system

    monkeypatch.setattr(system, "BUILTIN_NAMES", [PACKAGE])
    return seen


def _wheel_routes(app) -> set[str]:
    return {
        r.resource.canonical
        for r in app.router.routes()
        if r.resource is not None and r.resource.canonical.startswith(f"/api/apps/{NAME}/")
    }


@pytest.mark.parametrize("adopted", [False, True])
def test_the_route_loop_skips_a_released_package(wheel, monkeypatch, adopted):
    from kiro_crew.dashboard.routes import system

    seen = _fake_package(monkeypatch)
    _adopt_record() if adopted else write_builtin_record()
    app = web.Application()
    system.register(app)
    if adopted:
        assert seen["register"] == 0 and _wheel_routes(app) == set()
        assert seen["startup"] not in list(app.on_startup)
    else:
        assert seen["register"] == 1 and _wheel_routes(app) == {f"/api/apps/{NAME}/wheel"}
        assert seen["startup"] in list(app.on_startup)


def test_the_on_disable_import_is_gated_on_the_release_predicate():
    """Row 4: the hook import sits behind ``builtin_package_released`` in the disable route."""
    source = (_SRC / "apps" / "routes_enable.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    gated = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and "builtin_package_released(module_name)" in ast.unparse(node.test)
        and "BUILTIN_NAMES" in ast.unparse(node.test)
    ]
    assert len(gated) == 1
    assert "on_disable" in ast.unparse(gated[0])


def test_the_on_disable_predicate_answers_for_an_adopted_package(wheel):
    _adopt_record()
    assert adoption.builtin_package_released(PACKAGE)
    assert not adoption.builtin_package_released("not_a_package")
    assert adoption.released_builtin_packages([PACKAGE, "not_a_package"]) == {PACKAGE}


def test_a_builtin_owned_package_is_not_in_the_released_set(wheel):
    write_builtin_record()
    assert adoption.released_builtin_packages([PACKAGE]) == frozenset()


def test_an_adopted_package_mcp_verb_is_refused(wheel, monkeypatch, capsys):
    _adopt_record()
    from kiro_crew import cli

    monkeypatch.setattr(cli, "_BUILTIN_NAMES", [PACKAGE])
    monkeypatch.setattr(sys, "argv", ["kirocrew", f"mcp-{PACKAGE}"])
    monkeypatch.setattr(cli, "_builtin_mcp_server_available", lambda name: True)
    ran = []
    import kiro_crew.cli_commands as commands

    monkeypatch.setattr(commands, "_run_app_mcp_server", lambda name: ran.append(name))
    with pytest.raises(SystemExit) as exited:
        cli.main()
    assert exited.value.code == 1 and ran == []
    assert "kirocrew app mcp" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# Row 3: every wheel-root chooser resolves through the narrowed function
# --------------------------------------------------------------------------- #

_CHOOSERS = (
    "apps/hooks_integration.py",
    "apps/bridges_sources.py",
    "apps/lifecycle.py",
    "apps/manager_lookup.py",
    "apps/manager_activation.py",
    "apps/bridges_crons.py",
    "apps/bridges_mcp.py",
    "apps/backend_runtime/startup.py",
    "apps/cron_sdk.py",
)


@pytest.mark.parametrize("rel", _CHOOSERS)
def test_each_wheel_root_chooser_imports_the_narrowed_function(rel):
    tree = ast.parse((_SRC / rel).read_text(encoding="utf-8"))
    sources = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and any(alias.name == "shipped_builtin_app_root" for alias in node.names)
    }
    assert sources == {"kiro_crew.apps.execution"}


def test_the_backend_facade_serves_the_narrowed_function():
    from kiro_crew.apps import backend

    assert backend.shipped_builtin_app_root is exec_mod.shipped_builtin_app_root


def test_an_adopted_name_resolves_the_data_home_tree(wheel):
    from kiro_crew.apps.bridges_sources import _app_resource_root, _registration_source
    from kiro_crew.apps.hooks_integration import _app_hook_root

    _adopt_record()
    dest = manager.app_dir(NAME)
    assert _app_hook_root(NAME) == dest
    assert _app_resource_root(NAME) == dest
    assert _registration_source(NAME)[1] == dest
    assert not exec_mod.is_builtin_app(app_root=wheel, app_name=NAME)
    assert NAME not in exec_mod.builtin_app_names()


def test_a_builtin_owned_name_still_resolves_the_wheel(wheel):
    from kiro_crew.apps.hooks_integration import _app_hook_root

    write_builtin_record()
    assert _app_hook_root(NAME) == wheel
    assert exec_mod.is_builtin_app(app_root=wheel, app_name=NAME)


# --------------------------------------------------------------------------- #
# Rows 13, 18, 19: orphans, fresh installs, registry installs into the record
# --------------------------------------------------------------------------- #


def test_a_pinned_record_whose_package_is_gone_is_never_orphaned(wheel, monkeypatch):
    write_builtin_record()
    monkeypatch.setattr(exec_mod, "_builtin_manifest_sources", lambda: ())
    monkeypatch.setattr(manager, "discover_builtin_apps", lambda: [])
    assert NAME not in manager.detect_orphaned_builtins(force_refresh=True)
    assert not manager.migrated_builtin_cleanup_applies(NAME)
    rows = {row["name"]: row for row in manager.list_apps()}
    assert "orphaned" not in rows[NAME]
    assert rows[NAME]["adoption"] == {"state": "pending"}
    manager.invalidate_orphan_cache()


def test_the_migrated_list_never_names_a_pinned_app():
    from kiro_crew.apps.builtins import _MIGRATED_BUILTINS

    document = json.loads((_SRC / "apps" / "adoption-pins.json").read_text(encoding="utf-8"))
    pinned = {row["name"] for row in document["pins"]}
    assert set(_MIGRATED_BUILTINS).isdisjoint(pinned)


def test_the_escalation_sweep_never_names_a_pinned_app():
    """``register_builtin_apps`` deletes the names it lists; a pinned app is never one."""
    tree = ast.parse((_SRC / "apps" / "manager_builtins.py").read_text(encoding="utf-8"))
    swept = [
        {elt.value for elt in node.value.elts if isinstance(elt, ast.Constant)}
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(getattr(t, "id", "") == "_escalated" for t in node.targets)
        and isinstance(node.value, ast.List)
    ]
    assert len(swept) == 1 and swept[0]
    document = json.loads((_SRC / "apps" / "adoption-pins.json").read_text(encoding="utf-8"))
    assert swept[0].isdisjoint({row["name"] for row in document["pins"]})


def test_a_fresh_install_registers_a_pinned_name_as_a_built_in(wheel, monkeypatch):
    definition = shipped_manifest()
    monkeypatch.setattr(manager, "discover_builtin_apps", lambda: [definition])
    manager.register_builtin_apps()
    record = manager._read_installed(NAME)
    assert record is not None and manager.builtin_owns_installed(NAME)
    assert (record.origin, record.lifecycle) == ("builtin", "locked")
    rows = {row["name"]: row for row in manager.list_apps()}
    assert rows[NAME]["adoption"] == {"state": "pending"}
    manager.invalidate_orphan_cache()


def test_a_pinned_name_uninstalled_after_adoption_registers_nothing(wheel, monkeypatch):
    from builtin_adoption_helpers import seed_agent

    marker = {
        "state": "adopted",
        "repository": REPOSITORY,
        "commit": COMMIT,
        "addedName": True,
        "setRepository": True,
        "removedLocal": False,
    }
    seed_agent(manager.config_dir(), {"apps_builtin_adoption": {NAME: marker}})
    assert adoption.builtin_released(NAME)
    definition = shipped_manifest()
    monkeypatch.setattr(manager, "discover_builtin_apps", lambda: [definition])
    manager.register_builtin_apps()
    assert not manager.app_dir(NAME).exists()
    manager.invalidate_orphan_cache()


@pytest.mark.asyncio
async def test_a_registry_install_into_a_pinned_builtin_record_waits_for_adoption(
    wheel, monkeypatch
):
    write_builtin_record()
    from kiro_crew.apps import registry

    def never(*_a, **_k):
        raise AssertionError("no lookup or clone before the refusal")

    monkeypatch.setattr(registry, "_resolve_install_entry", never)
    result = await registry.install_from_registry(NAME)
    assert result["ok"] is False and result["code"] == "adoption_pending"
    assert "restart" in result["error"]


# --------------------------------------------------------------------------- #
# Row 25 and row 14 (N+k): the /api/apps adoption row
# --------------------------------------------------------------------------- #


def _ledger(state, reason="", commit=COMMIT):
    from kiro_crew.apps.builtin_adoption_state import AdoptionLedger

    write_ledger(AdoptionLedger(name=NAME, commit=commit, state=state, reason=reason))


@pytest.mark.parametrize(
    ("ledger", "row"),
    [
        (None, {"state": "pending"}),
        (("pending", "network_timeout"), {"state": "pending", "reason": "network_timeout"}),
        (("pending", ""), {"state": "pending"}),
        (("staged", ""), {"state": "staged"}),
        (("blocked", "admission_denied"), {"state": "blocked", "reason": "admission_denied"}),
        (("blocked", "not-a-code"), {"state": "blocked"}),
    ],
)
def test_the_row_for_a_builtin_owned_pinned_record(wheel, ledger, row):
    write_builtin_record()
    if ledger is not None:
        _ledger(*ledger)
    assert manager.get_app(NAME)["adoption"] == row


def test_a_ledger_for_another_commit_reads_as_pending(wheel):
    write_builtin_record()
    _ledger("blocked", "admission_denied", commit="d" * 40)
    assert manager.get_app(NAME)["adoption"] == {"state": "pending"}


def test_the_row_for_an_adopted_record_carries_the_legacy_nav(wheel):
    _adopt_record()
    assert manager.get_app(NAME)["adoption"] == {
        "state": "adopted",
        "legacyNav": {"route": f"/{NAME}", "icon": "Box"},
    }


def test_a_newer_adopted_version_flags_host_too_old_and_is_never_re_registered(wheel, monkeypatch):
    _adopt_record(version="9.0.0")
    dest = manager.app_dir(NAME)
    manifest = json.loads((dest / "app.json").read_text(encoding="utf-8"))
    manifest.update(version="9.0.0", minKiroCrewVersion="999.0.0")
    (dest / "app.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert manager.get_app(NAME)["adoption"]["hostTooOld"] is True
    stamped = (dest / "installed.json").read_bytes()
    data_before = (dest / "data" / "store.txt").read_bytes()
    monkeypatch.setattr(manager, "discover_builtin_apps", lambda: [shipped_manifest()])
    manager.register_builtin_apps()
    assert (dest / "installed.json").read_bytes() == stamped
    assert (dest / "data" / "store.txt").read_bytes() == data_before
    manager.invalidate_orphan_cache()


def test_no_row_for_an_unrelated_or_shadowing_record(wheel, monkeypatch, tmp_path):
    _adopt_record(sourceUrl="https://github.com/other/fork")
    assert "adoption" not in manager.get_app(NAME)
    _unpin(monkeypatch, tmp_path)
    write_builtin_record()
    assert "adoption" not in manager.get_app(NAME)


# --------------------------------------------------------------------------- #
# The config key
# --------------------------------------------------------------------------- #


def test_the_config_key_drops_every_malformed_marker():
    from kiro_crew.config.loader_builders import _builtin_adoption_markers

    good = {
        "state": "adopted",
        "repository": REPOSITORY,
        "commit": COMMIT,
        "addedName": True,
        "setRepository": False,
        "removedLocal": False,
    }
    raw = {
        NAME: good,
        "Bad Name": good,
        "extra-key": {**good, "x": 1},
        "bad-state": {**good, "state": "done"},
        "bad-commit": {**good, "commit": "abc"},
        "bad-flag": {**good, "addedName": "yes"},
        "not-a-dict": "adopted",
    }
    assert _builtin_adoption_markers(raw) == {NAME: good}
    assert _builtin_adoption_markers([good]) == {}


def test_the_config_key_is_capped():
    from kiro_crew.apps.builtin_adoption_markers import MAX_MARKERS, coerce_adoption_markers

    good = {
        "state": "adopted",
        "repository": REPOSITORY,
        "commit": COMMIT,
        "addedName": False,
        "setRepository": False,
        "removedLocal": False,
    }
    raw = {f"app-{i}": good for i in range(MAX_MARKERS + 5)}
    assert len(coerce_adoption_markers(raw)) == MAX_MARKERS


# --------------------------------------------------------------------------- #
# E8 and E10 on a tree
# --------------------------------------------------------------------------- #


def _pin_and_manifest(tmp_path, **manifest_overrides):
    import dataclasses

    from builtin_adoption_helpers import repository_manifest

    from kiro_crew.apps.builtin_adoption_pins import parse_adoption_pins
    from kiro_crew.apps.manifest import AppManifest

    pin = parse_adoption_pins({"schemaVersion": 1, "pins": [pin_row(COMMIT, "b" * 64, "c" * 64)]})[
        NAME
    ]
    raw = repository_manifest(**manifest_overrides)
    tree = write_tree(tmp_path / "tree", raw)
    if raw["name"] != NAME:
        pin = dataclasses.replace(pin, name=raw["name"])
    return pin, AppManifest.from_json_file(tree / "app.json"), tree


@pytest.mark.parametrize(
    "build_input",
    ["package.json", "pyproject.toml", "setup.py", "requirements.txt", ".gitattributes"],
)
def test_a_tree_that_needs_a_build_step_is_refused(tmp_path, build_input):
    from kiro_crew.apps.builtin_adoption_checks import pinned_tree_refusal

    pin, manifest, tree = _pin_and_manifest(tmp_path)
    assert pinned_tree_refusal(pin, manifest, tree) is None
    (tree / build_input).write_text("{}", encoding="utf-8")
    assert pinned_tree_refusal(pin, manifest, tree) == "tree_requires_build"


def test_a_name_a_core_table_keys_on_is_refused(tmp_path):
    from kiro_crew.apps.builtin_adoption_checks import core_named_apps, pinned_tree_refusal
    from kiro_crew.mcp_tools.apps import TOOL_APPS

    keyed = sorted(set(TOOL_APPS.values()))[0]
    assert keyed in core_named_apps()
    pin, manifest, tree = _pin_and_manifest(tmp_path, name=keyed)
    assert pinned_tree_refusal(pin, manifest, tree) == "pinned_feature_unsupported"


def test_a_shipped_version_unlike_the_pin_is_refused(tmp_path):
    from kiro_crew.apps.builtin_adoption_checks import pinned_tree_refusal

    pin, manifest, tree = _pin_and_manifest(tmp_path)
    shipped = types.SimpleNamespace(version="0.0.9")
    assert pinned_tree_refusal(pin, manifest, tree, shipped_manifest=shipped) == "version_mismatch"
    assert pinned_tree_refusal(pin, manifest, tree, record_version="0.2.0") == "version_mismatch"


def test_a_host_older_than_the_pin_needs_is_refused(tmp_path, monkeypatch):
    import dataclasses

    from kiro_crew.apps.builtin_adoption_checks import host_refusal

    host_with_seams(monkeypatch)
    pin, manifest, _tree = _pin_and_manifest(tmp_path, minKiroCrewVersion="999.0.0")
    assert host_refusal(pin, manifest) == "host_too_old"
    pin, manifest, _tree = _pin_and_manifest(tmp_path / "b")
    assert (
        host_refusal(dataclasses.replace(pin, min_app_sdk_level=99), manifest) == "host_sdk_too_old"
    )
    assert host_refusal(pin, manifest) is None


def test_no_shipped_pin_adopts_on_a_host_without_the_seams():
    """A pin adopts only where the structural route registry exists to mount its routes."""
    from kiro_crew.apps import route_registry
    from kiro_crew.apps.app_sdk_level import APP_SDK_LEVEL
    from kiro_crew.apps.builtin_adoption_pins import parse_adoption_pins

    document = json.loads((_SRC / "apps" / "adoption-pins.json").read_text(encoding="utf-8"))
    pins = parse_adoption_pins(document)
    assert pins
    has_seams = hasattr(route_registry, "_route_fields")
    for pin in pins.values():
        assert (pin.min_app_sdk_level <= APP_SDK_LEVEL) is has_seams, pin.name
