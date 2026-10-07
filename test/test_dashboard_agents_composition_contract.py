"""The dashboard agents handlers keep their surface and contracts while their owners move.

``kiro_crew.dashboard.handlers.agents`` is the agents handlers' import path and their
patch surface. Most of what it defined now lives in the modules of
``kiro_crew.dashboard.agent_admin``, one responsibility each, and
``agent_admin.compose`` runs every function they define on the facade's globals.
These tests pin:

* the surface: every name the facade bound before the split still resolves on it,
  the routes and the package re-exports dispatch to the facade's objects, and the
  seams sibling handlers import keep their identity;
* the composition: every owner function runs on the facade's globals, reads only
  names the facade binds, and captures no name a test rebinds on the facade;
* the guards: what repository guards read in ``agents.py`` by path stays there, and
  each guard re-keyed or widened to the owners still sees the code it guards;
* the owners' least-covered routes, characterized so each owner answers for itself.
"""

from __future__ import annotations

import ast
import builtins
import dis
import hashlib
import importlib
import importlib.util
import inspect
import json
import pkgutil
import re
import shutil
import subprocess
import sys
import textwrap
import types
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from source_corpus import repo_files_named, repo_root

import kiro_crew.dashboard.handlers as handlers_pkg
import kiro_crew.dashboard.handlers.agents as agents
from kiro_crew.dashboard import agent_admin
from kiro_crew.subprocess_utf8 import UTF8_TEXT

_FACADE = agents.__name__
_FACADE_PATH = Path(agents.__file__).resolve()
_OWNER_PACKAGE = agent_admin.__name__
_OWNER_DIR = Path(agent_admin.__file__).resolve().parent
_SRC = _FACADE_PATH.parents[3]

#: Every module-level name ``agents`` bound at the base the split was cut from: what
#: it defined and what it imported, private names included, because routes, the
#: handlers package, sibling handlers and tests read private names off it too. A
#: name bound only by ``import <module>`` of a stdlib module is left out: nothing
#: reads ``json`` or ``os`` off the facade, and pinning one would fail on the
#: removal of an unused import.
_BASE_NAMES = frozenset("""
        ACP_BACKEND_CLAUDE ACP_BACKEND_KIRO AGENT_FILENAME AgentInfo Any
        AppOwnershipUnreadable BodyPartReader CapabilityError ConfigReadError
        DEFAULT_MEMORY_STORE DashboardState EFFORT_LEVELS EFFORT_VALUES
        EntitlementRevalidating INSTALLED_META_FILENAME KAS_RESERVED_AGENT_IDS
        KiroCrewAgentConfig KiroCrewConfig LoopBoundLock MAX_AGENT_SKILLS
        MemberAlreadyExists MemberNameError OWNED_KIRO_AGENT_FILES Path
        SCHEMA_REGISTRY SCOPE_GLOBAL SLASH_COMMAND_DESCRIPTIONS
        SandboxUnavailableError Sequence SkillCatalogSnapshot
        TEMPLATE_DEFINITION_KEYS TEMPLATE_NAME_RE UnknownMemoryStore
        _AVATAR_CONTENT_TYPES _AVATAR_FILE_PIN_RE _AVATAR_GHOST_BOOL_TRAITS
        _AVATAR_GHOST_STR_TRAITS _AVATAR_IMAGE_EXTS _AVATAR_MAX_BYTES
        _AmbiguousDefaultTarget _AmbiguousTemplateName _AppRegisteredTemplate
        _BLOCKED_SLASH_COMMANDS _CAPABILITY_UNAVAILABLE
        _CATALOG_CACHE_MAX_FIELDS_PER_ROW _CONFIG_SCHEMA_ACP_BACKEND _CatalogCache
        _CatalogUnavailable _ForeignPrivateCopy _ForkBookkeepingFailed
        _LIST_MODELS_BACKGROUND_TIMEOUT_SECS _LIST_MODELS_CATALOG_TTL_SECS
        _LIST_MODELS_SUBPROCESS_TIMEOUT_SECS _LOADER_AVATAR_IMAGE_EXTS
        _MAX_CAPABILITY_PACKAGE_LEN _MODEL_LIST_STDERR_TAIL_CHARS _PublishNameBound
        _SLASH_COMMANDS _StaleBinding _UnverifiableLineage
        _VALID_CAPABILITY_PACKAGE_RE _WINDOWS_RESERVED_NAMES
        _advertised_backend_models _advertised_cc_models _agent_detail_candidates
        _agent_file_lock _agent_roster_row _alias_binding_template
        _app_declared_server_names _app_or_host_owned _atomic_json_write
        _audit_capability _avatar_stem _avatar_variant_paths _avatars_dir
        _binds_template _bounded_catalog _capability_manager _carries_mask
        _carry_motions_through_motionless_save _carry_pack_through_faceless_save
        _catalog_cache _cc_models _commit_agent_config _commit_agent_config_locked
        _commit_promoted_avatar _config_lock _consume_refresh_exception
        _crew_effort_rejected _crew_memory_store_rejected _discard_pending_avatar
        _do_agents_sync _drained_to_thread _drop_unbacked_app_entries _effort_inputs
        _entitled_kiro_models _err500 _fetch_kiro_catalog _find_agent_config
        _foreign_private_copy_owner _get_config_lock _history_key_for
        _image_body_complete _installed_agent_config _installed_template_alias
        _is_app_registered _is_ghost_shaped _is_reserved_basename
        _is_valid_capability_package _live_avatar_file _load_template_specs
        _member_slug_is_claimed _merge_resources_delta _merge_unowned_servers
        _model_pin_rejected _mutate_agent_package _name_would_be_masked
        _namespaced_agent_file_exists _normalize_model_key _on_disk_mcp_servers
        _pending_avatar_path _pin_entitlement_backend _promote_pending_avatar
        _prune_private_copy_of_deleted_crew _read_agent_spec _read_avatar_file
        _read_session_key _rebind_crew_locked _reclaim_deleted_member_crew_log
        _redact_external _refresh_forked_templates _refresh_session_defaults
        _registration_source _remove_avatar_files _reorder_named _require_owner
        _require_present_shape _reserved_binding_names _revalidate_crew_pin
        _rollback_promoted_avatar _roster_avatar _roster_mask _safe_avatar
        _safe_color _scoped_default _sel _shared_catalog_fetch _sniff_image_ext
        _spec_path_is_safe _spec_stem_on_disk _staging_token _supply_live_enum
        _unlink_copy_unless_referenced _wrap_list_models_argv
        _write_installed_config _write_spec_file active_project_dir
        advertised_model_ids agent_skill_keys agent_skill_views
        agent_spec_candidates agent_state agents_spec_lock annotations
        api_agent_config api_agent_detail api_agent_fork api_agent_publish
        api_agent_reset api_agents_installed api_capability_agents_install
        api_capability_agents_list api_capability_agents_uninstall
        api_capability_mcp_install api_capability_mcp_list
        api_capability_mcp_registry api_capability_mcp_uninstall
        api_capability_plugins_list api_capability_plugins_sync
        api_capability_skills_install api_capability_skills_list
        api_capability_skills_uninstall api_config_schema api_default_agent
        api_effort_levels api_kirocrew_agent_avatar_get
        api_kirocrew_agent_avatar_upload api_kirocrew_agent_delete
        api_kirocrew_agent_resolved_model api_kirocrew_agent_update
        api_kirocrew_agents api_kirocrew_agents_create api_kirocrew_agents_sync
        api_models api_slash_commands app_dir app_enabled_state
        apply_definition_patch apply_skill_mapping apps_dir capabilities_for
        capabilities_of catalog_row_would_drop cgroup_scope_argv
        clear_list_agents_cache clear_model_pin coerce_dict_section coerce_effort
        conditional_response config_entry_to_dict config_local_path config_path
        configured_sandbox_mode data_home discovery_executor dispatch_kiro_agent
        drained_to_thread emission_eligible_mcp_servers enumerate_skill_catalog
        get_reasoning_effort_ordered get_shipped_tools inject_kiro_cli_api_key
        install_agent is_claude_code is_deprecated_model is_link_or_junction
        is_markdown_spec iter_agent_spec_files key_new_crew kill_and_reap
        kiro_agents_dir_path list_agents loads_user_json logger maintenance_executor
        memory_store_binding_defect memory_store_namespace_lock model_is_unusable
        model_registry model_registry_namespace model_scope normalize_agent_model
        persist_member_config project_agent_names provision_member_memory
        read_bounded_json read_config_text read_only_reason_for_path
        reject_if_kiro_unverified replace_with_retry require_unmanaged_template
        resolve_agent_config_path resolve_agent_identity resolve_effective_model
        resolve_kiro_bin_for_spawn resolve_pin_spelling resolve_ssh_auth_sock
        retire_unpublished_allocation run_config_write
        sanitize_agent_config_governance scrub_agent_subprocess_env
        selectable_backend_values spawn_supervised_oneshot spec_model spec_str
        strong_content_etag subprocess_executor teams_mod update_config_locked
        validate_definition_patch validate_member_name web wrap_argv
        write_config_atomically
    """.split())

