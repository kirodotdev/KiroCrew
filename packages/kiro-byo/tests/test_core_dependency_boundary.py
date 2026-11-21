from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "src" / "kiro_byo"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            out.add("." * node.level + (node.module or ""))
        elif isinstance(node, ast.Import):
            out.update(alias.name for alias in node.names)
    return out


def test_product_neutral_leaf_contracts_do_not_depend_on_runtime_or_provider_code() -> None:
    forbidden = {
        ".acp_server", ".turn_runner", ".providers", ".native_tools", ".mcp_client",
        ".provider_admin", ".provider_catalog", ".provider_products",
    }
    for name in ("turn_state.py", "execution_intent.py", "session_events.py"):
        imports = _imports(ROOT / name)
        assert not imports & forbidden, f"{name} leaked upward/runtime dependency: {imports & forbidden}"


def test_mode_router_is_model_intelligence_only() -> None:
    imports = _imports(ROOT / "mode_router.py")
    forbidden = {
        ".providers", ".native_tools", ".mcp_client", ".acp_server", ".turn_runner",
        ".provider_admin", ".provider_products",
    }
    assert not imports & forbidden, f"mode_router leaked execution/provider dependency: {imports & forbidden}"


def test_provider_protocol_modules_do_not_depend_on_agent_or_policy_runtime() -> None:
    forbidden_suffixes = {
        ".turn_runner", ".acp_server", ".mode_router", ".policy_packs",
        ".execution_intent", ".native_tools", ".mcp_client",
    }
    for path in (ROOT / "providers").glob("*.py"):
        imports = _imports(path)
        leaks = {
            item for item in imports
            if any(item.endswith(suffix) for suffix in forbidden_suffixes)
        }
        assert not leaks, f"{path.name} leaked upward dependency: {sorted(leaks)}"


def test_capability_seams_remain_narrow_adapters_not_a_second_plugin_runtime() -> None:
    imports = _imports(ROOT / "capability_seams.py")
    allowed_internal = {".mode_router", ".session_events"}
    actual_internal = {item for item in imports if item.startswith(".")}
    assert actual_internal <= allowed_internal, (
        "capability_seams gained implementation-specific dependencies: "
        f"{sorted(actual_internal - allowed_internal)}"
    )


def test_execution_intent_contains_no_builtin_tiga_type_names() -> None:
    text = (ROOT / "execution_intent.py").read_text(encoding="utf-8").lower()
    for product_name in ("sky", "multi", "power", "glitter"):
        assert product_name not in text, f"ExecutionIntent core leaked product Type name: {product_name}"
