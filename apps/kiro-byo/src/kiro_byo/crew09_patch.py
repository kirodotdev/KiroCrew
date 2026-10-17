"""Fail-closed KiroCrew 0.9 semantic integration for Kiro BYO.

KiroCrew 0.9 moved multiplexed ACP hosts behind ``HarnessAdapter``.  Kiro BYO is
itself a multi-session ACP process, so 0.9 integrates it as a shared
``AcpRuntime`` harness instead of patching the legacy per-session ``AcpClient``
per-session spawn path used by older Crew architectures.

The transform deliberately patches existing Crew files only.  Ordinary Apps do
not yet have a public third-party backend registration API, so the App can still
install/rollback this adapter transactionally while failing closed when an
upstream semantic anchor moves.
"""
from __future__ import annotations

from dataclasses import dataclass
import ast
from typing import Callable

from . import crew_patch_common as common

PatchError = common.PatchError
_parse = common._parse
_add_import_name = common._add_import_name
_add_string_to_all = common._add_string_to_all
_add_collection_expr = common._add_collection_expr
_add_dict_entry = common._add_dict_entry
_insert_after_assignment = common._insert_after_assignment
_insert_before_class_member = common._insert_before_class_member
_insert_before_top_assignment = common._insert_before_top_assignment
_import_contains_name = common._import_contains_name
_collection_string_member = common._collection_string_member
_import_span = common._import_span
_insert_after_line = common._insert_after_line
_insert_before_line = common._insert_before_line

TARGET_VERSION = "0.9.0 adaptive registry/capabilities (shared AcpRuntime)"
TARGET_FILES: tuple[str, ...] = (
    "src/kiro_crew/agent_sdk/backends.py",
    "src/kiro_crew/acp_backends.py",
    "src/kiro_crew/acp/types.py",
    "src/kiro_crew/agent_sdk/host_auth.py",
    "src/kiro_crew/agent_sdk/tool_gate.py",
    "src/kiro_crew/acp/harness/__init__.py",
    "src/kiro_crew/acp/runtime.py",
    "src/kiro_crew/providers/mirrors/registry.py",
    "src/kiro_crew/members.py",
    "src/kiro_crew/dashboard/handlers/agents.py",
)


# Kiro BYO is self-served by the bundled Crew Python bound by the App integration.
_KIRO_BYO_LAUNCH = common._KIRO_BYO_LAUNCH


# Crew 0.9 keeps one owner module per ACP host.  The Kiro BYO harness itself is
# installed from ``integration/.../overlay/.../harness/kiro_byo.py``; this
# transformer only registers that module and patches the existing capability
# seams that ordinary Apps cannot yet contribute publicly.

_KIRO_BYO_MIRROR_09 = common._KIRO_BYO_MIRROR.replace(
    "            restricted_servers=frozenset(),\n            restricted_servers=frozenset(),\n",
    "            restricted_servers=frozenset(),\n",
)


def _has_top_assignment(text: str, name: str) -> bool:
    return any(
        isinstance(node, (ast.Assign, ast.AnnAssign))
        and (
            (isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets))
            or (isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == name)
        )
        for node in _parse(text).body
    )


def _has_top_function(text: str, name: str) -> bool:
    return any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
        for node in _parse(text).body
    )


def _identifier_collection_member(text: str, variable: str, identifier: str) -> bool:
    """Return whether a top-level literal/frozenset collection contains an identifier."""
    for node in _parse(text).body:
        target_name = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            target_name = node.targets[0].id
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target_name = node.target.id
        if target_name != variable:
            continue
        value = node.value
        if isinstance(value, ast.Call) and value.args:
            value = value.args[0]
        if isinstance(value, (ast.Set, ast.List, ast.Tuple)):
            return any(isinstance(item, ast.Name) and item.id == identifier for item in value.elts)
        return False
    return False