#: Each moved definition and the owner its responsibility puts it in. Adding or
#: removing an owner changes the composition, so the set is spelled out.
_BASE_OWNERS: dict[str, tuple[str, ...]] = {
    "app_mcp_ownership": tuple("""
        _on_disk_mcp_servers _drop_unbacked_app_entries _merge_unowned_servers
        AppOwnershipUnreadable _require_present_shape _app_declared_server_names
        _app_or_host_owned
        """.split()),
    "agent_config": tuple("""
        _find_agent_config _installed_agent_config _write_installed_config
        _commit_agent_config _commit_agent_config_locked api_agent_config
        """.split()),
    "default_agent": tuple("""
        _AmbiguousDefaultTarget _binds_template _alias_binding_template
        _AppRegisteredTemplate _is_app_registered _installed_template_alias
        api_default_agent
        """.split()),
    "capabilities": tuple("""
        _is_valid_capability_package _audit_capability api_capability_mcp_list
        api_capability_mcp_install api_capability_mcp_uninstall api_capability_skills_list
        api_capability_skills_install api_capability_skills_uninstall
        _mutate_agent_package api_capability_agents_install
        api_capability_agents_uninstall api_capability_plugins_list
        api_capability_plugins_sync api_capability_agents_list api_capability_mcp_registry
        """.split()),
    "template_lineage": tuple("""
        _spec_stem_on_disk _is_reserved_basename _write_spec_file _AmbiguousTemplateName
        _load_template_specs _StaleBinding _ForeignPrivateCopy _PublishNameBound
        _ForkBookkeepingFailed _rebind_crew_locked _UnverifiableLineage
        _foreign_private_copy_owner _reserved_binding_names
        _unlink_copy_unless_referenced
        """.split()),
    "fork_publish": ("api_agent_fork", "api_agent_publish", "api_agent_reset"),
    "agent_detail": tuple("""
        _agent_detail_candidates _merge_resources_delta _reorder_named api_agent_detail
        """.split()),
    "roster": tuple("""
        _roster_mask _carries_mask _roster_avatar _name_would_be_masked _agent_roster_row
        """.split()),
    "installed_agents": tuple("""
        _namespaced_agent_file_exists api_agents_installed api_kirocrew_agents_sync
        _do_agents_sync
        """.split()),
    "crew_records": tuple("""
        api_kirocrew_agent_resolved_model _refresh_session_defaults _crew_effort_rejected
        _crew_memory_store_rejected api_kirocrew_agents_create
        """.split()),
    "crew_update": ("_effort_inputs", "api_kirocrew_agent_update"),
    "crew_removal": tuple("""
        _member_slug_is_claimed _reclaim_deleted_member_crew_log
        _prune_private_copy_of_deleted_crew api_kirocrew_agent_delete
        """.split()),
    "avatars": tuple("""
        _carry_pack_through_faceless_save _carry_motions_through_motionless_save
        _is_ghost_shaped _avatars_dir _avatar_stem _avatar_variant_paths
        _pending_avatar_path _remove_avatar_files _discard_pending_avatar
        _read_avatar_file _staging_token _promote_pending_avatar _commit_promoted_avatar
        _rollback_promoted_avatar _live_avatar_file _sniff_image_ext _image_body_complete
        api_kirocrew_agent_avatar_upload
        """.split()),
}

#: SHA-256 of the sorted ``"<name> <kind> <signature>"`` lines of every name in
#: ``_BASE_OWNERS``, captured from the one-module file before the split: each moved
#: name keeps the kind and signature it had there.
_BASE_SHAPE_DIGEST = "54f616ae7fbc7dbee618439bb16204802f68596743729e9b02f8d74e083abde8"

#: Definitions that stay in the facade file. The seams every owner reads; the
#: config schema route; the model and picker reads with the crew model-pin checks,
#: which hold every backend capability and identity read the SDK-boundary ratchets
#: key to this path, the harness-parity sandbox literal and the names
#: ``model-selection.md`` cites; ``GET /api/agents``, whose ``project_agent_names``
#: call the hardened-reads census keys here; and the avatar ``GET``, the media route
#: the conditional-GET fence reads in this module.
_FACADE_DEFS = (
    "_err500",
    "_sel",
    "_require_owner",
    "_supply_live_enum",
    "api_config_schema",
    "_normalize_model_key",
    "_advertised_cc_models",
    "_entitled_kiro_models",
    "_cc_models",
    "_advertised_backend_models",
    "_wrap_list_models_argv",
    "_scoped_default",
    "_CatalogUnavailable",
    "_bounded_catalog",
    "_CatalogCache",
    "_fetch_kiro_catalog",
    "_shared_catalog_fetch",
    "_consume_refresh_exception",
    "own_models_backend",
    "_live_on",
    "harness_effort",
    "api_models",
    "api_effort_levels",
    "api_slash_commands",
    "api_kirocrew_agents",
    "_get_config_lock",
    "_pin_entitlement_backend",
    "_revalidate_crew_pin",
    "_model_pin_rejected",
    "api_kirocrew_agent_avatar_get",
)

_MOVED = frozenset(name for names in _BASE_OWNERS.values() for name in names)


def _owner(stem: str) -> types.ModuleType:
    return importlib.import_module(f"{_OWNER_PACKAGE}.{stem}")


def _owners() -> list[types.ModuleType]:
    return [_owner(info.name) for info in pkgutil.iter_modules([str(_OWNER_DIR)])]


def _owner_sources() -> dict[str, str]:
    return {
        path.stem: path.read_text(encoding="utf-8") for path in sorted(_OWNER_DIR.glob("[!_]*.py"))
    }


def _owner_functions() -> list[tuple[str, types.FunctionType]]:
    """``(label, function)`` for every function an owner's file defines at top level
    or as a member of a class the owner defines."""
    found: list[tuple[str, types.FunctionType]] = []
    for owner in _owners():
        for name, value in vars(owner).items():
            members = [(name, value)]
            if isinstance(value, type) and value.__module__ == owner.__name__:
                members = [(f"{name}.{k}", v) for k, v in vars(value).items()]
            for label, member in members:
                fn = getattr(member, "__func__", member)
                if isinstance(fn, types.FunctionType) and fn.__code__.co_filename == owner.__file__:
                    found.append((f"{owner.__name__.rsplit('.', 1)[-1]}.{label}", fn))
    return found


def _global_names(code: types.CodeType):
    """Every global a code object and its nested code objects read or write."""
    for instruction in dis.get_instructions(code):
        if instruction.opname in ("LOAD_GLOBAL", "STORE_GLOBAL", "DELETE_GLOBAL"):
            yield instruction.argval
    for constant in code.co_consts:
        if isinstance(constant, types.CodeType):
            yield from _global_names(constant)


def _run_child(tmp_path: Path, script: str, *args: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script), *args],
        capture_output=True,
        timeout=120,
        cwd=str(tmp_path),
        **UTF8_TEXT,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("ok")


# ── the surface ───────────────────────────────────────────────────────────────


def test_every_name_the_facade_bound_at_the_base_still_resolves() -> None:
    """Routes, the handlers package, sibling handlers and tests read private names
    off the facade as well as public ones, so every module-level binding survives."""
    assert len(_BASE_NAMES) == 274
    assert sorted(name for name in _BASE_NAMES if not hasattr(agents, name)) == []


def test_a_fresh_interpreter_sees_every_base_public_name(tmp_path: Path) -> None:
    """The public names resolve in a process that imports nothing else first, and the
    handlers package's re-exports are the facade's own objects there too."""
    public = sorted(name for name in _BASE_NAMES if not name.startswith("_"))
    assert len(public) > 140
    _run_child(
        tmp_path,
        """
        import sys
        import kiro_crew.dashboard.handlers as pkg
        import kiro_crew.dashboard.handlers.agents as agents
        missing = [n for n in sys.argv[1:] if not hasattr(agents, n)]
        assert missing == [], missing
        foreign = [n for n in sys.argv[1:] if hasattr(pkg, n) and n.startswith("api_")
                   and getattr(pkg, n) is not getattr(agents, n)]
        assert foreign == [], foreign
        print("ok")
        """,
        *public,
    )


def _package_reexports() -> list[str]:
    tree = ast.parse(Path(handlers_pkg.__file__).read_text(encoding="utf-8"))
    return [
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == _FACADE
        for alias in node.names
    ]


def test_the_routes_and_the_package_reach_the_facade_objects() -> None:
    """Every route the gateway registers for an agents handler and every name the
    handlers package re-exports is the facade's object. The owner-gate test's route
    walk selects handlers by ``__module__``, so every one of them still reads the
    facade's name."""
    from kiro_crew.dashboard import routes

    app = web.Application()
    routes.register_all(app)
    served = [
        route.handler
        for route in app.router.routes()
        if getattr(agents, getattr(route.handler, "__name__", ""), None) is not None
        and route.handler.__module__ == _FACADE
    ]
    assert len({id(h) for h in served}) == 31
    assert [h.__name__ for h in served if getattr(agents, h.__name__) is not h] == []
    reexports = _package_reexports()
    assert len(reexports) == 34
    assert [n for n in reexports if getattr(handlers_pkg, n) is not getattr(agents, n)] == []


