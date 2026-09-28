"""Architecture contracts for the Dev Fleet backend component split."""

from __future__ import annotations

import ast
import inspect
import sys
from types import ModuleType

import pytest

from kiro_crew.apps.builtins.dev_fleet import (
    fleet_state,
    http_api,
    live,
    repository,
    runtime,
    server,
    worktree_ops,
)

_COMPONENTS = (
    runtime,
    repository,
    live,
    fleet_state,
    worktree_ops,
    http_api,
)
_RANK = {component.__name__.rsplit(".", 1)[-1]: rank for rank, component in enumerate(_COMPONENTS)}


def test_facade_exports_have_one_owner_and_remain_read_compatible() -> None:
    owners: dict[str, str] = {}
    for component in _COMPONENTS:
        for name in component.__all__:
            assert name not in owners, f"{name} exported by two Dev Fleet components"
            owners[name] = component.__name__

    assert server._EXPORT_OWNERS == owners
    assert set(owners) <= set(dir(server))
    for component in _COMPONENTS:
        for name in component.__all__:
            assert getattr(server, name) is getattr(component, name)

    with pytest.raises(AttributeError, match="has no attribute"):
        getattr(server, "_not_a_dev_fleet_export")


def test_the_owner_table_holds_dotted_names_not_modules() -> None:
    """``sys.modules`` is where a module is stored, so this table holds no module.

    A mapping of resolved module objects is a second storage location for the owner,
    and the two disagree as soon as a component is replaced or reimported: the
    facade then reads and forwards writes to the discarded module while a direct
    importer holds the fresh one.
    """
    modules = sorted(
        name for name, owner in server._EXPORT_OWNERS.items() if isinstance(owner, ModuleType)
    )
    assert modules == [], (
        "the facade stores a resolved module object for these names, which is a "
        f"second storage location for the owner beside sys.modules: {modules}"
    )
    for name, owner in server._EXPORT_OWNERS.items():
        assert isinstance(owner, str), name
        assert owner.startswith("kiro_crew.apps.builtins.dev_fleet."), name


def test_reads_and_writes_follow_a_component_to_a_new_module_object() -> None:
    """A stand-in module stands for a reimported one, so no component body runs twice."""
    owner_name = repository.__name__
    name = "MAIN_REPO"
    assert server._EXPORT_OWNERS[name] == owner_name
    server._owner(name)  # resolve the owner first, so any cache is warm

    real = sys.modules[owner_name]
    original = getattr(real, name)
    stand_in = ModuleType(owner_name)
    read_sentinel = object()
    setattr(stand_in, name, read_sentinel)
    try:
        sys.modules[owner_name] = stand_in
        assert getattr(server, name) is read_sentinel, "the facade read the replaced component"
        write_sentinel = object()
        setattr(server, name, write_sentinel)
        assert (
            getattr(stand_in, name) is write_sentinel
        ), "a write through the facade missed the component in sys.modules"
        assert (
            getattr(real, name) is original
        ), "the write reached the module the facade resolved earlier"
    finally:
        sys.modules[owner_name] = real
    assert getattr(server, name) is original


def test_no_re_exported_name_also_names_a_component() -> None:
    """The precondition the forwarding ``__setattr__`` rests on.

    The import system binds a submodule onto its parent with ``setattr``, which the
    facade would send to a component instead of binding the submodule.
    """
    leaves = {component.__name__.rsplit(".", 1)[-1] for component in _COMPONENTS}
    assert leaves & set(server._EXPORT_OWNERS) == set()


def test_an_unimportable_component_is_loud_on_read_write_and_delete() -> None:
    """Resolving per use makes an unimportable owner raise where the base was silent.

    Here a READ goes through ``_owner`` as well, so all three verbs surface it. That is
    the honest outcome: the alternative is the defect -- reading and writing a module
    that is absent from ``sys.modules``, which nothing reports. A name outside the table
    is unaffected, because no import is attempted for it.
    """
    owner_name = repository.__name__
    name = "MAIN_REPO"
    real = sys.modules[owner_name]
    original = getattr(real, name)
    try:
        sys.modules[owner_name] = None  # type: ignore[assignment]
        with pytest.raises(ImportError):
            getattr(server, name)
        with pytest.raises(ImportError):
            setattr(server, name, object())
        with pytest.raises(ImportError):
            delattr(server, name)
        with pytest.raises(AttributeError):
            server._not_a_dev_fleet_export  # noqa: B018 - the access IS the assertion
    finally:
        sys.modules[owner_name] = real
    assert getattr(server, name) is original
    assert getattr(real, name) is original


