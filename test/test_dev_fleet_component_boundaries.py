"""Architecture contracts for the Dev Fleet backend component split."""

from __future__ import annotations

import ast
import inspect

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
    owners: dict[str, object] = {}
    for component in _COMPONENTS:
        for name in component.__all__:
            assert name not in owners, f"{name} exported by two Dev Fleet components"
            owners[name] = component

    assert server._EXPORT_OWNERS == owners
    assert set(owners) <= set(dir(server))
    for name, owner in owners.items():
        assert getattr(server, name) is getattr(owner, name)

    with pytest.raises(AttributeError, match="has no attribute"):
        getattr(server, "_not_a_dev_fleet_export")


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
        ("POST", "/api/restart-gateway", "api_dev_fleet_restart_gateway"),
        ("POST", "/api/make-live", "api_dev_fleet_make_live"),
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
