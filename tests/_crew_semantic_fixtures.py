"""Minimal semantic fixtures used by current shared ACP transform tests."""

def _minimal_backends_source(*, dynamic_registry: bool, zero_tool_capability: bool) -> str:
    sets = [
        "ACP_BACKENDS_KNOWN",
        "ACP_BACKENDS_INDEPENDENT_SETUP",
        "ACP_BACKENDS_SESSION_MCP_ARRAY",
        "BASELINE_SELECTABLE_BACKENDS",
        "ACP_BACKENDS_SESSION_SHARING",
        "ACP_BACKENDS_MEMBER_DISPATCH",
        "ACP_BACKENDS_MEMBER_PANEL",
        "ACP_BACKENDS_MEMBER_CAPABILITIES",
        "ACP_BACKENDS_MARKDOWN_AGENT_SPECS",
        "ACP_BACKENDS_USER_LEVEL_AGENT_SPECS_ONLY",
        "ACP_BACKENDS_STEER",
        "ACP_BACKENDS_COMPACT",
        "ACP_BACKENDS_INLINE_COMPACTION",
        "ACP_BACKENDS_ACP_RUNTIME",
        "ACP_BACKENDS_SESSION_EVICTION",
        "ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION",
        "ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION",
        "ACP_BACKENDS_EFFORT_FROM_ADVERTISED_OPTION",
        "ACP_BACKENDS_ADVERTISED_MODEL_SELECTION",
        "ACP_BACKENDS_SIDE_READONLY",
        "ACP_BACKENDS_CREW_FIRES_SPEC_HOOKS",
        "ACP_BACKENDS_HARNESS_OWNED_SESSIONS",
    ]
    if zero_tool_capability:
        sets.append("ACP_BACKENDS_HONOR_ZERO_TOOL_BAN")
    lines = [
        'ACP_BACKEND_DEEPSEEK = "deepseek"',
        'ACP_BACKEND_KIRO = ""',
        *[f"{name} = frozenset({{ACP_BACKEND_DEEPSEEK}})" for name in sets],
        "POLICY_ID_BY_BACKEND = {\n    ACP_BACKEND_DEEPSEEK: ACP_BACKEND_DEEPSEEK,\n}",
        'EFFORT_CONFIG_OPTION_IDS = {\n    ACP_BACKEND_DEEPSEEK: "effort",\n}',
        '_MODEL_REGISTRY_NAMESPACE_BY_BACKEND = {\n    ACP_BACKEND_DEEPSEEK: "deepseek",\n}',
        "class Routing:",
        '    UNVERIFIED = "unverified"',
        "ACP_BACKEND_ROUTING = {\n    ACP_BACKEND_DEEPSEEK: Routing.UNVERIFIED,\n}",
        "ACP_BACKEND_LAUNCH = {\n}",
    ]
    if dynamic_registry:
        lines += [
            "def routing_for(backend):",
            "    return ACP_BACKEND_ROUTING.get(backend, Routing.UNVERIFIED)",
            "def register_selectable_backend(backend):",
            "    if routing_for(backend) == Routing.UNVERIFIED:",
            "        raise ValueError(backend)",
        ]
    return "\n".join(lines) + "\n"


def _minimal_agents_source_for_member_backend() -> str:
    return '''from typing import Any
from kiro_crew.acp_backends import (
    ACP_BACKEND_KIRO,
    model_registry_namespace,
)
from kiro_crew.dashboard.agent_admin.crew_records import api_kirocrew_agent_resolved_model

_LIST_MODELS_TIMEOUT = 10.0

def _normalize_model_key(name: str) -> str:
    return str(name).strip().lower()

def _scoped_default(cfg, backend):
    return "auto"

def _pin_entitlement_backend(cfg: Any) -> str:
    default_backend = getattr(cfg.agent, "acp_backend", "")
    member_backend = getattr(cfg.agent, "member_acp_backend", "")
    if (
        capabilities_for(member_backend).model_id_namespace
        != capabilities_for(default_backend).model_id_namespace
    ):
        return member_backend
    return default_backend

async def _revalidate_crew_pin(model: str, request):
    if not model or model == "auto" or model_registry.acp_id_correction(model):
        return None
    cfg = await asyncio.to_thread(KiroCrewConfig.load)
    if is_claude_code(cfg.agent.provider):
        return None
    from kiro_crew.dashboard.handlers.core import _revalidate_role_pin_evidence
    return await _revalidate_role_pin_evidence(
        model, request, backend=_pin_entitlement_backend(cfg)
    )

async def api_models(request):
    backend = "kiro"
    cfg = object()
    if capabilities_for(backend).resolves_model_from_advertised_list:
        return web.json_response([])
    return web.json_response([])
'''