def test_facade_exports_forward_mutation_to_their_owner(monkeypatch) -> None:
    original = repository.MAIN_REPO
    sentinel = object()

    monkeypatch.setattr(server, "MAIN_REPO", sentinel)

    assert repository.MAIN_REPO is sentinel
    assert server.MAIN_REPO is sentinel
    monkeypatch.undo()
    assert repository.MAIN_REPO is original


def test_component_imports_follow_the_ownership_dag() -> None:
    """Lower-level owners must never call back through a higher component."""
    for component in _COMPONENTS:
        current = component.__name__.rsplit(".", 1)[-1]
        tree = ast.parse(inspect.getsource(component))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.module != "kiro_crew.apps.builtins.dev_fleet":
                continue
            for alias in node.names:
                imported_rank = _RANK.get(alias.name)
                if imported_rank is not None:
                    assert (
                        imported_rank < _RANK[current]
                    ), f"{current} imports higher-level component {alias.name}"
                assert alias.name != "server", f"{current} calls back through the facade"


def test_server_remains_a_thin_composition_facade() -> None:
    tree = ast.parse(inspect.getsource(server))
    definitions = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    assert definitions == {
        "__getattr__",
        "__dir__",
        "_owner",
        "_CompatibilityModule",
        "dev_fleet_startup",
        "dev_fleet_cleanup",
        "create_app",
        "main",
    }
    assert len(inspect.getsource(server).splitlines()) < 400


def test_route_manifest_and_http_adapter_ownership_are_stable() -> None:
    expected = [
        ("HEAD", "/health", "api_health"),
        ("GET", "/health", "api_health"),
        ("HEAD", "/api/health", "api_health"),
        ("GET", "/api/health", "api_health"),
        ("HEAD", "/api/fleet", "api_dev_fleet_fleet"),
        ("GET", "/api/fleet", "api_dev_fleet_fleet"),
        ("HEAD", "/api/worktree", "api_dev_fleet_worktree"),
        ("GET", "/api/worktree", "api_dev_fleet_worktree"),
        ("HEAD", "/api/pod/logs", "api_dev_fleet_pod_logs"),
        ("GET", "/api/pod/logs", "api_dev_fleet_pod_logs"),
        ("HEAD", "/api/run", "api_dev_fleet_run"),
        ("GET", "/api/run", "api_dev_fleet_run"),
        ("HEAD", "/api/prune-candidates", "api_dev_fleet_prune_candidates"),
        ("GET", "/api/prune-candidates", "api_dev_fleet_prune_candidates"),
        ("HEAD", "/api/prune-status", "api_dev_fleet_prune_status"),
        ("GET", "/api/prune-status", "api_dev_fleet_prune_status"),
        ("HEAD", "/api/disk", "api_dev_fleet_disk"),
        ("GET", "/api/disk", "api_dev_fleet_disk"),
        ("POST", "/api/sync", "api_dev_fleet_sync"),
        ("POST", "/api/worktree/remove", "api_dev_fleet_worktree_remove"),
        ("POST", "/api/prune-run", "api_dev_fleet_prune_run"),
        ("POST", "/api/pod/up", "api_dev_fleet_pod_up"),
        ("POST", "/api/pod/down", "api_dev_fleet_pod_down"),
        ("POST", "/api/pod/restart", "api_dev_fleet_pod_restart"),
        ("POST", "/api/pod/token", "api_dev_fleet_pod_token"),
        ("POST", "/api/pod/provision", "api_dev_fleet_pod_provision"),
        ("POST", "/api/pod/provision/dismiss", "api_dev_fleet_pod_provision_dismiss"),
        ("POST", "/api/rebase", "api_dev_fleet_rebase"),
        # NOT here: /api/restart-gateway and /api/make-live. They touch the live-target
        # pointer (or its cutover latch), which is masked from this sandboxed backend and
        # every child it spawns; the gateway process serves them under
        # /api/apps/dev-fleet/ (gateway_routes.py, pinned by test_dev_fleet_gateway_routes).
    ]
    actual = [
        (route.method, route.resource.canonical, route.handler.__name__)
        for route in server.create_app().router.routes()
    ]
    assert actual == expected
    assert all(
        route.handler.__module__ == http_api.__name__
        for route in server.create_app().router.routes()
    )
