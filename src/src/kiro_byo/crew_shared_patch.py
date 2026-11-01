"""Shared ACP semantic transformations used by the Crew 0.10 adapter.

Crew 0.10 uses ``HarnessAdapter`` and a shared ``AcpRuntime`` contract.
Kiro BYO registers a dedicated ACP Harness rather than patching the older
per-session ``AcpClient`` spawning path.

The transform deliberately patches existing Crew files only.  Ordinary Apps do
not yet have a public third-party backend registration API, so the App can still
install/rollback this adapter transactionally while failing closed when an
upstream semantic anchor moves.
"""
from __future__ import annotations

from dataclasses import dataclass
import ast
import re
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

TARGET_VERSION = "shared ACP/agent semantic transform for Crew 0.10"
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
    "src/kiro_crew/dashboard/chat_handlers.py",
    "src/kiro_crew/dashboard/chat_runner.py",
)


# Kiro BYO is self-served by the bundled Crew Python bound by the App integration.
_KIRO_BYO_LAUNCH = common._KIRO_BYO_LAUNCH


# Crew 0.9 keeps one owner module per ACP host.  The Kiro BYO harness itself is
# installed from ``integration/.../overlay/.../harness/kiro_byo.py``; this
# transformer only registers that module and patches the existing capability
# seams that ordinary Apps cannot yet contribute publicly.

_KIRO_BYO_MIRROR_SHARED = common._KIRO_BYO_MIRROR.replace(
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


def _assigned_local_name(node: ast.AST) -> str | None:
    """Return the simple local assigned by *node*, if the statement has one.

    Dashboard integration points deliberately key off Python semantics rather
    than exact source text. Insider builds frequently rename locals or reflow
    statements without changing the surrounding behavior; treating those
    formatter/name-only changes as incompatibilities made the integration much
    more brittle than the runtime contract itself.
    """
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return node.target.id
    if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
        return node.targets[0].id
    return None


def _assigned_value(node: ast.AST) -> ast.AST | None:
    if isinstance(node, ast.AnnAssign):
        return node.value
    if isinstance(node, ast.Assign):
        return node.value
    return None


def _unwrap_await(node: ast.AST | None) -> ast.AST | None:
    return node.value if isinstance(node, ast.Await) else node


def _call_terminal_name(node: ast.AST | None) -> str | None:
    node = _unwrap_await(node)
    if not isinstance(node, ast.Call):
        return None
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _function_node(text: str, name: str) -> ast.AsyncFunctionDef:
    tree = _parse(text)
    fn = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == name
        ),
        None,
    )
    if fn is None or fn.end_lineno is None:
        raise PatchError(f"Crew anchor missing: function {name}")
    return fn


def _semantic_assignments(fn: ast.AsyncFunctionDef) -> list[ast.Assign | ast.AnnAssign]:
    nodes = [node for node in ast.walk(fn) if isinstance(node, (ast.Assign, ast.AnnAssign))]
    return sorted(nodes, key=lambda node: (getattr(node, "lineno", 0), getattr(node, "col_offset", 0)))


def _line_indent(text: str, lineno: int) -> str:
    lines = text.splitlines()
    if lineno < 1 or lineno > len(lines):
        raise PatchError("Crew semantic anchor line is outside the source")
    line = lines[lineno - 1]
    return line[: len(line) - len(line.lstrip(" \t"))]


def _render_indented(lines: list[str], indent: str) -> str:
    return "\n".join((indent + line) if line else line for line in lines)


def _assignment_call_anchor(
    fn: ast.AsyncFunctionDef, terminal_name: str
) -> tuple[ast.Assign | ast.AnnAssign, str] | None:
    for node in _semantic_assignments(fn):
        local = _assigned_local_name(node)
        if local and _call_terminal_name(_assigned_value(node)) == terminal_name:
            return node, local
    return None


def _backend_attr_anchor(fn: ast.AsyncFunctionDef) -> tuple[ast.Assign | ast.AnnAssign, str] | None:
    """Find ``local = <provider>.capabilities.backend`` regardless of local names."""
    for node in _semantic_assignments(fn):
        local = _assigned_local_name(node)
        value = _unwrap_await(_assigned_value(node))
        if not local or not isinstance(value, ast.Attribute) or value.attr != "backend":
            continue
        capabilities = value.value
        if isinstance(capabilities, ast.Attribute) and capabilities.attr == "capabilities":
            return node, local
    return None