def _facade_importers() -> dict[str, set[str]]:
    """``{module path: names}`` every source file imports from the facade by name."""
    found: dict[str, set[str]] = {}
    for path in sorted((_SRC / "kiro_crew").rglob("*.py")):
        if path.resolve() == _FACADE_PATH or "_vendor" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        if _FACADE not in text:
            continue
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.ImportFrom) and node.module == _FACADE:
                found.setdefault(path.relative_to(_SRC).as_posix(), set()).update(
                    alias.name for alias in node.names
                )
    return found


def test_the_seams_other_modules_import_keep_their_identity() -> None:
    """Sibling handlers import the facade's privates by name, most of them inside a
    function. Each still resolves there, and the ones bound when a sibling loads are
    the composed objects -- so a sibling that loaded first and the facade agree."""
    from kiro_crew.dashboard.handlers import agent_catalog

    importers = _facade_importers()
    assert len(importers) >= 20
    imported = {name for names in importers.values() for name in names}
    assert {"_get_config_lock", "_roster_mask", "_roster_avatar", "_require_owner"} <= imported
    assert sorted(name for name in imported if not hasattr(agents, name)) == []
    for name in ("_agent_roster_row", "_name_would_be_masked", "_roster_mask"):
        assert getattr(agent_catalog, name) is getattr(agents, name)
        assert getattr(agent_catalog, name).__globals__ is vars(agents)
    # The messaging and file owners take the config lock from this module by name.
    assert agents._get_config_lock.__code__.co_filename == str(_FACADE_PATH)


#: Names the moved code imports in its own body, from the module that owns them. A
#: module-level import of any of them would put that module on the gateway's boot
#: path (or close an import cycle), so each stays function-local.
_IMPORTED_BY_NAME = {
    "DEGRADED_WHOLE_CONFIG": "kiro_crew.config.resolution",
    "MEMBER_CONFIG": "kiro_crew.eventlog.types",
    "REMOVE_REMOVED": "kiro_crew.crew_log.store",
    "_SENSITIVE_MASK": "kiro_crew.dashboard.handlers.core",
    "_get_mcp_lock": "kiro_crew.dashboard.handlers.mcp",
    "_is_background_only": "kiro_crew.dashboard.handlers.agent_catalog",
    "_offload_config_write": "kiro_crew.dashboard.handlers.mcp",
    "_sync_mcp_to_agent": "kiro_crew.dashboard.handlers.mcp",
    "get_service": "kiro_crew.eventlog.service",
    "inherited_template_action": "kiro_crew.dashboard.handlers.agent_capabilities",
    "member_slug": "kiro_crew.members",
    "normalize_member_source": "kiro_crew.dashboard.handlers.members",
    "redact_oauth_client_secrets": "kiro_crew.mcp_utils",
    "release_cached_memory_store": "kiro_crew.context",
    "release_markdown_memory_store": "kiro_crew.dashboard.handlers._shared",
    "restore_redacted_oauth_client_secrets": "kiro_crew.mcp_utils",
}


def test_the_lazy_imports_stay_inside_the_functions_that_need_them() -> None:
    local: dict[str, set[str]] = {name: set() for name in _IMPORTED_BY_NAME}
    top: list[str] = []
    for stem, source in _owner_sources().items():
        tree = ast.parse(source)
        module_level = {id(node) for node in tree.body}
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            for alias in node.names:
                if alias.name in local:
                    local[alias.name].add(node.module or "")
                    if id(node) in module_level:
                        top.append(f"{stem}:{node.lineno}:{alias.name}")
    assert local == {name: {module} for name, module in _IMPORTED_BY_NAME.items()}
    assert top == []


def test_a_star_import_carries_the_moved_public_names(tmp_path: Path) -> None:
    """The facade declares no ``__all__``, so every public binding goes out."""
    assert not hasattr(agents, "__all__")
    probe = tmp_path / "agents_star_probe.py"
    probe.write_text(
        "from kiro_crew.dashboard.handlers.agents import *  # noqa: F401,F403\n",
        encoding="utf-8",
    )
    spec = importlib.util.spec_from_file_location("agents_star_probe", probe)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in ("api_agent_detail", "api_kirocrew_agent_update", "AppOwnershipUnreadable"):
        assert getattr(module, name) is getattr(agents, name)


# ── the composition ───────────────────────────────────────────────────────────


def test_the_owner_set_is_the_package() -> None:
    assert {info.name for info in pkgutil.iter_modules([str(_OWNER_DIR)])} == set(_BASE_OWNERS)


def _shape(obj: Any) -> str:
    if inspect.isclass(obj):
        return "class"
    prefix = "async def " if inspect.iscoroutinefunction(obj) else "def "
    return prefix + str(inspect.signature(obj))


def test_every_moved_name_is_one_object_in_its_owner() -> None:
    """The facade's binding of a moved name is the owner's object, in the owner its
    responsibility names, and no name is defined by two owners."""
    strays = [
        f"{owner}:{name}"
        for owner, names in _BASE_OWNERS.items()
        for name in names
        if getattr(agents, name) is not vars(_owner(owner)).get(name)
    ]
    assert strays == []
    names = [name for group in _BASE_OWNERS.values() for name in group]
    assert len(names) == len(set(names)) == 94


def test_the_moved_names_keep_their_base_shapes() -> None:
    lines = sorted(f"{name} {_shape(getattr(agents, name))}" for name in _MOVED)
    assert len(lines) == 94
    digest = hashlib.sha256("\n".join(lines).encode()).hexdigest()
    assert digest == _BASE_SHAPE_DIGEST, "\n".join(lines)


def _module_assignments(source: str) -> set[str]:
    names = set()
    for node in ast.parse(source).body:
        targets = node.targets if isinstance(node, ast.Assign) else []
        if isinstance(node, ast.AnnAssign):
            targets = [node.target]
        names |= {t.id for t in targets if isinstance(t, ast.Name)}
    return names


def test_facade_state_stays_on_the_facade() -> None:
    """Owner functions reach module state by name through the facade's namespace,
    so a test that rebinds one there is the binding every function sees -- which
    holds only while no owner keeps a copy."""
    state = _module_assignments(_FACADE_PATH.read_text(encoding="utf-8"))
    assert {"logger", "_config_lock", "_catalog_cache", "_AVATAR_MAX_BYTES"} <= state
    assert {"_CAPABILITY_UNAVAILABLE", "_WINDOWS_RESERVED_NAMES", "_drained_to_thread"} <= state
    owned = {stem: sorted(_module_assignments(source)) for stem, source in _owner_sources().items()}
    assert {stem: names for stem, names in owned.items() if names} == {}


@pytest.mark.parametrize("name", _FACADE_DEFS)
def test_a_facade_definition_stays_in_the_facade_file(name: str) -> None:
    obj = getattr(agents, name)
    code = getattr(obj, "__code__", None)
    if code is not None:
        assert Path(code.co_filename).resolve() == _FACADE_PATH
    else:
        assert obj.__module__ == _FACADE
    assert [o.__name__ for o in _owners() if name in vars(o)] == []


