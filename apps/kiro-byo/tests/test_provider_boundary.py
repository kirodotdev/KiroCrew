from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = (
    ROOT / "src/kiro_byo/acp_server.py",
    ROOT / "src/kiro_byo/turn_runner.py",
)
VENDOR_IDS = {
    "anthropic", "claude", "gemini", "qwen", "deepseek", "moonshot", "kimi",
    "mistral", "zhipu", "cohere", "minimax", "openai", "responses",
}


def _is_providerish(node: ast.AST) -> bool:
    if isinstance(node, ast.Name):
        return "provider" in node.id.lower() or "transport" in node.id.lower()
    if isinstance(node, ast.Attribute):
        return "provider" in node.attr.lower() or "transport" in node.attr.lower()
    return False


def test_agent_harness_imports_only_provider_facade() -> None:
    for path in CORE:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                # Concrete wire adapters must remain behind kiro_byo.providers.
                assert not module.startswith("providers.")
                assert not module.startswith("kiro_byo.providers.")
                if node.level and module.startswith("providers."):
                    raise AssertionError(f"{path.name}: concrete provider import {module}")


def test_agent_harness_has_no_vendor_identity_branching() -> None:
    for path in CORE:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare) or not _is_providerish(node.left):
                continue
            for comparator in node.comparators:
                if isinstance(comparator, ast.Constant) and isinstance(comparator.value, str):
                    value = comparator.value.lower()
                    assert not any(vendor in value for vendor in VENDOR_IDS), (
                        f"{path.name}: provider/vendor branching leaked into the harness: {comparator.value!r}"
                    )


def test_crew_integration_transformers_use_no_vendor_specific_anchor() -> None:
    sources = (
        ROOT / "src/kiro_byo/crew09_patch.py",
        ROOT / "src/kiro_byo/crew_patch_common.py",
    )
    # Backend registration is structurally anchored to native Kiro / Kiro BYO
    # seams. Concrete provider/backend identities must not decide integration.
    for path in sources:
        source = path.read_text(encoding="utf-8")
        for vendor in VENDOR_IDS:
            assert f"ACP_BACKEND_{vendor.upper()}" not in source, (
                f"{path.name}: Crew integration leaked a vendor-specific backend anchor: {vendor}"
            )
            assert f"PROVIDER_LABEL_{vendor.upper()}" not in source, (
                f"{path.name}: Crew integration leaked a vendor-specific provider-label anchor: {vendor}"
            )