def _statement_with_call(
    fn: ast.AsyncFunctionDef, terminal_name: str, *, min_lineno: int = 0
) -> ast.stmt | None:
    """Return the earliest statement containing a call with *terminal_name*.

    This is a conservative fallback for source shapes that no longer assign the
    live provider to a local. It still requires a proven semantic operation in
    the same handler; if that operation disappears entirely we continue to fail
    closed rather than guessing at an insertion point.
    """
    matches: list[ast.stmt] = []
    for stmt in ast.walk(fn):
        if not isinstance(stmt, ast.stmt) or getattr(stmt, "lineno", 0) < min_lineno:
            continue
        for node in ast.walk(stmt):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
                if name == terminal_name:
                    matches.append(stmt)
                    break
    if not matches:
        return None
    return min(matches, key=lambda stmt: (stmt.lineno, stmt.col_offset))


def _statement_with_name(
    fn: ast.AsyncFunctionDef, identifier: str, *, min_lineno: int = 0
) -> ast.stmt | None:
    """Return the earliest statement that semantically references *identifier*."""
    matches: list[ast.stmt] = []
    for stmt in ast.walk(fn):
        if not isinstance(stmt, ast.stmt) or getattr(stmt, "lineno", 0) < min_lineno:
            continue
        if any(isinstance(node, ast.Name) and node.id == identifier for node in ast.walk(stmt)):
            matches.append(stmt)
    if not matches:
        return None
    return min(matches, key=lambda stmt: (stmt.lineno, stmt.col_offset))


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
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        value = node.value
        if value is None:
            return False
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
    """Preserve Crew's native member backend precedence, including overrides.

    BYO is selectable for member sessions through ``agent.member_acp_backend``.
    Choosing BYO for ordinary chat MUST NOT silently replace that selection.
    Upgrade the historical BYO-owned early return in place without modifying
    any of Crew's genuine selector branches.
    """
    marker = "# Kiro BYO: Crew owns member backend selection"
    old_marker = "# Kiro BYO whole-Crew backend inheritance"
    if old_marker in text:
        # Only remove the exact four-line block BYO previously inserted. Do not
        # guess at differently edited Crew code or swallow unexpected mutations.
        pattern = re.compile(
            r"(?m)^(?P<indent>[ \t]*)" + re.escape(old_marker) + r"\r?\n"
            r"(?P=indent)from kiro_crew\.acp_backends import ACP_BACKEND_KIRO_BYO\r?\n"
            r"(?P=indent)if is_member_session_key\(session_key\) and "
            r"configured_default == ACP_BACKEND_KIRO_BYO"
            r"(?: and backend_override is None)?:\r?\n"
            r"(?P=indent)[ \t]+return configured_default\r?\n"
        )
        text, count = pattern.subn("", text)
        if count != 1 or old_marker in text:
            raise PatchError("legacy BYO member selector override is not safe to remove")
    if "def resolve_member_provider_backend(" in text:
        # Very old 0.2.x helper rewrote the native selector itself. Only allow a
        # clean reinstall for that unknown owner shape, not a misleading patch.
        raise PatchError("legacy member selector rewrite needs clean Crew reinstall")
    tree = _parse(text)
    selector = next((node for node in tree.body
                     if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                     and node.name == "select_provider_backend"), None)
    if selector is None or not selector.body:
        raise PatchError("Crew member backend-selection seam moved")
    arg_names = {arg.arg for arg in (*selector.args.posonlyargs, *selector.args.args, *selector.args.kwonlyargs)}
    if not {"session_key", "member_backend", "configured_default"}.issubset(arg_names):
        raise PatchError("Crew select_provider_backend signature changed")
    has_member_predicate = any(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == "is_member_session_key" and node.args
        and isinstance(node.args[0], ast.Name) and node.args[0].id == "session_key"
        for node in ast.walk(selector)
    )
    if not has_member_predicate:
        raise PatchError("Crew select_provider_backend member predicate moved")
    # A marker is kept so the installer can distinguish BYO-owned, audited
    # source from foreign edits without introducing a second backend selector.
    # BYO owns the *effective* member execution route while the main harness
    # is BYO; Crew retains its original persisted member preferences, which
    # become active again when the main harness returns to official.
    guard_marker = "# Kiro BYO: enforce BYO for new member sessions (no official fallback)"
    insert_at = selector.body[0].lineno - 1
    first = selector.body[0]
    if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str) and first.end_lineno):
        insert_at = first.end_lineno
    indent = " " * (selector.col_offset + 4)
    # Respect the real per-session override when present, but do not silently
    # run it through Kiro Agent / Kiro CLI under BYO main mode.  Refusing a
    # conflicting override is safer than unexpected official credit charges.
    has_override = "backend_override" in arg_names
    guard = [
        indent + marker,
        indent + guard_marker,
        indent + 'if is_member_session_key(session_key) and configured_default == "kiro-byo":',
    ]
    if has_override:
        guard.extend([
            indent + '    if backend_override is not None and backend_override != "kiro-byo":',
            indent + '        raise RuntimeError("Kiro BYO blocked member session: explicit official backend override while BYO is active")',
        ])
    guard.extend([
        indent + '    from kiro_crew.acp_backends import resolve_selected_backend as _byo_resolve_member_backend',
        indent + '    if _byo_resolve_member_backend("kiro-byo") != "kiro-byo":',
        indent + '        raise RuntimeError("Kiro BYO member backend is unavailable; official fallback refused")',
        indent + '    return "kiro-byo"',
    ])
    # Never use the ownership marker alone as proof that a previously patched
    # selector still enforces the fail-closed route. A partially overwritten
    # install may retain the comment while losing the backend check or return.
    # Require the complete BYO-owned guard as one contiguous, exact block.
    if guard_marker in text:
        owned_block = "\n".join(guard)
        if text.count(guard_marker) != 1 or text.count(owned_block) != 1:
            raise PatchError("Crew BYO member dispatch guard is incomplete or modified")
        return text
    # The previous BYO patch had only the marker. Replace that exact line
    # in-place; do not stack guards on every upgrade. If the source is pristine,
    # insert the guard after the docstring just as the old marker was inserted.
    if marker in text:
        lines = text.splitlines(keepends=True)
        hits = [i for i, existing in enumerate(lines) if existing.strip() == marker]
        if len(hits) != 1 or not lines[hits[0]].startswith(indent):
            raise PatchError("existing BYO member selector marker is ambiguous")
        lines[hits[0]:hits[0]+1] = [line + "\n" for line in guard]
        return "".join(lines)
    if insert_at == selector.body[0].lineno - 1:
        return _insert_before_line(text, insert_at, "\n".join(guard))
    return _insert_after_line(text, insert_at, "\n".join(guard))


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



