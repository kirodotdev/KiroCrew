"""KiroCrew 0.11.0-rc.1 *source-specific* ACP adapter patch.

0.11 moved capability declarations out of backends.py into leaf owner modules
and split session/load and member backend selection. Never recycle the 0.10
patcher against that different source. Every touched seam is independently
validated and the installer stages/compiles the whole set before writing.

This candidate registers BYO for Mate Worker Kit when its projection seam
matches the RC1 source contract. Live Windows Worker execution and tool-scope
end-to-end verification remain pending; the installer still fails closed on
unknown upstream source layouts.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import crew10_patch as legacy
from . import crew_shared_patch as shared
from . import crew_patch_common as c

PatchError = c.PatchError
_P = 'src/kiro_crew/'
_CORE = _P+'agent_sdk/backends.py'
_OWNER_MEMBERS = _P+'members_parts/member_sessions.py'
_OWNER_LOAD = _P+'acp/runtime_session_load.py'
_HANDLERS = _P+'dashboard/chat_handlers.py'
_LOADER_BUILDERS = _P+'config/loader_builders.py'
# Crew 0.11 split the turn clock and completion footer out of chat_runner.py.
_DISPATCH_CLOCK = _P+'dashboard/chat_turn/turn_dispatch_gates.py'
_TURN_FOOTER = _P+'dashboard/chat_turn/turn_stats.py'

_CAPABILITIES = {
    'backends_mcp.py': (
        'ACP_BACKENDS_SESSION_MCP_ARRAY',
        'ACP_BACKENDS_MARKDOWN_AGENT_SPECS',
        'ACP_BACKENDS_USER_LEVEL_AGENT_SPECS_ONLY',
        'ACP_BACKENDS_HONOR_ZERO_TOOL_BAN',
    ),
    'backends_members.py': ('ACP_BACKENDS_MEMBER_DISPATCH','ACP_BACKENDS_MEMBER_PANEL','ACP_BACKENDS_MATE_WORKER_KIT'),
    'backends_sessions.py': (
        'ACP_BACKENDS_SESSION_SHARING','ACP_BACKENDS_ACP_RUNTIME',
        'ACP_BACKENDS_SESSION_EVICTION','ACP_BACKENDS_HARNESS_OWNED_SESSIONS',
    ),
    'backends_turns.py': ('ACP_BACKENDS_STEER','ACP_BACKENDS_COMPACT','ACP_BACKENDS_INLINE_COMPACTION'),
    'backends_models.py': (
        'ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION','ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION',
        'ACP_BACKENDS_EFFORT_FROM_ADVERTISED_OPTION','ACP_BACKENDS_ADVERTISED_MODEL_SELECTION',
    ),
    'backends_routing.py': ('ACP_BACKENDS_SIDE_READONLY','ACP_BACKENDS_CREW_FIRES_SPEC_HOOKS'),
}

TARGET_FILES = (
    _CORE,
    *(_P+'agent_sdk/'+name for name in _CAPABILITIES),
    _P+'agent_sdk/backends_launch.py',
    _P+'acp_backends.py', _P+'acp/types.py',
    _P+'agent_sdk/host_auth.py', _P+'agent_sdk/tool_gate.py',
    _P+'acp/harness/__init__.py', _OWNER_LOAD,
    _P+'providers/mirrors/registry.py',_OWNER_MEMBERS,_LOADER_BUILDERS,
    _P+'dashboard/handlers/agents.py',
    _P+'dashboard/chat_api/slot_effort.py',_HANDLERS,
    _P+'dashboard/chat_title.py',
    _DISPATCH_CLOCK, _TURN_FOOTER,
)


def _core(text: str) -> str:
    text = c._insert_after_assignment(text, 'ACP_BACKEND_KIRO',
        '# Kiro BYO registered ACP backend (Crew 0.11 RC1 source contract)\n'
        'ACP_BACKEND_KIRO_BYO = "kiro-byo"',
        'ACP_BACKEND_KIRO_BYO = "kiro-byo"')
    for name in ('ACP_BACKENDS_KNOWN','ACP_BACKENDS_INDEPENDENT_SETUP'):
        text = c._add_collection_expr(text,name,'ACP_BACKEND_KIRO_BYO')
    text = c._add_dict_entry(text,'POLICY_ID_BY_BACKEND',
        'ACP_BACKEND_KIRO_BYO:', 'ACP_BACKEND_KIRO_BYO: ACP_BACKEND_KIRO_BYO,')
    marker='register_selectable_backend(ACP_BACKEND_KIRO_BYO)'
    if marker not in text:
        text=text.rstrip('\n')+'\n\n# Kiro BYO registers after Crew imports its routing owner.\n'+marker+'\n'
    return text


def _owner(text: str, name: str) -> str:
    text=c._add_import_name(text,'kiro_crew.agent_sdk.backends','ACP_BACKEND_KIRO_BYO')
    for cap in _CAPABILITIES[name]:
        text=c._add_collection_expr(text,cap,'ACP_BACKEND_KIRO_BYO')
    if name=='backends_models.py':
        text=c._add_dict_entry(text,'EFFORT_CONFIG_OPTION_IDS','ACP_BACKEND_KIRO_BYO:',
            'ACP_BACKEND_KIRO_BYO: "effortLevel",')
        text=c._add_dict_entry(text,'_MODEL_REGISTRY_NAMESPACE_BY_BACKEND',
            'ACP_BACKEND_KIRO_BYO:', 'ACP_BACKEND_KIRO_BYO: "kiro_byo",')
    if name=='backends_routing.py':
        text=c._insert_before_class_member(text,'Routing','UNVERIFIED',
            '    # Every BYO native and MCP tool execution invokes Crew permission.\n'
            '    ACP_PERMISSION_GATE = "acp_permission_gate"',
            'ACP_PERMISSION_GATE = "acp_permission_gate"')
        text=c._add_dict_entry(text,'ACP_BACKEND_ROUTING','ACP_BACKEND_KIRO_BYO:',
            'ACP_BACKEND_KIRO_BYO: Routing.ACP_PERMISSION_GATE,')
    return text


def _launch(text: str) -> str:
    text=c._add_import_name(text,'kiro_crew.agent_sdk.backends','ACP_BACKEND_KIRO_BYO')
    return c._add_dict_entry(text,'ACP_BACKEND_LAUNCH','ACP_BACKEND_KIRO_BYO:',c._KIRO_BYO_LAUNCH)


def _load(text: str) -> str:
    import_anchor='from kiro_crew.acp.types import ACP_BACKEND_KAS, METHOD_SESSION_LOAD, JsonRpcMessage'
    if 'from kiro_crew.agent_sdk.backends import ACP_BACKEND_KIRO_BYO' not in text:
        if text.count(import_anchor)!=1:
            raise PatchError('0.11 session/load backend import moved')
        text=text.replace(import_anchor,import_anchor+'\nfrom kiro_crew.agent_sdk.backends import ACP_BACKEND_KIRO_BYO',1)
    original='    if self._acp_backend == ACP_BACKEND_KAS:\n'
    patched='    if self._acp_backend in {ACP_BACKEND_KAS, ACP_BACKEND_KIRO_BYO}:\n'
    import ast
    tree=c._parse(text)
    fn=next((n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='load_session'),None)
    if fn is None or fn.end_lineno is None: raise PatchError('0.11 session/load owner shape moved')
    lines=text.splitlines(keepends=True)
    block=''.join(lines[fn.lineno-1:fn.end_lineno])
    if patched in block:return text
    if block.count(original)!=1 or 'await self._kas_custom_agents(' not in block:
        raise PatchError('0.11 session/load projection guard moved')
    block=block.replace(original,patched,1)
    return ''.join(lines[:fn.lineno-1])+block+''.join(lines[fn.end_lineno:])



def _loader_builders(text: str) -> str:
    """In BYO main mode, use BYO for *new* member sessions only.

    Crew still owns the native model selection and persisted member backend.
    The effective member backend cannot be a stale explicit KAS/CLI preference
    while BYO is active. Switching back to official reactivates the original
    preference without altering any user config or running session.
    """
    marker = '# Kiro BYO: implicit member harness follows the BYO main harness.'
    strict = '# Kiro BYO: enforce BYO member backend while main BYO is active.'
    old = ("    value = backend_from_stored(agent_data.get(key))\n"
           "    if value is None:\n"
           "        default = section.default(key)\n"
           "        value = default if default in selectable_backends() else None\n")
    prior = ("    value = backend_from_stored(agent_data.get(key))\n"
             "    if value is None:\n"
             "        " + marker + "\n"
             "        # Explicit member choices (including KAS) still win. Keeping this\n"
             "        # at load time means switching the main harness back restores\n"
             "        # Crew's normal member default without leaving a hidden BYO pin.\n"
             "        main_value = backend_from_stored(agent_data.get(\"acp_backend\"))\n"
             "        if key == _MEMBER_HARNESS_KEY and main_value == ACP_BACKEND_KIRO_BYO:\n"
             "            value = ACP_BACKEND_KIRO_BYO\n"
             "        else:\n"
             "            default = section.default(key)\n"
             "            value = default if default in selectable_backends() else None\n")
    new = ("    value = backend_from_stored(agent_data.get(key))\n"
           "    " + strict + "\n"
           "    if key == _MEMBER_HARNESS_KEY and backend_from_stored(agent_data.get(\"acp_backend\")) == ACP_BACKEND_KIRO_BYO:\n"
           "        value = ACP_BACKEND_KIRO_BYO\n"
           "    if value is None:\n"
           "        default = section.default(key)\n"
           "        value = default if default in selectable_backends() else None\n")
    # An installed guard must remain complete. A marker alone is not proof
    # that the effective member backend cannot silently return to KAS/CLI.
    if strict in text:
        if text.count(strict) != 1 or text.count(new) != 1:
            raise PatchError('0.11 BYO member default guard is incomplete or modified')
        return text
    if text.count(prior) == 1:
        return text.replace(prior, new, 1)
    import_anchor = '        ACP_BACKEND_KIRO,\n        backend_from_stored,'
    if text.count(import_anchor) != 1:
        raise PatchError('0.11 loader backend import shape moved')
    text = text.replace(import_anchor,
                        '        ACP_BACKEND_KIRO,\n        ACP_BACKEND_KIRO_BYO,\n        backend_from_stored,', 1)
    if text.count(old) != 1:
        raise PatchError('0.11 loader backend choice shape moved')
    return text.replace(old, new, 1)

def _handlers(text: str) -> str:
    # 0.11 moved compose to _facade_compose but keeps slot_effort as an owner.
    import ast
    tree=c._parse(text)
    calls=[n for n in ast.walk(tree) if isinstance(n,ast.Call) and
           isinstance(n.func,ast.Name) and n.func.id=='_facade_compose']
    if len(calls)!=1 or not any(isinstance(n,ast.Name) and n.id=='_owner_slot_effort'
                                 for n in ast.walk(calls[0])):
        raise PatchError('0.11 dashboard owner composition seam moved')
    if legacy._BACKEND_IMPORT not in text:
        span,node=c._import_span(text,'kiro_crew.agent_sdk.backends')
        text=c._insert_after_line(text,node.end_lineno or span.end,legacy._BACKEND_IMPORT)
    for helper in legacy._HELPERS:
        text=c._add_import_name(text,legacy._SLOT_EFFORT_MODULE,helper)
    return text


def _ensure_runtime_capability_import(text: str) -> str:
    """Import at runtime: the 0.11 turn_stats TYPE_CHECKING import is inert."""
    statement = "from kiro_crew.agent_sdk.capabilities import capabilities_of\n"
    if statement in text:
        return text
    future = "from __future__ import annotations\n"
    if text.count(future) != 1:
        raise PatchError("0.11 turn owner future-import contract changed")
    return text.replace(future, future + statement, 1)


def _patch_dispatch_clock(text: str) -> str:
    """Retain the initial turn clock for BYO; other Crew backends are untouched.

    The 0.11 chat runner stores the original clock on the per-turn object in
    turn_entry.py. The dispatch gates reset that clock after substantial setup.
    This hook is deliberately anchored in the owning 0.11 module, not the old
    shared chat_runner.py patch used on 0.10.
    """
    marker = "# Kiro BYO: retain the initial turn clock across dispatch setup."
    if marker in text:
        return text
    anchor = "    _turn_t0 = time.monotonic()\n    turn._turn_t0 = _turn_t0\n"
    if text.count(anchor) != 1 or "async def _turn_dispatch_gates(" not in text:
        raise PatchError("0.11 dispatch turn clock contract changed")
    text = _ensure_runtime_capability_import(text)
    replacement = (
        "    " + marker + "\n"
        "    if capabilities_of(client).backend != \"kiro-byo\":\n"
        "        _turn_t0 = time.monotonic()\n"
        "        turn._turn_t0 = _turn_t0\n"
    )
    return text.replace(anchor, replacement, 1)


def _patch_turn_footer(text: str) -> str:
    """Report Crew-side monotonic elapsed time for BYO, not provider latency.

    The elapsed field in 0.11 lives in turn_stats._on_complete, and previously
    prefers nonzero provider usage.duration_ms. That is not a user Turn clock.
    Keep the upstream-reported duration for every official backend unchanged.
    """
    marker = "# Kiro BYO: footer elapsed is turn wall time, not provider duration."
    if marker in text:
        return text
    anchor = "        _turn_elapsed_ms = int(_u.duration_ms or (time.monotonic() - _turn_t0) * 1000)\n"
    if text.count(anchor) != 1 or "async def _on_complete(" not in text:
        raise PatchError("0.11 turn stats footer contract changed")
    text = _ensure_runtime_capability_import(text)
    replacement = (
        "        " + marker + "\n"
        "        if capabilities_of(client).backend == \"kiro-byo\":\n"
        "            _turn_elapsed_ms = max(0, int((time.monotonic() - _turn_t0) * 1000))\n"
        "        else:\n"
        "            _turn_elapsed_ms = int(_u.duration_ms or (time.monotonic() - _turn_t0) * 1000)\n"
    )
    return text.replace(anchor, replacement, 1)


def patch_text(relative: str, text: str) -> str:
    rel=relative.replace('\\','/')
    if rel==_CORE: patched=_core(text)
    elif rel.startswith(_P+'agent_sdk/') and rel.removeprefix(_P+'agent_sdk/') in _CAPABILITIES:
        patched=_owner(text,rel.removeprefix(_P+'agent_sdk/'))
    elif rel==_P+'agent_sdk/backends_launch.py': patched=_launch(text)
    elif rel==_OWNER_LOAD: patched=_load(text)
    elif rel==_OWNER_MEMBERS: patched=shared._patch_members(text)
    elif rel==_LOADER_BUILDERS: patched=_loader_builders(text)
    elif rel==_HANDLERS: patched=_handlers(text)
    elif rel==_P+'dashboard/chat_api/slot_effort.py': patched=legacy._patch_slot_effort(text)
    elif rel==_P+'dashboard/chat_title.py': patched=legacy._patch_chat_title(text)
    elif rel==_DISPATCH_CLOCK: patched=_patch_dispatch_clock(text)
    elif rel==_TURN_FOOTER: patched=_patch_turn_footer(text)
    elif rel in shared._PATCHERS: patched=shared.patch_text(rel,text)
    else: raise PatchError(f'no Crew 0.11 transform for {rel}')
    c._parse(patched)
    return patched



def _previous_member_capability_installation(text: str, rendered: str) -> bool:
    """Prove that an *older, complete* BYO member capability patch can be upgraded.

    Crew 0.11 RC1's Mate Worker Kit support was added *after* BYO had already
    registered the member-dispatch and member-panel capabilities.  Those existing
    two registrations plus the real backend import prove that this is the older
    BYO-owned seam.  The only permitted rewrite is adding BYO to the new Mate
    capability.  An unknown partial/foreign edit still fails closed.
    """
    import ast

    tree = c._parse(text)
    imported = any(
        isinstance(node, ast.ImportFrom)
        and node.module == 'kiro_crew.agent_sdk.backends'
        and any(alias.name == 'ACP_BACKEND_KIRO_BYO' and alias.asname is None
                for alias in node.names)
        for node in tree.body
    )
    if not imported:
        return False

    wanted = {
        'ACP_BACKENDS_MEMBER_DISPATCH': True,
        'ACP_BACKENDS_MEMBER_PANEL': True,
        'ACP_BACKENDS_MATE_WORKER_KIT': False,
    }
    declarations: dict[str, bool] = {}
    for node in tree.body:
        name = c._assignment_name(node)
        if name not in wanted:
            continue
        if name in declarations:
            return False
        value = getattr(node, 'value', None)
        if value is None:
            return False
        byo_uses = sum(
            isinstance(part, ast.Name) and part.id == 'ACP_BACKEND_KIRO_BYO'
            for part in ast.walk(value)
        )
        if byo_uses > 1:
            return False
        declarations[name] = byo_uses == 1
    if declarations != wanted:
        return False

    try:
        expected = c._add_collection_expr(
            text, 'ACP_BACKENDS_MATE_WORKER_KIT', 'ACP_BACKEND_KIRO_BYO'
        )
        # This must be precisely one known, deterministic BYO addition.  There
        # is no blanket permission to rewrite unrelated source or other edits.
        return (
            rendered != text
            and rendered == expected
            and patch_text('src/kiro_crew/agent_sdk/backends_members.py', rendered) == rendered
        )
    except PatchError:
        return False


# Reconstruct the exact BYO-owned 0.2.5 catalogue helper before this fix.
# Old installed Crew sources use that helper; recognizing its exact AST lets
# the installer upgrade it transactionally without weakening foreign-edit checks.
_PREVIOUS_AGENTS_MODEL_HELPER = c._KIRO_BYO_MODEL_HELPER.replace(
    "    if _catalog_error:\n        namespace = model_registry_namespace(ACP_BACKEND_KIRO_BYO)\n",
    "    if not model_rows or not type_rows:\n        namespace = model_registry_namespace(ACP_BACKEND_KIRO_BYO)\n",
    1,
).replace(
    "    # configured_default belongs to the Crew host config, not necessarily\n"
    "    # to the active BYO Provider catalogue.  Never launder a stale KAS/CLI\n"
    "    # model pin back into the selectable BYO model list.\n",
    "    default = (configured_default or \"\").strip()\n"
    "    if default and _normalize_model_key(default) != \"auto\" and not default.startswith(\"kiro-byo-type:\"):\n"
    "        return [\n"
    "            {\n"
    "                \"model_name\": default,\n"
    "                \"display_name\": default,\n"
    "                \"description\": \"Configured default\",\n"
    "                \"context_window\": model_registry.model_window(default) or 0,\n"
    "            }\n"
    "        ]\n",
    1,
).replace(
    "    # ACP advertisement is a recovery path ONLY if reading BYO's catalogue\n"
    "    # failed.  A successfully read empty provider list is authoritative; old\n"
    "    # session advertisements must not resurrect models after a provider removal.\n",
    "",
    1,
)

# The earlier 0.2.5 builds used this *owned* member-backend display helper.
# It was replaced when fail-closed member execution routing was added.  Older
# installs may therefore be valid but no longer byte-identical to this build.
# Recognize the exact historical function, not arbitrary BYO-looking source.
_PREVIOUS_AGENTS_MEMBER_BACKEND_HELPER = '''def _kiro_byo_effective_member_backend(cfg: Any) -> str:
    """Respect Crew's independently configured member-DM backend.

    A global chat selection does not transfer authority to change another
    session class's backend. BYO for member DMs must be selected explicitly.
    """
    return getattr(cfg.agent, "member_acp_backend", "")
'''


def _previous_owned_agents_installation(text: str, rendered: str) -> bool:
    """Accept only a provably BYO-owned agents.py helper upgrade.

    Refuse partial patch fragments and unrecognized edits.  Prior builds
    already installed the catalogue helper, BYO model-discovery branch and
    member-pin call site.  The current version only needs to replace BYO-owned
    helpers or complete their BYO-owned resolved-model wrapper.  AST comparison
    of all other nodes proves the transform does not mutate Crew-owned logic.
    """
    import ast

    rel = _P + 'dashboard/handlers/agents.py'
    helpers = {
        '_kiro_byo_models': (_PREVIOUS_AGENTS_MODEL_HELPER, c._KIRO_BYO_MODEL_HELPER,),
        '_kiro_byo_effective_member_backend': (
            _PREVIOUS_AGENTS_MEMBER_BACKEND_HELPER,
            c._KIRO_BYO_EFFECTIVE_MEMBER_BACKEND_HELPER,
        ),
        '_kiro_byo_resolved_model': (c._KIRO_BYO_RESOLVED_MODEL_HELPER,),
    }
    try:
        before, after = c._parse(text), c._parse(rendered)

        def signature(node: ast.AST) -> str:
            return ast.dump(node, include_attributes=False)

        def functions(tree: ast.Module) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
            result = {}
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in helpers:
                    if node.name in result:
                        raise ValueError('duplicate BYO helper')
                    result[node.name] = node
            return result

        found = functions(before)
        rendered_helpers = functions(after)
        if not {'_kiro_byo_models', '_kiro_byo_effective_member_backend'} <= found.keys():
            return False
        if set(rendered_helpers) != set(helpers):
            return False
        # The model-catalogue code must be the exact bundled BYO-owned code.
        # Earlier candidate builds have the same helper, while the member
        # backend helper has the two known implementations listed above.
        for name, node in found.items():
            if not any(
                signature(node) == signature(c._parse(snippet).body[0])
                for snippet in helpers[name]
            ):
                return False
        for name, node in rendered_helpers.items():
            if signature(node) != signature(c._parse(helpers[name][-1]).body[0]):
                return False

        # Ownership also requires three cooperating Crew integration anchors,
        # not just a coincidental comment/string/helper name.
        imported = any(
            isinstance(node, ast.ImportFrom)
            and node.module == 'kiro_crew.acp_backends'
            and any(alias.name == 'ACP_BACKEND_KIRO_BYO' for alias in node.names)
            for node in before.body
        )
        pin = next((node for node in before.body
                    if isinstance(node, ast.FunctionDef)
                    and node.name == '_pin_entitlement_backend'), None)
        models_endpoint = next((node for node in before.body
                                if isinstance(node, ast.AsyncFunctionDef)
                                and node.name == 'api_models'), None)
        if not imported or pin is None or models_endpoint is None:
            return False
        if not any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == '_kiro_byo_effective_member_backend'
            for node in ast.walk(pin)
        ):
            return False
        if not any(
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Compare)
            and any(isinstance(operand, ast.Name) and operand.id == 'ACP_BACKEND_KIRO_BYO'
                    for operand in ast.walk(node.test))
            and any(isinstance(expr, ast.Call)
                    and isinstance(expr.func, ast.Name)
                    and expr.func.id == '_kiro_byo_models'
                    for expr in ast.walk(node))
            for node in ast.walk(models_endpoint)
        ):
            return False

        # Remove *only* BYO-owned top-level helper definitions and the
        # BYO-owned resolved-model binding from both ASTs, then compare the
        # remaining full Crew semantic trees for exact equality.
        def without_owned(tree: ast.Module) -> str:
            result = []
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in helpers:
                    continue
                if (isinstance(node, ast.Assign)
                        and len(node.targets) == 1
                        and isinstance(node.targets[0], ast.Name)
                        and node.targets[0].id == 'api_kirocrew_agent_resolved_model'
                        and isinstance(node.value, ast.Name)
                        and node.value.id == '_kiro_byo_resolved_model'):
                    continue
                result.append(node)
            return signature(ast.Module(body=result, type_ignores=[]))

        if without_owned(before) != without_owned(after):
            return False
        # Complete the old integration in no more than one deterministic step.
        return rendered != text and patch_text(rel, rendered) == rendered
    except (ValueError, SyntaxError, PatchError, StopIteration, TypeError):
        return False


@dataclass(frozen=True)
class TransformInspection:
    state:str
    detail:str=''


def inspect(relative:str,text:str)->TransformInspection:
    try:
        changed=patch_text(relative,text)
    except PatchError as exc:
        return TransformInspection('incompatible',str(exc))
    if changed==text:
        return TransformInspection('patched','idempotent owned source')
    if ('kiro_byo' in text.lower() or 'kirobyo' in text.lower()
            or '# Kiro BYO: Crew owns member backend selection' in text):
        rel = relative.replace('\\', '/')
        if (rel == _OWNER_MEMBERS
                and '# Kiro BYO: Crew owns member backend selection' in text
                and '# Kiro BYO: enforce BYO for new member sessions (no official fallback)' not in text
                and patch_text(rel, changed) == changed):
            return TransformInspection('upgradeable', 'older BYO member routing upgraded to no-official-fallback guard')
        if (rel == _LOADER_BUILDERS
                and '# Kiro BYO: implicit member harness follows the BYO main harness.' in text
                and '# Kiro BYO: enforce BYO member backend while main BYO is active.' not in text
                and patch_text(rel, changed) == changed):
            return TransformInspection('upgradeable', 'older BYO member default upgraded to strict BYO runtime selection')
        if (rel == _P + 'dashboard/handlers/agents.py'
                and _previous_owned_agents_installation(text, changed)):
            return TransformInspection(
                'upgradeable',
                'verified older BYO dashboard model helpers; safe in-place upgrade',
            )
        if (relative.replace('\\', '/') == _P + 'agent_sdk/backends_members.py'
                and _previous_member_capability_installation(text, changed)):
            return TransformInspection(
                'upgradeable',
                'prior BYO member capabilities are owned; Mate Worker Kit registration can be added safely',
            )
        # Do not assume arbitrary partial modifications are BYO-owned.
        return TransformInspection('incompatible','partial BYO source detected: restore pristine Crew before retrying')
    return TransformInspection('clean','source-specific semantic transform ready')