def _patch_backends(text: str) -> str:
    # Anchor Kiro BYO beside Crew's native backend identifier rather than a
    # third-party provider/backend constant. This keeps the integration seam
    # provider-neutral and makes the dependency match the architecture: Kiro
    # API is a peer backend to native Kiro, not a derivative of another vendor.
    text = _insert_after_assignment(
        text,
        "ACP_BACKEND_KIRO",
        '''# Kiro BYO: API-native, multiplexed ACP harness. Every executable native or MCP\n# call returns through session/request_permission before execution.\nACP_BACKEND_KIRO_BYO = "kiro-byo"''',
        'ACP_BACKEND_KIRO_BYO = "kiro-byo"',
    )

    # These are verified facts for the 0.9 shared-runtime integration. Kiro BYO
    # can carry member dispatch/panel servers per session through AcpRuntime and
    # its permission-gated MCP array. It also follows KAS's Markdown-agent seam:
    # Crew parses the user-level JSON/Markdown spec and Kiro BYO consumes the
    # projected active agent over session/new and session/load.
    #
    # Do NOT add MEMBER_CAPABILITIES. Crew defines that as *full saved-spec
    # loading* with runtime activation evidence. In insider.6 the saved spec
    # includes its own model pin, while Kiro BYO deliberately owns provider/model
    # selection behind the backend boundary; claiming that row would therefore be
    # false even though prompt/tools/member dispatch are projected correctly.
    dynamic_selectable_registry = _has_top_function(text, "register_selectable_backend")
    has_zero_tool_capability = _has_top_assignment(text, "ACP_BACKENDS_HONOR_ZERO_TOOL_BAN")

    memberships = [
        "ACP_BACKENDS_KNOWN",
        "ACP_BACKENDS_INDEPENDENT_SETUP",
        "ACP_BACKENDS_SESSION_MCP_ARRAY",
        "ACP_BACKENDS_SESSION_SHARING",
        "ACP_BACKENDS_MEMBER_DISPATCH",
        "ACP_BACKENDS_MEMBER_PANEL",
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
    if not dynamic_selectable_registry:
        # Older 0.9 Insider builds predate Crew's dynamic selectable registry.
        # Keep the legacy seam only for that source shape.
        memberships.append("BASELINE_SELECTABLE_BACKENDS")
    if has_zero_tool_capability:
        # The public release/0.9 RC has a first-class name for a contract Kiro BYO
        # already implements natively: an explicit saved-agent tools=[] projection
        # hides both native and MCP tools inside the Kiro BYO harness. This is not
        # Crew AcpClient's SessionProjection.zero_tools enforcement path.
        memberships.append("ACP_BACKENDS_HONOR_ZERO_TOOL_BAN")

    # Insider.6 describes MARKDOWN_AGENT_SPECS as a host-format fact: KAS is a
    # member precisely because Crew reads the Markdown spec and sends its agent
    # projection over the wire. Kiro BYO uses the same Crew parser/resolver in
    # KiroByoHarness.session_extras(), so this is genuine format support rather
    # than a label-only capability. Its resolver intentionally reads only
    # paths.kiro_agents_dir(), matching KAS's USER_LEVEL_AGENT_SPECS_ONLY scope.
    # Gate both names on the host source so older 0.9 shapes stay fail-closed.
    if _has_top_assignment(text, "ACP_BACKENDS_MARKDOWN_AGENT_SPECS"):
        memberships.append("ACP_BACKENDS_MARKDOWN_AGENT_SPECS")
    if _has_top_assignment(text, "ACP_BACKENDS_USER_LEVEL_AGENT_SPECS_ONLY"):
        memberships.append("ACP_BACKENDS_USER_LEVEL_AGENT_SPECS_ONLY")

    for variable in memberships:
        text = _add_collection_expr(text, variable, "ACP_BACKEND_KIRO_BYO")

    text = _add_dict_entry(
        text,
        "POLICY_ID_BY_BACKEND",
        "ACP_BACKEND_KIRO_BYO:",
        "ACP_BACKEND_KIRO_BYO: ACP_BACKEND_KIRO_BYO,",
    )
    text = _add_dict_entry(
        text,
        "EFFORT_CONFIG_OPTION_IDS",
        "ACP_BACKEND_KIRO_BYO:",
        'ACP_BACKEND_KIRO_BYO: "effortLevel",',
    )
    text = _add_dict_entry(
        text,
        "_MODEL_REGISTRY_NAMESPACE_BY_BACKEND",
        "ACP_BACKEND_KIRO_BYO:",
        'ACP_BACKEND_KIRO_BYO: "kiro_byo",',
    )
    text = _insert_before_class_member(
        text,
        "Routing",
        "UNVERIFIED",
        '''    # Kiro BYO owns the executable dispatcher. Local hard-deny checks run first;\n    # fresh Crew-projected safe allowedTools grants may skip the prompt, while\n    # every unmatched native/MCP execution returns through Crew permission.\n    ACP_PERMISSION_GATE = "acp_permission_gate"''',
        'ACP_PERMISSION_GATE = "acp_permission_gate"',
    )
    text = _add_dict_entry(
        text,
        "ACP_BACKEND_ROUTING",
        "ACP_BACKEND_KIRO_BYO:",
        "ACP_BACKEND_KIRO_BYO: Routing.ACP_PERMISSION_GATE,",
    )
    text = _add_dict_entry(
        text,
        "ACP_BACKEND_LAUNCH",
        "ACP_BACKEND_KIRO_BYO:",
        _KIRO_BYO_LAUNCH,
    )

    # Crew 0.9 main moved selectable-backend truth behind a single dynamic
    # registry. Register only after every backend fact/routing table and the
    # registry functions themselves have been defined: register_selectable_backend()
    # calls routing_for() and intentionally rejects UNVERIFIED backends. This is
    # still a semantic source integration (Apps do not yet have a public arbitrary
    # backend bootstrap API), but it no longer pollutes Crew's public baseline
    # literal and therefore follows Crew's own governance/configuration contract.
    marker = "register_selectable_backend(ACP_BACKEND_KIRO_BYO)"
    if dynamic_selectable_registry and marker not in text:
        if not text.endswith("\n"):
            text += "\n"
        text += (
            "\n# Kiro BYO third-party backend registration. Keep this after Crew's registry \n"
            "# helpers so routing validation is authoritative.\n"
            f"{marker}\n"
        )
    return text


def _patch_members(text: str) -> str:
    """Make the selected Kiro BYO backend authoritative for member DM sessions.

    The integration deliberately does *not* rewrite Crew's native member-backend
    algorithm.  It adds one narrow precondition to the existing selector: when
    the configured default is Kiro BYO and the key is a member session, return
    that already-validated default immediately.  Every other backend falls
    through byte-for-byte to Crew's own logic.

    This is intentionally tolerant of harmless refactors inside the native
    member branch (logging, import placement, local variable names, formatting).
    We only require the authoritative selector and its member-session predicate
    to still exist.  If Crew moves or removes that semantic gate, fail closed.
    """
    direct_marker = "# Kiro BYO whole-Crew backend inheritance"
    if direct_marker in text:
        return text

    # A fully-installed pre-reaudit Kiro BYO used an equivalent helper rewrite.
    # Keep it accepted so upgrading the App does not classify our own previous
    # integration as foreign merely because the implementation became narrower.
    if (
        "def resolve_member_provider_backend(" in text
        and "backend = resolve_member_provider_backend(member_backend, configured_default)" in text
    ):
        return text

    tree = _parse(text)
    selector = next(
        (
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "select_provider_backend"
        ),
        None,
    )
    if selector is None or not selector.body:
        raise PatchError("Crew member backend-selection seam moved")

    arg_names = {arg.arg for arg in (*selector.args.posonlyargs, *selector.args.args, *selector.args.kwonlyargs)}
    required_args = {"session_key", "member_backend", "configured_default"}
    if not required_args.issubset(arg_names):
        raise PatchError("Crew select_provider_backend signature changed")

    has_member_predicate = any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "is_member_session_key"
        and node.args
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == "session_key"
        for node in ast.walk(selector)
    )
    if not has_member_predicate:
        raise PatchError("Crew select_provider_backend member predicate moved")

    first_stmt = selector.body[0]
    insert_after = None
    if (
        isinstance(first_stmt, ast.Expr)
        and isinstance(first_stmt.value, ast.Constant)
        and isinstance(first_stmt.value.value, str)
        and first_stmt.end_lineno is not None
    ):
        insert_after = first_stmt.end_lineno
    else:
        insert_after = first_stmt.lineno - 1

    indent = " " * (selector.col_offset + 4)
    snippet = (
        f"{indent}{direct_marker}\n"
        f"{indent}from kiro_crew.acp_backends import ACP_BACKEND_KIRO_BYO\n"
        f"{indent}if is_member_session_key(session_key) and configured_default == ACP_BACKEND_KIRO_BYO:\n"
        f"{indent}    return configured_default"
    )
    if insert_after == first_stmt.lineno - 1:
        return _insert_before_line(text, insert_after, snippet)
    return _insert_after_line(text, insert_after, snippet)