def _patch_chat_runner(text: str) -> str:
    """Make the BYO footer measure the user's whole visible turn.

    Crew starts ``_turn_t0`` at ``_run_chat`` entry, then resets the same local
    immediately before provider dispatch for its native footer metric. Kiro BYO
    has meaningful setup between those points (session acquisition, projection /
    context preparation and a possible cold start), so that reset makes the
    footer visibly shorter than the time a person actually waited after Send.

    Keep Crew's upstream timing semantics for every other backend. For Kiro BYO
    only, skip the late reset and prefer that local wall clock over a possibly
    shorter provider-reported duration at EVENT_COMPLETE. Provider telemetry is
    untouched; this changes only the dashboard turn footer / persisted turn_stats
    elapsed value for the BYO session.
    """
    marker = "# Kiro BYO: user-visible elapsed includes pre-dispatch setup."
    if marker in text:
        return text

    tree = _parse(text)
    run = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_run_chat"
        ),
        None,
    )
    if run is None or run.end_lineno is None:
        raise PatchError("Crew dashboard _run_chat seam moved")

    lines = text.splitlines(keepends=True)
    start = run.lineno - 1
    end = run.end_lineno
    block = "".join(lines[start:end])

    # The first assignment is the entry clock and must remain untouched. The
    # second one is Crew's pre-dispatch footer reset beside the per-turn stats
    # block. Match only the assignment so this works across 0.9 source shapes
    # that do or do not mirror the clock onto an auxiliary turn object.
    import re
    reset_re = re.compile(
        r"^(?P<indent>[ \t]+)_turn_t0 = time\.monotonic\(\)\n",
        flags=re.MULTILINE,
    )
    resets = list(reset_re.finditer(block))
    if len(resets) != 2:
        raise PatchError(
            f"Crew dashboard turn-clock seam changed: expected 2 assignments, found {len(resets)}"
        )
    second = resets[1]
    indent = second.group("indent")
    replacement = (
        f"{indent}{marker}\n"
        f'{indent}if capabilities_of(client).backend != "kiro-byo":\n'
        f"{indent}    _turn_t0 = time.monotonic()\n"
    )
    block = block[: second.start()] + replacement + block[second.end() :]

    # EVENT_COMPLETE normally prefers a non-zero provider duration. For BYO the
    # footer contract is explicitly visible wall time, so never let a shorter
    # upstream transport duration replace the entry clock preserved above.
    completion = (
        "                    _turn_elapsed_ms = int(_u.duration_ms or "
        "(time.monotonic() - _turn_t0) * 1000)\n"
    )
    if block.count(completion) != 1:
        raise PatchError("Crew dashboard EVENT_COMPLETE elapsed seam moved")
    completion_replacement = (
        '                    _turn_elapsed_ms = int(\n'
        '                        (time.monotonic() - _turn_t0) * 1000\n'
        '                        if capabilities_of(client).backend == "kiro-byo"\n'
        '                        else (_u.duration_ms or (time.monotonic() - _turn_t0) * 1000)\n'
        '                    )\n'
    )
    block = block.replace(completion, completion_replacement, 1)
    return "".join(lines[:start]) + block + "".join(lines[end:])

