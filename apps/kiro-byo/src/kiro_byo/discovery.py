"""Model discovery for OpenAI-compatible providers.

Most provider presets expose an OpenAI-shaped ``/models`` endpoint. A small
number use the same chat protocol but publish model metadata from a different
URL or with a slightly different JSON envelope. Discovery normalizes those
catalogues without changing the inference transport.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlparse

from .provider_catalog import normalize_reasoning_capability, normalize_tool_use_capability


class DiscoveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class DiscoveredModel:
    id: str
    name: str
    context_window: int | None = None
    max_output_tokens: int | None = None
    input_modalities: tuple[str, ...] = ()
    output_modalities: tuple[str, ...] = ()
    effort_supported_levels: tuple[str, ...] = ()
    effort_default_level: str = ""
    reasoning_mode: str = "unknown"
    reasoning_supported: bool | None = None
    reasoning_can_disable: bool | None = None
    reasoning_default_enabled: bool | None = None
    reasoning_disable_value: str = ""
    reasoning_enable_value: str = ""
    reasoning_auto_value: str = ""
    reasoning_supports_auto: bool | None = None
    reasoning_fixed_level: str = ""
    capability_source: str = "provider"
    tool_use: bool | None = None
    transport: str = ""


_KNOWN_HOSTS: tuple[tuple[str, str, str], ...] = (
    ("api.deepseek.com", "deepseek", "DeepSeek"),
    ("api.moonshot.cn", "moonshot", "Moonshot / Kimi"),
    ("api.xiaomimimo.com", "xiaomi", "小米 MiMo / Xiaomi"),
    ("token-plan-cn.xiaomimimo.com", "xiaomi", "小米 MiMo Token Plan"),
    ("token-plan-sgp.xiaomimimo.com", "xiaomi", "小米 MiMo Token Plan"),
    ("token-plan-ams.xiaomimimo.com", "xiaomi", "小米 MiMo Token Plan"),
    ("dashscope.aliyuncs.com", "qwen", "阿里云百炼 / Qwen"),
    ("open.bigmodel.cn", "zhipu", "智谱 AI / GLM"),
    ("ark.cn-beijing.volces.com", "volcengine", "火山引擎 / Ark"),
    ("api.hunyuan.cloud.tencent.com", "hunyuan", "腾讯混元"),
    ("tokenhub.tencentmaas.com", "hunyuan", "腾讯混元 / TokenHub"),
    ("tokenhub-intl.tencentmaas.com", "hunyuan", "腾讯混元 / TokenHub"),
    ("qianfan.baidubce.com", "qianfan", "百度千帆 / ERNIE"),
    ("api.minimax.io", "minimax", "MiniMax"),
    ("api.minimax.cn", "minimax", "MiniMax"),
    ("api.minimaxi.com", "minimax", "MiniMax"),
    ("api.stepfun.com", "stepfun", "阶跃星辰 / StepFun"),
    ("api.openai.com", "openai", "OpenAI"),
    ("api.anthropic.com", "anthropic", "Anthropic / Claude"),
    ("generativelanguage.googleapis.com", "gemini", "Google Gemini"),
    ("api.x.ai", "grok", "xAI / Grok"),
    ("api.mistral.ai", "mistral", "Mistral AI"),
    ("api.cohere.ai", "cohere", "Cohere"),
    ("api.cohere.com", "cohere", "Cohere"),
    ("api.groq.com", "groq", "Groq"),
)


def infer_provider_identity(base_url: str) -> tuple[str, str]:
    host = (urlparse(base_url).hostname or "provider").lower()
    for domain, provider_id, display_name in _KNOWN_HOSTS:
        if host == domain or host.endswith("." + domain):
            return provider_id, display_name
    clean_host = host[4:] if host.startswith("api.") else host
    provider_id = re.sub(r"[^a-z0-9]+", "-", clean_host).strip("-") or "provider"
    return provider_id, clean_host


def _positive_int(*values: Any) -> int | None:
    for value in values:
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and value > 0:
            return value
        if isinstance(value, float) and value > 0 and value.is_integer():
            return int(value)
    return None


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item) for item in value if isinstance(item, str) and item)


def _modality_tuple(value: Any) -> tuple[str, ...]:
    """Normalize common provider modality vocabulary without inventing support."""
    aliases = {
        "images": "image", "vision": "image",
        "texts": "text",
        "audios": "audio", "speech": "audio",
        "videos": "video",
        "files": "file", "document": "file", "documents": "file",
    }
    out: list[str] = []
    seen: set[str] = set()
    for item in _string_tuple(value):
        key = item.strip().lower()
        if not key:
            continue
        key = aliases.get(key, key)
        if key not in seen:
            seen.add(key)
            out.append(key)
    return tuple(out)


def _catalog_rows(data: Any, endpoint: str) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        raise DiscoveryError(f"GET {endpoint} did not return a JSON object")
    rows = data.get("data")
    if not isinstance(rows, list):
        # Cohere's native model catalogue uses {"models": [...]} while its
        # inference endpoint is OpenAI-compatible.
        rows = data.get("models")
    if not isinstance(rows, list):
        raise DiscoveryError(f"GET {endpoint} did not return a supported models list")
    return [row for row in rows if isinstance(row, dict)]


def _is_chat_candidate(row: dict[str, Any]) -> bool:
    methods = row.get("supportedGenerationMethods")
    if isinstance(methods, list) and methods:
        supported_methods = {str(value).strip() for value in methods if isinstance(value, str)}
        if supported_methods and not (supported_methods & {"generateContent", "streamGenerateContent", "interactions"}):
            return False

    endpoints = row.get("endpoints")
    if isinstance(endpoints, list) and endpoints:
        supported = {str(value).strip().lower() for value in endpoints if isinstance(value, str)}
        if supported and not (supported & {"chat", "chat-completions", "chat_completions"}):
            return False

    kind = str(row.get("type") or "").strip().lower()
    known_non_chat = {
        "embedding", "embeddings", "rerank", "reranking", "text2image", "image2image",
        "text2video", "image2video", "audio", "speech", "tts", "asr", "transcription",
    }
    return kind not in known_non_chat


def discover_models(
    base_url: str,
    api_key: str,
    *,
    timeout_seconds: float = 30.0,
    endpoint: str = "",
    auth_style: str = "bearer",
    extra_headers: Mapping[str, str] | None = None,
) -> tuple[str, str, list[DiscoveredModel]]:
    endpoint = endpoint.strip() or (base_url.rstrip("/") + "/models")
    headers: dict[str, str] = {"Accept": "application/json"}
    auth_style = str(auth_style or "bearer").strip().lower()
    if api_key:
        if auth_style == "bearer":
            headers["Authorization"] = f"Bearer {api_key}"
        elif auth_style == "x-api-key":
            headers["X-Api-Key"] = api_key
        elif auth_style == "x-goog-api-key":
            headers["X-Goog-Api-Key"] = api_key
        elif auth_style not in {"none", ""}:
            raise DiscoveryError(f"unsupported model-discovery auth style {auth_style!r}")
    if extra_headers:
        headers.update({str(key): str(value) for key, value in extra_headers.items()})

    request = urllib.request.Request(endpoint, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            # base_url is user-entered, so cap the read. A hostile/misconfigured
            # endpoint returning a multi-GB /models body would otherwise exhaust
            # memory (unlike the streamed chat path, discovery buffers in full).
            _max = 16 * 1024 * 1024
            payload = response.read(_max + 1)
            if len(payload) > _max:
                raise DiscoveryError(f"GET {endpoint} returned an oversized model catalogue")
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            detail = ""
        suffix = f": {detail}" if detail else ""
        raise DiscoveryError(f"GET {endpoint} failed with HTTP {exc.code}{suffix}") from exc
    except urllib.error.URLError as exc:
        raise DiscoveryError(f"GET {endpoint} failed: {exc.reason}") from exc
    except TimeoutError as exc:
        raise DiscoveryError(f"GET {endpoint} timed out") from exc

    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DiscoveryError(f"GET {endpoint} returned invalid JSON") from exc

    rows = _catalog_rows(data, endpoint)
    models: list[DiscoveredModel] = []
    seen: set[str] = set()
    for row in rows:
        if not _is_chat_candidate(row):
            continue

        raw_id = row.get("id")
        if not isinstance(raw_id, str) or not raw_id.strip():
            # Cohere's model catalogue uses `name` as its stable model id.
            raw_id = row.get("name")
        if not isinstance(raw_id, str) or not raw_id.strip():
            continue
        model_id = raw_id.strip()
        # Gemini's catalogue identifies models as ``models/gemini-*`` while
        # Interactions expects the bare model id.
        if model_id.startswith("models/"):
            model_id = model_id.split("/", 1)[1]
        if model_id in seen:
            continue
        seen.add(model_id)

        raw_name = row.get("display_name")
        if not isinstance(raw_name, str) or not raw_name.strip():
            raw_name = row.get("displayName")
        if not isinstance(raw_name, str) or not raw_name.strip():
            raw_name = row.get("name")
        name = raw_name.strip() if isinstance(raw_name, str) and raw_name.strip() else model_id
        if name.startswith("models/"):
            name = model_id

        architecture = row.get("architecture") if isinstance(row.get("architecture"), dict) else {}
        input_modalities = _modality_tuple(row.get("input_modalities") or architecture.get("input_modalities"))
        output_modalities = _modality_tuple(row.get("output_modalities") or architecture.get("output_modalities"))

        effort = row.get("effort") if isinstance(row.get("effort"), dict) else {}
        capabilities = row.get("capabilities") if isinstance(row.get("capabilities"), dict) else {}
        effort_levels = _string_tuple(effort.get("supported_levels"))
        if not effort_levels:
            # xAI and a growing number of richer catalogues publish the exact
            # reasoning vocabulary under capabilities.reasoning_effort. Preserve
            # provider order and let normalization split `none` from intensities.
            effort_levels = _string_tuple(capabilities.get("reasoning_effort"))
        effort_default = effort.get("default_level")
        if not isinstance(effort_default, str) or not effort_default:
            effort_default = capabilities.get("default_reasoning_effort")
        if not isinstance(effort_default, str):
            effort_default = ""
        raw_reasoning = row.get("reasoning") if isinstance(row.get("reasoning"), dict) else {}
        merged_reasoning = dict(raw_reasoning)
        for key, target in (("mode", "mode"), ("can_disable", "can_disable"), ("fixed_level", "fixed_level")):
            if target not in merged_reasoning and key in effort:
                merged_reasoning[target] = effort[key]
        reasoning = normalize_reasoning_capability(
            effort_levels=effort_levels,
            effort_default=effort_default,
            raw_reasoning=merged_reasoning,
            source="provider",
        )

        models.append(
            DiscoveredModel(
                model_id,
                name,
                context_window=_positive_int(
                    row.get("context_window"), row.get("context_length"), row.get("max_context_length"),
                    row.get("inputTokenLimit"),
                ),
                max_output_tokens=_positive_int(
                    row.get("max_output_tokens"),
                    row.get("max_completion_tokens"),
                    row.get("max_completions_tokens"),
                    row.get("outputTokenLimit"),
                ),
                input_modalities=input_modalities,
                output_modalities=output_modalities,
                effort_supported_levels=tuple(reasoning["effort"]["levels"]),
                effort_default_level=str(reasoning["effort"]["default"] or ""),
                reasoning_mode=str(reasoning["mode"]),
                reasoning_supported=reasoning["thinking"]["supported"] if isinstance(reasoning["thinking"]["supported"], bool) else None,
                reasoning_can_disable=reasoning["thinking"]["can_disable"] if isinstance(reasoning["thinking"]["can_disable"], bool) else None,
                reasoning_default_enabled=reasoning["thinking"]["default_enabled"] if isinstance(reasoning["thinking"]["default_enabled"], bool) else None,
                reasoning_disable_value=str(reasoning["thinking"]["disable_value"] or ""),
                reasoning_enable_value=str(reasoning["thinking"]["enable_value"] or ""),
                reasoning_auto_value=str(reasoning["thinking"]["auto_value"] or ""),
                reasoning_supports_auto=reasoning["thinking"]["supports_auto"] if isinstance(reasoning["thinking"]["supports_auto"], bool) else None,
                reasoning_fixed_level=str(reasoning["effort"]["fixed_level"] or ""),
                capability_source=str(reasoning["source"] or "provider"),
                tool_use=normalize_tool_use_capability(row),
            )
        )

    if not models:
        raise DiscoveryError(f"GET {endpoint} returned zero usable chat models")

    provider_id, provider_name = infer_provider_identity(base_url)
    return provider_id, provider_name, models
