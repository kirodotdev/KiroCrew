#!/usr/bin/env python3
"""Fail-closed release metadata checks for Kiro BYO.

The script intentionally uses only the standard library so it can run before dev
extras are installed. CI separately runs tests, coverage, lint, typecheck and wheel
builds; this check prevents version/documentation metadata from drifting between
packaging surfaces.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
import tomllib
from pathlib import Path

try:
    from scripts.release_file_policy import unsafe_release_path
except ModuleNotFoundError:  # direct `python scripts/release_check.py` invocation
    from release_file_policy import unsafe_release_path

ROOT = Path(__file__).resolve().parents[1]

# KiroCrew App manifests require SemVer, not merely a Python/PEP 440 version.
# Examples accepted: 0.9.12, 0.9.12-dev.2, 1.0.0-rc.2+build.5
_SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


def _is_semver(value: str) -> bool:
    return _SEMVER_RE.fullmatch(value) is not None


def _version_facts() -> dict[str, str]:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    app = json.loads((ROOT / "app.json").read_text(encoding="utf-8"))
    init_text = (ROOT / "src/kiro_byo/__init__.py").read_text(encoding="utf-8")
    protocol_text = (ROOT / "src/kiro_byo/protocol.py").read_text(encoding="utf-8")
    init_match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', init_text, re.M)
    agent_match = re.search(r'^AGENT_VERSION\s*=\s*["\']([^"\']+)["\']', protocol_text, re.M)
    if init_match is None or agent_match is None:
        raise RuntimeError("version constants are missing")
    return {
        "VERSION": (ROOT / "VERSION").read_text(encoding="utf-8").strip(),
        "pyproject": str(pyproject["project"]["version"]),
        "app": str(app["version"]),
        "package": init_match.group(1),
        "agent": agent_match.group(1),
    }


_FORBIDDEN_PRE_RELEASE_IDENTITIES = (
    "Kiro" + "API",
    "Kiro" + " API",
    "Kiro" + "-API",
    "kiro" + "-api",
    "kiro" + "_api",
    "KIRO" + "_API",
    "Kiro" + "Api",
)


def _stale_identity_leaks() -> list[str]:
    """Return source files that still carry the abandoned pre-release identity."""
    leaked: list[str] = []
    skipped_parts = {".git", ".pytest_cache", "__pycache__", "build", "dist"}
    for path in ROOT.rglob("*"):
        if not path.is_file() or any(part in skipped_parts for part in path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if any(token in text for token in _FORBIDDEN_PRE_RELEASE_IDENTITIES):
            leaked.append(path.relative_to(ROOT).as_posix())
    return sorted(leaked)


def _release_state_leaks() -> list[str]:
    """Return mutable/user-specific files that must never ship in a release tree."""
    leaked: list[str] = []
    for rel in ("data/sessions", "data/telemetry", "data/runtime_trace", "data/agent_runtime", "data/runs", "data/agent_workspaces", "data/audit"):
        root = ROOT / rel
        if not root.exists():
            continue
        leaked.extend(
            path.relative_to(ROOT).as_posix()
            for path in root.rglob("*")
            if path.is_file()
        )
    for rel in ("config/providers.json", "config/native_tools.json", "config/acp_agents.json"):
        path = ROOT / rel
        if path.is_file():
            leaked.append(rel)
    return sorted(leaked)




def _policy_pack_release_check() -> str | None:
    path = ROOT / "src/kiro_byo/policy_packs/tiga.json"
    if not path.is_file():
        return "built-in Tiga Policy Pack is missing"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return f"built-in Tiga Policy Pack is unreadable: {exc}"
    if raw.get("schema_version") != 4 or raw.get("id") != "tiga":
        return "built-in Tiga Policy Pack identity/schema drift"
    policy_ids = [row.get("id") for row in raw.get("policies", []) if isinstance(row, dict)]
    if policy_ids != ["sky", "multi", "power", "glitter"] or raw.get("default_policy") != "multi":
        return "built-in Tiga Policy Pack policy/default drift"
    for row in raw.get("policies", []):
        if not isinstance(row, dict):
            return "built-in Tiga Policy Pack contains a non-object policy"
        execution = row.get("execution") if isinstance(row.get("execution"), dict) else {}
        routing = execution.get("routing") if isinstance(execution.get("routing"), dict) else {}
        # R24 moved from the older adaptive/fit schema to explicit tier pools
        # and bounded scoring weights. Validate the live contract, not retired keys.
        expected_pool = {"sky": "fast", "multi": "any", "power": "strong", "glitter": "strong"}
        if routing.get("tier_pool") != expected_pool.get(row.get("id")):
            return f"built-in Tiga policy {row.get('id')} has an invalid tier pool"
        weights = [routing.get(key) for key in ("quality", "speed", "economy")]
        if not all(isinstance(weight, (int, float)) and not isinstance(weight, bool)
                   and 0 <= weight <= 1 for weight in weights):
            return f"built-in Tiga policy {row.get('id')} has invalid routing weights"
        if abs(sum(weights) - 1.0) > 1e-6:
            return f"built-in Tiga policy {row.get('id')} routing weights do not sum to 1"
        if not isinstance(routing.get("complexity_shift"), dict):
            return f"built-in Tiga policy {row.get('id')} has no complexity shift policy"
        orchestration = execution.get("orchestration") if isinstance(execution.get("orchestration"), dict) else None
        if orchestration is None:
            return f"built-in Tiga policy {row.get('id')} no longer declares orchestration policy data"
        try:
            max_agents = int(orchestration.get("max_agents") or 0)
            max_parallel = int(orchestration.get("max_parallel") or 0)
            max_depth = int(orchestration.get("max_depth") or 0)
        except (TypeError, ValueError):
            return f"built-in Tiga policy {row.get('id')} has invalid orchestration bounds"
        expected_parallel = {"sky": 1, "multi": 2, "power": 3, "glitter": 4}.get(str(row.get("id") or ""))
        if (
            orchestration.get("single_writer") is not True
            or not (1 <= max_agents <= 6)
            or expected_parallel is None
            or max_parallel != expected_parallel
            or max_parallel > max_agents
            or not (1 <= max_depth <= 2)
        ):
            return f"built-in Tiga policy {row.get('id')} broke bounded default orchestration"
        secondary = execution.get("secondary") if isinstance(execution.get("secondary"), dict) else None
        if secondary is not None and not isinstance(secondary.get("scoring"), dict):
            return f"built-in Tiga policy {row.get('id')} no longer declares secondary scoring"
        if row.get("id") == "glitter" and not isinstance(execution.get("collaboration_routing"), dict):
            return "built-in Glitter worker routing is no longer policy data"
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    package_data = pyproject.get("tool", {}).get("setuptools", {}).get("package-data", {}).get("kiro_byo", [])
    if "policy_packs/*.json" not in package_data:
        return "wheel package-data no longer includes policy_packs/*.json"
    return None



def _translated_ui_has(ui: str, phrase: str) -> bool:
    """Validate a visible UI contract through the localized resource catalogue."""
    resources = ROOT / "src/kiro_byo/locales/zh-CN.json"
    try:
        messages = json.loads(resources.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return any(phrase in value and f'tr("{key}"' in ui for key, value in messages.items())


def _mode_type_ui_release_check() -> str | None:
    protocol = (ROOT / "src/kiro_byo/protocol.py").read_text(encoding="utf-8")
    crew_patch = (ROOT / "src/kiro_byo/crew_patch_common.py").read_text(encoding="utf-8")
    ui = (ROOT / "ui/index.mjs").read_text(encoding="utf-8")
    if "kiro-byo-mode" in protocol or "kiro-byo-mode" in crew_patch or "kiro-byo-mode" in ui:
        return "legacy virtual Mode sentinel leaked back into product/runtime surfaces"
    if 'TYPE_SELECTION_PREFIX = "kiro-byo-type:"' not in protocol:
        return "Type selection wire prefix is missing"
    if 'return f"{TYPE_SELECTION_PREFIX}{mode}:{type_value}"' not in protocol:
        return "Type selection wire id is no longer namespaced by Mode"
    for label in ("Sky Type", "Multi Type", "Power Type", "Glitter Type"):
        if label not in (ROOT / "src/kiro_byo/policy_packs/tiga.json").read_text(encoding="utf-8"):
            return f"Tiga Type missing from built-in pack: {label}"
    if "changeType" in ui or "body:JSON.stringify({mode:packId,type:value})" in ui:
        return "management Type cards became a global Type mutation surface again"
    if "onClick:()=>setViewingType(pol.id)" not in ui or not _translated_ui_has(ui, "点击卡片仅查看详情"):
        return "management Type cards no longer have view-only semantics"
    if not _translated_ui_has(ui, "KiroCrew 只选择真实模型；选择“自动”时由当前 BYO Type 路由。"):
        return "management UI no longer explains the Model/Type boundary"
    # Crew 0.10 intentionally shares one React-Query catalogue across every
    # picker. BYO must therefore return one stable payload shape and handle the
    # Chat/non-Chat distinction only at presentation/routing state boundaries.
    for marker in (
        'return type_rows + model_rows',
        '_kiro_byo_get_policy_pack(mode_pack)',
        '_kiro_byo_reconcile_slot_routing',
    ):
        if marker not in crew_patch:
            return "Crew model catalogue lost the stable Type+model contract"
    for forbidden in ('kiro_byo_surface', 'request.headers.get("Referer"', '_chat_surface'):
        if forbidden in crew_patch:
            return "Crew model catalogue regressed to page-dependent shared-cache behavior"
    if 'name.startswith("kiro-byo-type:")' not in crew_patch:
        return "Crew model fallback no longer recognizes Type sentinels"
    return None


def _session_event_release_check() -> str | None:
    doc = ROOT / "README.md"
    module = ROOT / "src/kiro_byo/session_events.py"
    store = ROOT / "src/kiro_byo/session_store.py"
    if not doc.is_file() or "### 边界和会话事件" not in doc.read_text(encoding="utf-8"):
        return "README session event model documentation is missing"
    if not module.is_file():
        return "session event runtime module is missing"
    if not store.is_file():
        return "session snapshot store module is missing"
    text = store.read_text(encoding="utf-8")
    if '"schema_version": 3' not in text or '"events": session.events' not in text:
        return "schema-v3 session persistence no longer stores canonical events"
    if 'payload["messages"] = projection.messages' not in text:
        return "schema-v3 restore no longer derives messages from canonical events"
    if 'payload["route_pins"] = projection.route_pins' not in text:
        return "schema-v3 restore no longer derives routing pins from canonical events"
    event_text = module.read_text(encoding="utf-8")
    for marker in ("def derive_messages", "def ensure_event_projection", "def append_lifecycle"):
        if marker not in event_text:
            return f"session event runtime contract missing: {marker}"
    return None

def _reasoning_surface_release_check() -> str | None:
    server = (ROOT / "src/kiro_byo/acp_server.py").read_text(encoding="utf-8")
    crew_models = (ROOT / "src/kiro_byo/crew_patch_common.py").read_text(encoding="utf-8")
    dashboard = (ROOT / "src/kiro_byo/crew_dashboard_patch.py").read_text(encoding="utf-8")
    crew_patch = (ROOT / "src/kiro_byo/crew_shared_patch.py").read_text(encoding="utf-8")
    ui = (ROOT / "ui/index.mjs").read_text(encoding="utf-8")
    required = (
        (server, "reasoning effort is managed automatically by the active Type", "Type effort hard boundary"),
        (server, "if selected_type is None:", "concrete-only live effort config surface"),
        (crew_models, "kiro_byo_effort_levels", "per-model Crew effort capability metadata"),
        (crew_patch, "Type owns effort internally", "cold-session Type effort suppression"),
        (crew_patch, "suppress Crew backend fallback", "live-session Type effort fallback suppression"),
        (crew_patch, "effort_managed_by_type", "Crew REST Type effort hard boundary"),
        (crew_patch, 'ACP_BACKEND_KIRO_BYO: "effortLevel"', "Crew concrete-model advertised effort option mapping"),
        (crew_patch, "Kiro BYO: user-visible elapsed includes pre-dispatch setup.", "BYO full-turn elapsed clock"),
        (ui, "function effortText", "BYO unknown-effort UI contract"),
        (ui, "I18N:未知能力不会被猜测成支持", "unknown capability fail-closed UI"),
        (ui, "I18N:思考力度", "consistent effort terminology"),
    )
    for text, marker, label in required:
        if marker.startswith("I18N:"):
            if not _translated_ui_has(text, marker[5:]):
                return f"0.1.5 reasoning-policy translation contract missing: {label}"
        elif marker not in text:
            return f"0.1.5 reasoning-policy contract missing: {label}"
    # Type rows must not advertise a global effort capability; concrete models
    # keep their own exact vocabulary.
    if "kiro_byo_effort_dynamic" in crew_models:
        return "0.1.5 reasoning-policy drift: Type rows advertise session-wide effort"
    # Type and Model are separate product axes. Crew's legacy chat picker still
    # transports a Type as a namespaced virtual model id, but only on /chat. The
    # shell must hide cached Type rows everywhere else and suppress the concrete
    # model effort control while a Type row owns effort internally.
    for marker in ('html:not(.kiro-byo-chat-surface)', 'html.kiro-byo-chat-surface', 'data-kiro-byo-type-active', 'data-kiro-byo-effort-block'):
        if marker not in dashboard:
            return "reasoning-policy contract missing: chat-only Type presentation boundary"
    for forbidden in ('window.location.reload()', 'kiro_byo_surface', 'markChatModelsRequest', 'window.fetch ='):
        if forbidden in dashboard:
            return "dashboard navigation regressed to reload/page-dependent model fetching"
    for forbidden in ("kiro-byo-effort-capability-guard", "data-kiro-byo-effort-hidden", "fetch('/api/models'"):
        if forbidden in dashboard:
            return "reasoning-policy drift: dashboard reimplemented provider model capability discovery"
    if "（在 KiroCrew 对话右下角调整）" in ui:
        return "0.1.5 reasoning-policy drift: redundant effort-location note returned"
    return None


def _reasoning_language_release_check() -> str | None:
    """Freeze the provider-neutral visible-reasoning language contract introduced in 0.1.6."""
    module_path = ROOT / "src/kiro_byo/reasoning_language.py"
    if not module_path.is_file():
        return "0.1.6 reasoning-language policy module is missing"
    module = module_path.read_text(encoding="utf-8")
    config = (ROOT / "src/kiro_byo/config.py").read_text(encoding="utf-8")
    runner = (ROOT / "src/kiro_byo/turn_runner.py").read_text(encoding="utf-8")
    server = (ROOT / "src/kiro_byo/acp_server.py").read_text(encoding="utf-8")
    session_state = (ROOT / "src/kiro_byo/acp_session_state.py").read_text(encoding="utf-8")
    admin = (ROOT / "src/kiro_byo/provider_admin.py").read_text(encoding="utf-8")
    routes = (ROOT / "backend/routes.py").read_text(encoding="utf-8")
    ui = (ROOT / "ui/index.mjs").read_text(encoding="utf-8")
    required = (
        (module, "class ReasoningLanguageDecision", "typed language decision"),
        (module, "def detect_conversation_language", "Auto conversation-language detection"),
        (module, "natural/original spelling", "technical-term preservation"),
        (module, "reveal hidden/private chain-of-thought", "hidden-reasoning boundary"),
        (config, 'reasoning_language: str = "auto"', "config default"),
        (runner, "_apply_reasoning_language_policy", "per-turn policy resolution"),
        (runner, "reasoning_language_context=", "primary/worker provider injection"),
        (runner, 'getattr(session, "language_instruction"', "review/relay policy reuse"),
        (session_state, "reasoning_language_instruction", "ephemeral session language state"),
        (server, "from .acp_session_state import Session, PendingClientRequest", "server uses canonical session state"),
        (admin, "set_reasoning_language_preference", "management mutation"),
        (routes, '"/preferences/reasoning-language"', "KiroCrew App API"),
        (ui, "function SettingsPage", "KiroCrew App settings UI"),
        (ui, "I18N:不会改变 Thinking 开关、effort 或最终回答语言", "reasoning-control separation"),
    )
    for text, marker, label in required:
        if marker.startswith("I18N:"):
            if not _translated_ui_has(text, marker[5:]):
                return f"0.1.6 reasoning-language translation contract missing: {label}"
        elif marker not in text:
            return f"0.1.6 reasoning-language contract missing: {label}"
    # The policy must remain a presentation hint, not a post-hoc translation
    # pipeline that adds latency or mutates provider-authentic reasoning.
    for forbidden in ("translate_reasoning", "reasoning_translation_provider"):
        if forbidden in runner or forbidden in module:
            return "0.1.6 reasoning-language drift: post-hoc reasoning translation added"
    return None


def _adaptive_routing_release_check() -> str | None:
    """Freeze the 0.1.7 deterministic adaptive-routing contract."""
    adaptive_path = ROOT / "src/kiro_byo/adaptive_routing.py"
    dashboard_path = ROOT / "src/kiro_byo/routing_dashboard.py"
    if not adaptive_path.is_file() or not dashboard_path.is_file():
        return "0.1.7 adaptive-routing modules are missing"
    adaptive = adaptive_path.read_text(encoding="utf-8")
    router = (ROOT / "src/kiro_byo/mode_router.py").read_text(encoding="utf-8")
    telemetry = (ROOT / "src/kiro_byo/telemetry.py").read_text(encoding="utf-8")
    runner = (ROOT / "src/kiro_byo/turn_runner.py").read_text(encoding="utf-8")
    route_event_file = ROOT / "src/kiro_byo/routing_events.py"
    if not route_event_file.is_file():
        return "0.1.7 adaptive-routing contract missing: route event projection"
    routing_events = route_event_file.read_text(encoding="utf-8")
    routes = (ROOT / "backend/routes.py").read_text(encoding="utf-8")
    ui = (ROOT / "ui/index.mjs").read_text(encoding="utf-8")
    required = (
        (adaptive, "class AdaptiveRoutingSnapshot", "runtime evidence snapshot"),
        (adaptive, "completed-turn first-visible and total latency", "real latency evidence"),
        (adaptive, "_model_evidence_outcome", "request outcome versus model failure separation"),
        (adaptive, "_MODEL_FAILURE_CATEGORIES", "attributable failure category boundary"),
        (adaptive, "if attributable is None:", "ignore cancelled or non-provider evidence"),
        (adaptive, "reliability_adjustment", "bounded reliability evidence"),
        (router, "task_profile_for_signals", "deterministic task profile"),
        (router, "profile=adaptive_profile", "router consumes policy-shaped runtime evidence"),
        (router, "fallback_scoring", "capability fallback scoring is policy-driven"),
        (router, "collaboration_routing", "Glitter worker routing is policy-driven"),
        (router, "recent latency blended", "routing explanation exposes adaptive speed"),
        (telemetry, 'task_profile: str = ""', "task-profile telemetry"),
        (routing_events, '"task_profile": decision.signals.task_profile', "route trace task profile"),
        (runner, "data=selected_route_event_data(", "Turn Engine emits shared route event"),
        (routes, 'AppRoute("GET", "/routing", get_routing_dashboard)', "read-only routing API"),
        (ui, "I18N:最近一次任务特征", "Modes-page task-profile evidence UI"),
        (ui, "I18N:最近 60 小时模型使用情况", "recent model-usage evidence UI"),
        (ui, "I18N:模型使用占比", "runtime route evidence summary UI"),
        (ui, "I18N:新会话默认 Type", "policy-driven Type UI contract"),
    )
    for text, needle, label in required:
        if needle.startswith("I18N:"):
            if not _translated_ui_has(text, needle[5:]):
                return f"0.1.7 adaptive-routing translation contract missing: {label}"
        elif needle not in text:
            return f"0.1.7 adaptive-routing contract missing: {label}"
    match = re.search(r"schema_version:\s*int\s*=\s*(\d+)", telemetry)
    if match is None or int(match.group(1)) < 2:
        return "0.1.7 adaptive-routing contract missing: routing telemetry schema"
    # Dynamic routing must remain evidence-driven, not delegated to an LLM or
    # exposed as a user-adjustable weight console.
    for forbidden in ("router_llm", "router_model_completion", "setRoutingWeight", "routing-weight-slider"):
        if forbidden in adaptive or forbidden in router or forbidden in ui:
            return f"0.1.7 adaptive-routing drift: forbidden surface {forbidden}"
    return None



def _agent_runtime_release_check() -> str | None:
    """Freeze the unified, product-neutral execution-planning boundary."""
    runtime_path = ROOT / "src/kiro_byo/agent_runtime.py"
    if not runtime_path.is_file():
        return "unified Agent Runtime module is missing"
    runtime = runtime_path.read_text(encoding="utf-8")
    router = (ROOT / "src/kiro_byo/mode_router.py").read_text(encoding="utf-8")
    intent = (ROOT / "src/kiro_byo/execution_intent.py").read_text(encoding="utf-8")
    runner = (ROOT / "src/kiro_byo/turn_runner.py").read_text(encoding="utf-8")
    lifecycle_path = ROOT / "src/kiro_byo/agent_plan_lifecycle.py"
    if not lifecycle_path.is_file():
        return "unified Agent Runtime lifecycle module missing"
    lifecycle = lifecycle_path.read_text(encoding="utf-8")
    child_path = ROOT / "src/kiro_byo/subagent_execution.py"
    if not child_path.is_file():
        return "isolated child execution seam is missing"
    child = child_path.read_text(encoding="utf-8")
    prompt_routing_path = ROOT / "src/kiro_byo/prompt_routing.py"
    if not prompt_routing_path.is_file():
        return "prompt routing boundary module is missing"
    prompt_routing = prompt_routing_path.read_text(encoding="utf-8")
    server = (ROOT / "src/kiro_byo/acp_server.py").read_text(encoding="utf-8")
    dispatch = (ROOT / "src/kiro_byo/acp_dispatch.py").read_text(encoding="utf-8")
    policy = (ROOT / "src/kiro_byo/policy_packs.py").read_text(encoding="utf-8")
    required = (
        (runtime, "class TaskProfile", "multi-dimensional TaskProfile"),
        (runtime, "class AgentSpec", "AgentSpec contract"),
        (runtime, "class AgentTask", "AgentTask identity"),
        (runtime, "class AgentResult", "structured AgentResult"),
        (runtime, "class ExecutionPlan", "ExecutionPlan graph"),
        (runtime, "class AgentRegistry", "bounded AgentRegistry"),
        (runtime, "class AgentRuntime", "bounded parallel AgentRuntime"),
        (runtime, "def tasks_for_role", "multiple child tasks per role"),
        (runtime, "def send_message", "stable task continuation mailbox"),
        (policy, "class OrchestrationProfile", "data-driven orchestration policy"),
        (intent, "orchestration: OrchestrationProfile", "compiled orchestration intent"),
        (router, "def build_execution_plan", "product-neutral execution planner"),
        (router, "profile.breadth", "multi-dimensional worker admission"),
        (runner, "execution_plan: ExecutionPlan | None", "Turn Engine ExecutionPlan seam"),
        (lifecycle, "agent.plan.created", "Agent lifecycle trace"),
        (child, "agent.subagents.started", "parallel subagent lifecycle trace"),
        (child, "def _run_isolated_subagent", "isolated child execution"),
        (dispatch, "'_kiro/agent/resume': '_handle_agent_resume_request'", "agent continuation control plane"),
        (server, "selection_kind", "selection truth kind"),
        (server, "selection_source", "selection truth source"),
        (prompt_routing, "model.route.bypassed", "concrete-model routing bypass truth"),
        (prompt_routing, "def _select_type_prompt_route", "shared Type prompt routing planner"),
    )
    for text, marker, label in required:
        if marker not in text:
            return f"unified Agent Runtime contract missing: {label}"
    # Verify the wired mixins structurally, not a fragile verbatim class line.
    turn_classes = [node for node in ast.parse(runner).body
                    if isinstance(node, ast.ClassDef) and node.name == "TurnRunnerMixin"]
    if len(turn_classes) != 1:
        return "TurnRunner mixin missing or multiply defined"
    mixins = {node.id for node in turn_classes[0].bases if isinstance(node, ast.Name)}
    if not {"AgentPlanLifecycleMixin", "SubagentExecutionMixin", "PromptRoutingMixin", "NativeToolDispatchMixin", "McpToolDispatchMixin", "ProviderLoopTerminalMixin"} <= mixins:
        return "Turn Engine child-agent/routing/native/MCP/terminal mixins not wired"
    native_path = ROOT / "src/kiro_byo/native_tool_dispatch.py"
    if not native_path.is_file():
        return "native dispatch security boundary module missing"
    native_text = native_path.read_text(encoding="utf-8")
    for marker in (
        "def _execute_native_tool", "def _approve_native_scope",
        "def _run_authorized_native_invocation", "_pre_dispatch_tool_interrupt",
        "revoke_scope_roots", "tool.call.completed", "session.native.audit.record",
    ):
        if marker not in native_text:
            return f"native dispatch safety contract missing: {marker}"
    terminal_path = ROOT / "src/kiro_byo/provider_loop_terminal.py"
    if not terminal_path.is_file():
        return "provider-loop terminal recovery boundary missing"
    terminal_text = terminal_path.read_text(encoding="utf-8")
    for marker in (
        "def _finish_provider_loop_cancelled_before_invoke",
        "def _finish_provider_loop_provider_error",
        "def _finish_provider_loop_internal_error",
        "_recover_current_turn", "preserve_side_effects=state.side_effect_risk_seen",
        "runtime.internal_error", "error_category", "self.result(",
    ):
        if marker not in terminal_text:
            return f"provider-loop terminal recovery contract missing: {marker}"
    # Terminal handling is intentionally downstream of RecoveryPolicy. It must
    # neither select models nor retry a failed invocation on its own.
    if any(marker in terminal_text for marker in ("provider.retry", "model.route.fallback", "live_provider_failure")):
        return "terminal completion unexpectedly contains retry/fallback decisions"
    # Type names belong in Policy Pack data/semantics, not in the generic plan
    # compiler. The legacy compatibility helper name is allowed outside it.
    plan_start = router.find("def build_execution_plan")
    plan_end = router.find("def glitter_review_target", plan_start)
    plan_body = router[plan_start:plan_end if plan_end >= 0 else None]
    for forbidden in ('== "sky"', '== "multi"', '== "power"', '== "glitter"'):
        if forbidden in plan_body:
            return f"execution planner branches on product Type: {forbidden}"
    return None

def _durable_recovery_release_check() -> str | None:
    recovery = ROOT / "src/kiro_byo/recovery_policy.py"
    durable = ROOT / "src/kiro_byo/durable_runs.py"
    runner = ROOT / "src/kiro_byo/turn_runner.py"
    server = ROOT / "src/kiro_byo/acp_server.py"
    if not recovery.is_file() or not durable.is_file():
        return "durable recovery runtime modules are missing"
    recovery_text = recovery.read_text(encoding="utf-8")
    durable_text = durable.read_text(encoding="utf-8")
    runner_text = runner.read_text(encoding="utf-8")
    server_text = server.read_text(encoding="utf-8")
    for marker in ("class RecoveryPolicy", "live_provider_failure", "durable_run"):
        if marker not in recovery_text:
            return f"durable recovery policy contract missing: {marker}"
    for marker in ("def prepare_resume", "def restart_recovery_state", '"retry_count": 0', '"resume_count": 0', '"replan_count": 0'):
        if marker not in durable_text:
            return f"durable run recovery contract missing: {marker}"
    for marker in ("tool_batch_admitted", "durable_recovery_rewind", "RecoveryPolicy.live_provider_failure"):
        if marker not in runner_text:
            return f"turn recovery safety boundary missing: {marker}"
    for marker in ("_kiro/run/retry", "_kiro/run/resume", "_kiro/run/replan", "durableRecovery"):
        if marker not in server_text:
            return f"ACP durable recovery control missing: {marker}"
    return None



def _provider_adapter_shape_release_check() -> str | None:
    """Keep same-wire vendor adapters thin and transport-neutral.

    Zhipu, Moonshot/Kimi, Qwen and Xiaomi MiMo all use the same native Chat
    completion/SSE engine on this path.  Their modules may own request-policy
    quirks, but must not recreate HTTP request or SSE parsing loops.
    """
    provider_dir = ROOT / "src/kiro_byo/providers"
    shared_path = provider_dir / "chat_completions_common.py"
    if not shared_path.is_file():
        return "shared native Chat transport engine is missing"
    shared = shared_path.read_text(encoding="utf-8")
    if any(f"def {symbol}" not in shared for symbol in (
        "run_native_chat_completion", "make_native_chat_turn", "make_native_chat_text",
    )):
        return "shared native Chat adapter factory or transport engine is missing"
    for name in ("zhipu_chat.py", "moonshot_chat.py", "qwen_chat.py", "xiaomi_mimo.py"):
        path = provider_dir / name
        if not path.is_file():
            return f"native Chat provider adapter is missing: {name}"
        text = path.read_text(encoding="utf-8")
        if "make_native_chat_turn" not in text or "make_native_chat_text" not in text:
            return f"native Chat provider adapter bypasses shared factories: {name}"
        if "run_native_chat_completion(" in text:
            return f"native Chat provider adapter duplicated shared engine call: {name}"
        if "urllib.request.Request(" in text or "iter_sse" in text:
            return f"native Chat provider adapter duplicated HTTP/SSE transport: {name}"
    return None


def _ui_backend_contract_check() -> str | None:
    """Fail release when the App shell references missing pages/routes.

    ``node --check`` catches syntax only; a missing React page symbol such as
    ``AccountsPage`` is still syntactically valid and fails only when clicked.
    Keep a lightweight product-level contract gate here.
    """
    ui = (ROOT / "ui/index.mjs").read_text(encoding="utf-8")
    routes = (ROOT / "backend/routes.py").read_text(encoding="utf-8")

    defined_pages = set(re.findall(r"function\s+([A-Za-z_][A-Za-z0-9_]*Page)\s*\(", ui))
    referenced_pages = set(re.findall(r"h\(\s*([A-Za-z_][A-Za-z0-9_]*Page)\b", ui))
    missing_pages = sorted(referenced_pages - defined_pages)
    if missing_pages:
        return "management UI references undefined page components: " + ", ".join(missing_pages)

    registered_pairs = set(re.findall(r'AppRoute\(\s*"(GET|POST|PUT|DELETE|PATCH)"\s*,\s*"([^"]+)"', routes))
    registered = {path for _method, path in registered_pairs}
    # Static App API calls are exact. Dynamic template calls are guarded by the
    # complete product route contract below so interpolation cannot hide a
    # missing credential/refresh/model-preference endpoint.
    static_calls = set(re.findall(r'''api\(\s*['"](/[^'"?`$]*)''', ui))
    required = {
        "/status", "/presets", "/providers", "/routing", "/accounts",
        "/diagnostics", "/runtime-trace", "/integration",
        "/acp-conformance", "/mode", "/preferences/reasoning-language",
    }
    for path in sorted(static_calls & required):
        if path not in registered:
            return f"management UI calls unregistered App route: {path}"

    product_routes = {
        ("GET", "/status"), ("GET", "/presets"), ("GET", "/providers"),
        ("POST", "/providers/test"), ("POST", "/providers"),
        ("POST", "/providers/{provider_id}/refresh"),
        ("PUT", "/providers/{provider_id}/credential"),
        ("DELETE", "/providers/{provider_id}"),
        ("PUT", "/providers/{provider_id}/models/{model_id}/thinking"),
        ("GET", "/routing"), ("PUT", "/mode"), ("GET", "/accounts"),
        ("GET", "/diagnostics"), ("DELETE", "/diagnostics"),
        ("GET", "/runtime-trace"), ("DELETE", "/runtime-trace"),
        ("PUT", "/preferences/reasoning-language"),
        ("GET", "/acp-conformance"), ("GET", "/integration"),
        ("POST", "/integration/install"), ("POST", "/integration/uninstall"),
    }
    missing_routes = sorted(product_routes - registered_pairs)
    if missing_routes:
        rendered = ", ".join(f"{method} {path}" for method, path in missing_routes)
        return "management product route contract is incomplete: " + rendered
    for stale in ("/acp-agents", "/workspaces/"):
        if stale in ui:
            return f"removed management surface leaked back into UI: {stale}"
    return None


def _source_hygiene_release_check() -> str | None:
    """Fail closed before packaging; never follow symlinks or ship local secrets."""
    bad: list[str] = []
    for path in ROOT.rglob("*"):
        if not path.is_file() and not path.is_symlink():
            continue
        rel = path.relative_to(ROOT)
        reason = "symbolic link" if path.is_symlink() else unsafe_release_path(rel)
        if reason:
            bad.append(f"{rel.as_posix()} ({reason})")
    if bad:
        return "editor/patch backup or sensitive artifacts remain in release tree: " + ", ".join(sorted(bad)[:8])
    return None


def _core_refinement_release_check() -> str | None:
    required = {
        "src/kiro_byo/turn_state.py": ("class TurnPhase", "class TurnState", "class RoundOutcome"),
        "src/kiro_byo/execution_intent.py": ("class ExecutionIntent", "def compile_execution_intent"),
        "src/kiro_byo/capability_seams.py": ("class RuntimeSeams", "class ModelRouter", "class SessionProjection"),
    }
    for rel, markers in required.items():
        path = ROOT / rel
        if not path.is_file():
            return f"0.1.3 core contract missing: {rel}"
        text = path.read_text(encoding="utf-8")
        for marker in markers:
            if marker not in text:
                return f"0.1.3 core contract drift in {rel}: {marker}"
    policy_text = (ROOT / "src/kiro_byo/policy_packs.py").read_text(encoding="utf-8")
    if "class UserPolicySemantics" not in policy_text or "class ExecutionPolicy" not in policy_text:
        return "Policy Pack semantics/execution split is missing"
    turn_text = (ROOT / "src/kiro_byo/turn_runner.py").read_text(encoding="utf-8")
    if "_runtime_seams" not in turn_text or "TurnPhase" not in turn_text:
        return "Turn Engine no longer consumes explicit phases/capability seams"
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metadata-only", action="store_true")
    parser.parse_args()

    facts = _version_facts()
    unique = set(facts.values())
    for surface, value in facts.items():
        print(f"{surface:<12} {value}")
    if not _is_semver(facts["app"]):
        print(
            f"FAIL: app.json version must be SemVer, got {facts['app']!r}",
            file=sys.stderr,
        )
        return 2
    if len(unique) != 1:
        print("FAIL: release version metadata drift", file=sys.stderr)
        return 8

    for legacy in (
        "v0.9-adaptive", "v0.10-adaptive", "v0.11-rc1",
    ):
        if (ROOT / "integration/kiro_crew" / legacy).exists():
            print(f"FAIL: retired Crew adapter still bundled: {legacy}", file=sys.stderr)
            return 3
    if json.loads((ROOT / "app.json").read_text(encoding="utf-8")).get("minKiroCrewVersion") != "0.12.0":
        print("FAIL: Crew stable floor must be 0.12.0", file=sys.stderr)
        return 3
    adapter_dir = ROOT / "integration/kiro_crew/v0.12-plus"
    compat12 = json.loads((adapter_dir / "compatibility.json").read_text(encoding="utf-8"))
    manifest12 = json.loads((adapter_dir / "manifest.json").read_text(encoding="utf-8"))
    if (compat12.get("transformer") != "adaptive12"
            or "allowed_crew_versions" in compat12
            or "max_version_exclusive" in compat12
            or compat12.get("min_version") != "0.12.0"
            or compat12.get("verified_versions") != []
            or compat12.get("verified_through")
            or compat12.get("semantic_unpinned_files") != compat12.get("generated_files")
            or set(manifest12.get("files") or []) != set(compat12.get("generated_files") or []) | set(compat12.get("created_files") or [])):
        print("FAIL: source-guarded KiroCrew 0.12+ candidate metadata drift", file=sys.stderr)
        return 3
    if not (adapter_dir / "overlay/src/kiro_crew/acp/harness/kiro_byo.py").is_file():
        print("FAIL: 0.12 BYO-owned Harness overlay missing", file=sys.stderr)
        return 3
    if "### 边界和会话事件" not in (ROOT / "README.md").read_text(encoding="utf-8"):
        print("FAIL: README architecture ownership boundary is missing", file=sys.stderr)
        return 4

    stale_identities = _stale_identity_leaks()
    if stale_identities:
        print(
            "FAIL: abandoned pre-release identity remains in: "
            + ", ".join(stale_identities),
            file=sys.stderr,
        )
        return 11

    policy_pack_error = _policy_pack_release_check()
    if policy_pack_error:
        print(f"FAIL: {policy_pack_error}", file=sys.stderr)
        return 12

    mode_type_error = _mode_type_ui_release_check()
    if mode_type_error:
        print(f"FAIL: {mode_type_error}", file=sys.stderr)
        return 13

    session_event_error = _session_event_release_check()
    if session_event_error:
        print(f"FAIL: {session_event_error}", file=sys.stderr)
        return 14

    core_error = _core_refinement_release_check()
    if core_error:
        print(f"FAIL: {core_error}", file=sys.stderr)
        return 15

    reasoning_error = _reasoning_surface_release_check()
    if reasoning_error:
        print(f"FAIL: {reasoning_error}", file=sys.stderr)
        return 16

    reasoning_language_error = _reasoning_language_release_check()
    if reasoning_language_error:
        print(f"FAIL: {reasoning_language_error}", file=sys.stderr)
        return 17

    adaptive_routing_error = _adaptive_routing_release_check()
    if adaptive_routing_error:
        print(f"FAIL: {adaptive_routing_error}", file=sys.stderr)
        return 18

    agent_runtime_error = _agent_runtime_release_check()
    if agent_runtime_error:
        print(f"FAIL: {agent_runtime_error}", file=sys.stderr)
        return 19

    durable_recovery_error = _durable_recovery_release_check()
    if durable_recovery_error:
        print(f"FAIL: {durable_recovery_error}", file=sys.stderr)
        return 20

    adapter_shape_error = _provider_adapter_shape_release_check()
    if adapter_shape_error:
        print(f"FAIL: {adapter_shape_error}", file=sys.stderr)
        return 20

    ui_contract_error = _ui_backend_contract_check()
    if ui_contract_error:
        print(f"FAIL: {ui_contract_error}", file=sys.stderr)
        return 22

    # ACP V3: do not advertise methods the server cannot dispatch. Keep
    # Crew-only legacy/internal handlers unadvertised instead of inventing parity.
    import runpy
    acp_audit = runpy.run_path(str(ROOT / "scripts" / "acp_capability_audit.py"))
    _acp_report, acp_errors = acp_audit["audit_contract"](ROOT)
    if acp_errors:
        print("FAIL: ACP capability contract: " + "; ".join(acp_errors), file=sys.stderr)
        return 24

    # V9: extracted leaf modules must not reverse-depend on ACP / Turn Runner.
    try:
        boundary_errors = runpy.run_path(
            str(ROOT / "scripts" / "boundary_guard.py"), run_name="_byo_boundary_guard"
        )["check"](ROOT)
    except (OSError, SyntaxError, ValueError) as exc:
        print(f"FAIL: dependency boundary verification unavailable: {exc}", file=sys.stderr)
        return 24
    if boundary_errors:
        print("FAIL: architecture boundary drift: " + "; ".join(boundary_errors), file=sys.stderr)
        return 24

    hygiene_error = _source_hygiene_release_check()
    if hygiene_error:
        print(f"FAIL: {hygiene_error}", file=sys.stderr)
        return 23

    leaks = _release_state_leaks()
    if leaks:
        print(
            "FAIL: mutable runtime/secret state is present in the release tree: "
            + ", ".join(leaks),
            file=sys.stderr,
        )
        return 10

    version = next(iter(unique))
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    if f"**Current release: {version}**" not in readme:
        print("FAIL: README current release marker drift", file=sys.stderr)
        return 5
    if "## 1. 快速安装与使用" not in readme or "## 5. 架构契约与维护原则" not in readme:
        print("FAIL: current README lacks installation or architecture guidance", file=sys.stderr)
        return 6
    # A single live README plus the standard CHANGELOG, not ever-growing
    # audit/release-note files. CHANGELOG.md is the Keep a Changelog standard
    # root file; detailed per-session dev notes still live under docs/changelog/.
    allowed_root_docs = {"README.md", "CHANGELOG.md"}
    other_docs = sorted(p.name for p in ROOT.glob("*.md") if p.name not in allowed_root_docs)
    if other_docs:
        print("FAIL: obsolete root Markdown documents: " + ", ".join(other_docs[:8]), file=sys.stderr)
        return 7

    print("PASS: release metadata and primary documentation are internally consistent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