_KIRO_BYO_COLD_EFFORT_HELPER = r'''async def _kiro_byo_cold_selection_capabilities(slot: _ChatSlot) -> dict[str, Any] | None:
    """Resolve BYO effort options before the ACP session exists.

    KiroCrew normally learns effort levels from the live ACP session. A fresh
    conversation has no provider yet, so its native selection-capabilities API
    reports ``known: false`` and the composer hides the effort selector. Kiro
    BYO already has an authoritative persisted catalog for the exact selected
    model/Type, so expose only that catalog fact during this cold interval.

    Concrete models expose their own selectable vocabulary. Types deliberately
    expose no session-wide effort control: the Type resolves effort per concrete
    invocation after routing, and models without effort remain valid candidates.
    No provider/model name heuristics are used, and unknown catalog rows stay unknown.
    """
    try:
        from kiro_byo.config import load_catalog_config as _load_kiro_byo_catalog
        from kiro_byo.paths import use_app_home as _kiro_byo_use_app_home
        from kiro_byo.protocol import (
            parse_type_selection as _kiro_byo_parse_type_selection,
            type_selection_id as _kiro_byo_type_selection_id,
        )
        from kiro_crew.apps.manager import app_data_dir as _kirocrew_app_data_dir

        _catalog_path = _kirocrew_app_data_dir("kiro-byo") / "config" / "providers.json"
        with _kiro_byo_use_app_home(_catalog_path.parents[1]):
            _cfg = await asyncio.to_thread(_load_kiro_byo_catalog, _catalog_path)

        _selected = str(getattr(slot, "model", "") or "").strip()
        if not _selected or _normalize_model(_selected) == "auto":
            if getattr(_cfg, "models", None) and _cfg.mode_pack and _cfg.mode_type:
                _selected = _kiro_byo_type_selection_id(_cfg.mode_pack, _cfg.mode_type)
            else:
                _selected = str(getattr(_cfg, "default_model", "") or "").strip()

        _type = _kiro_byo_parse_type_selection(_selected)
        _levels: list[str] = []
        if _type is not None:
            if _type.mode_id != _cfg.mode_pack:
                return None
            # Type owns effort internally. Its candidate models may expose
            # different vocabularies or no effort control at all, so a global
            # slider is deliberately unavailable even though individual calls
            # can still use the selected model's real levels.
            _levels = []
        else:
            _resolved = _cfg.resolve_model(_selected)
            if _resolved is None:
                return None
            _levels = [str(v).strip().lower() for v in _resolved[1].selectable_effort_levels if str(v).strip()]

        # Register BEFORE the native POST validator sees a cold-session pick.
        # This also supports future provider-defined level names without making
        # the dashboard guess them from the model/provider name.
        _safe = cap_effort_capability_levels(_levels, source="Kiro BYO cold catalog")
        _retained = register_reasoning_effort_values(_safe)
        return {
            "known": True,
            "backend": ACP_BACKEND_KIRO_BYO,
            "effort_supported": bool(_retained),
            "effort_levels": _retained,
            "model_effort_pair_ids": False,
        }
    except Exception:
        logger.warning("Kiro BYO cold effort capability unavailable", exc_info=True)
        return None
'''