def test_every_base_definition_is_in_exactly_one_place() -> None:
    """The facade keeps what ``_FACADE_DEFS`` names and the owners hold the rest:
    together they are the one-module file's definitions, each once."""
    defined = {
        node.name
        for node in ast.parse(_FACADE_PATH.read_text(encoding="utf-8")).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    assert defined == set(_FACADE_DEFS)
    assert len(defined | _MOVED) == len(defined) + len(_MOVED) == 124


def test_the_owners_log_as_the_facade() -> None:
    """Log capture keyed to ``kiro_crew.dashboard.handlers.agents`` keeps seeing the
    moved sites: an owner function logs through the facade's ``logger``."""
    assert agents.logger.name == _FACADE
    readers = [
        label for label, fn in _owner_functions() if "logger" in set(_global_names(fn.__code__))
    ]
    assert len(readers) >= 20


def test_every_owner_function_runs_on_the_facade_globals() -> None:
    """A patch of ``kiro_crew.dashboard.handlers.agents.<name>`` reaches an owner
    function only because the function reads the facade's globals, not its own."""
    labels = {label for label, _ in _owner_functions()}
    assert len(labels) >= 85
    strays = [
        label
        for label, fn in _owner_functions()
        if fn.__globals__ is not vars(agents) or fn.__module__ != _FACADE
    ]
    assert strays == []


def test_the_sweep_reports_a_global_the_facade_does_not_bind() -> None:
    """The name sweep can fail, nested bodies included."""

    def _probe() -> object:
        def _inner() -> object:
            return _absent_from_the_agents_namespace  # noqa: F821

        return _inner

    assert "_absent_from_the_agents_namespace" in set(_global_names(_probe.__code__))


def test_every_global_an_owner_function_reads_is_bound_on_the_facade() -> None:
    """An owner's own imports are inert for its functions, so a name missing from
    the facade surfaces only when its line runs -- often inside an ``except`` that
    turns the NameError into a refusal. The sweep makes it a test failure instead."""
    namespace = vars(agents)
    unresolved = sorted(
        (label, name)
        for label, fn in _owner_functions()
        for name in set(_global_names(fn.__code__))
        if name not in namespace and not hasattr(builtins, name)
    )
    assert unresolved == []


def test_a_patch_of_the_facade_reaches_an_owner_function(monkeypatch: pytest.MonkeyPatch) -> None:
    """The contract the rebinding exists for: the roster mask (roster) reads the
    facade's redactor, and the package-name check (capabilities) reads the facade's
    length bound."""
    monkeypatch.setattr(agents, "_redact_external", lambda text: "changed")
    assert agents._roster_mask("benign") != "benign"
    assert agents._is_valid_capability_package("pkg")
    monkeypatch.setattr(agents, "_MAX_CAPABILITY_PACKAGE_LEN", 2)
    assert not agents._is_valid_capability_package("pkg")


def test_module_and_qualname_still_name_the_facade() -> None:
    """Reprs and pickling by reference read as before the split: every owner
    function resolves back through its own ``__module__`` and ``__qualname__``. A
    class keeps its owner module, which is where ``inspect`` finds its source."""
    wrong = []
    for label, fn in _owner_functions():
        target: object = sys.modules[fn.__module__]
        for part in fn.__qualname__.split("."):
            target = (
                vars(target).get(part) if isinstance(target, type) else getattr(target, part, None)
            )
            target = getattr(target, "__func__", target)
        if target is not fn:
            wrong.append(label)
    assert wrong == []
    assert agents.AppOwnershipUnreadable.__module__ == f"{_OWNER_PACKAGE}.app_mcp_ownership"
    assert agents._StaleBinding.__module__ == f"{_OWNER_PACKAGE}.template_lineage"


def test_a_moved_function_reads_its_source_from_its_owner() -> None:
    source = inspect.getsource(agents.api_agent_fork)
    assert source.startswith("async def api_agent_fork(")
    assert inspect.getsourcefile(agents.api_agent_fork) == _owner("fork_publish").__file__


def test_the_compose_copy_matches_its_siblings() -> None:
    """The three dashboard compositions share one technique; each package keeps its
    own copy so no package imports another's, and the copies stay identical."""

    def body(package: str) -> str:
        text = (_SRC / "kiro_crew/dashboard" / package / "__init__.py").read_text(encoding="utf-8")
        return text[text.index("def compose(") :]

    assert body("agent_admin") == body("file_api") == body("messaging_api")


# ── one edge ──────────────────────────────────────────────────────────────────


def _package_of(path: Path) -> str:
    parts = list(path.resolve().relative_to(_SRC).with_suffix("").parts)
    return ".".join(parts[:-1])


def _import_targets(tree: ast.Module, package: str) -> list[tuple[ast.AST, str]]:
    """``(node, dotted module)`` for every module a tree imports, spelled any way:
    ``import a.b``, ``from a import b``, relative imports resolved against
    *package*, and a string-literal ``import_module(...)`` / ``__import__(...)``."""
    found: list[tuple[ast.AST, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((node, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                base = importlib.util.resolve_name("." * node.level + base, package)
            found.append((node, base))
            found.extend((node, f"{base}.{alias.name}") for alias in node.names)
        elif (
            isinstance(node, ast.Call)
            and getattr(node.func, "attr", getattr(node.func, "id", ""))
            in ("import_module", "__import__")
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            and not node.args[0].value.startswith(".")
        ):
            found.append((node, node.args[0].value))
    return found


def _within(target: str, module: str) -> bool:
    return target == module or target.startswith(f"{module}.")


def _type_checking_nodes(tree: ast.Module) -> set[int]:
    """Nodes under a module-level ``if TYPE_CHECKING:`` body; its ``else`` runs."""
    return {
        id(sub)
        for node in tree.body
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING"
        for stmt in node.body
        for sub in ast.walk(stmt)
    }


def _owner_runtime_edges(source: str, package: str) -> list[int]:
    """Lines where an owner imports a project module outside ``TYPE_CHECKING`` at
    module level: the facade, a sibling owner, or anything else under ``kiro_crew``."""
    tree = ast.parse(source)
    guarded = _type_checking_nodes(tree)
    module_level = {id(sub) for node in tree.body for sub in ast.walk(node)}
    function_bodies = {
        id(sub)
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for sub in ast.walk(node)
    }
    return sorted(
        {
            node.lineno
            for node, target in _import_targets(tree, package)
            if _within(target, "kiro_crew")
            and id(node) not in guarded
            and id(node) in module_level
            and id(node) not in function_bodies
        }
    )


@pytest.mark.parametrize(
    ("source", "flagged"),
    [
        ("from . import roster\n", True),
        ("from .roster import _roster_mask\n", True),
        ("from ..handlers import agents\n", True),
        ("from kiro_crew.dashboard.handlers import agents\n", True),
        ("import kiro_crew.dashboard.handlers.agents as handlers\n", True),
        (
            "import importlib\nimportlib.import_module('kiro_crew.dashboard.handlers.agents')\n",
            True,
        ),
        ("__import__('kiro_crew.dashboard.agent_admin.roster')\n", True),
        ("if TYPE_CHECKING:\n    from kiro_crew.dashboard.handlers.agents import _sel\n", False),
        ("def f():\n    from kiro_crew.mcp_utils import redact_oauth_client_secrets\n", False),
        ("import asyncio\nfrom aiohttp import web\n", False),
    ],
)
def test_the_owner_edge_check_sees_every_spelling(source: str, flagged: bool) -> None:
    assert bool(_owner_runtime_edges(source, _OWNER_PACKAGE)) is flagged


def test_nothing_but_the_facade_imports_an_owner() -> None:
    """The facade is the one import path and the one patch surface."""
    importers = []
    for path in sorted((_SRC / "kiro_crew").rglob("*.py")):
        resolved = path.resolve()
        if _OWNER_DIR in resolved.parents or resolved == _FACADE_PATH or "_vendor" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        if "agent_admin" not in text:
            continue
        importers.extend(
            f"{path.relative_to(_SRC)}:{node.lineno}"
            for node, target in _import_targets(ast.parse(text), _package_of(path))
            if _within(target, _OWNER_PACKAGE)
        )
    assert importers == []


def test_an_owner_imports_project_modules_only_for_type_checking() -> None:
    """An owner's module-level imports are stdlib and third-party only, plus its
    ``TYPE_CHECKING`` names from the facade. Its project names come from the
    facade's globals, so an owner adds nothing to the gateway's boot import graph and
    cannot close a cycle with the facade; the lazy imports the moved code made at the
    base stay inside their functions (``_IMPORTED_BY_NAME``)."""
    offenders = {
        stem: lines
        for stem, source in _owner_sources().items()
        if (lines := _owner_runtime_edges(source, _OWNER_PACKAGE))
    }
    assert offenders == {}
    for source in _owner_sources().values():
        tree = ast.parse(source)
        guarded = [
            node
            for node in ast.walk(tree)
            if id(node) in _type_checking_nodes(tree) and isinstance(node, ast.ImportFrom)
        ]
        assert [node.module for node in guarded] in ([], [_FACADE])


def test_a_fresh_facade_import_loads_every_owner(tmp_path: Path) -> None:
    """Importing the facade imports every owner with it: none loads lazily on a
    later call, so the import order stays the one the one-module file had."""
    _run_child(
        tmp_path,
        """
        import sys
        import kiro_crew.dashboard.handlers.agents
        missing = [n for n in sys.argv[1:]
                   if f"kiro_crew.dashboard.agent_admin.{n}" not in sys.modules]
        assert missing == [], missing
        print("ok")
        """,
        *_BASE_OWNERS,
    )


def test_a_sibling_that_loads_first_still_binds_the_composed_seams(tmp_path: Path) -> None:
    """``agent_catalog`` imports three roster seams by name at module level. Loaded
    before anything else, it pulls the facade in, and what it binds is the composed
    object running on the facade's globals."""
    _run_child(
        tmp_path,
        """
        import kiro_crew.dashboard.handlers.agent_catalog as catalog
        import kiro_crew.dashboard.handlers.agents as agents
        for name in ("_agent_roster_row", "_name_would_be_masked", "_roster_mask"):
            assert getattr(catalog, name) is getattr(agents, name), name
            assert getattr(catalog, name).__globals__ is vars(agents), name
        print("ok")
        """,
    )


def test_a_second_facade_import_recomposes_the_owners_onto_it(tmp_path: Path) -> None:
    _run_child(
        tmp_path,
        """
        import sys
        import kiro_crew.dashboard.handlers.agents as first
        del sys.modules["kiro_crew.dashboard.handlers.agents"]
        import kiro_crew.dashboard.handlers.agents as second
        from kiro_crew.dashboard.agent_admin import roster
        assert second is not first
        assert second._roster_mask.__globals__ is vars(second)
        assert roster._roster_mask is second._roster_mask
        print("ok")
        """,
    )


# ── the patch reach ───────────────────────────────────────────────────────────

_PATCH_CALLS = ("setattr", "patch.object", "delattr")
_MULTIPLE_OPTIONS = frozenset({"spec", "create", "spec_set", "autospec", "new_callable"})
_FACADE_STRING = re.compile(r"""^kiro_crew\.dashboard\.handlers\.agents\.(\w+)$""")

#: The patches whose attribute the scan cannot resolve from the source, keyed by
#: (test file, enclosing function), with the names each one rebinds.
_RESOLVED_DYNAMIC_PATCHES: dict[tuple[str, str], frozenset[str]] = {}


def _facade_aliases(tree: ast.Module) -> set[str]:
    """Every expression spelling a test module binds to the facade, to a fixed point."""
    aliases = {_FACADE, f"sys.modules[{_FACADE!r}]", f'sys.modules["{_FACADE}"]'}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            aliases |= {a.asname for a in node.names if a.name == _FACADE and a.asname}
        elif isinstance(node, ast.ImportFrom) and node.module == "kiro_crew.dashboard.handlers":
            aliases |= {a.asname or a.name for a in node.names if a.name == "agents"}
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target, value = node.targets[0], node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                target, value = node.target, node.value
            else:
                continue
            if not isinstance(target, ast.Name) or target.id in aliases:
                continue
            if ast.unparse(value) in aliases or _imports_the_facade(value):
                aliases.add(target.id)
                changed = True
    return aliases


def _imports_the_facade(node: ast.AST) -> bool:
    """``import_module(<facade>)``, or ``__import__(<facade>, fromlist=...)`` with a
    non-empty fromlist (which returns the facade itself, not its root package)."""
    if not (
        isinstance(node, ast.Call)
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == _FACADE
    ):
        return False
    func = ast.unparse(node.func)
    if func.endswith("import_module"):
        return True
    fromlist = {k.arg: k.value for k in node.keywords}.get("fromlist")
    if fromlist is None and len(node.args) >= 4:
        fromlist = node.args[3]
    return (
        func == "__import__" and isinstance(fromlist, (ast.List, ast.Tuple)) and bool(fromlist.elts)
    )


def _parametrized_strings(function: ast.AST) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for decorator in getattr(function, "decorator_list", []):
        if not (
            isinstance(decorator, ast.Call)
            and ast.unparse(decorator.func).endswith("parametrize")
            and len(decorator.args) >= 2
            and isinstance(decorator.args[0], ast.Constant)
            and isinstance(decorator.args[1], (ast.List, ast.Tuple))
        ):
            continue
        names = [n.strip() for n in str(decorator.args[0].value).split(",")]
        if len(names) == 1:
            values = {e.value for e in decorator.args[1].elts if isinstance(e, ast.Constant)}
            if values and all(isinstance(v, str) for v in values):
                found[names[0]] = values
    return found


def _resolve_name(node: ast.AST, params: dict[str, set[str]]) -> set[str] | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.Name) and node.id in params:
        return set(params[node.id])
    return None


def _facade_strings(tree: ast.Module, aliases: set[str]) -> set[str]:
    """Every name a test module binds to the facade's dotted path, to a fixed point."""
    strings: set[str] = set()
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target, value = node.targets[0], node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                target, value = node.target, node.value
            else:
                continue
            if isinstance(target, ast.Name) and target.id not in strings:
                if _string_text(value, strings, aliases) == _FACADE:
                    strings.add(target.id)
                    changed = True
    return strings


def _string_text(node: ast.AST, strings: set[str], aliases: set[str]) -> str | None:
    """The text one piece of a string spells: a constant, a name bound to the
    facade's dotted path, or ``<facade alias>.__name__``; None for anything else."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name) and node.id in strings:
        return _FACADE
    if (
        isinstance(node, ast.Attribute)
        and node.attr == "__name__"
        and ast.unparse(node.value) in aliases
    ):
        return _FACADE
    return None


def _string_pieces(node: ast.AST) -> list[ast.AST]:
    if isinstance(node, ast.JoinedStr):
        return [v.value if isinstance(v, ast.FormattedValue) else v for v in node.values]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _string_pieces(node.left) + _string_pieces(node.right)
    return [node]


def _resolve_target(
    node: ast.AST, params: dict[str, set[str]], strings: set[str], aliases: set[str]
) -> set[str] | None:
    """The names a string patch target rebinds on the facade: an empty set when it
    names another module, None when it names the facade but the scan cannot tell
    which attribute."""
    pieces = _string_pieces(node)
    head = ""
    for index, piece in enumerate(pieces):
        text = _string_text(piece, strings, aliases)
        if text is None:
            break
        head += text
    else:
        match = _FACADE_STRING.match(head)
        return {match.group(1)} if match else set()
    if not head.startswith(f"{_FACADE}."):
        return set()
    if head == f"{_FACADE}." and index == len(pieces) - 1:
        return _resolve_name(pieces[index], params)
    return None


def _patched_names_in(text: str) -> tuple[set[str], set[str]]:
    """``(names, dynamic)``: first-level names one test source rebinds on the
    facade, and the enclosing functions of each patch whose name the scan cannot
    resolve -- which fails the reach test closed unless it is a resolved one."""
    # Every spelling below names the facade's dotted path or imports ``agents`` from
    # the handlers package, so a source with neither holds no facade patch.
    if "handlers.agents" not in text and not (
        "kiro_crew.dashboard.handlers import" in text and re.search(r"\bagents\b", text)
    ):
        return set(), set()
    tree = ast.parse(text)
    aliases = _facade_aliases(tree)
    strings = _facade_strings(tree, aliases)
    found: set[str] = set()
    dynamic: set[str] = set()
    functions = [
        n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    seen: set[int] = set()
    scopes = [(tree, {}, "<module>")] + [
        (fn, _parametrized_strings(fn), fn.name) for fn in functions
    ]
    for scope, params, label in reversed(scopes):
        for node in ast.walk(scope):
            if id(node) in seen:
                continue
            seen.add(id(node))
            if isinstance(node, ast.Call):
                func = ast.unparse(node.func)
                kw = {k.arg: k.value for k in node.keywords if k.arg}
                target = node.args[0] if node.args else kw.get("target")
                if target is not None and (
                    ast.unparse(target) in aliases or _imports_the_facade(target)
                ):
                    if func.endswith(_PATCH_CALLS):
                        name = (
                            node.args[1]
                            if len(node.args) >= 2
                            else kw.get("attribute", kw.get("name"))
                        )
                        resolved = _resolve_name(name, params) if name is not None else None
                        if resolved is None:
                            dynamic.add(label)
                        else:
                            found |= resolved
                    elif func.endswith("patch.multiple"):
                        if any(k.arg is None for k in node.keywords):
                            dynamic.add(label)
                        found |= {k for k in kw if k not in _MULTIPLE_OPTIONS}
                elif target is not None and func.split(".")[-1] in ("patch", "setattr", "delattr"):
                    resolved = _resolve_target(target, params, strings, aliases)
                    if resolved is None:
                        dynamic.add(label)
                    else:
                        found |= resolved
            elif isinstance(node, (ast.Assign, ast.AugAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Attribute) and ast.unparse(target.value) in aliases:
                        found.add(target.attr)
    found |= set(re.findall(r"""["']kiro_crew\.dashboard\.handlers\.agents\.(\w+)["']""", text))
    return found, dynamic


def _facade_patched_names() -> set[str]:
    root = repo_root()
    here = Path(__file__).resolve()
    found: set[str] = set()
    unresolved = []
    for path in repo_files_named(".py"):
        parts = path.relative_to(root).parts
        in_tests = parts[0] == "test" or (parts[0] == "src" and "tests" in parts)
        if in_tests and path.resolve() != here:
            names, dynamic = _patched_names_in(path.read_text(encoding="utf-8", errors="replace"))
            found |= names
            for label in dynamic:
                key = (path.name, label)
                if key in _RESOLVED_DYNAMIC_PATCHES:
                    found |= _RESOLVED_DYNAMIC_PATCHES[key]
                else:
                    unresolved.append(key)
    assert unresolved == [], "a test patches a facade name the scan cannot resolve"
    return found


def _captured_names(source: str) -> set[str]:
    """Names an owner module binds or evaluates when it LOADS, outside
    ``TYPE_CHECKING``: everything a later patch of the facade cannot reach."""
    tree = ast.parse(source)
    guarded = _type_checking_nodes(tree)
    found: set[str] = set()

    def loads(node: ast.AST) -> set[str]:
        return {
            n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
        }

    def visit(statements: list[ast.stmt]) -> None:
        for node in statements:
            if id(node) in guarded:
                continue
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                found.update((a.asname or a.name).split(".")[0] for a in node.names)
                found.update(a.name.split(".")[-1] for a in node.names)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for part in node.decorator_list + node.args.defaults:
                    found.update(loads(part))
                for part in node.args.kw_defaults:
                    if part is not None:
                        found.update(loads(part))
            elif isinstance(node, ast.ClassDef):
                for part in node.decorator_list + node.bases + [k.value for k in node.keywords]:
                    found.update(loads(part))
                for stmt in node.body:
                    if isinstance(stmt, (ast.AnnAssign, ast.Assign)) and stmt.value is not None:
                        found.update(loads(stmt.value))
            elif isinstance(node, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
                for field in ("test", "iter", "items"):
                    value = getattr(node, field, None)
                    if isinstance(value, ast.AST):
                        found.update(loads(value))
                    elif isinstance(value, list):
                        for item in value:
                            found.update(loads(item))
                for block in ("body", "orelse", "finalbody"):
                    visit(getattr(node, block, []))
                for handler in getattr(node, "handlers", []):
                    if handler.type is not None:
                        found.update(loads(handler.type))
                    visit(handler.body)
            elif not (
                node is tree.body[0]
                and isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                found.update(loads(node))

    visit(tree.body)
    return found


def test_the_patch_scan_reads_every_spelling() -> None:
    planted = (
        "import importlib, sys\n"
        "import kiro_crew.dashboard.handlers.agents as handlers\n"
        "from kiro_crew.dashboard.handlers import agents as ag\n"
        "facade = importlib.import_module('kiro_crew.dashboard.handlers.agents')\n"
        "alias = facade\n"
        "held = sys.modules['kiro_crew.dashboard.handlers.agents']\n"
        "def test(monkeypatch):\n"
        "    monkeypatch.setattr(handlers, 'first', 1)\n"
        "    monkeypatch.setattr(ag, 'second', 2)\n"
        "    patch.object(alias, 'third')\n"
        "    ag.fourth = 4\n"
        "    monkeypatch.setattr('kiro_crew.dashboard.handlers.agents.fifth', 5)\n"
        "    monkeypatch.setattr(ag.Shared, 'attr', 7)\n"
        "    monkeypatch.setattr(other, 'not_the_facade', 8)\n"
        "    monkeypatch.delattr(ag, 'sixth')\n"
        "    patch.object(target=alias, attribute='seventh')\n"
        "    patch.multiple(held, eighth=1, create=True)\n"
        "    patch('kiro_crew.dashboard.handlers.agents.KiroCrewConfig.load')\n"
        "@pytest.mark.parametrize('which', ['ninth', 'tenth'])\n"
        "def test_param(which):\n"
        "    patch(f'kiro_crew.dashboard.handlers.agents.{which}')\n"
        "FACADE = 'kiro_crew.dashboard.handlers.agents'\n"
        "other = 'kiro_crew.dashboard.handlers.members'\n"
        "def test_strings(monkeypatch):\n"
        "    path = FACADE\n"
        "    patch(f'{path}.eleventh')\n"
        "    patch(FACADE + '.twelfth')\n"
        "    monkeypatch.setattr(f'{ag.__name__}.thirteenth', 13)\n"
        "    patch(f'{other}.not_the_facade')\n"
        "    patch(f'{FACADE}_twin.not_the_facade')\n"
    )
    names, dynamic = _patched_names_in(planted)
    assert names == {
        "first",
        "second",
        "third",
        "fourth",
        "fifth",
        "sixth",
        "seventh",
        "eighth",
        "ninth",
        "tenth",
        "eleventh",
        "twelfth",
        "thirteenth",
    }
    assert dynamic == set()
    unresolvable = (
        "from kiro_crew.dashboard.handlers import agents as ag\n"
        "def _drive(monkeypatch, name):\n"
        "    monkeypatch.setattr(ag, name, 1)\n"
    )
    assert _patched_names_in(unresolvable) == (set(), {"_drive"})


def test_the_capture_scan_flags_what_an_owner_evaluates_when_it_loads() -> None:
    planted = (
        '"""An owner."""\n'
        "from typing import TYPE_CHECKING\n"
        "import asyncio as aio\n"
        "if TYPE_CHECKING:\n"
        "    from kiro_crew.dashboard.handlers.agents import _sel\n"
        "LIMIT = _CAP * 2\n"
        "def f(x=_DEFAULT, *, y=_KW):\n"
        "    return _sel(), list_agents\n"
        "class C(_Base):\n"
        "    attr: int = _CLASS_BODY\n"
    )
    captured = _captured_names(planted)
    assert {"aio", "asyncio", "_CAP", "_DEFAULT", "_KW", "_Base", "_CLASS_BODY"} <= captured
    assert {"_sel", "list_agents"} & captured == set()


def test_no_owner_captures_a_name_tests_rebind_on_the_facade() -> None:
    """An owner that imported, defaulted or evaluated a rebound name when it loaded
    would keep that object, and a patch of the facade would silently stop applying
    there. An owner may DEFINE one: the facade's binding of it is the composed copy,
    and every caller reads it through the facade's globals."""
    patched = _facade_patched_names()
    assert {
        "_require_owner",
        "_sel",
        "list_agents",
        "update_config_locked",
        "write_config_atomically",
        "_rebind_crew_locked",
        "_refresh_session_defaults",
        "_write_spec_file",
        "_installed_agent_config",
        "sanitize_agent_config_governance",
        "_AVATAR_MAX_BYTES",
        "_capability_manager",
        "wrap_argv",
    } <= patched
    assert len(patched) >= 45
    for stem, source in _owner_sources().items():
        assert _captured_names(source) & patched == set(), stem
        defined = {name for name in vars(_owner(stem)) if name in patched}
        assert all(getattr(agents, name) is vars(_owner(stem))[name] for name in defined), stem


# ── the guards keep their reach ───────────────────────────────────────────────

#: Constructs repository guards read in ``dashboard/handlers/agents.py`` by path: the
#: harness-parity sandbox literal; the conditional-GET helper the media-route fence
#: reads this module for; the backend capability and identity reads the SDK-boundary
#: ratchets key here; the ``project_agent_names`` call the hardened-reads census keys
#: here; and the one ACP-layer import the boundary baseline counts for this path. An
#: owner that grew one would move it out of the guard's sight, so each stays here.
_STAYS_IN_THE_FACADE = (
    r"return wrap_argv\(argv, mode=configured_sandbox_mode\(\), is_kiro_cli=True\)",
    r"\bconditional_response\(",
    r"\bcapabilities_(?:for|of)\(|\bis_claude_code\(|\bACP_BACKEND_CLAUDE\b",
    r'operation="api_kirocrew_agents"',
    r"\bkiro_crew\.(?:acp|providers)\b",
)


@pytest.mark.parametrize("pattern", _STAYS_IN_THE_FACADE)
def test_a_construct_a_guard_reads_in_the_facade_stays_there(pattern: str) -> None:
    assert re.search(pattern, _FACADE_PATH.read_text(encoding="utf-8"))
    holders = [stem for stem, source in _owner_sources().items() if re.search(pattern, source)]
    assert holders == []


def _mirror(tmp_path: Path, *extra: str) -> Path:
    """A checkout-shaped ``src`` copy of the facade, its owners and *extra* files."""
    root = tmp_path / "checkout" / "src"
    rels = [
        "kiro_crew/dashboard/handlers/agents.py",
        *(f"kiro_crew/dashboard/agent_admin/{path.name}" for path in _OWNER_DIR.glob("*.py")),
        *extra,
    ]
    for rel in rels:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(_SRC / rel, target)
    return root


def _red_green(path: Path, check: Any, new: str) -> None:
    """*check* passes, fails once *new* is appended to *path*, and passes again once
    the file is restored."""
    text = path.read_text(encoding="utf-8")
    check()
    path.write_text(text + new, encoding="utf-8")
    with pytest.raises(AssertionError):
        check()
    path.write_text(text, encoding="utf-8")
    check()


def test_the_sdk_identity_ratchet_reads_the_owners(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import test_agent_sdk_capabilities as guard

    rel = "dashboard/handlers/agents.py"
    assert len(guard._composed_owner_rels(rel)) == len(_BASE_OWNERS)
    root = _mirror(tmp_path) / "kiro_crew"
    monkeypatch.setattr(guard, "SRC", root)
    planted = "\n\ndef _planted(backend):\n    return backend == ACP_BACKEND_CLAUDE\n"
    _red_green(
        root / "dashboard/agent_admin/roster.py",
        lambda: guard.test_no_migrated_consumer_reads_a_backend_identity(rel),
        planted,
    )


def test_the_provider_literal_ratchet_reads_the_owners(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import test_agent_sdk_provider_identity as guard

    rel = "dashboard/handlers/agents.py"
    assert len(guard._composed_owner_paths(rel)) == len(_BASE_OWNERS)
    root = _mirror(tmp_path) / "kiro_crew"
    monkeypatch.setattr(guard, "SRC", root)
    planted = '\n\ndef _planted(provider):\n    return provider == "claude_code"\n'
    _red_green(
        root / "dashboard/agent_admin/crew_update.py",
        lambda: guard.test_no_converted_file_compares_the_literal(rel),
        planted,
    )


def test_the_import_hoist_ratchet_reads_the_owners(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import test_agent_import_hoist as guard

    root = _mirror(tmp_path, *(rel for rel in guard._HOISTED_FILES if "agents.py" not in rel))
    monkeypatch.setattr(guard, "_SRC", root)
    planted = "\n\ndef _planted():\n    from kiro_crew.agent import install_agent\n"
    _red_green(
        root / "kiro_crew/dashboard/agent_admin/capabilities.py",
        guard.test_no_function_local_agent_imports_remain,
        planted,
    )


def test_the_default_model_fence_reads_the_owners(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The str()-coercion fence reads the facade and every owner, and its floor
    fails when the owners it counts on are missing."""
    import test_agent_default_model as guard

    fence = guard.TestApiDoesNotStringifyModels()
    text = fence._handler_source()
    assert all(source in text for source in _owner_sources().values())
    owners = _mirror(tmp_path) / "kiro_crew/dashboard/agent_admin"
    monkeypatch.setattr(agent_admin, "__file__", str(owners / "__init__.py"))
    planted = "\n\ndef _planted(body):\n    return normalize_agent_model(str(body))\n"
    _red_green(
        owners / "crew_records.py", fence.test_no_str_coercion_around_the_normalizer, planted
    )
    for path in owners.glob("crew_*.py"):
        path.unlink()
    with pytest.raises(AssertionError):
        fence.test_no_str_coercion_around_the_normalizer()


def test_the_media_route_fence_reads_the_owners(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The conditional-GET fence reads every owner with the facade, so a raw
    ``If-None-Match`` compare grown in an owner fails it."""
    import test_conditional_get as guard

    owners = _mirror(tmp_path) / "kiro_crew/dashboard/agent_admin"
    monkeypatch.setattr(agent_admin, "__file__", str(owners / "__init__.py"))
    planted = "\n\ndef _planted(request):\n    return request.if_none_match\n"
    _red_green(owners / "avatars.py", guard.test_every_media_route_uses_the_shared_helper, planted)


def _mutated(tmp_path: Path, stem: str, old: str, new: str) -> types.ModuleType:
    """The *stem* owner loaded from a copy with *old* replaced by *new*, so
    ``inspect.getsource`` of its functions reads the mutated text."""
    source = (_OWNER_DIR / f"{stem}.py").read_text(encoding="utf-8")
    assert source.count(old) == 1, old
    path = tmp_path / f"mutated_{stem}.py"
    path.write_text(source.replace(old, new), encoding="utf-8")
    spec = importlib.util.spec_from_file_location(f"mutated_{stem}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_roster_snapshot_lockstep_reads_the_update_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lockstep check follows the crew PUT handler wherever it lives, and fails
    when that handler's ``_ev_before`` snapshot drops a field."""
    import test_crew_display_name as guard

    check = guard.TestProjectionLockstep().test_agents_handler_snapshots_carry_every_config_field
    check()
    mutated = _mutated(
        tmp_path,
        "crew_update",
        '            "display_name": agent.display_name,\n        }\n        changed',
        "        }\n        changed",
    )
    monkeypatch.setattr(agents, "api_kirocrew_agent_update", mutated.api_kirocrew_agent_update)
    with pytest.raises(AssertionError):
        check()


def test_the_reclaim_placement_check_reads_the_delete_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The structural check follows the crew DELETE handler wherever it lives, and
    fails when its reclaim leaves the namespace-lock hold."""
    import test_crew_log_member_unit_reclaim as guard

    guard.test_no_reclaim_call_sits_outside_a_namespace_lock_hold()
    mutated = _mutated(tmp_path, "crew_removal", "        @memory_store_namespace_lock()\n", "")
    monkeypatch.setattr(
        guard.agents_mod, "api_kirocrew_agent_delete", mutated.api_kirocrew_agent_delete
    )
    with pytest.raises(AssertionError):
        guard.test_no_reclaim_call_sits_outside_a_namespace_lock_hold()


def test_the_error_code_rows_match_each_owner(tmp_path: Path) -> None:
    """The baseline's per-file rows for the facade and its owners are each one's
    measured count and sum to the one-module file's 46, and the scan counts a
    response planted in an owner against that owner."""
    import test_error_code_contract as guard

    baseline = json.loads((repo_root() / "error-code-baseline.json").read_text(encoding="utf-8"))
    rows = {
        path: counts
        for path, counts in baseline["files"].items()
        if path == "dashboard/handlers/agents.py" or path.startswith("dashboard/agent_admin/")
    }
    live = guard.tally(guard.scan())
    assert rows == {path: live[path] for path in rows}
    assert sum(counts.get("missing_code", 0) for counts in rows.values()) == 46
    assert {path for path in live if path.startswith("dashboard/agent_admin/")} <= set(rows)
    root = _mirror(tmp_path) / "kiro_crew"
    owner = root / "dashboard/agent_admin/roster.py"
    scan = guard._scan_uncached.__wrapped__
    before = guard.tally(scan(root))
    owner.write_text(
        owner.read_text(encoding="utf-8")
        + '\n\nasync def _planted():\n    return web.json_response({"error": "x"}, status=400)\n',
        encoding="utf-8",
    )
    after = guard.tally(scan(root))
    path = "dashboard/agent_admin/roster.py"
    assert after[path]["missing_code"] == before.get(path, {}).get("missing_code", 0) + 1


#: The JSON-body register rows the move re-keyed, by the owner each route now
#: lives in.
_REKEYED_REGISTER_ROWS = (
    "agent_admin/agent_config.py::api_agent_config",
    "agent_admin/default_agent.py::api_default_agent",
    "agent_admin/capabilities.py::api_capability_mcp_install",
    "agent_admin/capabilities.py::api_capability_mcp_uninstall",
    "agent_admin/capabilities.py::api_capability_skills_install",
    "agent_admin/capabilities.py::api_capability_skills_uninstall",
)


def test_the_json_body_register_names_the_owners() -> None:
    import test_json_object_body_guard as guard

    sites = guard._call_sites()
    assert [row for row in _REKEYED_REGISTER_ROWS if row not in sites] == []
    assert [row for row in _REKEYED_REGISTER_ROWS if row not in guard._CAP_REGISTER] == []
    assert (
        sorted(key for key in guard._CAP_REGISTER if key.startswith("handlers/agents.py::")) == []
    )
    assert sorted(key for key in sites if key.startswith("handlers/agents.py::")) == []


def test_the_hardened_read_census_names_the_owners() -> None:
    """The spec-read labels moved with their call sites; only ``GET /api/agents``'s
    project-name scan stays keyed to the facade."""
    import test_agent_spec_hardened_reads as guard

    facade = "kiro_crew/dashboard/handlers/agents.py"
    reads = guard._labelled_call_sites("_read_agent_spec")
    assert facade not in reads
    assert {path: pairs for path, pairs in reads.items() if "/agent_admin/" in path} == {
        "kiro_crew/dashboard/agent_admin/agent_detail.py": [("api_agent_detail", "dashboard")] * 3,
        "kiro_crew/dashboard/agent_admin/fork_publish.py": [
            ("api_agent_fork", "dashboard"),
            ("api_agent_publish", "dashboard"),
        ],
        "kiro_crew/dashboard/agent_admin/installed_agents.py": [("api_agents_sync", "dashboard")],
        "kiro_crew/dashboard/agent_admin/template_lineage.py": [("forward:operation", "dashboard")],
    }
    assert guard._labelled_call_sites("project_agent_names")[facade] == [
        ("api_kirocrew_agents", "dashboard")
    ]


def test_every_redacting_owner_is_classified_like_the_facade() -> None:
    """The owners that call a redactor sit in the same posture bucket as the facade,
    and none grows a gate-side baseline log site."""
    from test_security_posture import _REDACTOR_CALL_RE, _gate_side_baseline_log_sites

    from kiro_crew import security_posture

    redacting = {
        f"dashboard/agent_admin/{stem}.py"
        for stem, source in _owner_sources().items()
        if _REDACTOR_CALL_RE.search(source)
    }
    assert len(redacting) == 4
    allowlisted = security_posture.NON_EGRESS_REDACTION_MODULES
    assert redacting | {"dashboard/handlers/agents.py"} <= allowlisted
    sinks = {module for _label, module, _detail in security_posture._REDACTION_SINKS}
    assert {m for m in sinks if "agent_admin" in m or m.endswith("handlers/agents.py")} == set()
    assert not _gate_side_baseline_log_sites(_FACADE_PATH.read_text(encoding="utf-8"))
    assert {
        stem for stem, s in _owner_sources().items() if _gate_side_baseline_log_sites(s)
    } == set()


# ── the review rules keep their reach ─────────────────────────────────────────


def _globstar(pattern: str) -> re.Pattern[str]:
    """An AUTOSDE pattern as a regex: ``**/`` spans zero or more directories, ``*``
    and ``?`` stay inside one path segment."""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out, i = out + "(?:[^/]+/)*", i + 3
        elif pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pattern[i] in "*?":
            out, i = out + ("[^/]*" if pattern[i] == "*" else "[^/]"), i + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.compile(out + r"\Z")


def _owner_paths() -> list[str]:
    root = repo_root()
    return sorted(path.relative_to(root).as_posix() for path in _OWNER_DIR.glob("*.py"))


def _rules_missing_owners(rules: list[dict]) -> tuple[set[str], dict[str, list[str]]]:
    """``(ids of rules matching the facade, {id: owner files those rules miss})``."""
    facade = "src/kiro_crew/dashboard/handlers/agents.py"
    matched: set[str] = set()
    missing: dict[str, list[str]] = {}
    for rule in rules:
        patterns = [_globstar(p) for p in rule.get("file-patterns", [])]
        if not any(p.match(facade) for p in patterns):
            continue
        matched.add(rule["id"])
        gaps = [o for o in _owner_paths() if not any(p.match(o) for p in patterns)]
        if gaps:
            missing[rule["id"]] = gaps
    return matched, missing


def test_the_globstar_matcher_reads_patterns_as_the_reviewers_do() -> None:
    assert _globstar("src/a/**/*.py").match("src/a/b.py")
    assert _globstar("src/a/**/*.py").match("src/a/x/y/b.py")
    assert not _globstar("src/a/*.py").match("src/a/x/b.py")
    assert _globstar("src/a/**").match("src/a/x/b.py")


def test_every_review_rule_on_the_facade_also_covers_its_owners() -> None:
    """A rule that reviews the facade reviews the code composed into it: an owner
    outside its patterns would take moved code out of that rule's sight."""
    import yaml

    root = repo_root()
    rules = [
        rule
        for name in ("AUTOSDE.yaml", "website/AUTOSDE.yaml")
        for rule in yaml.safe_load((root / name).read_text(encoding="utf-8"))["custom-rules"]
    ]
    matched, missing = _rules_missing_owners(rules)
    assert {
        "memory-store-seam",
        "no-new-work-on-gateway-boot-path",
        "feature-map-correctness",
    } <= matched
    assert missing == {}
    narrowed = [
        (
            {**rule, "file-patterns": [p for p in rule["file-patterns"] if "agent_admin" not in p]}
            if rule["id"] == "memory-store-seam"
            else rule
        )
        for rule in rules
    ]
    assert _rules_missing_owners(narrowed)[1].keys() == {"memory-store-seam"}


# ── compose, on its own ───────────────────────────────────────────────────────


def _write_module(tmp_path: Path, name: str, source: str) -> types.ModuleType:
    path = tmp_path / f"{name}.py"
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_compose_rebinds_functions_and_class_members(tmp_path: Path) -> None:
    """On synthetic modules: a module function, a nested function, a method, a
    static method and a property of an owner class all read the host namespace
    afterwards; a function the owner merely imported is left alone; the owner class
    keeps its own module; and a second compose onto a fresh namespace moves them."""
    owner = _write_module(
        tmp_path,
        "agent_admin_compose_owner_probe",
        """
        from os.path import join

        def helper():
            return VALUE

        def outer():
            def inner():
                return VALUE
            return inner

        class Tally:
            def method(self):
                return VALUE

            @staticmethod
            def static():
                return VALUE

            @property
            def value(self):
                return VALUE
        """,
    )
    namespace = {"__name__": "agent_admin_compose_host_probe", "VALUE": "host"}
    namespace["helper"] = owner.helper
    agent_admin.compose(namespace, (owner,))

    assert namespace["helper"]() == "host" and owner.helper is namespace["helper"]
    assert owner.outer()() == "host"
    assert owner.Tally().method() == "host"
    assert owner.Tally.static() == "host"
    assert owner.Tally().value == "host"
    assert owner.join.__module__ != "agent_admin_compose_host_probe"
    assert owner.helper.__module__ == "agent_admin_compose_host_probe"
    assert owner.Tally.__module__ == "agent_admin_compose_owner_probe"
    namespace["VALUE"] = "patched"
    assert namespace["helper"]() == "patched"

    fresh = {"__name__": "agent_admin_compose_host_probe", "VALUE": "fresh"}
    agent_admin.compose(fresh, (owner,))
    assert owner.helper() == "fresh"


# ── the owners' least-covered routes, characterized ───────────────────────────


class _FakeManager:
    """A capability manager answering the skill and agent seams with one result."""

    def __init__(self, *, available: bool = True, ok: bool = True, message: str = "") -> None:
        self._available = available
        self.result = types.SimpleNamespace(ok=ok, message=message)
        self.calls: list[tuple[str, str]] = []

    def available(self) -> bool:
        return self._available

    async def install_skill(self, package: str) -> Any:
        self.calls.append(("install_skill", package))
        return self.result

    async def uninstall_skill(self, package: str) -> Any:
        self.calls.append(("uninstall_skill", package))
        return self.result

    async def list_skills(self) -> list[dict]:
        raise RuntimeError("boom")


def _post(body: object) -> Any:
    """A request whose JSON body is *body*, as the capability route tests build one."""
    request = MagicMock()
    request.json = AsyncMock(return_value=body)
    request.app = {"state": MagicMock()}
    return request


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "op"),
    [
        ("api_capability_skills_install", "install_skill"),
        ("api_capability_skills_uninstall", "uninstall_skill"),
    ],
)
async def test_a_skill_package_mutation_rebuilds_the_agent_config(
    handler: str, op: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _FakeManager()
    monkeypatch.setattr(agents, "_require_owner", AsyncMock(return_value=None))
    monkeypatch.setattr(agents, "_capability_manager", lambda: manager)
    rebuild = MagicMock()
    monkeypatch.setattr(agents, "install_agent", rebuild)
    request = _post({"package": " pkg-1 "})
    response = await getattr(agents, handler)(request)
    assert response.status == 200
    assert json.loads(response.body) == {"ok": True, "package": "pkg-1"}
    assert manager.calls == [(op, "pkg-1")]
    rebuild.assert_called_once_with()
    request.app["state"].push_refresh.assert_called_once_with("agents")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "handler", ["api_capability_skills_install", "api_capability_skills_uninstall"]
)
async def test_a_skill_package_mutation_refuses_before_and_after_the_seam(
    handler: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agents, "_require_owner", AsyncMock(return_value=None))
    rebuild = MagicMock()
    monkeypatch.setattr(agents, "install_agent", rebuild)
    route = getattr(agents, handler)

    missing = await route(_post({"package": "  "}))
    assert (missing.status, json.loads(missing.body)) == (400, {"error": "package required"})

    monkeypatch.setattr(agents, "_capability_manager", lambda: _FakeManager(available=False))
    absent = await route(_post({"package": "pkg"}))
    assert (absent.status, json.loads(absent.body)) == (
        503,
        {"error": agents._CAPABILITY_UNAVAILABLE},
    )

    failed = _FakeManager(ok=False, message="x" * 600)
    monkeypatch.setattr(agents, "_capability_manager", lambda: failed)
    refused = await route(_post({"package": "pkg"}))
    assert refused.status == 500
    assert json.loads(refused.body) == {"error": "x" * 500}
    rebuild.assert_not_called()


@pytest.mark.asyncio
async def test_a_capability_list_failure_is_a_correlated_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agents, "_capability_manager", lambda: _FakeManager())
    response = await agents.api_capability_skills_list(make_mocked_request("GET", "/x"))
    body = json.loads(response.body)
    assert response.status == 500 and body["error"] == "internal error"
    assert re.fullmatch(r"[0-9a-f]{12}", body["id"])


@pytest.mark.asyncio
async def test_the_installed_listing_puts_kirocrew_first(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [types.SimpleNamespace(name=n, to_dict=lambda n=n: {"name": n}) for n in "bak"]
    rows.append(types.SimpleNamespace(name="kirocrew", to_dict=lambda: {"name": "kirocrew"}))
    monkeypatch.setattr(agents, "list_agents", lambda: iter(rows))
    response = await agents.api_agents_installed(make_mocked_request("GET", "/x"))
    assert [row["name"] for row in json.loads(response.body)] == ["kirocrew", "a", "b", "k"]


def _sync_config() -> Any:
    from kiro_crew.config.loader import KiroCrewConfig

    cfg = MagicMock(spec=KiroCrewConfig)
    cfg.agents = {}
    cfg.memory_stores = {}
    return cfg


@pytest.mark.asyncio
async def test_a_sync_refuses_a_credential_shaped_discovered_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.agent_discovery import AgentInfo

    shaped = "ghp_" + "a" * 36
    found = [
        AgentInfo(
            name=shaped, filename=f"{shaped}.json", description="", model="auto", source="package"
        )
    ]
    write = MagicMock()
    with (
        patch.object(agents.KiroCrewConfig, "load", return_value=_sync_config()),
        patch.object(agents, "list_agents", return_value=found),
        patch.object(agents, "update_config_locked", write),
    ):
        response = await agents._do_agents_sync(make_mocked_request("POST", "/x"))
    assert json.loads(response.body)["synced"] == []
    write.assert_not_called()


@pytest.mark.asyncio
async def test_a_failed_sync_scan_is_audited_and_answers_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sel = MagicMock()
    with (
        patch.object(agents.KiroCrewConfig, "load", return_value=_sync_config()),
        patch.object(agents, "list_agents", side_effect=OSError("unreadable")),
        patch.object(agents, "_sel", return_value=sel),
    ):
        response = await agents._do_agents_sync(make_mocked_request("POST", "/x"))
    assert response.status == 500
    assert json.loads(response.body) == {"ok": False, "error": "sync failed", "synced": []}
    sel.log_api_access.assert_called_once_with(
        caller="dashboard", operation="agent.auto_sync", outcome="failure", source="agent_sync"
    )


def test_an_unreadable_agents_dir_reports_no_namespaced_app_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unreadable = MagicMock()
    unreadable.glob.side_effect = OSError("denied")
    monkeypatch.setattr(agents, "kiro_agents_dir_path", lambda: unreadable)
    assert agents._namespaced_agent_file_exists("helper") is False
