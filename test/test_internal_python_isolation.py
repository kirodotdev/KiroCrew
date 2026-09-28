from __future__ import annotations

import ast
import site
import sys
from pathlib import Path
from typing import Callable, cast

import pytest

from kiro_crew import platform_compat

pytestmark = pytest.mark.xdist_group(name="tree_scan_internal_python_isolation")

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC_ROOT = _REPO_ROOT / "src" / "kiro_crew"

# Each row owns an argv that can run under the desktop bundle's interpreter.
# Keeping the inventory explicit makes a new process boundary a reviewed change.
_INTERNAL_PYTHON_SPAWN_SITES = (
    ("platform_compat.py", "reexec_python_module"),
    ("agent.py", "_kirocrew_mcp_invocation"),
    ("apps/backend.py", "_provision_app_deps_locked"),
    ("apps/backend.py", "_start_app_backend_body"),
    ("apps/bridges.py", "_pin_host_cli_command"),
    ("apps/bridges.py", "resolve_stdio_command"),
    ("apps/builtins/auto_improvement/backend/deps.py", "install_deps"),
    ("apps/builtins/dev_fleet/runtime.py", "_find_cli"),
    ("apps/registry.py", "_run_app_build"),
    ("cli.py", "_child_argv"),
    ("cli_server.py", "_spawn_detached_gateway"),
    ("cli_server.py", "_refresh_agent_config"),
    ("computer_use/overlay.py", "CursorOverlay._spawn"),
    ("dashboard/handlers/memory.py", "_ensure_pip_available"),
    ("dashboard/handlers/memory.py", "api_memory_enable_embeddings"),
    ("mcp_gateway/manager.py", "GatewayManager._spawn_once"),
    ("mcp_gateway/rewriter.py", "_build_stub_entry"),
    ("piper_runtime.py", "PiperRuntime._start"),
    ("pod/unit.py", "_kirocrew_argv"),
    ("slack/gateway.py", "GatewayOrchestrator._check_missing_deps"),
    ("slack/gateway.py", "GatewayOrchestrator._auto_apply_update"),
    ("testing/harness.py", "_launch_gateway"),
    ("testing/harness.py", "spawn_feature_gateway"),
)


def _definition(tree: ast.Module, qualified_name: str) -> ast.AST:
    body: list[ast.stmt] = tree.body
    node: ast.AST | None = None
    for part in qualified_name.split("."):
        node = next(
            (
                item
                for item in body
                if isinstance(item, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                and item.name == part
            ),
            None,
        )
        assert node is not None, f"missing inventoried spawn site {qualified_name}"
        body = node.body  # type: ignore[attr-defined]
    return node


def _calls_shared_isolator(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        fn = child.func
        if isinstance(fn, ast.Name) and fn.id == "isolated_python_argv":
            return True
        if isinstance(fn, ast.Attribute) and fn.attr == "isolated_python_argv":
            return True
    return False


def test_every_internal_python_spawn_site_uses_the_shared_isolator() -> None:
    missing: list[str] = []
    for relative_path, qualified_name in _INTERNAL_PYTHON_SPAWN_SITES:
        path = _SRC_ROOT / relative_path
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if not _calls_shared_isolator(_definition(tree, qualified_name)):
            missing.append(f"{relative_path}::{qualified_name}")
    assert not missing, "internal Python spawn bypasses isolated_python_argv: " + ", ".join(missing)


def test_isolated_python_argv_disables_user_site_without_weakening_I(monkeypatch) -> None:
    helper = getattr(platform_compat, "isolated_python_argv", None)
    assert helper is not None, "platform_compat must own the shared Python isolation helper"
    isolated = cast(Callable[..., list[str]], helper)
    monkeypatch.setattr(site, "ENABLE_USER_SITE", False)
    monkeypatch.setattr(platform_compat, "is_bundled_interpreter", lambda: False)

    assert isolated("-m", "kiro_crew") == [sys.executable, "-s", "-m", "kiro_crew"]
    assert isolated("-mkiro_crew.apps.x") == [sys.executable, "-s", "-mkiro_crew.apps.x"]
    assert isolated("-cimport server") == [sys.executable, "-s", "-cimport server"]
    assert isolated("-s", "-m", "kiro_crew") == [sys.executable, "-s", "-m", "kiro_crew"]
    assert isolated("-us", "-m", "kiro_crew") == [
        sys.executable,
        "-us",
        "-m",
        "kiro_crew",
    ]
    assert isolated("-I", "-c", "pass") == [sys.executable, "-I", "-c", "pass"]
    assert isolated("server.py", executable="/opt/app/python") == [
        "/opt/app/python",
        "-s",
        "server.py",
    ]


def test_isolated_python_argv_preserves_nonbundled_user_site_parent(monkeypatch) -> None:
    isolated = cast(Callable[..., list[str]], platform_compat.isolated_python_argv)
    monkeypatch.setattr(site, "ENABLE_USER_SITE", True)
    monkeypatch.setattr(platform_compat, "is_bundled_interpreter", lambda: False)

    assert isolated("-m", "kiro_crew") == [sys.executable, "-m", "kiro_crew"]
    assert isolated("-m", "kiro_crew", force_isolation=True) == [
        sys.executable,
        "-s",
        "-m",
        "kiro_crew",
    ]


def test_bundled_parent_isolates_every_helper_routed_argv(
    bundled_python_with_user_site,
) -> None:
    isolated = cast(Callable[..., list[str]], platform_compat.isolated_python_argv)

    assert isolated("-m", "kiro_crew") == [sys.executable, "-s", "-m", "kiro_crew"]
    assert isolated("-m", "pip") == [sys.executable, "-s", "-m", "pip"]
    assert isolated("server.py", executable="/bundle/python") == [
        "/bundle/python",
        "-s",
        "server.py",
    ]