_KIRO_BYO_TYPE_SELECTION_HELPER = r'''async def _kiro_byo_type_selection_active(slot: _ChatSlot) -> bool:
    """Whether the slot's effective BYO selection is a Tiga Type.

    Type selections deliberately own reasoning effort internally. This helper
    is shared by the live selection-capabilities endpoint and the effort-write
    endpoint so KiroCrew never re-introduces its generic backend-wide fallback
    slider after a BYO ACP session has already started.

    The explicit namespaced slot selection is authoritative. For a still-empty
    or ``auto`` slot, resolve the same persisted BYO default that the cold
    capability bridge uses. Unknown/unreadable state fails closed as non-Type;
    the BYO ACP server still refuses Type effort as the final protocol boundary.
    """
    try:
        from kiro_byo.config import load_catalog_config as _load_kiro_byo_catalog
        from kiro_byo.paths import use_app_home as _kiro_byo_use_app_home
        from kiro_byo.protocol import (
            parse_type_selection as _kiro_byo_parse_type_selection,
            type_selection_id as _kiro_byo_type_selection_id,
        )
        from kiro_crew.apps.manager import app_data_dir as _kirocrew_app_data_dir

        _selected = str(getattr(slot, "model", "") or "").strip()
        if _selected and _normalize_model(_selected) != "auto":
            return _kiro_byo_parse_type_selection(_selected) is not None

        _catalog_path = _kirocrew_app_data_dir("kiro-byo") / "config" / "providers.json"
        with _kiro_byo_use_app_home(_catalog_path.parents[1]):
            _cfg = await asyncio.to_thread(_load_kiro_byo_catalog, _catalog_path)
        if getattr(_cfg, "models", None) and _cfg.mode_pack and _cfg.mode_type:
            _selected = _kiro_byo_type_selection_id(_cfg.mode_pack, _cfg.mode_type)
        else:
            _selected = str(getattr(_cfg, "default_model", "") or "").strip()
        return _kiro_byo_parse_type_selection(_selected) is not None
    except Exception:
        logger.warning("Kiro BYO Type selection probe unavailable", exc_info=True)
        return False
'''


