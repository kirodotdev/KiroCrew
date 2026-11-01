from __future__ import annotations

import json
import unittest
from pathlib import Path

from kiro_byo import crew_shared_patch
from kiro_byo.capabilities import capability_report

ROOT = Path(__file__).resolve().parents[1]
HARNESS_REL = "src/kiro_crew/acp/harness/kiro_byo.py"


class Crew09AdapterTests(unittest.TestCase):
    def test_runtime_report_does_not_duplicate_crew_backend_card(self) -> None:
        report = capability_report()
        self.assertEqual(report["source"], "runtime-introspection")
        self.assertNotIn("verified", report)
        self.assertNotIn("total", report)
        self.assertEqual(report["mcp_transports"], ["stdio", "http"])
        self.assertIn("session/list", report["standard_methods"])
        self.assertIn("session/resume", report["standard_methods"])

    def test_harness_is_dedicated_owner_module(self) -> None:
        harness_path = ROOT / "integration/kiro_crew/v0.10-adaptive/overlay" / HARNESS_REL
        source = harness_path.read_text(encoding="utf-8")
        self.assertIn("class KiroByoHarness(MembershipHarness):", source)
        self.assertIn('env["KIRO_BYO_HOME"]', source)
        self.assertIn("return requested", source)
        markers = crew_shared_patch._REQUIRED_MARKERS["src/kiro_crew/acp/harness/__init__.py"]
        self.assertIn("from kiro_crew.acp.harness.kiro_byo import KiroByoHarness", markers)
        self.assertIn("ACP_BACKEND_KIRO_BYO: KiroByoHarness", markers)
        # The semantic patch only imports/registers the owner module; it must not
        # inline a second harness implementation into Crew's registry module.
        patch_source = (ROOT / "src/kiro_byo/crew_shared_patch.py").read_text(encoding="utf-8")
        self.assertNotIn("class KiroByoHarness(MembershipHarness):", patch_source)

    def test_crew09_effort_uses_current_kiro_v3_config_id(self) -> None:
        source = (ROOT / "src/kiro_byo/crew_shared_patch.py").read_text(encoding="utf-8")
        self.assertIn('ACP_BACKEND_KIRO_BYO: "effortLevel"', source)
        self.assertNotIn('ACP_BACKEND_KIRO_BYO: "reasoning_effort"', source)


if __name__ == "__main__":
    unittest.main()


def test_crew09_markdown_support_is_declared_without_full_saved_member_overclaim() -> None:
    source = (ROOT / "src" / "kiro_byo" / "crew_shared_patch.py").read_text(encoding="utf-8")
    membership_block = source.split("memberships = [", 1)[1].split("]", 1)[0]
    assert '"ACP_BACKENDS_MEMBER_CAPABILITIES"' not in membership_block
    assert 'memberships.append("ACP_BACKENDS_MARKDOWN_AGENT_SPECS")' in source
    assert 'memberships.append("ACP_BACKENDS_USER_LEVEL_AGENT_SPECS_ONLY")' in source
    assert "full saved-spec" in source
    assert "provider/model" in source


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


def test_crew09_current_registry_shape_does_not_pollute_public_baseline() -> None:
    source = _minimal_backends_source(dynamic_registry=True, zero_tool_capability=True)
    patched = crew_shared_patch.patch_text("src/kiro_crew/agent_sdk/backends.py", source)
    baseline_block = patched.split("BASELINE_SELECTABLE_BACKENDS =", 1)[1].split("\n", 1)[0]
    assert "ACP_BACKEND_KIRO_BYO" not in baseline_block
    assert "register_selectable_backend(ACP_BACKEND_KIRO_BYO)" in patched
    zero_block = patched.split("ACP_BACKENDS_HONOR_ZERO_TOOL_BAN =", 1)[1].split("\n", 1)[0]
    assert "ACP_BACKEND_KIRO_BYO" in zero_block
    dispatch_block = patched.split("ACP_BACKENDS_MEMBER_DISPATCH =", 1)[1].split("\n", 1)[0]
    panel_block = patched.split("ACP_BACKENDS_MEMBER_PANEL =", 1)[1].split("\n", 1)[0]
    markdown_block = patched.split("ACP_BACKENDS_MARKDOWN_AGENT_SPECS =", 1)[1].split("\n", 1)[0]
    user_scope_block = patched.split("ACP_BACKENDS_USER_LEVEL_AGENT_SPECS_ONLY =", 1)[1].split("\n", 1)[0]
    member_saved_block = patched.split("ACP_BACKENDS_MEMBER_CAPABILITIES =", 1)[1].split("\n", 1)[0]
    assert "ACP_BACKEND_KIRO_BYO" in dispatch_block
    assert "ACP_BACKEND_KIRO_BYO" in panel_block
    assert "ACP_BACKEND_KIRO_BYO" in markdown_block
    assert "ACP_BACKEND_KIRO_BYO" in user_scope_block
    assert "ACP_BACKEND_KIRO_BYO" not in member_saved_block
    assert crew_shared_patch.inspect("src/kiro_crew/agent_sdk/backends.py", patched).state == "patched"