def _patch_runtime(text: str) -> str:
    """Re-project Kiro BYO's active agent on ``session/load``.

    KiroCrew 0.9's shared runtime reinitializes a resumed session's MCP/tool
    surface, but its resume projection seam is intentionally hard-coded to KAS:
    there is no public HarnessAdapter capability for "re-send this agent on
    resume" in insider.6. Kiro BYO has the same wire-registered-session shape
    and deliberately does not persist authorization grants, so a fresh Crew
    projection must ride session/load as it already rides session/new.

    This is deliberately the narrowest possible source seam: only the existing
    KAS resume-projection branch is widened. All projection/orchestration logic
    remains owned by AcpRuntime + HarnessAdapter.
    """
    text = _add_import_name(text, "kiro_crew.acp.types", "ACP_BACKEND_KIRO_BYO")
    old = "        if self._acp_backend == ACP_BACKEND_KAS:\n"
    new = (
        "        if self._acp_backend in {ACP_BACKEND_KAS, ACP_BACKEND_KIRO_BYO}:\n"
    )

    tree = _parse(text)
    load = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "load_session"
        ),
        None,
    )
    if load is None or load.end_lineno is None:
        raise PatchError("Crew AcpRuntime.load_session seam moved")

    lines = text.splitlines(keepends=True)
    start = load.lineno - 1
    end = load.end_lineno
    block = "".join(lines[start:end])
    if new in block:
        return text
    if block.count(old) != 1 or "await self._kas_custom_agents(" not in block:
        raise PatchError("Crew session/load agent re-projection seam moved")
    block = block.replace(old, new, 1)
    return "".join(lines[:start]) + block + "".join(lines[end:])