def _patch_chat_handlers(text: str) -> str:
    """Bridge KiroCrew's cold-session effort endpoint to BYO's model catalog."""
    # Current Crew keeps this backend import on one line; add a sibling import
    # instead of rewriting upstream formatting. Idempotent across reinstall.
    import_marker = "from kiro_crew.agent_sdk.backends import ACP_BACKEND_KIRO_BYO"
    if import_marker not in text:
        span, node = _import_span(text, "kiro_crew.agent_sdk.backends")
        text = _insert_after_line(text, node.end_lineno or span.end, import_marker)

    if not _has_top_function(text, "_kiro_byo_cold_selection_capabilities"):
        tree = _parse(text)
        anchor_node = next(
            (
                node
                for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "_configured_backend_for_slot"
            ),
            None,
        )
        if anchor_node is None:
            raise PatchError("Crew anchor missing: function _configured_backend_for_slot")
        text = _insert_before_line(text, anchor_node.lineno - 1, _KIRO_BYO_COLD_EFFORT_HELPER)

    if not _has_top_function(text, "_kiro_byo_type_selection_active"):
        tree = _parse(text)
        anchor_node = next(
            (
                node
                for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "_configured_backend_for_slot"
            ),
            None,
        )
        if anchor_node is None:
            raise PatchError("Crew anchor missing: function _configured_backend_for_slot")
        text = _insert_before_line(text, anchor_node.lineno - 1, _KIRO_BYO_TYPE_SELECTION_HELPER)

    branch_marker = "_byo_capabilities = await _kiro_byo_cold_selection_capabilities(slot)"
    if branch_marker not in text:
        fn = _function_node(text, "api_chat_slot_selection_capabilities")
        anchor = _assignment_call_anchor(fn, "_configured_backend_for_slot")
        if anchor is None:
            raise PatchError("Crew cold capability semantic backend anchor changed")
        backend_stmt, backend_local = anchor
        if backend_stmt.end_lineno is None:
            raise PatchError("Crew cold capability semantic backend anchor has no span")
        indent = _line_indent(text, backend_stmt.lineno)
        snippet = _render_indented(
            [
                f"if {backend_local} == ACP_BACKEND_KIRO_BYO:",
                "    _byo_capabilities = await _kiro_byo_cold_selection_capabilities(slot)",
                "    if _byo_capabilities is not None:",
                "        return web.json_response(_byo_capabilities)",
            ],
            indent,
        )
        text = _insert_after_line(text, backend_stmt.end_lineno, snippet)

    live_marker = "# Kiro BYO Type owns reasoning effort; suppress Crew backend fallback."
    if live_marker not in text:
        fn = _function_node(text, "api_chat_slot_selection_capabilities")
        anchor = _backend_attr_anchor(fn)
        if anchor is None:
            raise PatchError("Crew live capability semantic backend anchor changed")
        backend_stmt, backend_local = anchor
        if backend_stmt.end_lineno is None:
            raise PatchError("Crew live capability semantic backend anchor has no span")
        indent = _line_indent(text, backend_stmt.lineno)
        snippet = _render_indented(
            [
                "# Kiro BYO Type owns reasoning effort; suppress Crew backend fallback.",
                f"if {backend_local} == ACP_BACKEND_KIRO_BYO and await _kiro_byo_type_selection_active(slot):",
                "    return web.json_response({",
                '        "known": True,',
                f'        "backend": {backend_local},',
                '        "effort_supported": False,',
                '        "effort_levels": [],',
                '        "model_effort_pair_ids": False,',
                "    })",
            ],
            indent,
        )
        text = _insert_after_line(text, backend_stmt.end_lineno, snippet)

    effort_marker = '"code": "effort_managed_by_type"'
    if effort_marker not in text:
        fn = _function_node(text, "api_chat_slot_reasoning_effort")
        assignments = _semantic_assignments(fn)
        session_line = 0
        for assign_node in assignments:
            if _assigned_local_name(assign_node) == "session_key" and _call_terminal_name(_assigned_value(assign_node)) == "effective_session_key":
                session_line = assign_node.lineno
                break

        live_anchor: tuple[ast.Assign | ast.AnnAssign, str] | None = None
        for assign_node in assignments:
            local = _assigned_local_name(assign_node)
            if not local or assign_node.lineno < session_line:
                continue
            if _call_terminal_name(_assigned_value(assign_node)) == "get_provider":
                live_anchor = (assign_node, local)
                break

        if live_anchor is not None:
            live_stmt, live_local = live_anchor
            if live_stmt.end_lineno is None:
                raise PatchError("Crew reasoning-effort semantic live-provider anchor has no span")
            indent = _line_indent(text, live_stmt.lineno)
            snippet = _render_indented(
                [
                    "_byo_backend = (",
                    f"    {live_local}.capabilities.backend",
                    f"    if isinstance({live_local}, AcpProvider)",
                    "    else await _configured_backend_for_slot(slot)",
                    ")",
                    "if _byo_backend == ACP_BACKEND_KIRO_BYO and await _kiro_byo_type_selection_active(slot):",
                    "    return web.json_response(",
                    "        {",
                    '            "error": "reasoning effort is managed automatically by the active Type",',
                    '            "code": "effort_managed_by_type",',
                    "        },",
                    "        status=409,",
                    "    )",
                ],
                indent,
            )
            text = _insert_after_line(text, live_stmt.end_lineno, snippet)
        else:
            # Some Insider snapshots inline the provider lookup and never bind a
            # ``_live`` local. Anchor before the first persistence operation
            # instead, still inside the handler's transactional section. This is
            # semantic and variable-name agnostic but remains fail-closed if Crew
            # removes/restructures the effort commit entirely.
            commit_stmt = _statement_with_name(
                fn, "_remember_reasoning_effort_for_restore", min_lineno=session_line
            )
            if commit_stmt is None:
                raise PatchError("Crew reasoning-effort semantic commit anchor changed")
            indent = _line_indent(text, commit_stmt.lineno)
            snippet = _render_indented(
                [
                    "_byo_live = state.sessions.get_provider(effective_session_key(slot))",
                    "_byo_backend = (",
                    "    _byo_live.capabilities.backend",
                    "    if isinstance(_byo_live, AcpProvider)",
                    "    else await _configured_backend_for_slot(slot)",
                    ")",
                    "if _byo_backend == ACP_BACKEND_KIRO_BYO and await _kiro_byo_type_selection_active(slot):",
                    "    return web.json_response(",
                    "        {",
                    '            "error": "reasoning effort is managed automatically by the active Type",',
                    '            "code": "effort_managed_by_type",',
                    "        },",
                    "        status=409,",
                    "    )",
                ],
                indent,
            )
            text = _insert_before_line(text, commit_stmt.lineno - 1, snippet)
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
        text, "MIRRORS", _KIRO_BYO_MIRROR_SHARED, "class KiroByoMirror"
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
    "src/kiro_crew/dashboard/chat_handlers.py": _patch_chat_handlers,
    "src/kiro_crew/dashboard/chat_runner.py": _patch_chat_runner,
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
        "# Kiro BYO: Crew owns member backend selection",
    ),
    "src/kiro_crew/dashboard/handlers/agents.py": (
        "def _kiro_byo_models(",
        "if backend == ACP_BACKEND_KIRO_BYO:",
        "def _kiro_byo_effective_member_backend(",
        "member_backend = _kiro_byo_effective_member_backend(cfg)",
    ),
    "src/kiro_crew/dashboard/chat_handlers.py": (
        "def _kiro_byo_cold_selection_capabilities(",
        "def _kiro_byo_type_selection_active(",
        "_byo_capabilities = await _kiro_byo_cold_selection_capabilities(slot)",
        "# Kiro BYO Type owns reasoning effort; suppress Crew backend fallback.",
        '"code": "effort_managed_by_type"',
        "from kiro_crew.agent_sdk.backends import ACP_BACKEND_KIRO_BYO",
    ),
    "src/kiro_crew/dashboard/chat_runner.py": (
        "# Kiro BYO: user-visible elapsed includes pre-dispatch setup.",
        'capabilities_of(client).backend == "kiro-byo"',
    ),
}

