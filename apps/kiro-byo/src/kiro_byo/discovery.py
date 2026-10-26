"""Model discovery and model-catalog normalization.

Kiro BYO treats a model's *catalog identity* separately from the identifier sent
on the inference wire.  Most APIs use the same string for both, but managed
platforms may require an endpoint/deployment/inference-profile id instead.

Discovery is intentionally capability-conservative: provider metadata may prove
support, while missing metadata stays unknown.  The parser understands the
common OpenAI/Gemini/Cohere shapes plus richer Model Studio style catalogues
(``output.models`` with modality/capability/model_info metadata).
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunsplit

from .provider_catalog import model_routing_from_row, normalize_reasoning_capability, normalize_tool_use_capability


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
    # Stable catalogue identity and actual inference target are separate.  For
    # direct APIs these are identical. Managed platforms may use deployment,
    # endpoint or inference-profile identifiers on the wire.
    invocation_target: str = ""
    invocation_target_kind: str = "model"
    lifecycle: str = "active"
    catalog_source: str = "provider-discovery"
    # Optional management-plane routing metadata. These are optimization hints,
    # not protocol capabilities; missing values stay unknown and may be filled
    # by Kiro BYO curated metadata or a local operator override.
    routing_quality: float | None = None
    routing_speed: float | None = None
    routing_economy: float | None = None
    routing_tiers: tuple[str, ...] = ()
    routing_source: str = "unknown"
    routing_confidence: str = "unknown"
    routing_provenance: tuple[tuple[str, str, str], ...] = ()

    @property
    def wire_model_id(self) -> str:
        return self.invocation_target or self.id


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
        # Cohere's native model catalogue uses {"models": [...]}.
        rows = data.get("models")
    if not isinstance(rows, list):
        # Alibaba Model Studio and similar management-plane catalogues use
        # {"output": {"models": [...]}}.
        output = data.get("output")
        rows = output.get("models") if isinstance(output, dict) else None
    if not isinstance(rows, list):
        raise DiscoveryError(f"GET {endpoint} did not return a supported models list")
    return [row for row in rows if isinstance(row, dict)]


def _capability_names(row: dict[str, Any]) -> set[str]:
    caps = row.get("capabilities")
    if not isinstance(caps, list):
        return set()
    return {str(value).strip().lower() for value in caps if isinstance(value, str) and value.strip()}


_NON_AGENT_MODEL_ID_RE = re.compile(
    r"(?:^|[-_/])(embedding|embed|rerank|tts|asr|transcrib(?:e|er|ing)|image|text-to-image|video|text-to-video)(?:$|[-_/])"
)


def is_agent_model_id(model_id: str) -> bool:
    """Conservative last-resort filter for text-agent model pickers.

    This is intentionally a deny heuristic rather than a generation allowlist:
    an unknown future chat/reasoning model remains usable, while obvious
    embedding/TTS/ASR/image/video products stay out of the agent picker even
    when a provider's discovery response supplies only an id/name.
    """
    return not bool(_NON_AGENT_MODEL_ID_RE.search(str(model_id or "").strip().lower()))


def filter_agent_models(models: list[DiscoveredModel]) -> list[DiscoveredModel]:
    """Apply the id-only safety filter after a discovery boundary.

    Real discovery performs richer row-level filtering first. Keeping this
    second pass at the boundary also protects injected/test/custom discovery
    implementations that return normalized DiscoveredModel rows directly.
    """
    return [model for model in models if is_agent_model_id(model.id)]


def _is_chat_candidate(row: dict[str, Any]) -> bool:
    methods = row.get("supportedGenerationMethods")
    if isinstance(methods, list) and methods:
        supported_methods = {str(value).strip() for value in methods if isinstance(value, str)}
        if supported_methods and not (supported_methods & {"generateContent", "streamGenerateContent", "interactions"}):
            return False

    endpoints = row.get("endpoints")
    if isinstance(endpoints, list) and endpoints:
        supported = {str(value).strip().lower() for value in endpoints if isinstance(value, str)}
        if supported and not (supported & {"chat", "chat-completions", "chat_completions", "responses"}):
            return False

    kind = str(row.get("type") or "").strip().lower()
    known_non_chat = {
        "embedding", "embeddings", "rerank", "reranking", "text2image", "image2image",
        "text2video", "image2video", "audio", "speech", "tts", "asr", "transcription",
    }
    if kind in known_non_chat:
        return False

    # Rich catalogues can state model categories directly. Text generation,
    # reasoning and understanding models are viable; pure generation/vector/
    # speech rows are not Kiro agent models.
    cap_names = _capability_names(row)
    if cap_names:
        agent_caps = {"tg", "reasoning", "vu", "multimodal-omni", "realtime-omni"}
        non_agent_caps = {
            "ig", "vg", "asr", "tts", "me", "tr", "3d-generation",
            "realtime-text-to-speech", "realtime-asr", "realtime-audio-translate",
        }
        if cap_names & agent_caps:
            return True
        if cap_names <= non_agent_caps:
            return False

    inference_metadata = row.get("inference_metadata") if isinstance(row.get("inference_metadata"), dict) else {}
    architecture = row.get("architecture") if isinstance(row.get("architecture"), dict) else {}
    response_modality = _modality_tuple(row.get("output_modalities") or architecture.get("output_modalities"))
    if not response_modality:
        response_modality = _modality_tuple(inference_metadata.get("response_modality"))
    if response_modality and "text" not in response_modality:
        return False

    # Last-resort exclusion for catalogues that expose only an id/name. This is
    # deliberately a *deny* heuristic, not an allowlist, so a newly launched
    # chat model remains discoverable without a Kiro BYO update.
    raw_id = row.get("id") or row.get("model") or row.get("name") or ""
    model_id = str(raw_id).strip().lower()
    return is_agent_model_id(model_id)


def _wire_target(row: dict[str, Any], model_id: str) -> tuple[str, str]:
    for key, kind in (
        ("invocation_target", "target"),
        ("endpoint_id", "endpoint"),
        ("endpointId", "endpoint"),
        ("deployment_name", "deployment"),
        ("deploymentName", "deployment"),
        ("inference_profile_id", "inference-profile"),
        ("inferenceProfileId", "inference-profile"),
    ):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip(), kind
    return model_id, "model"


def _lifecycle(row: dict[str, Any]) -> str:
    status = str(row.get("lifecycle") or row.get("status") or "").strip().lower()
    if status in {"active", "stable", "preview", "experimental", "deprecated", "shutdown", "retired", "retiring"}:
        return "shutdown" if status == "retired" else status
    if row.get("deprecated") is True:
        return "deprecated"
    offline = row.get("inference_offline_info")
    if isinstance(offline, dict) and offline.get("offline_time"):
        return "retiring"
    return "active"


def _fetch_json(endpoint: str, headers: Mapping[str, str], timeout_seconds: float) -> Any:
    request = urllib.request.Request(endpoint, headers=dict(headers), method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
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
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DiscoveryError(f"GET {endpoint} returned invalid JSON") from exc


def _with_page(endpoint: str, page_no: int, page_size: int) -> str:
    parts = urlsplit(endpoint)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["page_no"] = str(page_no)
    query.setdefault("page_size", str(page_size))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _catalog_documents(endpoint: str, headers: Mapping[str, str], timeout_seconds: float) -> list[Any]:
    """Fetch one catalogue, following Model Studio style page metadata safely."""
    first = _fetch_json(endpoint, headers, timeout_seconds)
    documents = [first]
    output = first.get("output") if isinstance(first, dict) else None
    if not isinstance(output, dict) or not isinstance(output.get("models"), list):
        return documents
    total = _positive_int(output.get("total")) or len(output["models"])
    page_no = _positive_int(output.get("page_no")) or 1
    page_size = _positive_int(output.get("page_size")) or max(1, len(output["models"]))
    received = len(output["models"])
    # Bound management-plane pagination so a malformed service cannot create an
    # unbounded setup request. 100 pages * 100 rows is already far beyond an
    # agent picker's practical catalogue size.
    while received < total and len(documents) < 100:
        page_no += 1
        page_endpoint = _with_page(endpoint, page_no, page_size)
        doc = _fetch_json(page_endpoint, headers, timeout_seconds)
        page_output = doc.get("output") if isinstance(doc, dict) else None
        page_rows = page_output.get("models") if isinstance(page_output, dict) else None
        if not isinstance(page_rows, list) or not page_rows:
            break
        documents.append(doc)
        received += len(page_rows)
    return documents


def _routing_score(value: Any) -> float | None:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    if 0.0 <= score <= 1.0:
        return score
    return None


def _routing_metadata(row: Mapping[str, Any]) -> tuple[float | None, float | None, float | None, tuple[str, ...]]:
    """Read optional provider-supplied routing hints without inventing them.

    There is no universal provider schema for quality/speed/cost classes. We
    therefore accept only explicit fields. Curated/local metadata can fill the
    same normalized shape later.
    """
    routing = row.get("routing") if isinstance(row.get("routing"), Mapping) else {}
    quality = _routing_score(routing.get("quality", row.get("routing_quality")))
    speed = _routing_score(routing.get("speed", row.get("routing_speed")))
    economy = _routing_score(routing.get("economy", row.get("routing_economy")))
    raw_tiers = routing.get("tiers") if isinstance(routing.get("tiers"), (list, tuple)) else row.get("routing_tiers")
    tiers: list[str] = []
    singular = routing.get("tier", row.get("tier"))
    for raw in ([singular] if singular is not None else []) + (list(raw_tiers) if isinstance(raw_tiers, (list, tuple)) else []):
        tier = str(raw or "").strip().lower()
        if tier in {"fast", "balanced", "strong"} and tier not in tiers:
            tiers.append(tier)
    return quality, speed, economy, tuple(tiers)


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

    documents = _catalog_documents(endpoint, headers, timeout_seconds)
    rows: list[dict[str, Any]] = []
    for document in documents:
        rows.extend(_catalog_rows(document, endpoint))

    models: list[DiscoveredModel] = []
    seen: set[str] = set()
    for row in rows:
        if not _is_chat_candidate(row):
            continue

        raw_id = row.get("id")
        if not isinstance(raw_id, str) or not raw_id.strip():
            raw_id = row.get("model")
        if not isinstance(raw_id, str) or not raw_id.strip():
            # Cohere's model catalogue uses `name` as its stable model id.
            raw_id = row.get("name")
        if not isinstance(raw_id, str) or not raw_id.strip():
            continue
        model_id = raw_id.strip()
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
        inference_metadata = row.get("inference_metadata") if isinstance(row.get("inference_metadata"), dict) else {}
        input_modalities = _modality_tuple(
            row.get("input_modalities") or architecture.get("input_modalities") or inference_metadata.get("request_modality")
        )
        output_modalities = _modality_tuple(
            row.get("output_modalities") or architecture.get("output_modalities") or inference_metadata.get("response_modality")
        )

        effort = row.get("effort") if isinstance(row.get("effort"), dict) else {}
        capabilities_obj = row.get("capabilities") if isinstance(row.get("capabilities"), dict) else {}
        effort_levels = _string_tuple(effort.get("supported_levels"))
        if not effort_levels:
            effort_levels = _string_tuple(capabilities_obj.get("reasoning_effort"))
        effort_default = effort.get("default_level")
        if not isinstance(effort_default, str) or not effort_default:
            effort_default = capabilities_obj.get("default_reasoning_effort")
        if not isinstance(effort_default, str):
            effort_default = ""
        raw_reasoning = row.get("reasoning") if isinstance(row.get("reasoning"), dict) else {}
        merged_reasoning = dict(raw_reasoning)
        for key, target in (("mode", "mode"), ("can_disable", "can_disable"), ("fixed_level", "fixed_level")):
            if target not in merged_reasoning and key in effort:
                merged_reasoning[target] = effort[key]
        if "reasoning" in _capability_names(row) and "supported" not in merged_reasoning:
            # The management-plane catalogue proves reasoning exists but does
            # not necessarily prove an on/off switch or effort vocabulary.
            merged_reasoning["supported"] = True
        reasoning = normalize_reasoning_capability(
            effort_levels=effort_levels,
            effort_default=effort_default,
            raw_reasoning=merged_reasoning,
            source="provider-discovery",
        )

        model_info = row.get("model_info") if isinstance(row.get("model_info"), dict) else {}
        target, target_kind = _wire_target(row, model_id)
        lifecycle = _lifecycle(row)
        if lifecycle == "shutdown":
            continue
        routing_quality, routing_speed, routing_economy, routing_tiers = _routing_metadata(row)
        routing_meta = model_routing_from_row({
            "routing": {
                "quality": routing_quality,
                "speed": routing_speed,
                "economy": routing_economy,
                "tiers": list(routing_tiers),
                "source": "provider-discovery",
                "confidence": "high",
            },
            "catalog_source": "provider-discovery",
        })
        routing_provenance = tuple(
            (field, str(meta.get("source") or "unknown"), str(meta.get("confidence") or "unknown"))
            for field, meta in sorted((routing_meta.get("provenance") or {}).items())
            if isinstance(meta, dict)
        )
        models.append(
            DiscoveredModel(
                model_id,
                name,
                context_window=_positive_int(
                    row.get("context_window"), row.get("context_length"), row.get("max_context_length"),
                    row.get("inputTokenLimit"), model_info.get("context_window"),
                ),
                max_output_tokens=_positive_int(
                    row.get("max_output_tokens"), row.get("max_completion_tokens"),
                    row.get("max_completions_tokens"), row.get("outputTokenLimit"),
                    model_info.get("max_output_tokens"),
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
                capability_source=str(reasoning["source"] or "provider-discovery"),
                tool_use=normalize_tool_use_capability(row),
                invocation_target=target,
                invocation_target_kind=target_kind,
                lifecycle=lifecycle,
                catalog_source="provider-discovery",
                routing_quality=routing_quality,
                routing_speed=routing_speed,
                routing_economy=routing_economy,
                routing_tiers=routing_tiers,
                routing_source=str(routing_meta.get("source") or "provider-discovery"),
                routing_confidence=str(routing_meta.get("confidence") or "high"),
                routing_provenance=routing_provenance,
            )
        )

    if not models:
        raise DiscoveryError(f"GET {endpoint} returned zero usable chat models")

    provider_id, provider_name = infer_provider_identity(base_url)
    return provider_id, provider_name, models