def _patch_harness_init(text: str) -> str:
    if "from kiro_crew.acp.harness.kiro_byo import KiroByoHarness" not in text:
        anchor = "from kiro_crew.acp.harness.kiro import KiroHarness\n"
        if anchor not in text:
            raise PatchError("Crew 0.9 dedicated harness import anchor moved")
        text = text.replace(
            anchor,
            anchor + "from kiro_crew.acp.harness.kiro_byo import KiroByoHarness\n",
            1,
        )
    text = _add_import_name(text, "kiro_crew.acp.types", "ACP_BACKEND_KIRO_BYO")
    text = _add_string_to_all(text, "KiroByoHarness")
    text = _add_dict_entry(
        text,
        "_HARNESSES",
        "ACP_BACKEND_KIRO_BYO:",
        "ACP_BACKEND_KIRO_BYO: KiroByoHarness,",
    )
    return text


def _patch_registry(text: str) -> str:
    text = _add_import_name(text, "kiro_crew.acp_backends", "ACP_BACKEND_KIRO_BYO")
    if "from kiro_crew.acp.session_mcp import (" not in text:
        span, node = _import_span(text, "kiro_crew.acp_backends")
        text = _insert_after_line(
            text,
            node.end_lineno or span.end,
            """from kiro_crew.acp.session_mcp import (\n    IDENTITY_BOUND_SERVERS,\n    acp_server_element,\n    session_mcp_projection,\n)\nfrom kiro_crew.agent import managed_mcp_spec_entry""",
        )
    if "from kiro_crew.providers.mirrors.base import Concern, Disposition, Ruling, SessionProjection" not in text:
        span, node = _import_span(text, "kiro_crew.providers.mirrors.base")
        text = _insert_after_line(
            text,
            node.end_lineno or span.end,
            "from kiro_crew.providers.mirrors.base import Concern, Disposition, Ruling, SessionProjection",
        )
    if "from kiro_crew.providers.mirrors.identity import control_plane_identity_env, with_env" not in text:
        span, node = _import_span(text, "kiro_crew.providers.mirrors.base")
        text = _insert_after_line(
            text,
            node.end_lineno or span.end,
            "from kiro_crew.providers.mirrors.identity import control_plane_identity_env, with_env",
        )
    text = _insert_before_top_assignment(
        text, "MIRRORS", _KIRO_BYO_MIRROR_09, "class KiroByoMirror"
    )
    text = _add_dict_entry(
        text, "MIRRORS", "ACP_BACKEND_KIRO_BYO:", "ACP_BACKEND_KIRO_BYO: KiroByoMirror,"
    )
    projection = """ACP_BACKEND_KIRO_BYO: McpProjection(
    kind=ProjectionKind.MIRROR,
    reason=(
        "Kiro BYO accepts the saved agent's per-session mcpServers array and "
        "honours the active agent's allowedTools no-prompt grants and routes every other MCP invocation through session/request_permission before execution."
    ),
    per_tool_deny=PerToolDeny.PER_CALL,
),"""
    return _add_dict_entry(text, "PROJECTIONS", "ACP_BACKEND_KIRO_BYO:", projection)


