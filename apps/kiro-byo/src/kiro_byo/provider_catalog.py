"""Provider presets and model-capability normalization for Kiro BYO 0.1.0.

Presets are connection templates, not capability claims. Model capabilities are
resolved per *model* from provider discovery metadata first, then conservative
protocol defaults. Unknown means unknown: Kiro BYO does not invent a reasoning
effort selector just because a provider brand is known.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


REASONING_MODES = {"none", "fixed", "levels", "toggle", "adaptive", "unknown"}


@dataclass(frozen=True)
class ProviderPreset:
    id: str
    display_name: str
    default_base_url: str
    transport: str = "openai-compatible"
    requires_api_key: bool = True
    available: bool = True
    category: str = "official"
    note: str = ""
    # Model discovery may differ from the chat compatibility endpoint. These
    # fields are runtime-only and are intentionally not exposed as credentials.
    discovery_url: str = ""
    discovery_auth: str = "bearer"
    discovery_headers: tuple[tuple[str, str], ...] = ()
    # Optional account features backed by documented official endpoints.
    # Empty means Kiro BYO intentionally exposes no account UI for this preset.
    account_capabilities: tuple[str, ...] = ()
    model_transport_overrides: bool = False
    # Some official /models catalogues mix chat models with ASR/TTS/image-only
    # products.  An allowlist keeps Kiro Crew's text-agent picker from exposing
    # endpoints that cannot participate in the agent loop. Empty means no filter.
    discovery_model_allowlist: tuple[str, ...] = ()
    # Additional first-party endpoints that preserve the same native transport.
    # This is useful when one vendor exposes separate official billing/region
    # endpoints (for example MiMo Token Plan) that are not relays.
    official_base_urls: tuple[str, ...] = ()
    # Some official subscription endpoints publish a fixed supported model set
    # but do not document a /models catalogue. These rows are treated as an
    # official static catalogue and still receive verified model profiles.
    static_model_ids: tuple[str, ...] = ()

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "default_base_url": self.default_base_url,
            "transport": self.transport,
            "transport_label": transport_label(self.transport),
            "native_transport": self.transport not in {"openai-compatible", "openai-chat-completions"},
            "requires_api_key": self.requires_api_key,
            "available": self.available,
            "category": self.category,
            "note": self.note,
            "account_capabilities": list(self.account_capabilities),
            "transport_strategy": "model" if self.model_transport_overrides else "provider",
            "transport_strategy_label": "按模型优化" if self.model_transport_overrides else transport_label(self.transport),
            "official_base_urls": list(self.official_base_urls),
            "static_model_ids": list(self.static_model_ids),
        }


# Keep this list about connection UX. Capability differences belong to model
# metadata, not the provider row. Official presets may select a verified native
# transport, while custom/relay endpoints always retain the compatibility fallback.
_PROVIDER_PRESETS: tuple[ProviderPreset, ...] = (
    # Mainland China model vendors / official platforms.
    # Prefer the provider's current agent-native protocol when it materially
    # preserves reasoning/tool state or unlocks provider-side caching. When an
    # official API is itself OpenAI-compatible, keep the compatibility transport
    # instead of inventing a fake "native" layer.
    ProviderPreset(
        "deepseek", "DeepSeek", "https://api.deepseek.com",
        transport="deepseek-responses", category="china",
        note="原生 Responses API：保留 reasoning/tool items，使用 DeepSeek 自动上下文缓存。",
        account_capabilities=("balance",),
    ),
    ProviderPreset(
        "moonshot", "Moonshot / Kimi", "https://api.moonshot.cn/v1",
        transport="moonshot-chat-v1", category="china",
        note="原生 Kimi Chat：完整保留 reasoning_content/tool_calls；K3 自动利用上下文缓存，K2.7/K2.6 保留式思考按模型契约处理。",
    ),
    ProviderPreset(
        "xiaomi", "小米 MiMo / Xiaomi", "https://api.xiaomimimo.com/v1",
        transport="xiaomi-mimo-chat", category="china",
        note="统一的小米 MiMo 入口：sk- Key 使用按量付费 API；tp-/ttp- Key 会自动切换到 Token Plan 中国集群。也可手动填写新加坡/欧洲 Token Plan 官方 Base URL。",
        discovery_url="https://api.xiaomimimo.com/v1/models",
        discovery_model_allowlist=(
            "mimo-v2.6-pro", "mimo-v2.6-flash", "mimo-v2.6-pro-ultraspeed",
        ),
        official_base_urls=(
            "https://token-plan-cn.xiaomimimo.com/v1",
            "https://token-plan-sgp.xiaomimimo.com/v1",
            "https://token-plan-ams.xiaomimimo.com/v1",
        ),
    ),
    ProviderPreset(
        "xiaomi-tokenplan", "小米 MiMo Token Plan", "https://token-plan-cn.xiaomimimo.com/v1",
        transport="xiaomi-mimo-chat", category="china",
        note="官方 Token Plan（tp-/ttp- Key）。默认中国集群；新加坡/欧洲套餐可在高级设置中改为对应官方 Base URL，仍保持 MiMo 原生适配。",
        official_base_urls=(
            "https://token-plan-cn.xiaomimimo.com/v1",
            "https://token-plan-sgp.xiaomimimo.com/v1",
            "https://token-plan-ams.xiaomimimo.com/v1",
        ),
        # Xiaomi documents the Token Plan model entitlement separately from the
        # pay-as-you-go /models API. Keep only text-agent-capable V2.6 models.
        static_model_ids=("mimo-v2.6-pro", "mimo-v2.6-flash"),
    ),
    ProviderPreset(
        "qwen", "阿里云百炼 / Qwen", "https://dashscope.aliyuncs.com/compatible-mode/v1",
        transport="qwen-chat-v1", category="china",
        note="按模型优化：Qwen Chat 保留 reasoning_content、模型级 enable_thinking 与流式 usage；支持 Responses API 的 Qwen 3.5–3.8 系列进一步使用 Responses + previous_response_id 与百炼 Session 缓存。",
        model_transport_overrides=True,
    ),
    ProviderPreset(
        "zhipu", "智谱 AI / GLM", "https://open.bigmodel.cn/api/paas/v4",
        transport="zhipu-chat-v4", category="china",
        note="原生 GLM Chat 扩展：保留 reasoning_content/tool_calls，按模型发送 thinking 与 reasoning_effort，并利用官方隐式上下文缓存。",
    ),
    ProviderPreset(
        "volcengine", "火山引擎 / Ark / 豆包", "https://ark.cn-beijing.volces.com/api/v3",
        transport="ark-responses", category="china",
        note="原生 Responses API：回传完整 reasoning/tool items，并使用稳定 prompt_cache_key 提升隐式缓存命中。",
    ),
    ProviderPreset(
        "hunyuan", "腾讯混元", "https://tokenhub.tencentmaas.com/v1", category="china",
        note="当前腾讯官方 TokenHub 接口；Hy3 / Hy4 preview 自动使用 Responses，其余模型按官方协议能力回退到 Chat Completions。",
        model_transport_overrides=True,
    ),
    ProviderPreset(
        "qianfan", "百度千帆 / ERNIE", "https://qianfan.baidubce.com/v2",
        transport="qianfan-responses", category="china",
        note="原生 Responses API：使用 previous_response_id 复用服务端会话状态，并完整保留 reasoning/tool items。深度思考开关仅在模型能力明确时显示。",
    ),
    ProviderPreset(
        "minimax", "MiniMax", "https://api.minimax.io/anthropic/v1",
        transport="minimax-messages", category="china",
        note="官方推荐 Anthropic Messages 兼容接口：保留 interleaved thinking/tool blocks，并启用显式 prompt caching。中国大陆站 API Key 请把 API 地址改为 https://api.minimax.cn/anthropic/v1。",
        discovery_auth="x-api-key",
    ),
    ProviderPreset(
        "stepfun", "阶跃星辰 / StepFun", "https://api.stepfun.com/v1", category="china",
        note="官方当前公开推理接口即 OpenAI Chat 兼容协议；Kiro BYO 保持该官方协议，不伪造未公开的原生能力。",
    ),

    # International model vendors / official inference platforms.
    ProviderPreset(
        "openai", "OpenAI", "https://api.openai.com/v1",
        transport="openai-responses", category="global",
        note="原生 Responses API（store=false）：完整回传加密 reasoning items，并按会话固定 prompt_cache_key。",
    ),
    ProviderPreset(
        "anthropic", "Anthropic / Claude", "https://api.anthropic.com/v1",
        transport="anthropic-messages", category="global",
        note="原生 Messages API：保留 thinking/signature、tool_use/tool_result 与 prompt caching。",
        discovery_auth="x-api-key",
        discovery_headers=(("anthropic-version", "2023-06-01"),),
    ),
    ProviderPreset(
        "gemini", "Google Gemini", "https://generativelanguage.googleapis.com/v1beta",
        transport="gemini-interactions", category="global",
        note="原生 Interactions API：保留 thought signatures，并优先使用 previous_interaction_id 复用服务端状态。",
        discovery_url="https://generativelanguage.googleapis.com/v1beta/models",
        discovery_auth="x-goog-api-key",
    ),
    ProviderPreset(
        "grok", "xAI / Grok", "https://api.x.ai/v1",
        transport="xai-responses", category="global",
        note="原生 Responses API：完整回传加密 reasoning items，按会话固定 prompt_cache_key，并读取 xAI 完整语言模型能力目录。",
        discovery_url="https://api.x.ai/v1/language-models",
    ),
    ProviderPreset(
        "mistral", "Mistral AI", "https://api.mistral.ai/v1",
        transport="mistral-chat-v1", category="global",
        note="原生 Chat API：保留 Mistral ThinkChunk/tool_calls，并按会话发送 prompt_cache_key 提高缓存命中率。",
    ),
    ProviderPreset(
        "cohere", "Cohere", "https://api.cohere.com",
        transport="cohere-v2", category="global",
        note="原生 Chat V2：保留 Cohere thinking/tool-call 结构。",
        discovery_url="https://api.cohere.com/v1/models",
    ),
    ProviderPreset(
        "groq", "Groq", "https://api.groq.com/openai/v1",
        transport="groq-responses", category="global",
        note="官方 Responses API；保持无状态完整上下文回传（Groq 当前不支持 previous_response_id / prompt_cache_key）。",
    ),

    # Relays, gateways, local servers and other compatible endpoints.
    ProviderPreset(
        "custom", "自定义 / 中转站（OpenAI Compatible）", "",
        requires_api_key=False, category="custom",
        note="用于中转站、自建服务或其他 OpenAI Compatible endpoint。",
    ),
)

TRANSPORT_LABELS: dict[str, str] = {
    "openai-compatible": "OpenAI Compatible",
    "openai-chat-completions": "Chat Completions",
    "openai-responses": "原生 Responses API",
    "deepseek-responses": "原生 Responses API",
    "ark-responses": "原生 Responses API",
    "xai-responses": "原生 Responses API",
    "groq-responses": "官方 Responses API",
    "anthropic-messages": "原生 Messages API",
    "gemini-interactions": "原生 Interactions API",
    "cohere-v2": "原生 Chat V2",
    "qwen-chat-v1": "官方 Qwen Chat API",
    "qwen-responses": "官方 Responses API",
    "hunyuan-responses": "官方 Responses API",
    "mistral-chat-v1": "原生 Chat API",
    "moonshot-chat-v1": "官方 Kimi Chat API",
    "xiaomi-mimo-chat": "官方 MiMo Chat API",
    "minimax-messages": "官方 Messages 兼容 API",
    "qianfan-responses": "原生 Responses API",
    "zhipu-chat-v4": "官方 GLM Chat API",
}


def transport_label(transport: str) -> str:
    value = str(transport or "").strip().lower()
    return TRANSPORT_LABELS.get(value, value or "未知")


def official_model_profile(preset_id: str, model_id: str) -> dict[str, Any]:
    """Return verified per-model capability overrides for official endpoints.

    Profiles are deliberately narrow. They exist only where provider docs define
    a model-specific contract that /models does not reliably expose. Unknown
    capabilities remain unknown rather than being guessed from brand/model names.
    """
    preset = str(preset_id or "").strip().lower()
    mid = str(model_id or "").strip().lower().removeprefix("models/")

    # DeepSeek's /models is rich in 2026, but keep these aliases profiled so
    # manually-entered ids and stale catalogues still get the official contract.
    if preset == "deepseek":
        if mid in {"deepseek-flash", "deepseek-v4-flash", "deepseek-v4-flash-vision-exp"}:
            return {
                "context_window": 1_048_576,
                "max_output_tokens": 393_216,
                "input_modalities": ["text", "image"],
                "output_modalities": ["text"],
                "effort_supported_levels": ["low", "high", "max"],
                "effort_default_level": "high",
                "reasoning": {
                    "mode": "levels", "supported": True, "can_disable": True,
                    "default_enabled": True, "disable_value": "none",
                    "effort": {"levels": ["low", "high", "max"], "default": "high"},
                    "source": "official-profile",
                },
                "tool_use": True,
                "capability_source": "official-profile",
            }
        if mid == "deepseek-v4-pro":
            return {
                "context_window": 1_048_576,
                "max_output_tokens": 393_216,
                "input_modalities": ["text"],
                "output_modalities": ["text"],
                "effort_supported_levels": ["low", "high", "max"],
                "effort_default_level": "high",
                "reasoning": {
                    "mode": "levels", "supported": True, "can_disable": True,
                    "default_enabled": True, "disable_value": "none",
                    "effort": {"levels": ["low", "high", "max"], "default": "high"},
                    "source": "official-profile",
                },
                "tool_use": True,
                "capability_source": "official-profile",
            }


    if preset == "openai":
        # Current OpenAI flagship/recent reasoning families. Reasoning on/off
        # and effort remain separate: models that accept `none` expose a
        # thinking switch while Astra/6.1 Sol expose only the five enabled
        # intensity levels. Responses is required for full tool fidelity.
        common = {
            "context_window": 1_050_000,
            "max_output_tokens": 128_000,
            "input_modalities": ["text", "image"],
            "output_modalities": ["text"],
            "tool_use": True,
            "capability_source": "official-profile",
        }
        enabled5 = ["low", "medium", "high", "xhigh", "max"]
        if mid in {"gpt-6-astra", "gpt-6.1-sol"}:
            return {**common,
                "effort_supported_levels": enabled5, "effort_default_level": "medium",
                "reasoning": {"mode":"levels","supported":True,"can_disable":False,
                    "default_enabled":True,
                    "effort":{"levels":enabled5,"default":"medium"},
                    "source":"official-profile"}}
        if mid in {"gpt-6-sol", "gpt-6-luna"}:
            return {**common,
                "effort_supported_levels": enabled5, "effort_default_level": "medium",
                "reasoning": {"mode":"levels","supported":True,"can_disable":True,
                    "default_enabled":True,"disable_value":"none",
                    "effort":{"levels":enabled5,"default":"medium"},
                    "source":"official-profile"}}
        if mid in {"gpt-5.6", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"}:
            return {**common,
                "effort_supported_levels": enabled5, "effort_default_level": "medium",
                "reasoning": {"mode":"levels","supported":True,"can_disable":True,
                    "default_enabled":True,"disable_value":"none",
                    "effort":{"levels":enabled5,"default":"medium"},
                    "source":"official-profile"}}

    if preset == "grok":
        common = {
            "input_modalities": ["text", "image"],
            "output_modalities": ["text"],
            "tool_use": True,
            "capability_source": "official-profile",
        }
        if mid in {"grok-4.7", "grok-4.6"}:
            levels = ["low", "medium", "high", "xhigh"]
            row = {**common, "effort_supported_levels": levels, "effort_default_level": "high",
                "reasoning": {"mode":"levels","supported":True,"can_disable":False,
                    "default_enabled":True,"effort":{"levels":levels,"default":"high"},
                    "source":"official-profile"}}
            if mid == "grok-4.7":
                row["context_window"] = 500_000
            return row
        if mid == "grok-4.5":
            levels = ["low", "medium", "high"]
            return {**common, "effort_supported_levels": levels, "effort_default_level": "high",
                "reasoning": {"mode":"levels","supported":True,"can_disable":False,
                    "default_enabled":True,"effort":{"levels":levels,"default":"high"},
                    "source":"official-profile"}}
        if mid == "grok-4.3":
            levels = ["low", "medium", "high"]
            return {**common, "effort_supported_levels": levels, "effort_default_level": "high",
                "reasoning": {"mode":"levels","supported":True,"can_disable":True,
                    "default_enabled":True,"disable_value":"none",
                    "effort":{"levels":levels,"default":"high"},
                    "source":"official-profile"}}

    if preset == "moonshot":
        # Kimi's current official API is OpenAI-shaped, but the provider adds
        # reasoning_content, preserved-thinking rules and model-specific thinking
        # controls that a generic compatibility adapter would lose in tool loops.
        # Keep these profiles narrow and based on the current Kimi model docs.
        common_mm = {
            "input_modalities": ["text", "image", "video"],
            "output_modalities": ["text"],
            "tool_use": True,
            "capability_source": "official-profile",
        }
        if mid == "kimi-k3":
            levels = ["low", "high", "max"]
            return {
                **common_mm,
                "context_window": 1_000_000,
                "max_output_tokens": 1_048_576,
                "effort_supported_levels": levels,
                "effort_default_level": "max",
                "reasoning": {
                    "mode": "levels", "supported": True, "can_disable": False,
                    "default_enabled": True,
                    "effort": {"levels": levels, "default": "max"},
                    "source": "official-profile",
                },
            }
        if mid in {"kimi-k2.7-code", "kimi-k2.7-code-highspeed"}:
            return {
                **common_mm,
                "context_window": 262_144,
                "max_output_tokens": 32_768,
                "reasoning": {
                    "mode": "fixed", "supported": True, "can_disable": False,
                    "default_enabled": True,
                    # K2.7 is always-thinking; omit `thinking` entirely rather
                    # than redundantly sending the sole legal `enabled` value.
                    "source": "official-profile",
                },
            }
        if mid == "kimi-k2.6":
            return {
                **common_mm,
                "context_window": 262_144,
                "max_output_tokens": 32_768,
                "reasoning": {
                    "mode": "toggle", "supported": True, "can_disable": True,
                    "default_enabled": True, "enable_value": "enabled",
                    "disable_value": "disabled",
                    "source": "official-profile",
                },
            }


    if preset in {"xiaomi", "xiaomi-tokenplan"}:
        # MiMo V2.6 Chat Completions uses a binary ``thinking.type`` switch.
        # In Agent tool loops Xiaomi requires assistant ``reasoning_content`` to
        # be replayed in later requests; the dedicated adapter preserves it.
        names = {
            "mimo-v2.6-pro": "MiMo-V2.6-Pro",
            "mimo-v2.6-flash": "MiMo-V2.6-Flash",
            "mimo-v2.6-pro-ultraspeed": "MiMo-V2.6-Pro-UltraSpeed",
        }
        if mid in names:
            return {
                "name": names[mid],
                "context_window": 1_000_000,
                "max_output_tokens": 131_072,
                "input_modalities": ["text", "image", "audio", "video"],
                "output_modalities": ["text"],
                "tool_use": True,
                "reasoning": {
                    "mode": "toggle", "supported": True, "can_disable": True,
                    "default_enabled": True, "enable_value": "enabled",
                    "disable_value": "disabled", "source": "official-profile",
                },
                "capability_source": "official-profile",
            }


    if preset == "zhipu":
        # Z.AI's first-party Chat API is OpenAI-shaped but adds native GLM
        # reasoning/tool semantics. Preserve reasoning_content across tool loops
        # and expose only model-specific thinking controls verified by current
        # GLM model documentation. Cache itself is implicit and automatic.
        common_glm = {
            "output_modalities": ["text"],
            "tool_use": True,
            "capability_source": "official-profile",
        }
        if mid in {"glm-5.3", "glm-5.3-flash", "glm-5.3-flashx"}:
            levels = ["low", "high", "max"]
            return {
                **common_glm,
                "effort_supported_levels": levels,
                "effort_default_level": "max",
                "reasoning": {
                    "mode": "levels", "supported": True, "can_disable": False,
                    "default_enabled": True, "enable_value": "enabled",
                    "effort": {"levels": levels, "default": "max"},
                    "source": "official-profile",
                },
            }
        if mid.startswith("glm-5.2"):
            levels = ["high", "max"]
            return {
                **common_glm,
                "context_window": 1_000_000,
                "effort_supported_levels": levels,
                "effort_default_level": "max",
                "reasoning": {
                    "mode": "levels", "supported": True, "can_disable": True,
                    "default_enabled": True, "enable_value": "enabled",
                    "disable_value": "disabled",
                    "effort": {"levels": levels, "default": "max"},
                    "source": "official-profile",
                },
            }
        if mid in {"glm-5", "glm-5.1", "glm-5-turbo", "glm-5v-turbo", "glm-4.7"}:
            row = {
                **common_glm,
                "reasoning": {
                    "mode": "toggle", "supported": True, "can_disable": True,
                    "default_enabled": True, "enable_value": "enabled",
                    "disable_value": "disabled",
                    "source": "official-profile",
                },
            }
            if mid == "glm-5v-turbo":
                row["input_modalities"] = ["text", "image", "video", "file"]
            return row

    if preset == "minimax":
        # MiniMax officially recommends its Anthropic-compatible Messages API.
        # Model contracts differ materially: M3.1 is always-thinking with five
        # effort levels, M3 can freely toggle thinking by adding/omitting the
        # adaptive block, while M2.x reasoning is always on and cannot be disabled.
        common_m3 = {
            "context_window": 1_000_000,
            "max_output_tokens": 524_288,
            "input_modalities": ["text", "image", "video"],
            "output_modalities": ["text"],
            "tool_use": True,
            "capability_source": "official-profile",
        }
        if mid == "minimax-m3.1-flash-preview":
            levels = ["low", "medium", "high", "xhigh", "max"]
            return {
                **common_m3,
                "effort_supported_levels": levels,
                "effort_default_level": "max",
                "reasoning": {
                    "mode": "levels", "supported": True, "can_disable": False,
                    "default_enabled": True, "enable_value": "adaptive",
                    "effort": {"levels": levels, "default": "max"},
                    "source": "official-profile",
                },
            }
        if mid == "minimax-m3":
            return {
                **common_m3,
                "reasoning": {
                    "mode": "toggle", "supported": True, "can_disable": True,
                    "default_enabled": False, "enable_value": "adaptive",
                    # MiniMax M3 disables thinking by omitting the field. Keep a
                    # private wire sentinel so Crew can expose a real Off switch
                    # without sending the provider-invalid `disabled` value.
                    "disable_value": "__omit__",
                    "source": "official-profile",
                },
            }
        if mid in {
            "minimax-m2", "minimax-m2.1", "minimax-m2.1-highspeed",
            "minimax-m2.5", "minimax-m2.5-highspeed",
            "minimax-m2.7", "minimax-m2.7-highspeed",
        }:
            return {
                "context_window": 204_800,
                "max_output_tokens": 204_800,
                "input_modalities": ["text"],
                "output_modalities": ["text"],
                "tool_use": True,
                "reasoning": {
                    "mode": "fixed", "supported": True, "can_disable": False,
                    "default_enabled": True, "source": "official-profile",
                },
                "capability_source": "official-profile",
            }

    if preset == "mistral":
        # Mistral exposes reasoning through Chat Completions, but thinking is a
        # first-class content chunk. The dedicated adapter preserves those
        # chunks verbatim across turns instead of flattening them to text.
        adjustable = {
            "mistral-small-latest", "mistral-medium-3-5",
            "mistral-large-4-0", "mistral-large-4", "mistral-large-latest",
        }
        if mid in adjustable:
            levels = ["minimal", "low", "medium", "high", "xhigh"]
            row: dict[str, Any] = {
                "effort_supported_levels": levels,
                "reasoning": {"mode":"levels","supported":True,"can_disable":True,
                    "disable_value":"none","enable_value":"high",
                    "effort":{"levels":levels},
                    "source":"official-profile"},
                "tool_use": True,
                "capability_source": "official-profile",
            }
            if mid in {"mistral-large-4-0", "mistral-large-4", "mistral-large-latest"}:
                row.update({"context_window": 1_000_000, "input_modalities": ["text", "image"], "output_modalities": ["text"]})
            return row

    if preset == "qwen":
        # Bailian's Responses API support is model-specific. Keep the provider
        # itself on Chat Completions and override only models documented for
        # Responses, so older/other catalogue entries never get forced through
        # an unsupported wire protocol.
        responses_supported = (
            mid.startswith("qwen3.8-"),
            mid.startswith("qwen3.7-max"),
            mid.startswith("qwen3.7-plus"),
            mid.startswith("qwen3.7-flash"),
            mid.startswith("qwen3.6-plus"),
            mid.startswith("qwen3.6-flash"),
            mid == "qwen3.6-35b-a3b",
            mid.startswith("qwen3.5-plus"),
            mid.startswith("qwen3.5-flash"),
            mid in {"qwen3.5-397b-a17b", "qwen3.5-122b-a10b", "qwen3.5-27b", "qwen3.5-35b-a3b"},
        )
        profile: dict[str, Any] = {}
        if any(responses_supported):
            profile["transport"] = "qwen-responses"
            profile["capability_source"] = "official-profile"
        # Qwen 3.8 explicitly separates the deep-thinking switch (`none`)
        # from three enabled effort levels. `none` must never appear as a
        # fourth effort button in Crew. Omni keeps only the verified transport
        # override because its multimodal/reasoning matrix is model-specific.
        if mid.startswith("qwen3.8-") and "omni" not in mid:
            profile.update({
                "effort_supported_levels": ["low", "medium", "xhigh"],
                "effort_default_level": "xhigh",
                "reasoning": {
                    "mode": "levels", "supported": True, "can_disable": True,
                    "default_enabled": True, "disable_value": "none",
                    "effort": {"levels": ["low", "medium", "xhigh"], "default": "xhigh"},
                    "source": "official-profile",
                },
            })
        if profile:
            return profile

        # Older/open Qwen3 and Qwen3-VL families still use the official Chat
        # Completions protocol.  Their thinking switch is a provider extension
        # (`enable_thinking`), not an effort value. Thinking-only snapshots and
        # QwQ stay fixed-on and must never expose a fake Off button.
        qwen3_tail = mid.removeprefix("qwen3-") if mid.startswith("qwen3-") else ""
        qwen3_open = bool(qwen3_tail and qwen3_tail[0].isdigit()) or mid.startswith("qwen3-vl-")
        if mid.startswith("qwq") or (qwen3_open and "thinking" in mid):
            return {
                "reasoning": {
                    "mode": "fixed", "supported": True, "can_disable": False,
                    "default_enabled": True, "source": "official-profile",
                },
                "capability_source": "official-profile",
            }
        if qwen3_open:
            return {
                "reasoning": {
                    "mode": "toggle", "supported": True, "can_disable": True,
                    "default_enabled": True, "enable_value": "true",
                    "disable_value": "false", "source": "official-profile",
                },
                "capability_source": "official-profile",
            }

    if preset == "hunyuan":
        # Tencent moved current Hunyuan service to TokenHub. Responses support
        # is explicitly model-specific; translation/role models remain on the
        # provider's Chat Completions transport.
        if mid == "hy4-preview":
            return {
                "transport": "hunyuan-responses",
                "context_window": 1_024_000,
                "max_output_tokens": 64_000,
                "effort_supported_levels": ["high"],
                "effort_default_level": "high",
                "reasoning": {
                    "mode": "levels", "supported": True, "can_disable": True,
                    "default_enabled": True, "disable_value": "none",
                    "effort": {"levels": ["high"], "default": "high"},
                    "source": "official-profile",
                },
                "tool_use": True,
                "capability_source": "official-profile",
            }
        if mid in {"hy3", "hy3-preview", "hy3-202608"}:
            return {
                "transport": "hunyuan-responses",
                "context_window": 256_000,
                "max_output_tokens": 128_000,
                "effort_supported_levels": ["low", "high"],
                "effort_default_level": "high",
                "reasoning": {
                    "mode": "levels", "supported": True, "can_disable": True,
                    "default_enabled": True, "disable_value": "none",
                    "effort": {"levels": ["low", "high"], "default": "high"},
                    "source": "official-profile",
                },
                "tool_use": True,
                "capability_source": "official-profile",
            }

    if preset == "gemini":
        # `minimal` is intentionally an effort level, NOT a deep-thinking-off
        # switch: Google explicitly says it may still reason on complex tasks.
        levels: tuple[str, ...] = ()
        default = ""
        if mid.startswith("gemini-3.8-flash") or mid.startswith("gemini-3.7-flash"):
            levels, default = ("low", "medium", "high"), "medium"
        elif mid.startswith("gemini-3.6-flash") or mid.startswith("gemini-3.5-flash"):
            levels, default = ("minimal", "low", "medium", "high"), "medium"
        elif mid.startswith("gemini-3.5-flash-lite"):
            levels, default = ("minimal", "low", "medium", "high"), "minimal"
        elif mid.startswith("gemini-3.1-pro-preview"):
            levels, default = ("low", "medium", "high"), "high"
        elif mid.startswith("gemini-3.1-flash-lite-image"):
            levels, default = ("minimal", "high"), "minimal"
        elif mid.startswith("gemini-3-flash-preview"):
            levels, default = ("minimal", "low", "medium", "high"), "high"
        elif mid.startswith("gemini-3-pro-preview"):
            levels, default = ("low", "high"), "high"
        elif mid.startswith("gemini-2.5-pro") or mid.startswith("gemini-2.5-flash"):
            levels, default = ("low", "medium", "high"), "high"
        if levels:
            return {
                "effort_supported_levels": list(levels),
                "effort_default_level": default,
                "reasoning": {
                    "mode": "levels", "supported": True, "can_disable": False,
                    "default_enabled": True,
                    "effort": {"levels": list(levels), "default": default},
                    "source": "official-profile",
                },
                "tool_use": True,
                "capability_source": "official-profile",
            }

    if preset == "anthropic":
        # Claude's modern thinking controls are model-specific. `disabled` is
        # a deep-thinking switch, while output_config.effort is an independent
        # intensity control. Some generations are always-on and therefore must
        # never expose a synthetic Off button.
        # Keep generation limits separate from the common modality/tool profile:
        # current Claude generations do not all share the 1M / 128K limits.
        # In particular, Claude 4.5 remains a 200K-context family.
        common = {
            "input_modalities": ["text", "image"],
            "output_modalities": ["text"],
            "tool_use": True,
            "capability_source": "official-profile",
        }
        million = {"context_window": 1_000_000, "max_output_tokens": 128_000}
        all5 = ["low", "medium", "high", "xhigh", "max"]
        if "fable-5-1" in mid:
            return {**common, **million, "effort_supported_levels": all5, "effort_default_level": "high",
                "reasoning": {"mode":"adaptive","supported":True,"can_disable":False,
                    "default_enabled":True,"enable_value":"adaptive",
                    "effort":{"levels":all5,"default":"high"},"source":"official-profile"}}
        if "opus-5-5" in mid:
            return {**common, **million, "effort_supported_levels": all5, "effort_default_level": "medium",
                "reasoning": {"mode":"adaptive","supported":True,"can_disable":False,
                    "default_enabled":True,"enable_value":"adaptive",
                    "effort":{"levels":all5,"default":"medium"},"source":"official-profile"}}
        if "sonnet-5-5" in mid:
            # `between_tools` is not a full thinking-off mode, so do not label it Off.
            return {**common, **million, "effort_supported_levels": all5, "effort_default_level": "high",
                "reasoning": {"mode":"adaptive","supported":True,"can_disable":False,
                    "default_enabled":True,"enable_value":"adaptive",
                    "effort":{"levels":all5,"default":"high"},"source":"official-profile"}}
        if "haiku-5-5" in mid:
            return {**common, **million, "effort_supported_levels": all5, "effort_default_level": "medium",
                "reasoning": {"mode":"adaptive","supported":True,"can_disable":True,
                    "default_enabled":True,"enable_value":"adaptive","disable_value":"disabled",
                    "effort":{"levels":all5,"default":"medium"},"source":"official-profile"}}
        if "sonnet-5" in mid and "5-5" not in mid:
            return {**common, **million, "effort_supported_levels": all5, "effort_default_level": "high",
                "reasoning": {"mode":"adaptive","supported":True,"can_disable":True,
                    "default_enabled":True,"enable_value":"adaptive","disable_value":"disabled",
                    "effort":{"levels":all5,"default":"high"},"source":"official-profile"}}
        if "opus-5" in mid and "5-5" not in mid:
            # Off is legal only when no enabled effort is sent. The adapter
            # suppresses output_config.effort whenever thinkingMode=off.
            return {**common, **million, "effort_supported_levels": all5, "effort_default_level": "high",
                "reasoning": {"mode":"adaptive","supported":True,"can_disable":True,
                    "default_enabled":True,"enable_value":"adaptive","disable_value":"disabled",
                    "effort":{"levels":all5,"default":"high"},"source":"official-profile"}}
        if any(token in mid for token in ("opus-4-8", "opus-4-7")):
            return {**common, **million, "effort_supported_levels": all5, "effort_default_level": "high",
                "reasoning": {"mode":"adaptive","supported":True,"can_disable":True,
                    "default_enabled":False,"enable_value":"adaptive","disable_value":"disabled",
                    "effort":{"levels":all5,"default":"high"},"source":"official-profile"}}
        if any(token in mid for token in ("opus-4-6", "sonnet-4-6")):
            levels = ["low", "medium", "high", "max"]
            return {**common, **million, "effort_supported_levels": levels, "effort_default_level": "high",
                "reasoning": {"mode":"adaptive","supported":True,"can_disable":True,
                    "default_enabled":False,"enable_value":"adaptive","disable_value":"disabled",
                    "effort":{"levels":levels,"default":"high"},"source":"official-profile"}}
        if "opus-4-5" in mid:
            return {**common, "context_window": 200_000, "max_output_tokens": 64_000,
                "reasoning": {"mode":"toggle","supported":True,"can_disable":True,
                    "default_enabled":False,"enable_value":"enabled","disable_value":"disabled",
                    "source":"official-profile"}}
        if "haiku-4-5" in mid:
            return {**common, "context_window": 200_000, "max_output_tokens": 64_000,
                "reasoning": {"mode":"toggle","supported":True,"can_disable":True,
                    "default_enabled":False,"enable_value":"enabled","disable_value":"disabled",
                    "source":"official-profile"}}
        if "sonnet-4-5" in mid:
            # Anthropic documents a 200K context window. Keep max output unset
            # here rather than inventing a value that is not verified by our
            # current official source set.
            return {**common, "context_window": 200_000,
                "reasoning": {"mode":"toggle","supported":True,"can_disable":True,
                    "default_enabled":False,"enable_value":"enabled","disable_value":"disabled",
                    "source":"official-profile"}}
        if mid.startswith("claude-"):
            return common

    if preset == "cohere":
        if mid == "command-a-plus-05-2026":
            return {
                "context_window": 128_000, "max_output_tokens": 64_000,
                "input_modalities": ["text", "image"], "output_modalities": ["text"],
                "reasoning": {"mode":"toggle","supported":True,"can_disable":True,"default_enabled":True,"enable_value":"enabled","disable_value":"disabled","source":"official-profile"},
                "tool_use": True, "capability_source": "official-profile",
            }
        if mid == "command-a-reasoning-08-2025":
            return {
                "context_window": 256_000, "max_output_tokens": 32_000,
                "input_modalities": ["text"], "output_modalities": ["text"],
                "reasoning": {"mode":"toggle","supported":True,"can_disable":True,"default_enabled":True,"enable_value":"enabled","disable_value":"disabled","source":"official-profile"},
                "tool_use": True, "capability_source": "official-profile",
            }

    return {}


def merge_official_model_profile(preset_id: str, row: dict[str, Any]) -> dict[str, Any]:
    """Overlay verified official facts on a discovered/manual model row."""
    result = dict(row)
    profile = official_model_profile(preset_id, str(row.get("id") or row.get("name") or ""))
    if not profile:
        return result
    for key, value in profile.items():
        if key == "reasoning" and isinstance(value, dict):
            existing = result.get("reasoning") if isinstance(result.get("reasoning"), dict) else {}
            merged = dict(existing)
            # Profile values are verified and intentionally win over sparse or
            # ambiguous provider catalogue fields.
            merged.update(value)
            result["reasoning"] = merged
        else:
            result[key] = value
    return result



def provider_presets() -> tuple[ProviderPreset, ...]:
    return _PROVIDER_PRESETS


def provider_preset(preset_id: str) -> ProviderPreset | None:
    wanted = str(preset_id or "").strip().lower()
    return next((preset for preset in _PROVIDER_PRESETS if preset.id == wanted), None)


def public_presets() -> list[dict[str, Any]]:
    # ``xiaomi-tokenplan`` remains an internal compatibility alias so existing
    # 0.1.0 configs keep loading, but the Provider picker exposes Xiaomi only
    # once. New Token Plan credentials are handled by the unified ``xiaomi``
    # preset and routed by key/base URL.
    return [
        preset.public_dict()
        for preset in _PROVIDER_PRESETS
        if preset.id != "xiaomi-tokenplan"
    ]


def _string_levels(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        if isinstance(item, str):
            level = item.strip()
            if level and level not in seen:
                seen.add(level)
                out.append(level)
    return tuple(out)


def normalize_reasoning_capability(
    *,
    effort_levels: Iterable[str] = (),
    effort_default: str = "",
    raw_reasoning: Any = None,
    source: str = "provider",
) -> dict[str, Any]:
    """Normalize model reasoning without conflating *Thinking* and *Effort*.

    Thinking answers whether deep reasoning exists and whether it can be
    enabled/disabled. Effort is the intensity *while thinking is enabled* and
    may expose a model-specific number of levels. Some provider catalogues put
    an ``off`` sentinel in the same wire list as effort values; that sentinel is
    retained as transport metadata but is never counted as an effort level.

    Unknown stays unknown. Kiro BYO only advertises controls that can be mapped
    to provider-declared wire values; it never fabricates a universal on/off or
    a fixed number of effort levels from the provider brand.
    """

    disable_names = {"none", "off", "disabled"}
    enable_names = {"on", "enabled", "thinking"}

    legacy_levels = _string_levels(list(effort_levels))
    legacy_default = str(effort_default or "").strip()
    raw = raw_reasoning if isinstance(raw_reasoning, dict) else {}
    raw_mode = str(raw.get("mode") or "").strip().lower()
    if raw_mode not in REASONING_MODES:
        raw_mode = ""

    raw_effort = raw.get("effort") if isinstance(raw.get("effort"), dict) else {}
    raw_thinking = raw.get("thinking") if isinstance(raw.get("thinking"), dict) else {}

    # New schema wins. Old ``reasoning.levels`` and top-level
    # effort_supported_levels remain accepted for configs written by older builds.
    explicit_effort_levels = _string_levels(raw_effort.get("levels"))
    legacy_reasoning_levels = _string_levels(raw.get("levels"))
    wire_levels = _string_levels(raw_effort.get("wire_levels"))
    if not wire_levels:
        wire_levels = legacy_reasoning_levels or legacy_levels

    default_wire = ""
    for candidate in (raw_effort.get("default"), raw.get("default"), legacy_default):
        if isinstance(candidate, str) and candidate.strip():
            default_wire = candidate.strip()
            break

    def _raw_bool(*values: Any) -> bool | None:
        for value in values:
            if isinstance(value, bool):
                return value
        return None

    def _raw_str(*values: Any) -> str:
        for value in values:
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    disable_value = _raw_str(raw_thinking.get("disable_value"), raw.get("disable_value"))
    if not disable_value:
        disable_value = next((v for v in wire_levels if v.lower() in disable_names), "")

    enable_value = _raw_str(raw_thinking.get("enable_value"), raw.get("enable_value"))
    auto_value = _raw_str(raw_thinking.get("auto_value"), raw.get("auto_value"))

    # If a catalogue explicitly identifies effort levels, trust it. Otherwise
    # split transport sentinels away from the intensity vocabulary. A pure
    # [none, thinking] pair is a toggle, not a one-level effort scale.
    if explicit_effort_levels:
        intensity_levels = explicit_effort_levels
    else:
        candidates = tuple(v for v in wire_levels if v.lower() not in disable_names)
        if raw_mode == "toggle" and len(candidates) == 1 and candidates[0].lower() in enable_names:
            enable_value = enable_value or candidates[0]
            intensity_levels = ()
        elif len(candidates) == 1 and candidates[0].lower() in enable_names and disable_value:
            enable_value = enable_value or candidates[0]
            intensity_levels = ()
        else:
            intensity_levels = candidates

    # De-duplicate again after splitting, preserving provider order exactly.
    intensity_levels = _string_levels(list(intensity_levels))

    effort_default_value = _raw_str(raw_effort.get("default"))
    if not effort_default_value and default_wire in intensity_levels:
        effort_default_value = default_wire
    if effort_default_value not in intensity_levels:
        effort_default_value = ""

    fixed_level = _raw_str(raw_effort.get("fixed_level"), raw.get("fixed_level"))
    if not fixed_level and len(intensity_levels) == 1:
        fixed_level = intensity_levels[0]
    if fixed_level and fixed_level not in intensity_levels:
        fixed_level = ""

    supported = _raw_bool(raw_thinking.get("supported"), raw.get("supported"))
    if supported is None:
        if raw_mode == "none":
            supported = False
        elif raw_mode in {"fixed", "levels", "toggle", "adaptive"}:
            supported = True
        elif wire_levels or intensity_levels or disable_value or enable_value:
            supported = True

    can_disable = _raw_bool(raw_thinking.get("can_disable"), raw.get("can_disable"))
    if supported is False:
        can_disable = False
    elif raw_mode == "fixed":
        # Fixed thinking is a provider statement: noisy can_disable metadata may
        # not turn it into a user-facing switch.
        can_disable = False
    elif can_disable is None and disable_value:
        can_disable = True

    default_enabled = _raw_bool(raw_thinking.get("default_enabled"), raw.get("default_enabled"))
    if default_enabled is None:
        if default_wire and disable_value and default_wire == disable_value:
            default_enabled = False
        elif default_wire and (default_wire in intensity_levels or default_wire == enable_value):
            default_enabled = True
        elif supported is False:
            default_enabled = False
        elif raw_mode == "fixed":
            default_enabled = True

    supports_auto = _raw_bool(raw_thinking.get("supports_auto"), raw.get("supports_auto"))
    if supports_auto is None and auto_value:
        supports_auto = True

    # Backward-compatible coarse mode. UI and new callers should use the nested
    # ``thinking`` and ``effort`` objects below instead of deriving one from this.
    if supported is False:
        mode = "none"
    elif raw_mode == "adaptive":
        mode = "adaptive"
    elif can_disable is True and not intensity_levels:
        mode = "toggle"
    elif len(intensity_levels) > 1:
        mode = "levels"
    elif supported is True:
        mode = "fixed"
    else:
        mode = "unknown"

    selectable = list(intensity_levels) if len(intensity_levels) > 1 else []
    effort = {
        "levels": list(intensity_levels),
        "selectable_levels": selectable,
        "default": effort_default_value,
        "fixed_level": fixed_level,
        "configurable": len(selectable) > 1,
        "level_count": len(intensity_levels),
    }
    thinking = {
        "supported": supported,
        "can_disable": can_disable,
        "default_enabled": default_enabled,
        "supports_auto": supports_auto,
        "disable_value": disable_value,
        "enable_value": enable_value,
        "auto_value": auto_value,
        "control": (
            "unsupported" if supported is False else
            "switchable" if can_disable is True else
            "fixed_on" if supported is True and can_disable is False else
            "adaptive" if raw_mode == "adaptive" else
            "unknown"
        ),
    }
    return {
        "mode": mode,
        # Legacy flat fields now refer only to effort intensity; ``off`` is not
        # an effort level. Keep them until old configs/callers have migrated.
        "levels": list(intensity_levels),
        "wire_levels": list(wire_levels),
        "selectable_levels": selectable,
        "default": effort_default_value,
        "fixed_level": fixed_level,
        "can_disable": can_disable,
        "supported": supported,
        "default_enabled": default_enabled,
        "disable_value": disable_value,
        "enable_value": enable_value,
        "auto_value": auto_value,
        "thinking": thinking,
        "effort": effort,
        "source": _raw_str(raw.get("source")) or source or "unknown",
    }


def normalize_tool_use_capability(row: dict[str, Any]) -> bool | None:
    """Normalize provider-supplied per-model tool-use metadata without brand heuristics."""
    for key in ("tool_use", "supports_tools", "supports_tool_use", "supports_function_calling"):
        value = row.get(key)
        if isinstance(value, bool):
            return value
    caps = row.get("capabilities")
    if isinstance(caps, dict):
        for key in ("tool_use", "tools", "function_calling"):
            value = caps.get(key)
            if isinstance(value, bool):
                return value
            # Claude and some other model catalogues expose capabilities as
            # {"tool_use": {"supported": true}} rather than a flat boolean.
            if isinstance(value, dict) and isinstance(value.get("supported"), bool):
                return bool(value["supported"])
    elif isinstance(caps, list):
        names = {str(value).strip().lower() for value in caps if isinstance(value, str)}
        if names & {"tool_use", "tools", "function_calling", "function-calling"}:
            return True
    params = row.get("supported_parameters")
    if isinstance(params, list):
        names = {str(value).strip().lower() for value in params if isinstance(value, str)}
        if names & {"tools", "tool_choice", "parallel_tool_calls", "functions", "function_call"}:
            return True
    return None


def model_capability_from_row(row: dict[str, Any]) -> dict[str, Any]:
    """Build the public capability object persisted/returned by admin APIs."""

    effort_levels = _string_levels(row.get("effort_supported_levels"))
    effort_default = str(row.get("effort_default_level") or "")
    reasoning = normalize_reasoning_capability(
        effort_levels=effort_levels,
        effort_default=effort_default,
        raw_reasoning=row.get("reasoning"),
        source=str(row.get("capability_source") or "provider"),
    )
    return {
        "reasoning": reasoning,
        "input_modalities": list(_string_levels(row.get("input_modalities"))),
        "output_modalities": list(_string_levels(row.get("output_modalities"))),
        "context_window": row.get("context_window") if isinstance(row.get("context_window"), int) else None,
        "max_output_tokens": row.get("max_output_tokens") if isinstance(row.get("max_output_tokens"), int) else None,
        "tool_use": normalize_tool_use_capability(row),
    }