def test_crew09_legacy_insider_shape_keeps_legacy_selectability_seam() -> None:
    source = _minimal_backends_source(dynamic_registry=False, zero_tool_capability=False)
    patched = crew_shared_patch.patch_text("src/kiro_crew/agent_sdk/backends.py", source)
    baseline_block = patched.split("BASELINE_SELECTABLE_BACKENDS =", 1)[1].split("\n", 1)[0]
    assert "ACP_BACKEND_KIRO_BYO" in baseline_block
    assert "register_selectable_backend(ACP_BACKEND_KIRO_BYO)" not in patched
    assert "ACP_BACKENDS_HONOR_ZERO_TOOL_BAN" not in patched
    assert crew_shared_patch.inspect("src/kiro_crew/agent_sdk/backends.py", patched).state == "patched"


def test_crew10_version_gate_preserves_audited_family() -> None:
    import re

    compat = json.loads((ROOT / "integration/kiro_crew/v0.10-adaptive/compatibility.json").read_text())
    candidate = compat["family_regex"]
    audited = compat["verified_family_regex"]
    # Broad syntax is only an initial candidate filter. The installer separately
    # enforces the 0.10 major/minor family before inspecting source seams.
    for version in ("0.10.0", "0.10.0-insider.1", "0.10.1", "0.10.2-rc.1"):
        assert re.fullmatch(candidate, version), version
    for version in ("0.10.0", "0.10.0-insider.4", "0.10.0rc1"):
        assert re.fullmatch(audited, version), version
    for version in ("0.10.1", "0.11.0", "0.9.0"):
        assert not re.fullmatch(audited, version), version