_PATCHERS: dict[str, Callable[[str], str]] = {
    "src/kiro_crew/agent_sdk/backends.py": _patch_backends,
    "src/kiro_crew/acp_backends.py": common._patch_acp_backends,
    "src/kiro_crew/acp/types.py": common._patch_types,
    "src/kiro_crew/agent_sdk/host_auth.py": common._patch_host_auth,
    "src/kiro_crew/agent_sdk/tool_gate.py": common._patch_tool_gate,
    "src/kiro_crew/acp/harness/__init__.py": _patch_harness_init,
    "src/kiro_crew/acp/runtime.py": _patch_runtime,
    "src/kiro_crew/providers/mirrors/registry.py": _patch_registry,
    "src/kiro_crew/members.py": _patch_members,
    "src/kiro_crew/dashboard/handlers/agents.py": common._patch_agents,
}


def patch_text(relative: str, text: str) -> str:
    rel = str(relative).replace("\\", "/")
    patcher = _PATCHERS.get(rel)
    if patcher is None:
        raise PatchError(f"no Crew 0.9 semantic patch registered for {rel}")
    patched = patcher(text)
    _parse(patched)
    return patched


@dataclass(frozen=True)
class TransformInspection:
    state: str  # clean | patched | upgradeable | incompatible
    detail: str = ""


