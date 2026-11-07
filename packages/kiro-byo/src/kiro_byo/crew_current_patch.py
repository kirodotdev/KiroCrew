"""Current source-guarded ACP seam transforms for KiroCrew 0.12+.

The host 0.12+ adapter owns version/source eligibility and its changed
session/load boundary. These other semantic seams were first audited against
0.11 and remain shared only where the current host contract passes the same
source checks. No version number alone authorizes a rewrite.

The active 0.12+ runtime uses this shared implementation directly; retired
Crew-version adapters are not maintained.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import crew_slot_patch as slot_patch
from . import crew_shared_patch as shared
from . import crew_patch_common as c

PatchError = c.PatchError
_P = 'src/kiro_crew/'
_CORE = _P+'agent_sdk/backends.py'
_OWNER_LOAD = _P+'acp/runtime_session_load.py'
_HANDLERS = _P+'dashboard/chat_handlers.py'
_LOADER_BUILDERS = _P+'config/loader_builders.py'

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
    _P+'acp/types.py',
    _P+'agent_sdk/host_auth.py',
    _P+'acp/harness/__init__.py', _OWNER_LOAD,
    _P+'providers/mirrors/registry.py',_LOADER_BUILDERS,
    _P+'dashboard/handlers/agents.py',
    _P+'dashboard/chat_api/slot_effort.py',
    _P+'dashboard/chat_title.py',
)


def _core(text: str) -> str:
    text = c._insert_after_assignment(text, 'ACP_BACKEND_KIRO',
        '# Kiro BYO registered ACP backend (source-guarded 0.12 owner)\n'
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



def _canonical_loader_identity_import(text: str) -> str:
    """Move the BYO member-choice identity off the retired import facade.

    Only the BYO-owned single import line inside `_read_backend_choice` may
    move. Crew's unrelated import names and stored backend mapping are intact.
    """
    import ast

    tree = c._parse(text)
    function = next((node for node in tree.body
                     if isinstance(node, ast.FunctionDef)
                     and node.name == '_read_backend_choice'), None)
    if function is None:
        raise PatchError('Crew member-choice owner function moved')
    facade = [node for node in function.body
              if isinstance(node, ast.ImportFrom)
              and node.module == 'kiro_crew.acp_backends']
    canonical = [node for node in function.body
                 if isinstance(node, ast.ImportFrom)
                 and node.module == 'kiro_crew.agent_sdk.backends'
                 and any(a.name == 'ACP_BACKEND_KIRO_BYO' for a in node.names)]
    if len(facade) != 1 or len(canonical) > 1:
        raise PatchError('Crew member-choice backend imports are ambiguous')
    owned = [alias for alias in facade[0].names if alias.name == 'ACP_BACKEND_KIRO_BYO']
    if len(owned) > 1 or any(alias.asname is not None for alias in owned):
        raise PatchError('Crew member-choice legacy BYO import is ambiguous')
    if canonical and (owned or len(canonical[0].names) != 1
                      or canonical[0].names[0].asname is not None):
        raise PatchError('Crew member-choice BYO identity has overlapping imports')
    if owned:
        node = facade[0]
        if node.end_lineno is None:
            raise PatchError('Crew member-choice facade import is incomplete')
        lines = text.splitlines(keepends=True)
        matching = [i for i in range(node.lineno - 1, node.end_lineno)
                    if lines[i].strip() == 'ACP_BACKEND_KIRO_BYO,']
        if len(matching) != 1:
            raise PatchError('Crew member-choice legacy BYO import layout changed')
        del lines[matching[0]]
        text = ''.join(lines)
    if not canonical:
        function = next((n for n in c._parse(text).body
                         if isinstance(n, ast.FunctionDef)
                         and n.name == '_read_backend_choice'), None)
        if function is None:
            raise PatchError('Crew member-choice import owner changed')
        nested = [n for n in function.body if isinstance(n, ast.ImportFrom)
                  and n.module == 'kiro_crew.acp_backends']
        if len(nested) != 1 or nested[0].end_lineno is None:
            raise PatchError('Crew member-choice import owner changed')
        node = nested[0]
        text = c._insert_after_line(
            text, node.end_lineno,
            '    from kiro_crew.agent_sdk.backends import ACP_BACKEND_KIRO_BYO',
        )
    return text


def _loader_builders(text: str) -> str:
    """Make new, unpinned crewmate sessions follow global Kiro/BYO both ways.

    The member default is derived at load time, not persisted, so switching
    back to Kiro works even if an old saved member default names BYO. Explicit
    per-crewmate harness pins and per-session overrides still win in Crew's
    original select_provider_backend() gate. Existing sessions are untouched.
    """
    text = _canonical_loader_identity_import(text)
    previous_marker = '# Kiro BYO: enforce BYO member backend while main BYO is active.'
    prior_marker = '# Kiro BYO: implicit member harness follows the BYO main harness.'
    marker = '# Kiro BYO: effective member default follows main official/BYO harness.'
    head = '    value = backend_from_stored(agent_data.get(key))\n'
    tail = ('    if value is None:\n'
            '        default = section.default(key)\n'
            '        value = default if default in selectable_backends() else None\n')
    fresh = head + tail
    prior = (head + '    if value is None:\n'
             '        ' + prior_marker + '\n'
             '        # Explicit member choices (including KAS) still win. Keeping this\n'
             '        # at load time means switching the main harness back restores\n'
             '        # Crew\'s normal member default without leaving a hidden BYO pin.\n'
             '        main_value = backend_from_stored(agent_data.get("acp_backend"))\n'
             '        if key == _MEMBER_HARNESS_KEY and main_value == ACP_BACKEND_KIRO_BYO:\n'
             '            value = ACP_BACKEND_KIRO_BYO\n'
             '        else:\n'
             '            default = section.default(key)\n'
             '            value = default if default in selectable_backends() else None\n')
    strict = (head + '    ' + previous_marker + '\n'
              '    if key == _MEMBER_HARNESS_KEY and backend_from_stored(agent_data.get("acp_backend")) == ACP_BACKEND_KIRO_BYO:\n'
              '        value = ACP_BACKEND_KIRO_BYO\n' + tail)
    updated = (head + '    ' + marker + '\n'
               '    if key == _MEMBER_HARNESS_KEY:\n'
               '        _main = backend_from_stored(agent_data.get("acp_backend"))\n'
               '        if _main == ACP_BACKEND_KIRO_BYO:\n'
               '            value = ACP_BACKEND_KIRO_BYO\n'
               '        elif _main in (None, ACP_BACKEND_KIRO, ACP_BACKEND_KAS):\n'
               '            value = ACP_BACKEND_KAS\n' + tail)
    if marker in text:
        if text.count(marker) != 1 or text.count(updated) != 1:
            raise PatchError('BYO global-follow loader rule incomplete or modified')
        return text
    if previous_marker in text or prior_marker in text:
        old = strict if previous_marker in text else prior
        if text.count(old) != 1 or text.count(previous_marker if previous_marker in text else prior_marker) != 1:
            raise PatchError('legacy BYO member loader rule incomplete or modified')
        return text.replace(old, updated, 1)
    if text.count(fresh) != 1:
        raise PatchError('Crew member backend loader shape moved')
    return text.replace(fresh, updated, 1)


def _handlers(text: str) -> str:
    # 0.11 moved compose to _facade_compose but keeps slot_effort as an owner.
    import ast
    tree=c._parse(text)
    calls=[n for n in ast.walk(tree) if isinstance(n,ast.Call) and
           isinstance(n.func,ast.Name) and n.func.id=='_facade_compose']
    if len(calls)!=1 or not any(isinstance(n,ast.Name) and n.id=='_owner_slot_effort'
                                 for n in ast.walk(calls[0])):
        raise PatchError('0.11 dashboard owner composition seam moved')
    if slot_patch._BACKEND_IMPORT not in text:
        span,node=c._import_span(text,'kiro_crew.agent_sdk.backends')
        text=c._insert_after_line(text,node.end_lineno or span.end,slot_patch._BACKEND_IMPORT)
    for helper in slot_patch._HELPERS:
        text=c._add_import_name(text,slot_patch._SLOT_EFFORT_MODULE,helper)
    return text


def patch_text(relative: str, text: str) -> str:
    rel=relative.replace('\\','/')
    if rel==_CORE: patched=_core(text)
    elif rel.startswith(_P+'agent_sdk/') and rel.removeprefix(_P+'agent_sdk/') in _CAPABILITIES:
        patched=_owner(text,rel.removeprefix(_P+'agent_sdk/'))
    elif rel==_P+'agent_sdk/backends_launch.py': patched=_launch(text)
    elif rel==_OWNER_LOAD: patched=_load(text)
    elif rel==_LOADER_BUILDERS: patched=_loader_builders(text)
    elif rel==_HANDLERS: patched=_handlers(text)
    elif rel==_P+'dashboard/chat_api/slot_effort.py': patched=slot_patch._patch_slot_effort(text)
    elif rel==_P+'dashboard/chat_title.py': patched=slot_patch._patch_chat_title(text)
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

_R81_AGENTS_MEMBER_BACKEND_HELPER = '''def _kiro_byo_effective_member_backend(cfg: Any) -> str:
    """Return effective new-member backend; do not rewrite stored preferences.

    Kiro BYO mode routes new member sessions through BYO and refuses a
    conflicting per-session official override.  Crew's saved member preference
    remains untouched and becomes effective again when exiting BYO mode.
    """
    if getattr(cfg.agent, "acp_backend", "") == ACP_BACKEND_KIRO_BYO:
        return ACP_BACKEND_KIRO_BYO
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
            _R81_AGENTS_MEMBER_BACKEND_HELPER,
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
            and node.module in {'kiro_crew.acp_backends', 'kiro_crew.agent_sdk.backends'}
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
                if (isinstance(node, ast.ImportFrom)
                        and node.module in {'kiro_crew.acp_backends', 'kiro_crew.agent_sdk.backends'}):
                    # R73 retires the backend facade. Ignore only the known BYO
                    # import moving from that facade to the real owner; compare
                    # every other imported symbol and Crew-owned AST unchanged.
                    names = [a for a in node.names if a.name != 'ACP_BACKEND_KIRO_BYO']
                    if names:
                        result.append(ast.ImportFrom(module=node.module, names=names, level=node.level))
                    continue
                result.append(node)
            return signature(ast.Module(body=result, type_ignores=[]))

        if without_owned(before) != without_owned(after):
            return False
        # Complete the old integration in no more than one deterministic step.
        return rendered != text and patch_text(rel, rendered) == rendered
    except (ValueError, SyntaxError, PatchError, StopIteration, TypeError):
        return False


def _previous_owned_types_facade_import(text: str, rendered: str) -> bool:
    """Verify the only difference is BYO moving off the retired ACP facade.

    R72's installed types.py is otherwise a fully patched owned seam.  Compare
    the complete semantic tree after removing just the BYO identity import;
    no changed Crew statements, symbols or authority may pass as an upgrade.
    """
    import ast

    rel = _P + 'acp/types.py'
    try:
        before, after = c._parse(text), c._parse(rendered)
        def has_import(tree: ast.Module, module: str) -> bool:
            return any(isinstance(node, ast.ImportFrom)
                       and node.module == module
                       and sum(a.name == 'ACP_BACKEND_KIRO_BYO' and a.asname is None
                               for a in node.names) == 1 for node in tree.body)
        if not (has_import(before, 'kiro_crew.acp_backends')
                and has_import(after, 'kiro_crew.agent_sdk.backends')):
            return False
        if has_import(after, 'kiro_crew.acp_backends'):
            return False
        if not any(isinstance(node, ast.Assign)
                   and any(isinstance(target, ast.Name)
                           and target.id == 'PROVIDER_LABEL_KIRO_BYO'
                           for target in node.targets) for node in before.body):
            return False
        def strip_owned_import(tree: ast.Module) -> str:
            result = []
            for node in tree.body:
                if isinstance(node, ast.ImportFrom) and node.module in {
                        'kiro_crew.acp_backends', 'kiro_crew.agent_sdk.backends'}:
                    names = [a for a in node.names if a.name != 'ACP_BACKEND_KIRO_BYO']
                    if len(names) != len(node.names):
                        if names:
                            result.append(ast.ImportFrom(module=node.module, names=names,
                                                         level=node.level))
                        continue
                result.append(node)
            return ast.dump(ast.Module(body=result, type_ignores=[]),
                            include_attributes=False)
        return (strip_owned_import(before) == strip_owned_import(after)
                and patch_text(rel, rendered) == rendered)
    except (ValueError, SyntaxError, PatchError, TypeError):
        return False


def _previous_owned_loader_facade_import(text: str, rendered: str) -> bool:
    """Accept strict R76 loader bindings with only the old BYO import moved."""
    import ast

    try:
        before, after = c._parse(text), c._parse(rendered)
        strict = '# Kiro BYO: enforce BYO member backend while main BYO is active.'
        if strict not in text:
            return False

        def imports(tree: ast.Module, module: str) -> list[ast.ImportFrom]:
            funcs = [n for n in tree.body if isinstance(n, ast.FunctionDef)
                     and n.name == '_read_backend_choice']
            if len(funcs) != 1:
                return []
            return [n for n in funcs[0].body if isinstance(n, ast.ImportFrom)
                    and n.module == module
                    and any(a.name == 'ACP_BACKEND_KIRO_BYO' for a in n.names)]

        if (len(imports(before, 'kiro_crew.acp_backends')) != 1
                or imports(before, 'kiro_crew.agent_sdk.backends')
                or imports(after, 'kiro_crew.acp_backends')
                or len(imports(after, 'kiro_crew.agent_sdk.backends')) != 1):
            return False

        class StripIdentity(ast.NodeTransformer):
            def visit_ImportFrom(self, node: ast.ImportFrom) -> ast.AST | None:
                if node.module not in {'kiro_crew.acp_backends',
                                       'kiro_crew.agent_sdk.backends'}:
                    return node
                remaining = [a for a in node.names if a.name != 'ACP_BACKEND_KIRO_BYO']
                if not remaining:
                    return None
                node.names = remaining
                return node

        return (text != rendered
                and ast.dump(StripIdentity().visit(before), include_attributes=False)
                == ast.dump(StripIdentity().visit(after), include_attributes=False)
                and patch_text(_LOADER_BUILDERS, rendered) == rendered)
    except (SyntaxError, ValueError, PatchError, TypeError):
        return False


def _previous_owned_registry_facade_import(text: str, rendered: str) -> bool:
    """Accept only the old BYO registry's exact identity-import relocation.

    This is the upgrade path for affected installations that could not boot
    Crew. No Crew-owned class, capability, mirror or policy expression may
    change as part of the migration.
    """
    import ast

    try:
        before, after = c._parse(text), c._parse(rendered)
        old_import = [n for n in before.body if isinstance(n, ast.ImportFrom)
                      and n.module == 'kiro_crew.acp_backends'
                      and any(a.name == 'ACP_BACKEND_KIRO_BYO' for a in n.names)]
        new_import = [n for n in after.body if isinstance(n, ast.ImportFrom)
                      and n.module == 'kiro_crew.agent_sdk.backends'
                      and len(n.names) == 1
                      and n.names[0].name == 'ACP_BACKEND_KIRO_BYO'
                      and n.names[0].asname is None]
        if len(old_import) != 1 or len(new_import) != 1:
            return False
        if any(isinstance(n, ast.ImportFrom)
               and n.module == 'kiro_crew.agent_sdk.backends'
               and any(a.name == 'ACP_BACKEND_KIRO_BYO' for a in n.names)
               for n in before.body):
            return False
        if not all(marker in text for marker in shared._REQUIRED_MARKERS[
                _P + 'providers/mirrors/registry.py']):
            return False

        def without_identity_import(module: ast.Module) -> str:
            statements = []
            for n in module.body:
                if (isinstance(n, ast.ImportFrom)
                        and n.module in {'kiro_crew.acp_backends',
                                         'kiro_crew.agent_sdk.backends'}):
                    aliases = [a for a in n.names if a.name != 'ACP_BACKEND_KIRO_BYO']
                    if len(aliases) != len(n.names):
                        if aliases:
                            statements.append(ast.ImportFrom(
                                module=n.module, names=aliases, level=n.level))
                        continue
                statements.append(n)
            return ast.dump(ast.Module(body=statements, type_ignores=[]),
                            include_attributes=False)

        return (rendered != text
                and without_identity_import(before) == without_identity_import(after)
                and patch_text(_P + 'providers/mirrors/registry.py', rendered) == rendered)
    except (ValueError, SyntaxError, PatchError, TypeError):
        return False


@dataclass(frozen=True)
class TransformInspection:
    state:str
    detail:str=''


def inspect(relative:str,text:str)->TransformInspection:
    try:
        changed=patch_text(relative,text)
    except PatchError as exc:
        # A retired seam has no transform registered any more (e.g.
        # member_sessions.py: BYO退役后不再给它打补丁). "no transform" on a
        # file that carries NO BYO trace means it is a clean, unpatched
        # official source -- which is exactly the state a retired seam should
        # be in. Report it as clean so the retirement check passes instead of
        # mistaking "no rule" for "foreign/partial". A file that still carries
        # BYO trace falls through to the real incompatible verdict below.
        if (str(exc).startswith('no Crew') and ' transform for ' in str(exc)
                and 'kiro_byo' not in text.lower() and 'kirobyo' not in text.lower()
                and '# Kiro BYO:' not in text):
            return TransformInspection('clean','retired seam is clean official source (no BYO transform, no BYO trace)')
        return TransformInspection('incompatible',str(exc))
    if changed==text:
        return TransformInspection('patched','idempotent owned source')
    if ('kiro_byo' in text.lower() or 'kirobyo' in text.lower()
            or '# Kiro BYO: Crew owns member backend selection' in text):
        rel = relative.replace('\\', '/')
        if (rel == _LOADER_BUILDERS
                and ('# Kiro BYO: implicit member harness follows the BYO main harness.' in text
                     or '# Kiro BYO: enforce BYO member backend while main BYO is active.' in text)
                and patch_text(rel, changed) == changed):
            return TransformInspection('upgradeable', 'verified BYO member default upgraded to bidirectional global follow')
        if (rel == _LOADER_BUILDERS
                and _previous_owned_loader_facade_import(text, changed)):
            return TransformInspection(
                'upgradeable',
                'verified old member-choice BYO identity import; Crew AST unchanged',
            )
        if (rel == _P + 'providers/mirrors/registry.py'
                and _previous_owned_registry_facade_import(text, changed)):
            return TransformInspection(
                'upgradeable',
                'verified old BYO mirror registry import; Crew AST unchanged',
            )
        if (rel == _P + 'acp/types.py'
                and _previous_owned_types_facade_import(text, changed)):
            return TransformInspection(
                'upgradeable',
                'verified R72 BYO facade identity import; full Crew AST unchanged',
            )
        if (rel == _P + 'dashboard/chat_api/slot_effort.py'
                and slot_patch.is_owned_facade_effort_upgrade(text, changed)
                and patch_text(rel, changed) == changed):
            return TransformInspection(
                'upgradeable',
                'verified BYO-owned Crew 0.12 facade effort binding repair; Crew AST preserved',
            )
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