_LEGACY_CHAT_HANDLER_MARKERS_015: tuple[str, ...] = (
    "def _kiro_byo_cold_selection_capabilities(",
    "_byo_capabilities = await _kiro_byo_cold_selection_capabilities(slot)",
    "from kiro_crew.agent_sdk.backends import ACP_BACKEND_KIRO_BYO",
)



def inspect(relative: str, text: str) -> TransformInspection:
    rel = str(relative).replace("\\", "/")
    markers = _REQUIRED_MARKERS.get(rel)
    if markers is None:
        return TransformInspection("incompatible", f"no Crew 0.9 semantic patch registered for {rel}")

    if rel == "src/kiro_crew/members.py" and "# Kiro BYO whole-Crew backend inheritance" in text:
        try:
            upgraded = patch_text(rel, text)
        except PatchError as exc:
            return TransformInspection("incompatible", str(exc))
        if upgraded != text:
            return TransformInspection("upgradeable", "legacy BYO member backend override must be removed")

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
        if (
            rel == "src/kiro_crew/dashboard/chat_handlers.py"
            and all(marker in text for marker in _LEGACY_CHAT_HANDLER_MARKERS_015)
            and not any(
                marker in text
                for marker in (
                    "def _kiro_byo_type_selection_active(",
                    "# Kiro BYO Type owns reasoning effort; suppress Crew backend fallback.",
                    '"code": "effort_managed_by_type"',
                )
            )
        ):
            try:
                upgraded = patch_text(rel, text)
            except PatchError as exc:
                return TransformInspection("incompatible", str(exc))
            if upgraded != text:
                return TransformInspection(
                    "upgradeable",
                    "Kiro BYO 0.1.5 chat capability bridge can be tightened in place",
                )
        return TransformInspection("incompatible", "partial/foreign Kiro BYO 0.9 patch")
    try:
        patched = patch_text(rel, text)
    except PatchError as exc:
        return TransformInspection("incompatible", str(exc))
    if all(present):
        if patched == text:
            return TransformInspection("patched")
        if rel in {
            "src/kiro_crew/dashboard/handlers/agents.py",
            "src/kiro_crew/dashboard/chat_handlers.py",
            "src/kiro_crew/members.py",
        }:
            # These dashboard seams are owned by the BYO integration and can be
            # tightened in place across BYO point/hotfix releases while the
            # surrounding Crew anchors remain marker-proven.
            return TransformInspection(
                "upgradeable",
                "older Kiro BYO dashboard integration can be upgraded in place",
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
