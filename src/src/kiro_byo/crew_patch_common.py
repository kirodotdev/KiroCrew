"""Shared fail-closed source-transform helpers for KiroCrew 0.10 integration.

The helpers in this module edit small, explicitly recognized Python syntax seams.
They are intentionally formatter-tolerant but shape-sensitive: when an upstream
boundary can no longer be proven equivalent, the transform raises ``PatchError``
instead of guessing.  Every transformation is idempotent so the same primitives
can be used by both App installation and startup self-repair.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from typing import Callable


class PatchError(RuntimeError):
    """Raised when an Crew semantic anchor is no longer trustworthy."""


@dataclass(frozen=True)
class _Span:
    start: int  # zero-based line index, inclusive
    end: int    # zero-based line index, exclusive


def _parse(text: str) -> ast.Module:
    try:
        return ast.parse(text)
    except SyntaxError as exc:
        raise PatchError(f"target is not valid Python: {exc}") from exc


def _assignment_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return node.target.id
    if isinstance(node, ast.Assign):
        for target in node.targets:
            if isinstance(target, ast.Name):
                return target.id
    return None


def _top_assignment_span(text: str, name: str) -> _Span:
    tree = _parse(text)
    for node in tree.body:
        if _assignment_name(node) == name:
            if node.end_lineno is None:
                break
            return _Span(node.lineno - 1, node.end_lineno)
    raise PatchError(f"Crew anchor missing: assignment {name}")


def _import_span(text: str, module: str) -> tuple[_Span, ast.ImportFrom]:
    tree = _parse(text)
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == module:
            if node.end_lineno is None:
                break
            return _Span(node.lineno - 1, node.end_lineno), node
    raise PatchError(f"Crew anchor missing: import from {module}")


def _replace_lines(text: str, span: _Span, replacement: str) -> str:
    lines = text.splitlines(keepends=True)
    newline = "\r\n" if "\r\n" in text else "\n"
    if replacement and not replacement.endswith(("\n", "\r")):
        replacement += newline
    lines[span.start:span.end] = replacement.splitlines(keepends=True)
    return "".join(lines)


def _insert_before_line(text: str, zero_index: int, snippet: str) -> str:
    lines = text.splitlines(keepends=True)
    newline = "\r\n" if "\r\n" in text else "\n"
    normalized = snippet.replace("\r\n", "\n").replace("\r", "\n")
    if normalized and not normalized.endswith("\n"):
        normalized += "\n"
    if newline != "\n":
        normalized = normalized.replace("\n", newline)
    lines[zero_index:zero_index] = normalized.splitlines(keepends=True)
    return "".join(lines)


def _insert_after_line(text: str, zero_index_exclusive: int, snippet: str) -> str:
    return _insert_before_line(text, zero_index_exclusive, snippet)


def _add_import_name(text: str, module: str, name: str) -> str:
    span, node = _import_span(text, module)
    if any(alias.name == name for alias in node.names):
        return text
    lines = text.splitlines(keepends=True)
    # The Crew imports we patch are parenthesised. Refuse a shape change rather
    # than rewriting a one-line import and guessing at formatter semantics.
    block = "".join(lines[span.start:span.end])
    if "(" not in block or ")" not in block:
        raise PatchError(f"Crew import shape changed: {module}")
    close_line = span.end - 1
    indent = "    "
    for i in range(span.start + 1, close_line):
        stripped = lines[i].lstrip(" \t")
        if stripped and not stripped.startswith("#"):
            indent = lines[i][: len(lines[i]) - len(stripped)]
            break
    return _insert_before_line(text, close_line, f"{indent}{name},")


def _add_collection_expr(text: str, variable: str, expr: str) -> str:
    span = _top_assignment_span(text, variable)
    lines = text.splitlines(keepends=False)
    block = "\n".join(lines[span.start:span.end])
    # Avoid substring false positives (for example expr="C" inside variable
    # name "CAPS").  Backend capability expressions are either identifiers or
    # quoted string literals, so exact token/string detection is sufficient.
    if expr.isidentifier():
        if re.search(rf"(?<![\w.]){re.escape(expr)}(?![\w.])", block):
            return text
    elif expr in block:
        return text
    # All targeted Crew declarations are literal set/list/dict containers, some
    # wrapped in frozenset(). Add immediately before the innermost closing
    # container; do not touch comments or reorder upstream members.
    close = "}" if "{" in block else "]" if "[" in block else None
    if close is None:
        raise PatchError(f"Crew collection shape changed: {variable}")
    pos = block.rfind(close)
    if pos < 0:
        raise PatchError(f"Crew collection close missing: {variable}")
    line_start = block.rfind("\n", 0, pos) + 1
    before_close_on_line = block[line_start:pos]
    if "\n" in block[:pos] and not before_close_on_line.strip():
        # Closing delimiter owns its line: keep the upstream multiline style.
        closing_indent = before_close_on_line
        block = (
            block[:line_start]
            + closing_indent
            + "    "
            + expr
            + ",\n"
            + block[line_start:]
        )
    else:
        # The literal may be one-line even when wrapped by frozenset(\n ... \n),
        # as Crew does for effort capabilities. Insert immediately before
        # the literal's closing delimiter without depending on that formatting.
        before = block[:pos]
        stripped = before.rstrip()
        if stripped.endswith(("{", "[")):
            addition = expr
        elif stripped.endswith(","):
            addition = " " + expr
        else:
            addition = ", " + expr
        block = before + addition + block[pos:]
    return _replace_lines(text, span, block)


def _add_dict_entry(text: str, variable: str, key_marker: str, entry: str) -> str:
    span = _top_assignment_span(text, variable)
    lines = text.splitlines(keepends=False)
    block = "\n".join(lines[span.start:span.end])
    if key_marker in block:
        return text
    pos = block.rfind("}")
    if pos < 0:
        raise PatchError(f"Crew mapping shape changed: {variable}")
    line_start = block.rfind("\n", 0, pos) + 1
    closing_indent = block[line_start:pos]
    if closing_indent.strip():
        raise PatchError(f"Crew mapping closing shape changed: {variable}")
    item_indent = closing_indent + "    "
    rendered = "\n".join(item_indent + line if line else line for line in entry.splitlines())
    block = block[:line_start] + rendered + "\n" + block[line_start:]
    return _replace_lines(text, span, block)


def _collection_string_member(text: str, variable: str, value: str) -> bool:
    """Return whether a top-level literal collection contains *value*.

    This is intentionally quote/formatter agnostic.  The adaptive adapter used
    to look for ``repr(value)`` textually, which wrote single quotes while
    Crew formats ``__all__`` with double quotes.  That made our own output look
    like a partial/foreign patch during post-install verification.
    """
    tree = _parse(text)
    for node in tree.body:
        if _assignment_name(node) != variable:
            continue
        value_node = getattr(node, "value", None)
        if isinstance(value_node, ast.Call) and value_node.args:
            value_node = value_node.args[0]
        if isinstance(value_node, (ast.List, ast.Tuple, ast.Set)):
            return any(
                isinstance(item, ast.Constant)
                and isinstance(item.value, str)
                and item.value == value
                for item in value_node.elts
            )
        return False
    return False


def _import_contains_name(text: str, module: str, name: str) -> bool:
    tree = _parse(text)
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == module:
            if any(alias.name == name for alias in node.names):
                return True
    return False


def _add_string_to_all(text: str, value: str) -> str:
    if _collection_string_member(text, "__all__", value):
        return text
    return _add_collection_expr(text, "__all__", repr(value))


def _insert_after_assignment(text: str, anchor: str, snippet: str, marker: str) -> str:
    if marker in text:
        return text
    span = _top_assignment_span(text, anchor)
    return _insert_after_line(text, span.end, snippet)


def _insert_before_class_member(
    text: str, class_name: str, anchor_member: str, snippet: str, marker: str
) -> str:
    if marker in text:
        return text
    tree = _parse(text)
    for cls in tree.body:
        if not isinstance(cls, ast.ClassDef) or cls.name != class_name:
            continue
        for node in cls.body:
            if _assignment_name(node) == anchor_member:
                return _insert_before_line(text, node.lineno - 1, snippet)
    raise PatchError(f"Crew anchor missing: {class_name}.{anchor_member}")


def _insert_after_method(
    text: str, class_name: str, anchor_method: str, snippet: str, marker: str
) -> str:
    if marker in text:
        return text
    tree = _parse(text)
    for cls in tree.body:
        if not isinstance(cls, ast.ClassDef) or cls.name != class_name:
            continue
        for node in cls.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == anchor_method:
                if node.end_lineno is None:
                    break
                return _insert_after_line(text, node.end_lineno, snippet)
    raise PatchError(f"Crew anchor missing: {class_name}.{anchor_method}")


def _insert_before_top_assignment(text: str, variable: str, snippet: str, marker: str) -> str:
    if marker in text:
        return text
    span = _top_assignment_span(text, variable)
    return _insert_before_line(text, span.start, snippet)


def _replace_once(text: str, anchor: str, replacement: str, marker: str) -> str:
    if marker in text:
        return text
    count = text.count(anchor)
    if count != 1:
        raise PatchError(f"Crew anchor count for {anchor!r}: expected 1, got {count}")
    return text.replace(anchor, replacement, 1)


def _insert_after_each(text: str, anchor: str, snippet: str, *, expected: int, marker: str) -> str:
    # Idempotence is counted per insertion. If the expected marker lines are
    # already present, leave the file alone.
    if text.count(marker) >= expected:
        return text
    count = text.count(anchor)
    if count != expected:
        raise PatchError(f"Crew anchor count for {anchor!r}: expected {expected}, got {count}")
    return text.replace(anchor, anchor + snippet)


def _insert_after_stripped_lines(
    text: str,
    anchor: str,
    snippet: str,
    *,
    min_expected: int,
    max_expected: int = 8,
    marker: str,
) -> str:
    """Insert after every matching logical line while preserving indentation.

    Crew may grow additional session/new/load/retry construction sites over time.
    Matching every occurrence lets harmless upstream additions survive without a
    Kiro BYO release, while the bounded count still fails closed on a rewrite.
    """
    lines = text.splitlines(keepends=True)
    wanted = anchor.strip()
    matches = [i for i, line in enumerate(lines) if line.strip() == wanted]
    if not (min_expected <= len(matches) <= max_expected):
        raise PatchError(
            f"Crew stripped-line anchor count for {wanted!r}: "
            f"expected {min_expected}..{max_expected}, got {len(matches)}"
        )
    marker_count = text.count(marker)
    if marker_count == len(matches):
        return text
    if marker_count:
        raise PatchError(
            f"Crew partial Kiro BYO MCP projection: "
            f"{marker_count} marker(s) for {len(matches)} anchor(s)"
        )
    newline = "\r\n" if "\r\n" in text else "\n"
    rendered = snippet.strip()
    for index in reversed(matches):
        content = lines[index].rstrip("\r\n")
        indent = content[: len(content) - len(content.lstrip(" \t"))]
        lines.insert(index + 1, indent + rendered + newline)
    return "".join(lines)


def _insert_method_body_guard(
    text: str, class_name: str, method_name: str, snippet: str, marker: str
) -> str:
    """Insert a guard after a method docstring using AST line boundaries."""
    if marker in text:
        return text
    tree = _parse(text)
    for cls in tree.body:
        if not isinstance(cls, ast.ClassDef) or cls.name != class_name:
            continue
        for node in cls.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name != method_name:
                continue
            if not node.body:
                raise PatchError(f"Crew empty method: {class_name}.{method_name}")
            first = node.body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
                and first.end_lineno is not None
            ):
                return _insert_after_line(text, first.end_lineno, snippet)
            return _insert_before_line(text, first.lineno - 1, snippet)
    raise PatchError(f"Crew anchor missing: {class_name}.{method_name}")


_KIRO_BYO_LAUNCH = '''ACP_BACKEND_KIRO_BYO: SelfServedLaunch(
    label="Kiro BYO",
    # Reuse Kiro Crew's bundled Python. The App copies ``kiro_byo`` into this
    # interpreter's site-packages and records it in KIRO_BYO_BIN.
    binary="python",
    acp_args=("-s", "-P", "-m", "kiro_byo", "acp"),
    bin_env_var="KIRO_BYO_BIN",
    # Kiro BYO is installed as a KiroCrew App; there is no separate backend
    # package for Crew to install from this backend card.
    install_command="",
    protocol_version=1,
    missing_hint=(
        "Open Kiro BYO -> Integration, choose Install / Update integration, "
        "then restart Kiro Crew completely."
    ),
),'''


_KIRO_BYO_AUTH = '''AgentAuthDeclaration(
    backend=ACP_BACKEND_KIRO_BYO,
    credential_leaves=(),
    home_override_env_vars=(),
    adapter_own_leaves=(),
    sign_in_remedy=(
        "Configure the selected provider API credentials in Kiro BYO "
        "(for example with configure.ps1), then start a new chat."
    ),
    signed_out_message=(
        "Kiro BYO has no Crew-host login. Configure the selected provider's "
        "API credentials in Kiro BYO, then start a new chat."
    ),
    host_logout_retires_children=False,
    entitlement_source=ENTITLEMENT_EXTERNAL_CREDENTIAL_STORE,
),'''


_KIRO_BYO_MIRROR = r'''class KiroByoMirror(AgentConfigMirror):
    """Project the saved agent's MCP surface into Kiro BYO.

    Kiro BYO applies its local sandbox and hard-deny policy before execution.
    Fresh Crew-projected safe ``allowedTools`` grants can follow Kiro's no-prompt
    contract; every unmatched MCP invocation returns through
    ``session/request_permission``. The mirror can therefore preserve third-party
    servers and per-tool restrictions without confusing visibility with
    authorization. Native read/glob/grep remain harness built-ins and additionally
    pass Kiro BYO's workspace sandbox.
    """

    backend = ACP_BACKEND_KIRO_BYO

    def rulings(self) -> Mapping[Concern, Ruling]:
        D = Disposition
        return {
            Concern.MCP_SERVERS: Ruling(D.TRANSLATED, "the saved agent's session MCP servers are delivered through session/new/session/load; calls not covered by the active agent's allowedTools return to Crew for permission"),
            Concern.TOOL_ALLOWLIST: Ruling(D.TRANSLATED, "the saved agent MCP tools allowlist is applied by session_mcp_projection before the array is delivered; native built-in policy remains host-gated"),
            Concern.DENIED_TOOLS: Ruling(D.TRANSLATED, "disabledTools stays mounted and is enforced per call from the trusted MCP identity on Kiro BYO's permission round trip"),
            Concern.AUTO_APPROVE: Ruling(D.TRANSLATED, "Crew-authored customAgents allowedTools is consumed as the per-session no-prompt grant; unmatched calls still return to Crew and Kiro BYO's local hard sandbox/deny always wins"),
            Concern.MODEL: Ruling(D.DELIVERED, "model selection is delivered through the ACP model config option"),
            Concern.MODEL_ALLOWLIST: Ruling(D.WITHHELD, "Kiro BYO advertises its provider-discovered model catalog rather than consuming saved-agent availableModels"),
            Concern.PERMISSION_MODE: Ruling(D.TRANSLATED, "executable calls not pre-approved by the active agent's allowedTools reach Crew's permission gate; local hard denies remain fail-closed before either path"),
            Concern.PROMPT: Ruling(D.DELIVERED, "Crew injects the selected agent system prompt as trusted context, including Markdown-agent bodies"),
            Concern.RESOURCES: Ruling(D.DELIVERED, "Crew injects agent resources as context rather than a harness-native config file"),
            Concern.HOOKS: Ruling(D.TRANSLATED, "Kiro BYO does not execute saved-spec hooks itself; Crew keeps hook execution in its turn loop"),
        }

    def session_projection(self, agent: str | None, **kwargs: Any) -> SessionProjection:
        projection = session_mcp_projection(
            agent,
            stub_server_names=kwargs.get("stub_server_names") or (),
            work_dir=kwargs.get("work_dir"),
        )
        disabled = frozenset(projection.disabled_servers)
        identity = control_plane_identity_env(
            str(kwargs.get("session_key") or ""),
            str(kwargs.get("channel_id") or ""),
            label="kiro-byo",
            session_token=str(kwargs.get("session_token") or ""),
        )
        by_name: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for element in projection.servers:
            if not isinstance(element, dict):
                continue
            name = str(element.get("name") or "")
            if not name or name in disabled or not projection.allowlist.grants(name):
                continue
            projected = dict(element)
            if name in IDENTITY_BOUND_SERVERS:
                projected = with_env(projected, identity)
            if name not in by_name:
                order.append(name)
            by_name[name] = projected

        for name in IDENTITY_BOUND_SERVERS:
            if name in by_name or name in disabled or not projection.allowlist.grants(name):
                continue
            managed = managed_mcp_spec_entry(name, include_opt_in=True)
            element = acp_server_element(name, managed)
            if element is None:
                continue
            by_name[name] = with_env(element, identity)
            order.append(name)

        return SessionProjection(
            params={"mcpServers": [by_name[name] for name in order if name in by_name]},
            disabled_servers=disabled,
            restricted_servers=frozenset(),
            denied_tools=frozenset(projection.disabled_tools),
            derived_spec_snapshot=projection.derived_spec_snapshot,
        )
'''


_KIRO_BYO_EFFECTIVE_MEMBER_BACKEND_HELPER = r'''def _kiro_byo_effective_member_backend(cfg: Any) -> str:
    """Return effective new-member backend; do not rewrite stored preferences.

    Kiro BYO mode routes new member sessions through BYO and refuses a
    conflicting per-session official override.  Crew's saved member preference
    remains untouched and becomes effective again when exiting BYO mode.
    """
    if getattr(cfg.agent, "acp_backend", "") == ACP_BACKEND_KIRO_BYO:
        return ACP_BACKEND_KIRO_BYO
    return getattr(cfg.agent, "member_acp_backend", "")
'''


_KIRO_BYO_RESOLVED_MODEL_HELPER = r'''async def _kiro_byo_resolved_model(request: web.Request) -> web.Response:
    """Kiro BYO view of ``GET /api/agents/resolved-model``.

    Crew answers ``model: ""`` when nothing pins a model and the backend picks,
    and the dashboard then labels the chip / status bar / pin row "auto". Under
    Kiro BYO the backend's own choice for a new session is the active Mode's
    default Type (Tiga: Multi), so report that instead of a Crew "auto" that
    does not exist here. Any other backend, or any pinned model, is returned
    exactly as Crew computed it, so switching back to Kiro restores "auto".
    """
    from kiro_crew.dashboard.agent_admin.crew_records import (
        api_kirocrew_agent_resolved_model as _kiro_byo_native_resolved_model,
    )

    response = await _kiro_byo_native_resolved_model(request)
    try:
        cfg = await asyncio.to_thread(KiroCrewConfig.load)
        if getattr(cfg.agent, "acp_backend", "") != ACP_BACKEND_KIRO_BYO:
            return response
        payload = json.loads(response.body)
        model = str(payload.get("model") or "").strip()
        if model and _normalize_model_key(model) != "auto":
            return response

        from kiro_byo.config import load_catalog_config as _load_kiro_byo_catalog
        from kiro_byo.paths import use_app_home as _kiro_byo_use_app_home
        from kiro_byo.protocol import type_selection_id as _kiro_byo_type_selection_id
        from kiro_crew.apps.manager import app_data_dir as _kirocrew_app_data_dir

        _catalog_path = _kirocrew_app_data_dir("kiro-byo") / "config" / "providers.json"
        with _kiro_byo_use_app_home(_catalog_path.parents[1]):
            api_cfg = await asyncio.to_thread(_load_kiro_byo_catalog, _catalog_path)
        # Same rule the BYO runtime applies to a new session (acp_server):
        # a Type is only the default while a concrete model can execute it.
        mode_pack = str(getattr(api_cfg, "mode_pack", "") or "").strip().lower()
        mode_type = str(getattr(api_cfg, "mode_type", "") or "").strip().lower()
        if not mode_pack or not mode_type or not getattr(api_cfg, "models", None):
            return response
        payload["model"] = _kiro_byo_type_selection_id(mode_pack, mode_type)
        return web.json_response(payload)
    except Exception:
        logger.warning("Kiro BYO resolved-model default unavailable", exc_info=True)
        return response
'''

_KIRO_BYO_RESOLVED_MODEL_REBIND = (
    "# Kiro BYO: serve the BYO-aware resolver under Crew's handler name.\n"
    "api_kirocrew_agent_resolved_model = _kiro_byo_resolved_model"
)


_KIRO_BYO_MODEL_HELPER = r'''async def _kiro_byo_models(request: web.Request, configured_default: str = "") -> list[dict]:
    """Build one stable KiroCrew model catalogue for the active BYO harness.

    KiroCrew 0.10 deliberately shares one React-Query model catalogue across
    Chat, Settings and other pickers.  Returning a page-dependent list corrupts
    that shared cache and forces expensive full-page reload workarounds.  BYO
    therefore advertises one deterministic shape while it owns the harness:
    active Mode Type sentinels followed by concrete provider models.  Chat may
    select a Type; non-Chat surfaces hide those virtual rows at presentation
    time while retaining the same cached data.
    """
    model_rows: list[dict] = []
    type_rows: list[dict] = []
    seen_models: set[str] = set()
    seen_types: set[str] = set()
    concrete_ids: list[str] = []
    mode_pack = ""
    _catalog_home = None
    _catalog_error = False
    _has_configured_providers = False

    # A catalogue refresh is the deterministic signal that KiroCrew has moved
    # onto the BYO harness.  Reconcile routing-only state before returning the
    # new list so an already-open Jev conversation updates in place instead of
    # waiting for a new chat.  Older Crew adapters may not provide the helper,
    # so this bridge is deliberately capability-checked.
    try:
        _state = request.app.get("state")
        if _state is not None:
            from kiro_crew.dashboard import chat_handlers as _kiro_byo_chat_handlers

            _reconcile = getattr(_kiro_byo_chat_handlers, "_kiro_byo_reconcile_slot_routing", None)
            if callable(_reconcile):
                await _reconcile(_state)
    except Exception:
        logger.debug("Kiro BYO slot routing reconciliation unavailable", exc_info=True)

    # Deliberately no request/query/referer surface detection here.  The model
    # list must have one stable shape for KiroCrew's shared React-Query cache.

    try:
        from kiro_byo.paths import use_app_home as _kiro_byo_use_app_home
        from kiro_crew.apps.manager import app_data_dir as _kirocrew_app_data_dir
        from kiro_byo.config import load_catalog_config as _load_kiro_byo_catalog

        _catalog_path = _kirocrew_app_data_dir("kiro-byo") / "config" / "providers.json"
        _catalog_home = _catalog_path.parents[1]
        with _kiro_byo_use_app_home(_catalog_home):
            api_cfg = await asyncio.to_thread(_load_kiro_byo_catalog, _catalog_path)
        _has_configured_providers = bool(getattr(api_cfg, "providers", ()))
        mode_pack = str(getattr(api_cfg, "mode_pack", "") or "").strip().lower()

        for public_id, model in api_cfg.public_models:
            public_id = str(public_id or "").strip()
            if not public_id or _normalize_model_key(public_id) == "auto" or public_id in seen_models:
                continue
            seen_models.add(public_id)
            concrete_ids.append(public_id)
            model_rows.append(
                {
                    "model_name": public_id,
                    "display_name": model.name,
                    "description": "",
                    "context_window": (
                        model.context_window
                        or model_registry.model_window(public_id)
                        or 0
                    ),
                    "kiro_byo_effort_levels": list(model.selectable_effort_levels),
                    "kiro_byo_reasoning_mode": str(model.reasoning_mode or "unknown"),
                    "kiro_byo_capability_source": str(model.capability_source or "unknown"),
                    "kiro_byo_catalog_source": str(model.catalog_source or "unknown"),
                }
            )

        if model_rows and mode_pack:
            from kiro_byo.policy_packs import get_policy_pack as _kiro_byo_get_policy_pack
            from kiro_byo.protocol import type_selection_id as _kiro_byo_type_selection_id
            with _kiro_byo_use_app_home(_catalog_home):
                _pack = _kiro_byo_get_policy_pack(mode_pack)
            if _pack is not None and getattr(_pack, "enabled", True):
                for _policy in _pack.policies:
                    _wire = _kiro_byo_type_selection_id(_pack.id, _policy.id)
                    if _wire in seen_types:
                        continue
                    seen_types.add(_wire)
                    type_rows.append(
                        {
                            "model_name": _wire,
                            "display_name": _policy.display_name,
                            "description": _policy.short_description,
                            "context_window": 0,
                        }
                    )
    except Exception:
        _catalog_error = True
        logger.warning(
            "Kiro BYO dashboard provider catalog unavailable; using ACP advertisement",
            exc_info=True,
        )

    # ACP is the runtime fallback.  Prefer local active-pack metadata for Type
    # labels, but accept runtime-advertised Type sentinels when that metadata is
    # unavailable.  Concrete advertised-model persistence remains concrete-only.
    # ACP advertisement is a recovery path ONLY if reading BYO's catalogue
    # failed.  A successfully read empty provider list is authoritative; old
    # session advertisements must not resurrect models after a provider removal.
    if _catalog_error:
        namespace = model_registry_namespace(ACP_BACKEND_KIRO_BYO)
        advertised = _advertised_cc_models(request, namespace)
        if not advertised:
            advertised = [
                {"model_name": m, "display_name": m, "description": ""}
                for m in model_registry.advertised_models(namespace)
            ]
        for entry in advertised:
            name = str(entry.get("model_name", "") or "").strip()
            if not name or _normalize_model_key(name) == "auto":
                continue
            if name.startswith("kiro-byo-type:"):
                if not type_rows and name not in seen_types:
                    seen_types.add(name)
                    type_rows.append(
                        {
                            "model_name": name,
                            "display_name": entry.get("display_name") or name,
                            "description": entry.get("description") or "",
                            "context_window": 0,
                        }
                    )
                continue
            if model_rows or name in seen_models:
                continue
            seen_models.add(name)
            concrete_ids.append(name)
            model_rows.append(
                {
                    "model_name": name,
                    "display_name": entry.get("display_name") or name,
                    "description": entry.get("description") or "",
                    "context_window": model_registry.model_window(name) or 0,
                }
            )

    if concrete_ids:
        try:
            namespace = model_registry_namespace(ACP_BACKEND_KIRO_BYO)
            if model_registry.refresh_advertised_models(namespace, concrete_ids):
                await asyncio.to_thread(model_registry.persist_advertised_models)
        except Exception:
            logger.warning("Kiro BYO advertised-model cache refresh failed", exc_info=True)

    if model_rows:
        return type_rows + model_rows

    # configured_default belongs to the Crew host config, not necessarily
    # to the active BYO Provider catalogue.  Never launder a stale KAS/CLI
    # model pin back into the selectable BYO model list.
    # A genuine fresh install with no providers can legitimately have an empty
    # catalogue. A broken catalogue or configured-but-empty provider must NOT
    # look healthy (HTTP 200 []) to the Crew agent/model editors.
    if _catalog_error or _has_configured_providers:
        raise web.HTTPServiceUnavailable(
            text="Kiro BYO model catalogue unavailable; check BYO Provider configuration and logs"
        )
    return []
'''


def _patch_acp_backends(text: str) -> str:
    text = _add_import_name(text, "kiro_crew.agent_sdk.backends", "ACP_BACKEND_KIRO_BYO")
    return _add_string_to_all(text, "ACP_BACKEND_KIRO_BYO")


def _patch_types(text: str) -> str:
    text = _add_import_name(text, "kiro_crew.acp_backends", "ACP_BACKEND_KIRO_BYO")
    if 'PROVIDER_LABEL_KIRO_BYO = "kiro_byo"' not in text:
        # Keep Kiro BYO's persisted-session identity beside Crew's native/default
        # label instead of anchoring the integration to any third-party backend.
        span = _top_assignment_span(text, "PROVIDER_LABEL_DEFAULT")
        text = _insert_after_line(text, span.end, 'PROVIDER_LABEL_KIRO_BYO = "kiro_byo"')
    text = _add_dict_entry(
        text,
        "PROVIDER_LABEL_BY_BACKEND",
        "ACP_BACKEND_KIRO_BYO:",
        "ACP_BACKEND_KIRO_BYO: PROVIDER_LABEL_KIRO_BYO,",
    )
    return text


def _patch_host_auth(text: str) -> str:
    text = _add_import_name(text, "kiro_crew.agent_sdk.backends", "ACP_BACKEND_KIRO_BYO")
    text = _insert_after_assignment(
        text,
        "ENTITLEMENT_HOST_VAULT",
        '''#: Provider credentials live in Kiro BYO's external OS credential store.
#: Crew neither owns nor reads that store, and no credential file needs to be
#: re-exposed inside the agent sandbox.
ENTITLEMENT_EXTERNAL_CREDENTIAL_STORE = "external_credential_store"''',
        'ENTITLEMENT_EXTERNAL_CREDENTIAL_STORE = "external_credential_store"',
    )
    text = _add_dict_entry(
        text,
        "ENTITLEMENT_LABELS",
        "ENTITLEMENT_EXTERNAL_CREDENTIAL_STORE:",
        'ENTITLEMENT_EXTERNAL_CREDENTIAL_STORE: "Kiro BYO credential store",',
    )
    text = _add_collection_expr(
        text, "ENTITLEMENT_SOURCES", "ENTITLEMENT_EXTERNAL_CREDENTIAL_STORE"
    )
    text = _add_string_to_all(text, "ENTITLEMENT_EXTERNAL_CREDENTIAL_STORE")
    text = _add_dict_or_tuple_auth(text)
    return text


def _add_dict_or_tuple_auth(text: str) -> str:
    if "backend=ACP_BACKEND_KIRO_BYO" in text:
        return text
    span = _top_assignment_span(text, "AGENT_AUTH_DECLARATIONS")
    lines = text.splitlines(keepends=False)
    block = "\n".join(lines[span.start:span.end])
    # Tuple starts with ``(``; insert before the first AgentAuthDeclaration so the
    # declaration remains easy to audit and parity tests see it immediately.
    marker = "    AgentAuthDeclaration("
    pos = block.find(marker)
    if pos < 0:
        raise PatchError("Crew AGENT_AUTH_DECLARATIONS shape changed")
    rendered = "\n".join("    " + line if line else line for line in _KIRO_BYO_AUTH.splitlines()) + "\n"
    block = block[:pos] + rendered + block[pos:]
    return _replace_lines(text, span, block)


def _insert_after_assignment_in_function(
    text: str, function_name: str, assignment_name: str, snippet: str, marker: str
) -> str:
    if marker in text:
        return text
    tree = _parse(text)
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name != function_name:
            continue
        for child in node.body:
            if _assignment_name(child) == assignment_name:
                if child.end_lineno is None:
                    break
                return _insert_after_line(text, child.end_lineno, snippet)
    raise PatchError(
        f"Crew anchor missing: {function_name} assignment {assignment_name}"
    )


def _patch_tool_gate(text: str) -> str:
    # Deliberately DO NOT add ACP_PERMISSION_GATE to ENFORCED_ROUTINGS. In
    # Crew that set is also coupled to Crew's OS credential-mask sandbox.
    # Native Windows has no supported sandbox backend, so marking Kiro BYO as
    # enforced would make an otherwise correctly permission-routed ACP harness
    # impossible to select there. Kiro BYO's own dispatcher asks Crew through
    # session/request_permission for calls not covered by a fresh, Crew-projected
    # safe allowedTools grant; local hard denies still run first.
    insertion = '''
    if routing is Routing.ACP_PERMISSION_GATE:
        return (
            Verdict.ROUTED,
            "the harness owns the executable tool dispatcher: active-agent allowedTools matches follow Kiro's no-prompt contract, while every other executable native/MCP call routes through session/request_permission before execution",
        )
'''
    return _insert_after_assignment_in_function(
        text,
        "routing_verdict",
        "routing",
        insertion,
        "if routing is Routing.ACP_PERMISSION_GATE:",
    )


def _replace_owned_top_function(text: str, name: str, snippet: str) -> str:
    """Replace a top-level Kiro BYO-owned helper without touching Crew code."""
    tree = _parse(text)
    node = next(
        (
            item
            for item in tree.body
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == name
        ),
        None,
    )
    if node is None:
        return text
    if node.end_lineno is None:
        raise PatchError(f"Kiro BYO helper {name} has no source span")
    current = "".join(text.splitlines(keepends=True)[node.lineno - 1 : node.end_lineno])
    wanted = snippet.rstrip("\r\n") + ("\r\n" if "\r\n" in text else "\n")
    if current.replace("\r\n", "\n") == wanted.replace("\r\n", "\n"):
        return text
    return _replace_lines(text, _Span(node.lineno - 1, node.end_lineno), snippet.rstrip("\r\n"))


def _patch_models_backend_reconcile(text: str) -> str:
    """Reconcile native and BYO catalog reads, not just BYO-specific ones."""
    marker = "# Kiro BYO: reconcile idle sessions on every backend catalogue read."
    tree = _parse(text)
    matches = [node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
               and node.name == "api_models"]
    if len(matches) != 1:
        raise PatchError("Crew api_models entry moved or duplicated")
    fn = matches[0]
    if marker in text:
        if marker not in "".join(text.splitlines(keepends=True)[fn.lineno - 1:fn.end_lineno]):
            raise PatchError("Kiro BYO model reconciliation marker outside api_models")
        return text
    first = fn.body[0]
    after = (first.end_lineno if isinstance(first, ast.Expr) and
             isinstance(first.value, ast.Constant) and isinstance(first.value.value, str)
             else fn.lineno)
    snippet = (
        "    # Kiro BYO: reconcile idle sessions on every backend catalogue read.\n"
        "    try:\n"
        "        _byo_state = request.app.get('state')\n"
        "        if _byo_state is not None:\n"
        "            from kiro_crew.dashboard import chat_handlers as _byo_chat\n"
        "            _byo_reconcile = getattr(_byo_chat, '_kiro_byo_reconcile_slot_routing', None)\n"
        "            if callable(_byo_reconcile):\n"
        "                await _byo_reconcile(_byo_state)\n"
        "    except Exception:\n"
        "        logger.debug('Kiro BYO backend slot sync unavailable', exc_info=True)\n"
    )
    return _insert_after_line(text, after, snippet)


def _patch_agents(text: str) -> str:
    text = _add_import_name(text, "kiro_crew.acp_backends", "ACP_BACKEND_KIRO_BYO")

    # Upgrade the older split-backend Kiro BYO patch when encountered. That
    # Upgrade historical BYO-owned model pin guards without widening member DM routing.
    if "def _kiro_byo_crew_pin_scope_reason(" in text:
        tree = _parse(text)
        legacy = next(
            (node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
             and node.name == "_kiro_byo_crew_pin_scope_reason"),
            None,
        )
        if legacy is None or legacy.end_lineno is None:
            raise PatchError("legacy Kiro BYO model-scope helper is malformed")
        text = _replace_lines(text, _Span(legacy.lineno - 1, legacy.end_lineno), "")
        legacy_guard = (
            "    _kiro_byo_scope_reason = _kiro_byo_crew_pin_scope_reason(model, cfg)\n"
            "    if _kiro_byo_scope_reason is not None:\n"
            "        return _kiro_byo_scope_reason\n"
        )
        text = text.replace(legacy_guard, "", 1)

    if "def _kiro_byo_effective_member_backend(" in text:
        text = _replace_owned_top_function(text, "_kiro_byo_effective_member_backend",
                                           _KIRO_BYO_EFFECTIVE_MEMBER_BACKEND_HELPER)
    else:
        text = _insert_before_function_name(
            text, "_pin_entitlement_backend",
            _KIRO_BYO_EFFECTIVE_MEMBER_BACKEND_HELPER,
            "def _kiro_byo_effective_member_backend(",
        )
    old_member_assignment = '    member_backend = getattr(cfg.agent, "member_acp_backend", "")\n'
    new_member_assignment = '    member_backend = _kiro_byo_effective_member_backend(cfg)\n'
    if new_member_assignment not in text:
        if old_member_assignment not in text:
            raise PatchError("Crew member model-entitlement backend seam moved")
        text = text.replace(old_member_assignment, new_member_assignment, 1)
    if "def _kiro_byo_models(" in text:
        # This helper is Kiro BYO-owned, so it is safe to upgrade in place even
        # when the surrounding Crew seam was patched by an older BYO release.
        text = _replace_owned_top_function(text, "_kiro_byo_models", _KIRO_BYO_MODEL_HELPER)
    else:
        text = (
            _insert_before_top_assignment(
                text,
                "_LIST_MODELS_TIMEOUT",
                _KIRO_BYO_MODEL_HELPER,
                "def _kiro_byo_models(",
            )
            if "_LIST_MODELS_TIMEOUT" in text
            else _insert_before_function_name(
                text, "_wrap_list_models_argv", _KIRO_BYO_MODEL_HELPER, "def _kiro_byo_models("
            )
        )
    text = _patch_agents_resolved_model(text)
    text = _patch_models_backend_reconcile(text)
    if "if backend == ACP_BACKEND_KIRO_BYO:" in text:
        return text

    branch = '''    if backend == ACP_BACKEND_KIRO_BYO:
        return web.json_response(
            await _kiro_byo_models(request, configured_default=_scoped_default(cfg, backend))
        )
'''
    old_anchor = '''    if backend == ACP_BACKEND_CODEX:
        return web.json_response(
            _codex_models(request, configured_default=_scoped_default(cfg, backend))
        )
'''
    if old_anchor in text:
        return text.replace(old_anchor, old_anchor + branch, 1)

    # Current Crew 0.9 release/Insider source no longer gives Codex a bespoke arm;
    # model discovery is driven by SessionCapabilities.resolves_model_from_advertised_list.
    # Kiro BYO still
    # owns a persisted provider catalog, so put its branch immediately before the
    # generic advertised-list path.
    generic_anchor = "    if capabilities_for(backend).resolves_model_from_advertised_list:\n"
    count = text.count(generic_anchor)
    if count != 1:
        raise PatchError(
            "Crew model-discovery boundary changed: "
            f"expected one advertised-list capability branch, got {count}"
        )
    return text.replace(generic_anchor, branch + generic_anchor, 1)


def _patch_agents_resolved_model(text: str) -> str:
    """Install/upgrade the BYO-aware ``resolved-model`` handler.

    ``handlers/__init__`` re-exports ``api_kirocrew_agent_resolved_model`` from
    this module, and the route table reads it from there. Rebinding the name
    here (after Crew's own import) therefore swaps the handler without touching
    Crew's ``crew_records`` module. The wrapper defers to Crew for every
    non-BYO backend, so switching back to Kiro restores native behaviour.
    """
    if "def _kiro_byo_resolved_model(" in text:
        text = _replace_owned_top_function(
            text, "_kiro_byo_resolved_model", _KIRO_BYO_RESOLVED_MODEL_HELPER
        )
    else:
        tree = _parse(text)
        anchor = next(
            (
                node
                for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "_kiro_byo_models"
            ),
            None,
        )
        if anchor is None or anchor.end_lineno is None:
            raise PatchError("Kiro BYO model helper missing; cannot place resolved-model helper")
        text = _insert_after_line(
            text, anchor.end_lineno, "\n\n" + _KIRO_BYO_RESOLVED_MODEL_HELPER.rstrip("\n")
        )
    if _KIRO_BYO_RESOLVED_MODEL_REBIND not in text:
        imported = any(
            isinstance(node, ast.ImportFrom)
            and node.module == "kiro_crew.dashboard.agent_admin.crew_records"
            and any(alias.name == "api_kirocrew_agent_resolved_model" for alias in node.names)
            for node in _parse(text).body
        )
        if not imported:
            raise PatchError("Crew resolved-model handler import seam moved")
        tree = _parse(text)
        helper = next(
            node
            for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_kiro_byo_resolved_model"
        )
        if helper.end_lineno is None:
            raise PatchError("Crew resolved-model helper has no line span")
        text = _insert_after_line(
            text, helper.end_lineno, "\n\n" + _KIRO_BYO_RESOLVED_MODEL_REBIND
        )
    return text


def _insert_before_function_name(text: str, function_name: str, snippet: str, marker: str) -> str:
    if marker in text:
        return text
    tree = _parse(text)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name:
            return _insert_before_line(text, node.lineno - 1, snippet)
    raise PatchError(f"Crew anchor missing: function {function_name}")