def _load_project_allowed_tools_helper():
    """Compile the real overlay helper without importing an installed KiroCrew."""
    import ast
    from types import SimpleNamespace

    source = (
        ROOT
        / "integration/kiro_crew/v0.10-adaptive/overlay"
        / HARNESS_REL
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    node = next(
        item
        for item in tree.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        and item.name == "_project_allowed_tools"
    )

    state: dict[str, object] = {
        "denied_by_governance": set(),
        "hook_blocked": False,
    }

    def pre_tool_hook_matchers(agent_id, spec):
        return ("@kirocrew-core/*",) if state["hook_blocked"] else ()

    def allowed_tools_to_permissions(entries, *, agent_id):
        entry = entries[0]
        # Model the official KAS safety distinction relevant to this adapter:
        # broad shell/filesystem names are deliberately not blanket no-prompt.
        caps = () if entry in {"shell", "read", "write", "fs_read", "fs_write"} else ("mcp",)
        return {"entry": entry, "caps": caps}

    def withhold_hook_gated_auto_approval(policy, hook_matchers, *, agent_id):
        if hook_matchers and str(policy["entry"]).startswith("@kirocrew-core"):
            return {**policy, "caps": ()}
        return policy

    def auto_approved_capabilities(policy):
        return tuple(policy["caps"])

    def may_skip_gate_now(entry):
        return entry not in state["denied_by_governance"]

    globals_ns = {
        "kas_agents": SimpleNamespace(pre_tool_hook_matchers=pre_tool_hook_matchers),
        "kas_permissions": SimpleNamespace(
            allowed_tools_to_permissions=allowed_tools_to_permissions,
            withhold_hook_gated_auto_approval=withhold_hook_gated_auto_approval,
            auto_approved_capabilities=auto_approved_capabilities,
        ),
        "may_skip_gate_now": may_skip_gate_now,
    }
    module = ast.Module(body=[node], type_ignores=[])
    ast.fix_missing_locations(module)
    overlay_path = ROOT / "integration/kiro_crew/v0.10-adaptive/overlay" / HARNESS_REL
    exec(compile(module, str(overlay_path), "exec"), globals_ns)
    return globals_ns["_project_allowed_tools"], state


def test_crew_overlay_projects_allowed_tools_through_official_safety_filters() -> None:
    helper, state = _load_project_allowed_tools_helper()

    # Managed MCP grant survives when governance permits it and no hook requires
    # the call to return through Crew's permission path.
    assert helper({"allowedTools": ["@kirocrew-core"]}, "kirocrew") == ["@kirocrew-core"]

    # Shell/filesystem blanket grants remain prompt-only; the KAS classifier is
    # authoritative rather than a parallel third-party security vocabulary.
    assert helper({"allowedTools": ["shell", "read", "@kirocrew-core"]}, "kirocrew") == [
        "@kirocrew-core"
    ]

    state["denied_by_governance"] = {"@kirocrew-core"}
    assert helper({"allowedTools": ["@kirocrew-core"]}, "kirocrew") == []

    state["denied_by_governance"] = set()
    state["hook_blocked"] = True
    assert helper({"allowedTools": ["@kirocrew-core"]}, "kirocrew") == []


def test_crew_overlay_preserves_absent_vs_explicit_empty_allowed_tools() -> None:
    helper, _state = _load_project_allowed_tools_helper()
    assert helper({}, "kirocrew") is None
    assert helper({"allowedTools": []}, "kirocrew") == []


def test_crew_harness_actually_carries_filtered_allowed_tools_descriptor() -> None:
    source = (
        ROOT
        / "integration/kiro_crew/v0.10-adaptive/overlay"
        / HARNESS_REL
    ).read_text(encoding="utf-8")
    for marker in (
        "_project_allowed_tools",
        "may_skip_gate_now",
        "allowed_tools_to_permissions",
        "withhold_hook_gated_auto_approval",
        'descriptor["allowedTools"] = allowed',
    ):
        assert marker in source


def _load_effective_member_backend_helper():
    'Compile the exact backend-only helper injected into Crew.'
    from types import SimpleNamespace
    from typing import Any

    from kiro_byo import crew_patch_common as common

    globals_ns = {
        "Any": Any,
        "ACP_BACKEND_KIRO_BYO": "kiro-byo",
    }
    exec(common._KIRO_BYO_EFFECTIVE_MEMBER_BACKEND_HELPER, globals_ns)
    return globals_ns["_kiro_byo_effective_member_backend"], SimpleNamespace


def test_crew_effective_member_backend_follows_active_byo_without_overwriting_saved_choice() -> None:
    helper, ns = _load_effective_member_backend_helper()

    selected_api = ns(agent=ns(acp_backend="kiro-byo", member_acp_backend="kas"))
    assert helper(selected_api) == "kiro-byo"
    assert helper(ns(agent=ns(acp_backend="kas", member_acp_backend="kiro-byo"))) == "kiro-byo"

    selected_official = ns(agent=ns(acp_backend="kiro", member_acp_backend="kas"))
    assert helper(selected_official) == "kas"

    helper_source = __import__("kiro_byo.crew_patch_common", fromlist=["x"])._KIRO_BYO_EFFECTIVE_MEMBER_BACKEND_HELPER
    for forbidden in ("MiMo", "DeepSeek", "Qwen", "Claude", "Gemini", "Kimi"):
        assert forbidden not in helper_source


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


def test_crew09_agents_pin_entitlement_follows_actual_member_backend() -> None:
    source = _minimal_agents_source_for_member_backend()
    patched = crew_shared_patch.patch_text("src/kiro_crew/dashboard/handlers/agents.py", source)

    assert "def _kiro_byo_effective_member_backend(" in patched
    assert "member_backend = _kiro_byo_effective_member_backend(cfg)" in patched
    assert "_kiro_byo_crew_pin_scope_reason" not in patched
    assert "backend=_pin_entitlement_backend(cfg)" in patched
    assert "if backend == ACP_BACKEND_KIRO_BYO:" in patched
    compile(patched, "agents.py", "exec")
    assert crew_shared_patch.inspect("src/kiro_crew/dashboard/handlers/agents.py", patched).state == "patched"


def test_crew09_agents_old_split_backend_patch_is_upgraded() -> None:
    source = _minimal_agents_source_for_member_backend()
    old_helper = '''def _kiro_byo_crew_pin_scope_reason(model: str, cfg: Any) -> str | None:
    return "legacy"

'''
    source = source.replace("def _pin_entitlement_backend", old_helper + "def _pin_entitlement_backend", 1)
    source = source.replace(
        "    cfg = await asyncio.to_thread(KiroCrewConfig.load)\n",
        "    cfg = await asyncio.to_thread(KiroCrewConfig.load)\n"
        "    _kiro_byo_scope_reason = _kiro_byo_crew_pin_scope_reason(model, cfg)\n"
        "    if _kiro_byo_scope_reason is not None:\n"
        "        return _kiro_byo_scope_reason\n",
        1,
    )
    patched = crew_shared_patch.patch_text("src/kiro_crew/dashboard/handlers/agents.py", source)
    assert "_kiro_byo_crew_pin_scope_reason" not in patched
    assert "_kiro_byo_scope_reason" not in patched
    assert "def _kiro_byo_effective_member_backend(" in patched



def test_crew09_agents_owned_model_helper_is_upgradeable_in_place() -> None:
    source = _minimal_agents_source_for_member_backend()
    current = crew_shared_patch.patch_text("src/kiro_crew/dashboard/handlers/agents.py", source)
    legacy = current.replace(
        "Build one stable KiroCrew model catalogue for the active BYO harness.",
        "legacy mixed model and Type catalogue",
        1,
    )
    assert legacy != current
    inspection = crew_shared_patch.inspect("src/kiro_crew/dashboard/handlers/agents.py", legacy)
    assert inspection.state == "upgradeable"
    upgraded = crew_shared_patch.patch_text("src/kiro_crew/dashboard/handlers/agents.py", legacy)
    assert upgraded == current
    assert crew_shared_patch.inspect("src/kiro_crew/dashboard/handlers/agents.py", upgraded).state == "patched"

def _minimal_members_source() -> str:
    return '''import logging
logger = logging.getLogger(__name__)

def is_member_session_key(session_key):
    return isinstance(session_key, str) and session_key.startswith("member-")

def select_provider_backend(
    session_key: str | None,
    member_backend: str,
    configured_default: str,
) -> str:
    from kiro_crew.acp_backends import resolve_selected_backend
    if is_member_session_key(session_key):
        backend = resolve_selected_backend(member_backend)
        logger.info(
            "member session %s: routing to acp_backend=%r " "(agent.member_acp_backend=%r)",
            session_key,
            backend,
            member_backend,
        )
        return backend
    return configured_default
'''


def test_crew09_member_patch_tolerates_harmless_import_and_logging_refactor() -> None:
    source = _minimal_members_source().replace(
        "    from kiro_crew.acp_backends import resolve_selected_backend\n",
        "",
        1,
    )
    source = source.replace(
        "import logging\n",
        "import logging\nfrom kiro_crew.acp_backends import resolve_selected_backend\n",
        1,
    )
    source = source.replace(
        '        logger.info(\n            "member session %s: routing to acp_backend=%r " "(agent.member_acp_backend=%r)",\n            session_key,\n            backend,\n            member_backend,\n        )\n',
        '        logger.debug("member backend=%s", backend)\n',
        1,
    )
    patched = crew_shared_patch.patch_text("src/kiro_crew/members.py", source)
    assert "# Kiro BYO: Crew owns member backend selection" in patched
    assert "configured_default == ACP_BACKEND_KIRO_BYO" not in patched
    assert crew_shared_patch.inspect("src/kiro_crew/members.py", patched).state == "patched"
    compile(patched, "members.py", "exec")


def test_crew09_backend_inheritance_root_member_and_switch_back() -> None:
    import sys
    import types

    source = crew_shared_patch.patch_text("src/kiro_crew/members.py", _minimal_members_source())
    fake = types.ModuleType("kiro_crew.acp_backends")
    fake.ACP_BACKEND_KIRO_BYO = "kiro-byo"
    fake.resolve_selected_backend = lambda value: value if value in {"kiro-byo", "kiro", "kas"} else "kiro"
    parent = types.ModuleType("kiro_crew")
    parent.acp_backends = fake
    old_parent = sys.modules.get("kiro_crew")
    old_child = sys.modules.get("kiro_crew.acp_backends")
    sys.modules["kiro_crew"] = parent
    sys.modules["kiro_crew.acp_backends"] = fake
    ns = {}
    try:
        exec(compile(source, "members.py", "exec"), ns)
        select = ns["select_provider_backend"]
        assert select("root-session", "kas", "kiro-byo") == "kiro-byo"
        assert select("spawn-worker-1", "kas", "kiro-byo") == "kiro-byo"
        assert select("member-atlas", "kas", "kiro-byo") == "kiro-byo"
        assert select("member-atlas", "kiro-byo", "kiro") == "kiro-byo"
        assert select("member-atlas", "kas", "kiro") == "kas"
        assert select("root-session", "kas", "kiro") == "kiro"
    finally:
        if old_parent is None:
            sys.modules.pop("kiro_crew", None)
        else:
            sys.modules["kiro_crew"] = old_parent
        if old_child is None:
            sys.modules.pop("kiro_crew.acp_backends", None)
        else:
            sys.modules["kiro_crew.acp_backends"] = old_child

    assert crew_shared_patch.inspect("src/kiro_crew/members.py", source).state == "patched"
    for forbidden in ("MiMo", "DeepSeek", "Qwen", "Claude", "Gemini", "Kimi"):
        assert forbidden not in source


def test_crew_harness_member_flags_widen_tools_and_grants_without_provider_logic() -> None:
    source = (ROOT / "integration/kiro_crew/v0.10-adaptive/overlay" / HARNESS_REL).read_text(encoding="utf-8")
    for marker in (
        'member_dispatch and "@kirocrew-dashboard"',
        'crew_panel and "@kirocrew-panel"',
        "agent_mod._MEMBER_DASHBOARD_GRANTS",
        "agent_mod._MEMBER_PANEL_GRANTS",
        "extra_grants=tuple(extra_grants)",
    ):
        assert marker in source
    for forbidden in ("MiMo", "DeepSeek", "Qwen", "Claude", "Gemini", "Kimi"):
        assert forbidden not in source


def test_member_grants_use_same_governance_filter_as_authored_allowed_tools() -> None:
    helper, state = _load_project_allowed_tools_helper()
    state["denied_by_governance"] = set()
    state["hook_blocked"] = False
    assert helper({}, "kirocrew", extra_grants=("@kirocrew-dashboard",)) == ["@kirocrew-dashboard"]
    state["denied_by_governance"] = {"@kirocrew-dashboard"}
    assert helper({}, "kirocrew", extra_grants=("@kirocrew-dashboard",)) == []


def _minimal_runtime_resume_source() -> str:
    return '''from kiro_crew.acp.types import (
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    METHOD_SESSION_LOAD,
)

class AcpRuntime:
    async def _kas_custom_agents(self, agent, **kwargs):
        return object()

    async def load_session(self, active_agent, member_session_key="", member_withheld=False, panel_mounted=False, session_key=""):
        kas_agents = None
        payload_snapshot = None
        if self._acp_backend == ACP_BACKEND_KAS:
            kas_extras = await self._kas_custom_agents(
                active_agent,
                member_dispatch=bool(member_session_key) and not member_withheld,
                crew_panel=panel_mounted,
                session_key=session_key,
            )
            kas_agents = kas_extras.custom_agents
        return kas_agents

    def unrelated_kas_check(self):
        if self._acp_backend == ACP_BACKEND_KAS:
            return True
        return False
'''


def test_crew09_runtime_resume_reprojects_kiro_byo_without_widening_other_kas_checks() -> None:
    source = _minimal_runtime_resume_source()
    patched = crew_shared_patch.patch_text("src/kiro_crew/acp/runtime.py", source)

    assert "ACP_BACKEND_KIRO_BYO" in patched
    assert "if self._acp_backend in {ACP_BACKEND_KAS, ACP_BACKEND_KIRO_BYO}:" in patched
    # The source patch is deliberately scoped to load_session only; unrelated KAS
    # behavior must remain bit-for-bit backend-specific.
    assert patched.count("if self._acp_backend == ACP_BACKEND_KAS:") == 1
    assert crew_shared_patch.inspect("src/kiro_crew/acp/runtime.py", patched).state == "patched"
    compile(patched, "runtime.py", "exec")


def test_markdown_agent_capability_matches_real_harness_projection_path() -> None:
    harness = (ROOT / "integration/kiro_crew/v0.10-adaptive/overlay" / HARNESS_REL).read_text(encoding="utf-8")
    # Kiro BYO does not parse Markdown itself.  Exactly like KAS, Crew owns the
    # source format and resolves it into the session projection.
    assert "agents_dir = paths.kiro_agents_dir()" in harness
    assert "kas_agents.load_agent_spec(agents_dir, agent)" in harness
    assert "kas_agents.resolve_prompt(spec, agent_id=agent, agents_dir=agents_dir)" in harness
    assert "yaml" not in harness.lower()

    patched = crew_shared_patch.patch_text(
        "src/kiro_crew/agent_sdk/backends.py",
        _minimal_backends_source(dynamic_registry=True, zero_tool_capability=True),
    )
    markdown_block = patched.split("ACP_BACKENDS_MARKDOWN_AGENT_SPECS =", 1)[1].split("\n", 1)[0]
    user_scope_block = patched.split("ACP_BACKENDS_USER_LEVEL_AGENT_SPECS_ONLY =", 1)[1].split("\n", 1)[0]
    saved_member_block = patched.split("ACP_BACKENDS_MEMBER_CAPABILITIES =", 1)[1].split("\n", 1)[0]
    assert "ACP_BACKEND_KIRO_BYO" in markdown_block
    assert "ACP_BACKEND_KIRO_BYO" in user_scope_block
    assert "ACP_BACKEND_KIRO_BYO" not in saved_member_block


def test_markdown_scope_capability_is_optional_on_older_09_source_shape() -> None:
    source = _minimal_backends_source(dynamic_registry=True, zero_tool_capability=True)
    source = "\n".join(
        line for line in source.splitlines()
        if not line.startswith("ACP_BACKENDS_USER_LEVEL_AGENT_SPECS_ONLY =")
    ) + "\n"
    patched = crew_shared_patch.patch_text("src/kiro_crew/agent_sdk/backends.py", source)
    assert "ACP_BACKEND_KIRO_BYO" in patched.split("ACP_BACKENDS_MARKDOWN_AGENT_SPECS =", 1)[1].split("\n", 1)[0]
    assert "ACP_BACKENDS_USER_LEVEL_AGENT_SPECS_ONLY" not in patched
    assert crew_shared_patch.inspect("src/kiro_crew/agent_sdk/backends.py", patched).state == "patched"


def test_crew09_chat_footer_keeps_entry_clock_only_for_kiro_byo() -> None:
    source = '''import time\n\nasync def _run_chat():\n    client = object()\n    _turn_t0 = time.monotonic()\n    # substantial pre-dispatch setup\n    _turn_t0 = time.monotonic()\n    if True:\n        if True:\n            if True:\n                if True:\n                    _turn_elapsed_ms = int(_u.duration_ms or (time.monotonic() - _turn_t0) * 1000)\n'''
    patched = crew_shared_patch.patch_text("src/kiro_crew/dashboard/chat_runner.py", source)
    # Entry assignment remains; the late reset is now conditional and therefore
    # still exists for every non-BYO backend.
    assert patched.count("_turn_t0 = time.monotonic()") == 2
    assert 'if capabilities_of(client).backend != "kiro-byo":' in patched
    assert "# Kiro BYO: user-visible elapsed includes pre-dispatch setup." in patched
    assert 'if capabilities_of(client).backend == "kiro-byo"' in patched
    assert "_u.duration_ms or (time.monotonic() - _turn_t0) * 1000" in patched
    assert crew_shared_patch.inspect("src/kiro_crew/dashboard/chat_runner.py", patched).state == "patched"
    compile(patched, "chat_runner.py", "exec")


def _minimal_chat_handlers_source() -> str:
    return '''from __future__ import annotations
import asyncio
import contextlib
import logging
from typing import Any
from kiro_crew.agent_sdk.backends import ACP_BACKENDS_MODEL_EFFORT_PAIR_IDS
from kiro_crew.dashboard.chat_persistence import (
    cap_effort_capability_levels,
    get_reasoning_effort_ordered,
    get_reasoning_effort_values,
    register_reasoning_effort_values,
)

logger = logging.getLogger(__name__)

class _ChatSlot:
    model = ""

class AcpProvider:
    pass

class DashboardState:
    pass

def _normalize_model(value):
    return str(value or "").lower()

def effective_session_key(slot):
    return "s"

def deny_app_slot_access(*args):
    return None

def slot_not_found():
    return None

class web:
    class Request:
        pass
    class Response:
        pass
    @staticmethod
    def json_response(value):
        return value

async def _configured_backend_for_slot(slot: _ChatSlot) -> str:
    return "kiro-byo"

async def api_chat_slot_selection_capabilities(request: web.Request) -> web.Response:
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if slot is None:
        return slot_not_found()
    denied = deny_app_slot_access(request.get("app", ""), slot, name, "slot_selection_capabilities")
    if denied is not None:
        return denied
    provider = state.sessions.get_provider(effective_session_key(slot))
    if not isinstance(provider, AcpProvider):
        backend = await _configured_backend_for_slot(slot)
        return web.json_response({
            "known": False,
            "model_effort_pair_ids": backend in ACP_BACKENDS_MODEL_EFFORT_PAIR_IDS,
        })
    backend = provider.capabilities.backend
    supported = provider.supports_effort()
    levels = []
    if supported and not levels:
        levels = ["low", "high"]
    return web.json_response({
        "known": True,
        "backend": backend,
        "effort_supported": supported and bool(levels),
        "effort_levels": levels,
        "model_effort_pair_ids": backend in ACP_BACKENDS_MODEL_EFFORT_PAIR_IDS,
    })

async def api_chat_slot_reasoning_effort(request: web.Request) -> web.Response:
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if slot is None:
        return slot_not_found()
    session_key = effective_session_key(slot)
    async with contextlib.AsyncExitStack():
        _live = state.sessions.get_provider(session_key)
        effort = "high"
        if effort and isinstance(_live, AcpProvider) and not _live.accepts_effort_level(effort):
            return web.json_response({"error": "not offered"})
        slot.reasoning_effort = effort
        return web.json_response({"ok": True})
'''


def test_crew09_cold_effort_bridge_uses_native_selection_capabilities_endpoint() -> None:
    relative = "src/kiro_crew/dashboard/chat_handlers.py"
    source = _minimal_chat_handlers_source()
    patched = crew_shared_patch.patch_text(relative, source)
    assert "async def _kiro_byo_cold_selection_capabilities(" in patched
    assert "from kiro_crew.agent_sdk.backends import ACP_BACKEND_KIRO_BYO" in patched
    assert "_byo_capabilities = await _kiro_byo_cold_selection_capabilities(slot)" in patched
    assert "async def _kiro_byo_type_selection_active(" in patched
    assert "suppress Crew backend fallback" in patched
    assert '"code": "effort_managed_by_type"' in patched
    # Regression for 0.1.5: the Type guard must run before Crew's generic
    # supported-but-empty fallback can synthesize a fake global effort menu.
    live_guard = patched.index("suppress Crew backend fallback")
    fallback = patched.index("if supported and not levels:")
    assert live_guard < fallback
    assert "register_reasoning_effort_values(_safe)" in patched
    assert "_cfg.resolve_model(_selected)" in patched
    # Type selections intentionally do not probe/union effort vocabularies;
    # effort belongs to the Type only after it chooses a concrete model.
    assert "_kiro_byo_route_model(" not in patched
    # Reinstall must not duplicate the helper or branch.
    repatched = crew_shared_patch.patch_text(relative, patched)
    assert repatched == patched
    assert crew_shared_patch.inspect(relative, patched).state == "patched"


def test_crew09_chat_handlers_015_bridge_is_upgradeable_in_place() -> None:
    # Reconstruct the 0.1.5 chat_handlers integration: cold bridge only, before
    # the live-provider Type guard and REST hard boundary were added.
    source = _minimal_chat_handlers_source()
    import_line = "from kiro_crew.agent_sdk.backends import ACP_BACKEND_KIRO_BYO\n"
    source = source.replace(
        "from kiro_crew.agent_sdk.backends import ACP_BACKENDS_MODEL_EFFORT_PAIR_IDS\n",
        "from kiro_crew.agent_sdk.backends import ACP_BACKENDS_MODEL_EFFORT_PAIR_IDS\n" + import_line,
        1,
    )
    source = source.replace(
        "async def _configured_backend_for_slot(slot: _ChatSlot) -> str:\n",
        crew_shared_patch._KIRO_BYO_COLD_EFFORT_HELPER
        + "\n\nasync def _configured_backend_for_slot(slot: _ChatSlot) -> str:\n",
        1,
    )
    source = source.replace(
        "        backend = await _configured_backend_for_slot(slot)\n",
        "        backend = await _configured_backend_for_slot(slot)\n"
        "        if backend == ACP_BACKEND_KIRO_BYO:\n"
        "            _byo_capabilities = await _kiro_byo_cold_selection_capabilities(slot)\n"
        "            if _byo_capabilities is not None:\n"
        "                return web.json_response(_byo_capabilities)\n",
        1,
    )
    inspection = crew_shared_patch.inspect("src/kiro_crew/dashboard/chat_handlers.py", source)
    assert inspection.state == "upgradeable"
    upgraded = crew_shared_patch.patch_text("src/kiro_crew/dashboard/chat_handlers.py", source)
    assert "suppress Crew backend fallback" in upgraded
    assert '"code": "effort_managed_by_type"' in upgraded
    assert crew_shared_patch.inspect("src/kiro_crew/dashboard/chat_handlers.py", upgraded).state == "patched"


def test_crew09_cold_effort_bridge_is_provider_and_vocabulary_neutral() -> None:
    helper = crew_shared_patch._KIRO_BYO_COLD_EFFORT_HELPER
    # The cold bridge consumes concrete-model metadata only, never provider/model
    # family names or a fixed low/medium/high ladder. Type effort stays internal.
    lower = helper.lower()
    for forbidden in ("deepseek", "anthropic", "openai"):
        assert forbidden not in lower
    for level in ("low", "medium", "high", "xhigh", "max"):
        assert f'"{level}"' not in lower
        assert f"'{level}'" not in lower
    assert "selectable_effort_levels" in helper
    assert "register_reasoning_effort_values" in helper


def test_crew09_chat_handlers_semantic_anchors_tolerate_local_renames() -> None:
    """Insider snapshots may rename/reformat locals without changing the contract."""
    relative = "src/kiro_crew/dashboard/chat_handlers.py"
    source = _minimal_chat_handlers_source()
    source = source.replace(
        "        backend = await _configured_backend_for_slot(slot)\n",
        "        configured_backend = await _configured_backend_for_slot(\n            slot\n        )\n",
        1,
    )
    source = source.replace(
        "    backend = provider.capabilities.backend\n",
        "    active_backend = provider.capabilities.backend\n",
        1,
    ).replace('"backend": backend,', '"backend": active_backend,', 1)
    source = source.replace(
        "        _live = state.sessions.get_provider(session_key)\n",
        "        current_provider = state.sessions.get_provider(\n            session_key\n        )\n",
        1,
    ).replace("isinstance(_live, AcpProvider)", "isinstance(current_provider, AcpProvider)", 1).replace(
        "_live.accepts_effort_level(effort)", "current_provider.accepts_effort_level(effort)", 1
    )

    patched = crew_shared_patch.patch_text(relative, source)
    assert "if configured_backend == ACP_BACKEND_KIRO_BYO:" in patched
    assert "if active_backend == ACP_BACKEND_KIRO_BYO" in patched
    assert "current_provider.capabilities.backend" in patched
    assert '"code": "effort_managed_by_type"' in patched
    assert crew_shared_patch.inspect(relative, patched).state == "patched"


def test_crew09_reasoning_effort_semantic_fallback_without_live_local() -> None:
    """A Crew build that inlines the provider probe remains safely patchable."""
    relative = "src/kiro_crew/dashboard/chat_handlers.py"
    source = _minimal_chat_handlers_source()
    source = source.replace(
        "        _live = state.sessions.get_provider(session_key)\n"
        "        effort = \"high\"\n"
        "        if effort and isinstance(_live, AcpProvider) and not _live.accepts_effort_level(effort):\n"
        "            return web.json_response({\"error\": \"not offered\"})\n"
        "        slot.reasoning_effort = effort\n",
        "        effort = \"high\"\n"
        "        if effort and isinstance(state.sessions.get_provider(session_key), AcpProvider):\n"
        "            pass\n"
        "        await asyncio.to_thread(_remember_reasoning_effort_for_restore, effort)\n"
        "        slot.reasoning_effort = effort\n",
        1,
    )
    patched = crew_shared_patch.patch_text(relative, source)
    marker = patched.index('"code": "effort_managed_by_type"')
    commit = patched.index("_remember_reasoning_effort_for_restore, effort")
    assert marker < commit
    assert "_byo_live = state.sessions.get_provider(effective_session_key(slot))" in patched
    assert crew_shared_patch.patch_text(relative, patched) == patched


def test_crew09_reasoning_effort_still_fails_closed_if_commit_contract_disappears() -> None:
    relative = "src/kiro_crew/dashboard/chat_handlers.py"
    source = _minimal_chat_handlers_source()
    source = source.replace(
        "        _live = state.sessions.get_provider(session_key)\n",
        "",
        1,
    )
    # No live-provider assignment and no recognized persistence commit remains.
    try:
        crew_shared_patch.patch_text(relative, source)
    except crew_shared_patch.PatchError as exc:
        assert "semantic commit anchor changed" in str(exc)
    else:
        raise AssertionError("semantic patch must fail closed when the effort commit contract disappears")
