from __future__ import annotations

from pathlib import Path

from kiro_byo.providers import registry


def test_registry_uses_one_generic_module_binding_instead_of_vendor_forwarder_classes() -> None:
    source = Path(registry.__file__).read_text(encoding="utf-8")
    assert "class _ModuleAdapter" in source
    for legacy in (
        "_ZhipuAdapter", "_QwenChatAdapter", "_MoonshotAdapter", "_XiaomiMiMoAdapter",
        "_AnthropicAdapter", "_GeminiAdapter", "_MistralAdapter", "_CohereAdapter",
    ):
        assert f"class {legacy}" not in source


def test_only_status_aware_protocols_keep_raw_request_opening() -> None:
    root = Path(registry.__file__).parent
    raw = []
    for path in root.glob("*.py"):
        if path.name in {"http_sse.py"}:
            continue
        text = path.read_text(encoding="utf-8")
        if "urllib.request.Request(" in text:
            raw.append(path.name)
    assert sorted(raw) == ["gemini_interactions.py", "responses_api.py"]


def test_product_aliases_do_not_create_new_adapter_classes() -> None:
    # Multiple commercial products may bind to the same executable protocol.
    xiaomi = registry._ADAPTERS["xiaomi-mimo-chat"]
    assert xiaomi.transport == "xiaomi-mimo-chat"
    assert not hasattr(registry, "_XiaomiTokenPlanAdapter")
