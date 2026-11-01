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
        ROOT / "src/kiro_byo/crew_shared_patch.py",
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


def test_native_chat_vendor_adapters_share_the_http_sse_engine() -> None:
    """Vendor policy modules must not grow their own HTTP/SSE transport copies.

    These four APIs intentionally differ only in request policy on this path.
    The common request/stream/tool/usage machinery lives in
    ``chat_completions_common`` so adding vendor quirks does not recreate the
    pre-0.2 adapter sprawl in every module.
    """
    provider_dir = ROOT / "src/kiro_byo/providers"
    shared = (provider_dir / "chat_completions_common.py").read_text(encoding="utf-8")
    assert "def run_native_chat_completion" in shared
    for name in ("zhipu_chat.py", "moonshot_chat.py", "qwen_chat.py", "xiaomi_mimo.py"):
        text = (provider_dir / name).read_text(encoding="utf-8")
        assert "run_native_chat_completion" in text, name
        assert "urllib.request.Request(" not in text, f"{name} duplicated request construction"
        assert "iter_sse" not in text, f"{name} duplicated SSE parsing"
        assert len(text.splitlines()) < 180, f"{name} should remain a thin request-policy adapter"


def _is_provider_type_expr(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "type"
        and isinstance(node.value, ast.Name)
        and node.value.id == "provider"
    )


def test_responses_shared_engine_uses_capabilities_not_vendor_type_branches() -> None:
    path = ROOT / "src/kiro_byo/providers/responses_api.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        operands = [node.left, *node.comparators]
        if not any(_is_provider_type_expr(item) for item in operands):
            continue
        literals = [
            item.value for item in operands
            if isinstance(item, ast.Constant) and isinstance(item.value, str)
        ]
        assert not literals, (
            "responses_api must branch on ResponsesCapabilities, not provider.type literals: "
            f"{literals}"
        )


def test_mistral_is_a_content_codec_over_shared_chat_engine() -> None:
    path = ROOT / "src/kiro_byo/providers/mistral_chat.py"
    text = path.read_text(encoding="utf-8")
    assert "ChatContentCodec" in text
    assert "run_native_chat_completion" in text
    assert "for raw_line in" not in text
    assert "iter_response_lines" not in text
    assert "read_response_bytes" not in text
    assert len(text.splitlines()) < 260


def test_provider_product_policy_does_not_branch_on_vendor_ids_outside_providers() -> None:
    """Commercial/vendor choices are data or provider-adapter concerns.

    Generic config/admin/product/account layers may branch on capabilities and
    product ids, but must not grow new ``preset == vendor`` control flow.
    """
    paths = (
        ROOT / "src/kiro_byo/config.py",
        ROOT / "src/kiro_byo/provider_admin.py",
        ROOT / "src/kiro_byo/provider_products.py",
        ROOT / "src/kiro_byo/account_status.py",
    )
    vendor_ids = {
        "anthropic", "gemini", "qwen", "deepseek", "moonshot", "mistral",
        "zhipu", "cohere", "minimax", "xiaomi", "xiaomi-tokenplan",
    }
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            literals = {
                item.value.lower()
                for item in [node.left, *node.comparators]
                if isinstance(item, ast.Constant) and isinstance(item.value, str)
            }
            leaked = literals & vendor_ids
            assert not leaked, f"{path.name}: vendor-id branch leaked outside providers/: {sorted(leaked)}"