_REQUIRED_MARKERS: dict[str, tuple[str, ...]] = {
    "src/kiro_crew/agent_sdk/backends.py": (
        'ACP_BACKEND_KIRO_BYO = "kiro-byo"',
        'ACP_PERMISSION_GATE = "acp_permission_gate"',
        "ACP_BACKEND_KIRO_BYO: Routing.ACP_PERMISSION_GATE",
        "ACP_BACKEND_KIRO_BYO: SelfServedLaunch(",
    ),
    "src/kiro_crew/acp_backends.py": (
        "ACP_BACKEND_KIRO_BYO",
    ),
    "src/kiro_crew/acp/types.py": (
        'PROVIDER_LABEL_KIRO_BYO = "kiro_byo"',
        "ACP_BACKEND_KIRO_BYO: PROVIDER_LABEL_KIRO_BYO",
    ),
    "src/kiro_crew/agent_sdk/host_auth.py": (
        'ENTITLEMENT_EXTERNAL_CREDENTIAL_STORE = "external_credential_store"',
        "backend=ACP_BACKEND_KIRO_BYO",
    ),
    "src/kiro_crew/agent_sdk/tool_gate.py": (
        "if routing is Routing.ACP_PERMISSION_GATE:",
    ),
    "src/kiro_crew/acp/harness/__init__.py": (
        "from kiro_crew.acp.harness.kiro_byo import KiroByoHarness",
        "ACP_BACKEND_KIRO_BYO: KiroByoHarness",
    ),
    "src/kiro_crew/acp/runtime.py": (
        "ACP_BACKEND_KIRO_BYO",
        "if self._acp_backend in {ACP_BACKEND_KAS, ACP_BACKEND_KIRO_BYO}:",
    ),
    "src/kiro_crew/providers/mirrors/registry.py": (
        "class KiroByoMirror",
        "ACP_BACKEND_KIRO_BYO: KiroByoMirror",
        "ACP_BACKEND_KIRO_BYO: McpProjection(",
    ),
    "src/kiro_crew/members.py": (
        "ACP_BACKEND_KIRO_BYO",
        "configured_default == ACP_BACKEND_KIRO_BYO",
    ),
    "src/kiro_crew/dashboard/handlers/agents.py": (
        "def _kiro_byo_models(",
        "if backend == ACP_BACKEND_KIRO_BYO:",
        "def _kiro_byo_effective_member_backend(",
        "member_backend = _kiro_byo_effective_member_backend(cfg)",
    ),
}


def inspect(relative: str, text: str) -> TransformInspection:
    rel = str(relative).replace("\\", "/")
    markers = _REQUIRED_MARKERS.get(rel)
    if markers is None:
        return TransformInspection("incompatible", f"no Crew 0.9 semantic patch registered for {rel}")

    if rel == "src/kiro_crew/acp_backends.py":
        present = [
            _import_contains_name(text, "kiro_crew.agent_sdk.backends", "ACP_BACKEND_KIRO_BYO"),
            _collection_string_member(text, "__all__", "ACP_BACKEND_KIRO_BYO"),
        ]
    elif rel == "src/kiro_crew/agent_sdk/backends.py":
        present = [marker in text for marker in markers]
        if _has_top_function(text, "register_selectable_backend"):
            present.append("register_selectable_backend(ACP_BACKEND_KIRO_BYO)" in text)
        else:
            present.append(
                _identifier_collection_member(
                    text, "BASELINE_SELECTABLE_BACKENDS", "ACP_BACKEND_KIRO_BYO"
                )
            )
        if _has_top_assignment(text, "ACP_BACKENDS_HONOR_ZERO_TOOL_BAN"):
            present.append(
                _identifier_collection_member(
                    text, "ACP_BACKENDS_HONOR_ZERO_TOOL_BAN", "ACP_BACKEND_KIRO_BYO"
                )
            )
        for variable in ("ACP_BACKENDS_MEMBER_DISPATCH", "ACP_BACKENDS_MEMBER_PANEL"):
            if _has_top_assignment(text, variable):
                present.append(
                    _identifier_collection_member(text, variable, "ACP_BACKEND_KIRO_BYO")
                )
    else:
        present = [marker in text for marker in markers]

    if any(present) and not all(present):
        return TransformInspection("incompatible", "partial/foreign Kiro BYO 0.9 patch")
    try:
        patched = patch_text(rel, text)
    except PatchError as exc:
        return TransformInspection("incompatible", str(exc))
    if all(present):
        if patched == text:
            return TransformInspection("patched")
        if rel == "src/kiro_crew/dashboard/handlers/agents.py":
            # The model-list helper is owned wholly by Kiro BYO. New releases
            # may replace that helper in place without needing Crew's pristine
            # source, while all surrounding Crew anchors remain marker-proven.
            return TransformInspection(
                "upgradeable",
                "older Kiro BYO dashboard model helper can be upgraded in place",
            )
        return TransformInspection(
            "incompatible",
            "Kiro BYO 0.9 markers are present but the semantic patch is incomplete",
        )
    if patched == text:
        return TransformInspection("incompatible", "clean source produced no Kiro BYO changes")
    return TransformInspection("clean")


def is_patched(relative: str, text: str) -> bool:
    return inspect(relative, text).state == "patched"
