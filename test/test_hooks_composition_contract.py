"""The hook subsystem keeps its surface and its contracts while its owners move.

``kiro_crew.hooks`` is the hook subsystem's import path and its patch surface: over a
hundred production modules import it and the tests rebind its names. Most of what it defined
now lives in the modules of ``kiro_crew.hook_runtime``, one responsibility each, and
``hook_runtime.compose`` runs every function they define on the facade's globals.

These tests pin:

* the surface: every name the facade bound before the split still resolves on it, with
  the same kind and signature, and the collaborators it re-offers are still their own
  module's objects;
* the composition: every owner function runs on the facade's globals, reads only names
  the facade binds, defines no module-level state, and reaches the facade only under
  ``TYPE_CHECKING``;
* the placement guards: what a repository guard reads in ``hooks.py`` by path, by
  module source or by registry key is still there, and the CI lanes keyed on that path
  also select the owners.

The gate's decision order is a security invariant, pinned as data and as behaviour by
``test/test_gate_tiers.py``.
"""

from __future__ import annotations

import ast
import builtins
import dis
import hashlib
import importlib
import importlib.util
import inspect
import pkgutil
import re
import subprocess
import sys
import textwrap
import types
import typing
from pathlib import Path

import pytest
import yaml
from source_corpus import repo_root

from kiro_crew import hook_runtime
from kiro_crew import hooks as hooks_mod
from kiro_crew.subprocess_utf8 import UTF8_TEXT

_FACADE = hooks_mod.__name__
_FACADE_PATH = Path(hooks_mod.__file__).resolve()
_OWNER_PACKAGE = hook_runtime.__name__
_OWNER_DIR = Path(hook_runtime.__file__).resolve().parent
_SRC = _FACADE_PATH.parent.parent
_ROOT = repo_root()

#: Every module-level name ``hooks.py`` bound at the base this split was cut from:
#: what it defined and what it imported, private names included, because over a
#: hundred production modules and the tests read private names off it too. A name bound only
#: by ``import <module>`` of a stdlib or third-party module is left out (SD39): nothing
#: reads ``json`` or ``re`` off the facade, and pinning one would fail on the removal
#: of an unused import. The project module objects it imports ARE pinned, because the
#: tests patch through them. ``jsonl_util`` is one of them for a second reason: the
#: moved ``safe_read_file`` reads ``jsonl_util.open_regular_nofollow`` as a facade
#: global, so the facade must bind it.
_BASE_NAMES = frozenset("""
    jsonl_util
    Any AutoReplyHook CORE_MCP_SERVER CU_CLASS_OBSERVE Callable ContextRule ContextVar
    FileTooLargeError GATE_CRASH_REASON HOOK_EVENTS HOOK_EVENTS_AGENT_REQUESTED
    HOOK_EVENTS_ALL HOOK_EVENTS_KAS_ONLY HOOK_EVENT_AGENT_SPAWN HOOK_EVENT_FILE_CREATED
    HOOK_EVENT_FILE_DELETED HOOK_EVENT_FILE_EDITED HOOK_EVENT_POST_TASK_EXECUTION
    HOOK_EVENT_POST_TOOL_USE HOOK_EVENT_PRE_TASK_EXECUTION HOOK_EVENT_PRE_TOOL_USE
    HOOK_EVENT_STOP HOOK_EVENT_USER_PROMPT_SUBMIT HOOK_EVENT_USER_TRIGGERED
    HOOK_INJECT_CONTEXT HOOK_MODIFY HOOK_PASSTHROUGH HOOK_REPLY HOOK_TIMEOUT_DEFAULT
    HOOK_TIMEOUT_MAX HOOK_TIMEOUT_MIN HookManager HookResult HooksConfig Iterable
    MAX_FILE_BYTES Mapping Path ScriptHook ScriptHookResult ScriptHookStore Sequence
    TARGET_PATH_KEYS TOOL_ALLOW TOOL_AUTO_APPROVE TOOL_DENY TargetPaths ToolHookResult
    TransformHook UserDeniedPattern _AUDIT_ONLY_READ_IDS _BUILTIN_APP_AGENTS
    _BUILTIN_APP_MCP_SERVERS _BUILTIN_APP_NAMES _BUNDLED_AUTO_APPROVE_TOOLS _DRIVE_ABS_RE
    _DRIVE_PREFIX_RE _EDIT_TOOL_KIND _GATE_UNCOUNTED _GLOBAL_INLINE_FLAGS_RE _HOOKS_FILE
    _HOOK_BASE_ENV_KEYS _HOOK_STREAM_CAP_BYTES _HOOK_TRUNCATION_MARKER
    _HOST_READ_ONLY_BUILTIN_TOOLS _INTERNAL_READ_ALLOWLIST _MAX_SCREENED_PATH_DEPTH
    _READ_ONLY_TOOL_KINDS _RECURSIVE_SEARCH_OPERATIONS _SEARCH_DENY_ARG_KEYS
    _SEARCH_DENY_ESCAPES _SEARCH_DENY_PREFIX _SEARCH_HOME_VARS _SEARCH_PATH_FIELD
    _SKILLS_ONLY_EVENTS _TARGET_PATH_MAX_NODES _TARGET_PATH_MAX_PATHS
    _TITLE_ONLY_GRANT_NOTED _TITLE_ONLY_GRANT_NOTED_CAP _TOOL_TITLE_PREFIXES
    _WINDOWS_LINK_CHAIN_MAX _WRITE_TOOL_KINDS _XATTR_UNSUPPORTED_ERRNOS _app_owns_mcp_server
    _audit_governance _audit_governance_hook_decision _bounded_pattern_search
    _builtin_app_for_agent _coerce_bool _communicate_capped _config_paths _context_matches
    _cu_read_only_auto_approve _darwin_case_alias_matches _decode_capped
    _emit_internal_read_audit _encode_search_field _expand_home_vars
    _fail_closed_on_gate_crash _fd_real_path _fold_extended_length_local
    _global_script_hook_store _governance_denial _governance_pinned_command_ids
    _hardlink_alias_matches _has_global_inline_flags _hook_subprocess_env
    _is_access_control_xattr _is_declared_builtin_mcp_server _is_first_party_app
    _is_host_read_only_builtin _is_representable_path _is_search_shaped
    _normalize_hook_timeout _normalize_search_path _normalize_tool_name
    _normalize_windows_link_target _note_title_only_grant_pattern
    _opened_file_matches_validated_path _opened_path_within_root _pinned_replace
    _read_capped_stream _screen_windows_links _script_hooks_capability_denied
    _search_deny_target _should_carry_xattr _spawn_policy_denial _tool_matches
    _unc_agents_root _unc_agents_root_cache _unc_data_home_root _unc_data_home_root_cache
    _validated_name_holds asdict audit_bash_exfiltration computer_use_action_classes
    computer_use_action_from_title contextmanager current_context dataclass
    dataclasses_replace edit_target_candidates effective_denied_regexes_from_config
    emit_internal_read_audit event_is_spawn_run field fire_tool_hooks get_global_hook_store
    hook_gate_kwargs hooks_config_from_config_dict identity_grant_covers_child is_edit_call
    is_read_only_bash is_sensitive_bash_command is_sensitive_path is_sensitive_write_path
    is_unc_shape is_unverifiable_path_refusal load_denied_commands_state logger
    mcp_identity_ref permission_pre_tool_block persisted_hook_store pinned_fs
    platform_compat pre_tool_match_names redact_via_context register_internal_read_path
    resolve_denied_notes resolve_effective_denied_regexes run_script_hook
    safe_copy_file_nolink safe_file_identity safe_read_file safe_read_file_bytes
    safe_read_file_bytes_nolink safe_read_file_bytes_with_identity safe_read_file_internal
    safe_read_prefix safe_write_file_nolink security sel sensitive_path_refusal
    set_builtin_app_agents set_builtin_app_mcp_servers set_builtin_app_names
    set_global_hook_store splice_denied_commands stat_identity target_paths
    unc_probe_allowed uncounted_gate validate_file_path validate_hook_fields
    verified_replace_file_nolink webhooks
""".split())

#: Each moved definition and the owner its responsibility puts it in. Adding or
#: removing an owner changes the composition, so the set is spelled out.
_BASE_OWNERS: dict[str, tuple[str, ...]] = {
    "denied_commands": tuple("""
        _governance_pinned_command_ids load_denied_commands_state
        hooks_config_from_config_dict splice_denied_commands
        resolve_effective_denied_regexes resolve_denied_notes
        effective_denied_regexes_from_config
        """.split()),
    "governance_gate": tuple("""
        _governance_denial _spawn_policy_denial _audit_governance
        _script_hooks_capability_denied _audit_governance_hook_decision
        """.split()),
    "tool_identity": tuple("""
        event_is_spawn_run hook_gate_kwargs _is_host_read_only_builtin
        _app_owns_mcp_server set_builtin_app_mcp_servers _is_declared_builtin_mcp_server
        set_builtin_app_names _is_first_party_app set_builtin_app_agents
        _builtin_app_for_agent mcp_identity_ref _note_title_only_grant_pattern
        identity_grant_covers_child _normalize_tool_name _context_matches _tool_matches
        _has_global_inline_flags
        """.split()),
    "search_targets": tuple("""
        _encode_search_field _expand_home_vars _normalize_search_path
        _is_search_shaped _search_deny_target
        """.split()),
    "windows_paths": tuple("""
        is_unc_shape _fold_extended_length_local unc_probe_allowed
        _is_representable_path _normalize_windows_link_target
        """.split()),
    "descriptor_identity": tuple("""
        _validated_name_holds _darwin_case_alias_matches _hardlink_alias_matches
        _opened_file_matches_validated_path _opened_path_within_root
        """.split()),
    "safe_reads": tuple("""
        validate_file_path safe_read_file safe_read_file_bytes safe_file_identity
        safe_read_file_bytes_with_identity stat_identity safe_read_file_bytes_nolink
        safe_read_prefix safe_copy_file_nolink
        """.split()),
    "pinned_writes": ("_pinned_replace", "safe_write_file_nolink", "verified_replace_file_nolink"),
    "internal_reads": tuple("""
        register_internal_read_path safe_read_file_internal _emit_internal_read_audit
        emit_internal_read_audit
        """.split()),
    "script_validation": ("_normalize_hook_timeout", "validate_hook_fields"),
    "stream_caps": ("_read_capped_stream", "_decode_capped", "_communicate_capped"),
    "hook_dispatch": tuple("""
        set_global_hook_store get_global_hook_store persisted_hook_store
        fire_tool_hooks pre_tool_match_names permission_pre_tool_block
        """.split()),
}

_MOVED = frozenset(name for names in _BASE_OWNERS.values() for name in names)

#: Definitions added to the owners after the split, by owner. Kept apart from
#: ``_BASE_OWNERS`` because the base names and shapes are a frozen record of the
#: one-module file; these are the tool gate's tiers (``gate_tiers``) and its typed
#: input (``ToolCall``, beside the extraction it absorbed).
_NEW_OWNER_DEFS: dict[str, tuple[str, ...]] = {
    "gate_tiers": tuple("""
        GateFacts _tier_unverifiable_shell _tier_targets _rule_sensitive_path
        _rule_sensitive_bash _rule_exfil _tier_param_paths _tier_write_protected
        _tier_deny_rules _tier_mcp_auto_deny _tier_search_target _tier_governance
        _tier_app_own_server _tier_operator_grants _tier_read_only shell_rules
        gate_tier_problems
        """.split()),
    "tool_identity": ("ToolCall",),
}

#: SHA-256 of the sorted ``"<name> <kind> <signature>"`` lines of every moved name,
#: captured from the one-module file before the split: each keeps the kind and the
#: signature it had there. ``safe_read_file_bytes_nolink``'s keyword-only
#: ``admit_hardlinked`` and ``ScriptHookStore``'s callers all read these shapes.
_BASE_SHAPE_DIGEST = "87cd6c619f0192c4acc0c630754c810d5d25c5dbdc6f433aab885428f78bc563"

#: Definitions that stay in the facade file, each because a guard, a contract or the
#: ``compose`` ordering reads it there. The reason per entry is in
#: ``docs/system-specs/modules/memory-skills-hooks.md``'s "Hook runtime owners".
_FACADE_DEFS = (
    "HookResult",
    "uncounted_gate",
    "ToolHookResult",
    "_fail_closed_on_gate_crash",
    "GateRule",
    "GateTier",
    "ContextRule",
    "AutoReplyHook",
    "TransformHook",
    "UserDeniedPattern",
    "HooksConfig",
    "HookManager",
    "_cu_read_only_auto_approve",
    "_unc_data_home_root",
    "_unc_agents_root",
    "_screen_windows_links",
    "FileTooLargeError",
    "_hook_subprocess_env",
    "ScriptHook",
    "ScriptHookResult",
    "run_script_hook",
    "ScriptHookStore",
)

#: Collaborators the facade re-offers that belong to another module. Their identity is
#: what ``test_agent_refactor_contracts`` and ``test_pinned_staging`` assert through
#: the facade, so the split may not replace one with a copy.
_COLLABORATORS = {
    "_coerce_bool": ("kiro_crew.config.fields", "_coerce_bool"),
    "_fd_real_path": ("kiro_crew.pinned_fs", "fd_real_path"),
    "is_sensitive_path": ("kiro_crew.security", "is_sensitive_path"),
    "is_unverifiable_path_refusal": ("kiro_crew.security", "is_unverifiable_path_refusal"),
    "sensitive_path_refusal": ("kiro_crew.security", "sensitive_path_refusal"),
    "is_read_only_bash": ("kiro_crew.security.readonly_bash", "is_read_only_bash"),
    "target_paths": ("kiro_crew.platform.tool_paths", "target_paths"),
    "is_edit_call": ("kiro_crew.platform.tool_paths", "is_edit_call"),
    "edit_target_candidates": ("kiro_crew.platform.tool_paths", "edit_target_candidates"),
    "redact_via_context": ("kiro_crew.platform", "redact_via_context"),
    "current_context": ("kiro_crew.platform", "current_context"),
}


def _owner(stem: str) -> types.ModuleType:
    return importlib.import_module(f"{_OWNER_PACKAGE}.{stem}")


def _owners() -> list[types.ModuleType]:
    return [_owner(info.name) for info in pkgutil.iter_modules([str(_OWNER_DIR)])]


def _owner_sources() -> dict[str, str]:
    return {
        path.stem: path.read_text(encoding="utf-8") for path in sorted(_OWNER_DIR.glob("[!_]*.py"))
    }


def _member_functions(member: object) -> list[types.FunctionType]:
    """The plain functions a class member wraps: itself, a static/class method, a property."""
    if isinstance(member, property):
        return [fn for fn in (member.fget, member.fset, member.fdel) if fn is not None]
    fn = getattr(member, "__func__", member)
    return [fn] if isinstance(fn, types.FunctionType) else []


def _owner_functions() -> list[tuple[str, types.FunctionType]]:
    """``(label, function)`` for every function an owner's file defines: the module's
    functions and every method, static/class method and property of its classes."""
    found: list[tuple[str, types.FunctionType]] = []
    for owner in _owners():
        stem = owner.__name__.rsplit(".", 1)[-1]
        for name, value in vars(owner).items():
            fn = getattr(value, "__func__", value)
            if isinstance(fn, types.FunctionType) and fn.__code__.co_filename == owner.__file__:
                found.append((f"{stem}.{name}", fn))
            elif isinstance(value, type) and value.__module__ == owner.__name__:
                for attr, member in vars(value).items():
                    for method in _member_functions(member):
                        if method.__code__.co_filename == owner.__file__:
                            found.append((f"{stem}.{name}.{attr}", method))
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
        timeout=180,
        cwd=str(tmp_path),
        **UTF8_TEXT,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("ok")


# ── the surface ───────────────────────────────────────────────────────────────


def test_every_name_the_facade_bound_at_the_base_still_resolves() -> None:
    """Over a hundred production modules and the tests read private names off this module as
    well as public ones, so every module-level binding survives the split."""
    assert len(_BASE_NAMES) == 197
    assert sorted(name for name in _BASE_NAMES if not hasattr(hooks_mod, name)) == []


def test_a_fresh_interpreter_sees_every_base_public_name(tmp_path: Path) -> None:
    """The public names resolve in a process that imports nothing else first."""
    public = sorted(name for name in _BASE_NAMES if not name.startswith("_"))
    assert len(public) > 90
    _run_child(
        tmp_path,
        """
        import sys
        import kiro_crew.hooks as hooks
        missing = [n for n in sys.argv[1:] if not hasattr(hooks, n)]
        assert missing == [], missing
        print("ok")
        """,
        *public,
    )


def test_a_star_import_carries_the_moved_public_names(tmp_path: Path) -> None:
    """The facade declares no ``__all__``, so every public binding goes out. Loaded
    through ``importlib``'s ``exec_module``, because CI's SAST refuses the builtin that
    runs a source string -- even for a literal one -- and a suppression is not an
    option."""
    assert not hasattr(hooks_mod, "__all__")
    probe = tmp_path / "hooks_star_probe.py"
    probe.write_text(
        "from kiro_crew.hooks import *  # noqa: F401,F403\n",
        encoding="utf-8",
    )
    spec = importlib.util.spec_from_file_location("hooks_star_probe", probe)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in ("validate_file_path", "safe_read_file_bytes_nolink", "unc_probe_allowed"):
        assert getattr(module, name) is getattr(hooks_mod, name)


def test_every_collaborator_is_still_the_source_object() -> None:
    """A collaborator the facade re-offers is the object its own module holds, not a
    copy -- which is what ``test_agent_refactor_contracts`` and
    ``test_pinned_staging`` assert through this module."""
    for name, (module, attribute) in _COLLABORATORS.items():
        source = getattr(importlib.import_module(module), attribute)
        assert getattr(hooks_mod, name) is source, f"hooks.{name} is not {module}.{attribute}"


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


def test_every_name_a_production_module_imports_from_the_facade_resolves() -> None:
    """Most of these imports are function-local, so a name that stopped resolving
    would first fail at request time rather than at import."""
    importers = _facade_importers()
    assert len(importers) >= 40
    imported = {name for names in importers.values() for name in names}
    assert sorted(name for name in imported if not hasattr(hooks_mod, name)) == []
    # The names the moved code is reached by from outside are among them.
    assert {"safe_read_file_bytes_nolink", "validate_file_path", "run_script_hook"} <= imported


# ── the composition ───────────────────────────────────────────────────────────


def test_the_owner_set_is_the_package() -> None:
    assert {info.name for info in pkgutil.iter_modules([str(_OWNER_DIR)])} == set(
        _BASE_OWNERS
    ) | set(_NEW_OWNER_DEFS)


def test_every_added_owner_definition_is_one_object_on_the_facade() -> None:
    """Each definition added since the split is defined in exactly the owner its
    responsibility names and bound on the facade as that same object."""
    strays = [
        f"{owner}:{name}"
        for owner, names in _NEW_OWNER_DEFS.items()
        for name in names
        if getattr(hooks_mod, name, None) is not vars(_owner(owner)).get(name)
    ]
    assert strays == []
    names = [name for group in _NEW_OWNER_DEFS.values() for name in group]
    assert len(names) == len(set(names)) and not set(names) & _MOVED


def _shape(obj: object) -> str:
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
        if getattr(hooks_mod, name) is not vars(_owner(owner)).get(name)
    ]
    assert strays == []
    names = [name for group in _BASE_OWNERS.values() for name in group]
    assert len(names) == len(set(names)) == 71


def test_the_moved_names_keep_their_base_shapes() -> None:
    lines = sorted(f"{name} {_shape(getattr(hooks_mod, name))}" for name in _MOVED)
    assert len(lines) == 71
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
    """Owner functions reach module state by name through the facade's namespace, so
    a test that rebinds one there is the binding every function sees -- which holds
    only while no owner keeps a copy. A ``global`` in a rebound owner function writes
    the facade's namespace for the same reason."""
    state = _module_assignments(_FACADE_PATH.read_text(encoding="utf-8"))
    assert {"MAX_FILE_BYTES", "_INTERNAL_READ_ALLOWLIST", "_HOOK_BASE_ENV_KEYS"} <= state
    assert {"_BUILTIN_APP_NAMES", "_BUILTIN_APP_AGENTS", "_global_script_hook_store"} <= state
    assert {"_unc_agents_root_cache", "_unc_data_home_root_cache", "_GATE_UNCOUNTED"} <= state
    assert {"_SEARCH_DENY_ARG_KEYS", "_TITLE_ONLY_GRANT_NOTED", "logger"} <= state
    owned = {stem: sorted(_module_assignments(source)) for stem, source in _owner_sources().items()}
    assert {stem: names for stem, names in owned.items() if names} == {}


@pytest.mark.parametrize("name", _FACADE_DEFS)
def test_a_facade_definition_stays_in_the_facade_file(name: str) -> None:
    # ``inspect.unwrap`` for ``uncounted_gate``, whose ``@contextmanager`` wrapper's
    # code object is contextlib's; the function it decorates is this module's.
    obj = inspect.unwrap(getattr(hooks_mod, name))
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
    assert len(defined | _MOVED) == len(defined) + len(_MOVED) == 93


def test_the_owners_log_as_the_facade() -> None:
    """Log capture keyed to ``kiro_crew.hooks`` keeps seeing the moved sites."""
    assert hooks_mod.logger.name == _FACADE
    readers = [
        label for label, fn in _owner_functions() if "logger" in set(_global_names(fn.__code__))
    ]
    assert len(readers) >= 8


def test_every_owner_function_runs_on_the_facade_globals() -> None:
    """A patch of ``kiro_crew.hooks.<name>`` reaches an owner function only because
    the function reads the facade's globals, not its own."""
    labels = {label for label, _ in _owner_functions()}
    assert len(labels) >= 71
    strays = [
        label
        for label, fn in _owner_functions()
        if fn.__globals__ is not vars(hooks_mod) or fn.__module__ != _FACADE
    ]
    assert strays == []


def test_the_sweep_reports_a_global_the_facade_does_not_bind() -> None:
    """The name sweep can fail, nested bodies included."""

    def _probe() -> object:
        def _inner() -> object:
            return _absent_from_the_hooks_namespace  # noqa: F821

        return _inner

    assert "_absent_from_the_hooks_namespace" in set(_global_names(_probe.__code__))


def test_every_global_an_owner_function_reads_is_bound_on_the_facade() -> None:
    """An owner's own imports are inert for its functions, so a name missing from the
    facade surfaces only when its line runs -- often inside an ``except`` that turns
    the NameError into a refusal. The sweep makes it a test failure instead."""
    namespace = vars(hooks_mod)
    unresolved = sorted(
        (label, name)
        for label, fn in _owner_functions()
        for name in set(_global_names(fn.__code__))
        if name not in namespace and not hasattr(builtins, name)
    )
    assert unresolved == []


def test_a_patch_of_the_facade_reaches_an_owner_function(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The contract the rebinding exists for, on the seams the tests actually use:
    the byte reader (``safe_reads``) reads the facade's size cap and its path
    validator, and the no-link reader reads the facade's sensitive-path gate."""
    target = tmp_path / "payload.txt"
    target.write_text("abcdef", encoding="utf-8")
    assert hooks_mod.safe_read_file_bytes(str(target)) == b"abcdef"

    monkeypatch.setattr(hooks_mod, "MAX_FILE_BYTES", 2)
    with pytest.raises(hooks_mod.FileTooLargeError):
        hooks_mod.safe_read_file_bytes(str(target))
    monkeypatch.undo()

    monkeypatch.setattr(hooks_mod, "validate_file_path", lambda raw: None)
    assert hooks_mod.safe_read_file_bytes(str(target)) is None
    monkeypatch.undo()

    monkeypatch.setattr(hooks_mod, "is_sensitive_path", lambda path: True)
    assert hooks_mod.safe_read_file_bytes_nolink(str(target)) is None


def test_a_global_write_from_an_owner_lands_on_the_facade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``set_builtin_app_names`` and ``set_global_hook_store`` declare ``global`` and
    live in owners. ``compose`` replaces a function's ``__globals__``, so the
    ``STORE_GLOBAL`` writes the facade's namespace -- and ``test_boot_smoke`` replaces
    both tables on this module per boot."""
    monkeypatch.setattr(hooks_mod, "_BUILTIN_APP_NAMES", frozenset())
    monkeypatch.setattr(hooks_mod, "_BUILTIN_APP_AGENTS", {})
    hooks_mod.set_builtin_app_names(["Dev_Fleet"])
    hooks_mod.set_builtin_app_agents({"Probe_Agent": "dev_fleet"})
    assert hooks_mod._BUILTIN_APP_NAMES == frozenset({"dev_fleet"})
    assert hooks_mod._is_first_party_app("DEV_FLEET") is True
    assert hooks_mod._builtin_app_for_agent("probe_agent") == "dev_fleet"

    monkeypatch.setattr(hooks_mod, "_global_script_hook_store", None)
    sentinel = object()
    hooks_mod.set_global_hook_store(sentinel)  # type: ignore[arg-type]
    assert hooks_mod._global_script_hook_store is sentinel
    assert hooks_mod.get_global_hook_store() is sentinel


def test_module_and_qualname_still_name_the_facade() -> None:
    """Reprs and pickling by reference read as before the split: every owner function
    resolves back through its own ``__module__`` and ``__qualname__``."""
    wrong = []
    for label, fn in _owner_functions():
        target: object = sys.modules[fn.__module__]
        for part in fn.__qualname__.split("."):
            target = (
                vars(target).get(part) if isinstance(target, type) else getattr(target, part, None)
            )
            target = getattr(target, "__func__", target)
        if isinstance(target, property):
            target = next((f for f in _member_functions(target) if f is fn), target)
        if target is not fn:
            wrong.append(label)
    assert wrong == []


def test_a_moved_function_reads_its_source_from_its_owner() -> None:
    source = inspect.getsource(hooks_mod.safe_read_file_bytes_nolink)
    assert source.startswith("def safe_read_file_bytes_nolink(")
    assert (
        inspect.getsourcefile(hooks_mod.safe_read_file_bytes_nolink)
        == _owner("safe_reads").__file__
    )


def test_the_compose_copy_matches_its_siblings() -> None:
    """Five compositions share one technique; each package keeps its own copy so no
    package imports another's, and the copies stay identical."""

    def body(rel: str) -> str:
        text = (_SRC / "kiro_crew" / rel / "__init__.py").read_text(encoding="utf-8")
        return text[text.index("def compose(") :]

    assert (
        body("hook_runtime")
        == body("dashboard/agent_admin")
        == body("dashboard/chat_api")
        == body("dashboard/file_api")
        == body("dashboard/messaging_api")
    )


def _write_module(tmp_path: Path, name: str, source: str) -> types.ModuleType:
    path = tmp_path / f"{name}.py"
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_compose_rebinds_functions_and_class_members(tmp_path: Path) -> None:
    """On synthetic modules, so every member shape is covered whatever the owners hold
    today: a module function,
    a nested function, a method, a static method, a class method and both halves of a
    property of an owner class all read the host namespace afterwards; a member that
    wraps a function the owner merely imported, a property over a builtin and a plain
    class attribute are left alone; the owner class keeps its own module; and a second
    compose onto a fresh namespace moves them all again."""
    owner = _write_module(
        tmp_path,
        "hook_runtime_compose_owner_probe",
        """
        from os.path import join

        def helper():
            return VALUE

        def outer():
            def inner():
                return VALUE
            return inner

        class Tally:
            LIMIT = 3
            joined = staticmethod(join)
            size = property(len)

            def method(self):
                return VALUE

            @staticmethod
            def static():
                return VALUE

            @classmethod
            def klass(cls):
                return VALUE

            @property
            def value(self):
                return VALUE

            @value.setter
            def value(self, new):
                self.seen = (new, VALUE)
        """,
    )
    untouched = {name: vars(owner.Tally)[name] for name in ("LIMIT", "joined", "size")}
    namespace = {"__name__": "hook_runtime_compose_host_probe", "VALUE": "host"}
    namespace["helper"] = owner.helper
    hook_runtime.compose(namespace, (owner,))

    assert namespace["helper"]() == "host" and owner.helper is namespace["helper"]
    assert owner.outer()() == "host"
    tally = owner.Tally()
    assert tally.method() == "host"
    assert owner.Tally.static() == "host"
    assert owner.Tally.klass() == "host"
    assert tally.value == "host"
    tally.value = "set"
    assert tally.seen == ("set", "host")
    assert {name: vars(owner.Tally)[name] for name in untouched} == untouched
    assert owner.join.__module__ != "hook_runtime_compose_host_probe"
    assert owner.helper.__module__ == "hook_runtime_compose_host_probe"
    assert owner.Tally.__module__ == "hook_runtime_compose_owner_probe"
    namespace["VALUE"] = "patched"
    assert namespace["helper"]() == "patched"
    assert owner.Tally.static() == "patched" and owner.Tally().value == "patched"

    fresh = {"__name__": "hook_runtime_compose_host_probe", "VALUE": "fresh"}
    hook_runtime.compose(fresh, (owner,))
    assert owner.helper() == "fresh"
    assert owner.Tally.klass() == "fresh" and owner.Tally().value == "fresh"


# ── one edge ──────────────────────────────────────────────────────────────────


def _package_of(path: Path) -> str:
    parts = list(path.resolve().relative_to(_SRC).with_suffix("").parts)
    return ".".join(parts[:-1])


def _import_targets(tree: ast.Module, package: str) -> list[tuple[ast.AST, str]]:
    """``(node, dotted module)`` for every module a tree imports, spelled any way."""
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
        ("from . import safe_reads\n", True),
        ("from .safe_reads import validate_file_path\n", True),
        ("from kiro_crew import hooks\n", True),
        ("from kiro_crew.hooks import MAX_FILE_BYTES\n", True),
        ("import kiro_crew.hooks as hooks\n", True),
        ("import importlib\nimportlib.import_module('kiro_crew.hooks')\n", True),
        ("__import__('kiro_crew.hook_runtime.safe_reads')\n", True),
        ("if TYPE_CHECKING:\n    from kiro_crew.hooks import MAX_FILE_BYTES\n", False),
        ("def f():\n    from kiro_crew.sandbox import create_subprocess_limited\n", False),
        ("import os\nimport stat as _stat\n", False),
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
        if "hook_runtime" not in text:
            continue
        importers.extend(
            f"{path.relative_to(_SRC)}:{node.lineno}"
            for node, target in _import_targets(ast.parse(text), _package_of(path))
            if _within(target, _OWNER_PACKAGE)
        )
    assert importers == []


def test_an_owner_imports_project_modules_only_for_type_checking() -> None:
    """An owner's module-level imports are stdlib only, plus its ``TYPE_CHECKING``
    names from the facade. ``sandbox`` is the one that matters: ``sandbox -> registry
    -> apps -> hooks`` is a real cycle, so a module-level import of it in an owner
    would make ``import kiro_crew.hooks`` raise and fail the suite at collection."""
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
    """Importing the facade imports every owner with it: none loads lazily on a later
    call, so the import order stays the one the one-module file had."""
    _run_child(
        tmp_path,
        """
        import sys
        import kiro_crew.hooks
        missing = [n for n in sys.argv[1:]
                   if f"kiro_crew.hook_runtime.{n}" not in sys.modules]
        assert missing == [], missing
        print("ok")
        """,
        *_BASE_OWNERS,
        *_NEW_OWNER_DEFS,
    )


def test_the_unc_root_memos_are_primed_on_the_facade(tmp_path: Path) -> None:
    """The two memos and their import-time priming stay in the facade. The facade's
    body runs BEFORE ``compose``, so priming an owner function would write the memo
    into the owner's namespace and leave the facade's cache empty -- and the first
    gate check would then pay a resolving accessor on the event loop."""
    for name in ("_unc_data_home_root", "_unc_agents_root"):
        fn = getattr(hooks_mod, name)
        assert Path(fn.__code__.co_filename).resolve() == _FACADE_PATH
    _run_child(
        tmp_path,
        """
        import kiro_crew.hooks as hooks
        assert hooks._unc_agents_root_cache is not None, "agents root memo not primed"
        assert hooks._unc_data_home_root_cache is not None, "data home memo not primed"
        print("ok")
        """,
    )


# ── the placement guards ──────────────────────────────────────────────────────


def test_the_gate_still_carries_what_the_source_guards_read() -> None:
    """Two guards read this file's own text rather than a symbol, so they constrain
    WHERE that construct lives: ``test_business_counters`` greps the file for the
    approval counter (its emit is ``ToolHookResult._count``), and
    ``test/link_screen_sites.py`` keys ``_screen_windows_links`` by this path. The gate
    table's kinds are a closed vocabulary that ``GateTierKind`` spells for the type
    checker and ``_GATE_TIER_KINDS`` orders for the soundness check."""
    source = _FACADE_PATH.read_text(encoding="utf-8")
    assert "APPROVAL_DECISIONS" in source and "emit_counter" in source
    assert "_screen_windows_links" in source
    module_level = {
        node.name
        for node in ast.parse(source).body
        if isinstance(node, (ast.FunctionDef, ast.ClassDef))
    }
    assert {"HookManager", "_screen_windows_links"} <= module_level
    assert typing.get_args(hooks_mod.GateTierKind) == hooks_mod._GATE_TIER_KINDS
    assert {tier.kind for tier in hooks_mod.GATE_TIERS} == set(hooks_mod._GATE_TIER_KINDS)


def test_the_ci_lanes_keyed_on_the_gate_also_select_the_owners() -> None:
    """Three lanes and one AUTOSDE rule gate on ``src/kiro_crew/hooks.py``. The rules
    they carry apply to the moved code too, so each also names the owners -- without
    this an edit to extracted gate code skips the denial-differential and
    security-scope lanes, and the descriptor tests skip the native macOS suite."""
    owner_rel = "src/kiro_crew/hook_runtime/safe_reads.py"

    def _matches(pattern: str, rel: str) -> bool:
        if pattern.endswith("/**"):
            return rel.startswith(pattern[:-2])
        if pattern.endswith("/"):
            return rel.startswith(pattern)
        return rel == pattern

    denial = yaml.safe_load(
        (_ROOT / ".github/workflows/denial-differential.yml").read_text(encoding="utf-8")
    )
    paths = denial[True]["pull_request"]["paths"]
    assert "src/kiro_crew/hooks.py" in paths
    assert any(_matches(p, owner_rel) for p in paths), paths

    macos = yaml.safe_load(
        (_ROOT / ".github/workflows/macos-on-demand.yml").read_text(encoding="utf-8")
    )
    steps = macos["jobs"]["decide"]["steps"]
    darwin = yaml.safe_load(
        next(step["with"]["filters"] for step in steps if step.get("id") == "filter")
    )["darwin"]
    assert "src/kiro_crew/hooks.py" in darwin
    assert any(_matches(p, owner_rel) for p in darwin), darwin

    scope = (_ROOT / ".github/workflows/security-scope-review.yml").read_text(encoding="utf-8")
    block = scope.split("# SCOPE-SURFACE-BEGIN", 1)[1].split("# SCOPE-SURFACE-END", 1)[0]
    surface = re.findall(r"^\s*'([^']+)'\s*$", block, re.M)
    assert "src/kiro_crew/hooks.py" in surface
    assert any(_matches(p, owner_rel) for p in surface), surface

    autosde = yaml.safe_load((_ROOT / "AUTOSDE.yaml").read_text(encoding="utf-8"))
    rules = [
        rule
        for rule in autosde["custom-rules"]
        if "src/kiro_crew/hooks.py" in (rule.get("file-patterns") or [])
    ]
    assert rules, "no AUTOSDE rule names the gate any more"
    for rule in rules:
        assert any(_matches(p, owner_rel) for p in rule["file-patterns"]), rule["id"]

    review = (_ROOT / ".github/workflows/code-review.yml").read_text(encoding="utf-8")
    tripwire = review.split("bool() truthiness", 1)[1].split("endgroup", 1)[0]
    assert "'src/kiro_crew/hooks.py'" in tripwire
    assert "'src/kiro_crew/hook_runtime/'" in tripwire


# ── the patch reach ───────────────────────────────────────────────────────────


def _facade_aliases(tree: ast.Module) -> set[str]:
    """Every expression a test module binds to the facade, to a fixed point."""
    aliases = {_FACADE, f"sys.modules[{_FACADE!r}]", f'sys.modules["{_FACADE}"]'}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            aliases |= {a.asname for a in node.names if a.name == _FACADE and a.asname}
            aliases |= {a.name for a in node.names if a.name == _FACADE and not a.asname}
        elif isinstance(node, ast.ImportFrom) and node.module == "kiro_crew":
            aliases |= {a.asname or a.name for a in node.names if a.name == "hooks"}
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
            if ast.unparse(value) in aliases or (
                isinstance(value, ast.Call)
                and value.args
                and isinstance(value.args[0], ast.Constant)
                and value.args[0].value == _FACADE
                and ast.unparse(value.func).endswith(("import_module", "__import__"))
            ):
                aliases.add(target.id)
                changed = True
    return aliases


def _string_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level ``NAME = "literal"`` bindings, for a target spelled by constant."""
    found: dict[str, str] = {}
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            found[node.targets[0].id] = node.value.value
    return found


def _dotted_target(expr: ast.AST, consts: dict[str, str]) -> str | None:
    """The attribute a dotted-string patch target names, when it is the facade's."""
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        text = expr.value
    elif isinstance(expr, ast.Name) and expr.id in consts:
        text = consts[expr.id]
    elif isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
        left = _dotted_text(expr.left, consts)
        right = _dotted_text(expr.right, consts)
        text = None if left is None or right is None else left + right
        if text is None:
            return None
    elif isinstance(expr, ast.JoinedStr):
        parts = []
        for value in expr.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            elif isinstance(value, ast.FormattedValue):
                resolved = _dotted_text(value.value, consts)
                if resolved is None:
                    return None
                parts.append(resolved)
            else:
                return None
        text = "".join(parts)
    else:
        return None
    prefix = f"{_FACADE}."
    return (
        text[len(prefix) :] if text.startswith(prefix) and "." not in text[len(prefix) :] else None
    )


def _dotted_text(expr: ast.AST, consts: dict[str, str]) -> str | None:
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        return expr.value
    if isinstance(expr, ast.Name) and expr.id in consts:
        return consts[expr.id]
    if isinstance(expr, ast.Attribute) and expr.attr == "__name__":
        return _FACADE if ast.unparse(expr.value) in {"hooks", "hooks_mod"} else None
    return None


def _facade_writes(source: str) -> set[str]:
    """Every facade attribute *source* patches, rebinds, deletes or asserts identity on.

    The spellings the corpus actually uses plus the ones a future test could reach for:
    ``monkeypatch.setattr``/``delattr``/``setitem`` in the object and dotted-string
    forms, ``mock.patch`` and ``patch.object`` through any alias and as a decorator,
    ``patch.multiple``, the ``setattr``/``delattr`` builtins, a plain attribute
    assignment, and a target spelled by a module-level string constant, an f-string or a
    concatenation.
    """
    tree = ast.parse(source)
    aliases = _facade_aliases(tree)
    consts = _string_constants(tree)
    found: set[str] = set()

    def _attr_of(node: ast.AST) -> str | None:
        if isinstance(node, ast.Attribute) and ast.unparse(node.value) in aliases:
            return node.attr
        return None

    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            found |= {a for a in (_attr_of(t) for t in targets) if a}
        elif isinstance(node, ast.Delete):
            found |= {a for a in (_attr_of(t) for t in node.targets) if a}
        elif isinstance(node, ast.Call):
            call = ast.unparse(node.func)
            kwargs = {k.arg: k.value for k in node.keywords}
            positional = list(node.args)
            if call.endswith(
                ("setattr", "delattr", "setitem", "object", "multiple")
            ) or re.fullmatch(r"(\w+\.)*(patch|mock)", call):
                target = kwargs.get("target")
                if target is None and positional:
                    target = positional[0]
                if target is not None:
                    name = _dotted_target(target, consts)
                    if name:
                        found.add(name)
                    elif ast.unparse(target) in aliases:
                        attr = kwargs.get("attribute")
                        if attr is None and len(positional) > 1:
                            attr = positional[1]
                        if isinstance(attr, ast.Constant) and isinstance(attr.value, str):
                            found.add(attr.value)
                        else:
                            found |= {k for k in kwargs if k not in ("target", "attribute")}
        elif isinstance(node, ast.Compare) and any(isinstance(op, ast.Is) for op in node.ops):
            for side in (node.left, *node.comparators):
                attr = _attr_of(side)
                if attr:
                    found.add(attr)
    return found


def _test_sources() -> list[tuple[str, str]]:
    """``(label, text)`` for every test module that may write the facade."""
    paths = sorted((_ROOT / "test").rglob("*.py"))
    paths += [
        path
        for path in sorted((_SRC / "kiro_crew").rglob("*.py"))
        if any(part.endswith("tests") for part in path.relative_to(_SRC).parts[:-1])
    ]
    out = []
    for path in paths:
        if path.resolve() == Path(__file__).resolve():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "kiro_crew.hooks" in text or "kiro_crew import hooks" in text:
            out.append((path.relative_to(_ROOT).as_posix(), text))
    return out


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "import kiro_crew.hooks as hooks\nmonkeypatch.setattr(hooks, 'MAX_FILE_BYTES', 1)\n",
            {"MAX_FILE_BYTES"},
        ),
        ("from kiro_crew import hooks as h\nmonkeypatch.setattr(h, 'os', 1)\n", {"os"}),
        (
            "monkeypatch.setattr('kiro_crew.hooks.validate_file_path', None)\n",
            {"validate_file_path"},
        ),
        (
            "from unittest import mock\nmock.patch('kiro_crew.hooks.run_script_hook')\n",
            {"run_script_hook"},
        ),
        (
            "from unittest.mock import patch as p\np.object(hooks, 'sel')\nimport kiro_crew.hooks as hooks\n",
            {"sel"},
        ),
        (
            "import kiro_crew.hooks as hooks\nF = 'kiro_crew.hooks'\nmp.setattr(f'{F}._fd_real_path', None)\n",
            {"_fd_real_path"},
        ),
        (
            "F = 'kiro_crew.hooks'\nmp.setattr(F + '.is_sensitive_path', None)\n",
            {"is_sensitive_path"},
        ),
        (
            "import kiro_crew.hooks as hooks\nhooks._BUILTIN_APP_NAMES = frozenset()\n",
            {"_BUILTIN_APP_NAMES"},
        ),
        (
            "import kiro_crew.hooks as hooks\nassert hooks._fd_real_path is pinned_fs.fd_real_path\n",
            {"_fd_real_path"},
        ),
        ("import kiro_crew.hooks as hooks\nx = hooks.MAX_FILE_BYTES\n", set()),
        ("monkeypatch.setattr('kiro_crew.security.is_sensitive_path', None)\n", set()),
        ("monkeypatch.setattr(other, 'MAX_FILE_BYTES', 1)\n", set()),
    ],
)
def test_the_patch_target_reader_sees_every_spelling(source: str, expected: set[str]) -> None:
    """The detector can find each form and ignores a read, a sibling module's target and
    an unrelated object -- so the sweep below is neither vacuous nor over-broad."""
    assert _facade_writes(source) == expected


def _patched_facade_names() -> set[str]:
    return {name for _, text in _test_sources() for name in _facade_writes(text)}


def test_the_corpus_sweep_finds_the_seams_the_suite_is_known_to_patch() -> None:
    """Non-vacuity on the real corpus, not only on synthetic sources."""
    patched = _patched_facade_names()
    assert len(patched) >= 35, sorted(patched)
    assert {
        "MAX_FILE_BYTES",
        # The two whole-module swaps that simulate Windows and macOS on a Linux runner.
        "os",
        "sys",
        # ``_stat`` is deliberately absent: the suite patches ``hooks._stat.S_ISREG``,
        # which writes an attribute of the shared ``stat`` module, not of this module --
        # so it is a facade READ, not a facade write. It still has to resolve here; it is
        # a stdlib alias ``_BASE_NAMES`` leaves out (SD39), so the guard that keeps it
        # bound is ``test_every_global_an_owner_function_reads_is_bound_on_the_facade``.
        "_fd_real_path",
        "validate_file_path",
        "is_sensitive_path",
        "run_script_hook",
        "_global_script_hook_store",
        "_BUILTIN_APP_NAMES",
        "load_denied_commands_state",
        "sel",
        "_governance_denial",
        "persisted_hook_store",
        "_cu_read_only_auto_approve",
    } <= patched, sorted(patched)
    assert sorted(name for name in patched if not hasattr(hooks_mod, name)) == []


#: Function-local imports an owner carries that shadow a facade name the tests patch,
#: with the ``hooks.py`` line each was moved from verbatim. Each predates this split and
#: shadowed the same name there, so the behaviour is unchanged -- which is exactly why
#: they are listed rather than silently dropped from the sweep: the raw scan is asserted
#: EQUAL to this table, so a NEW shadow fails even though these two do not.
#: ``_audit_governance`` and ``_emit_internal_read_audit`` each re-import ``sel`` inside
#: a ``try`` so a missing SEL backend degrades to a logged warning instead of raising out
#: of an audit path; the facade's module-level ``sel`` is what
#: ``HookManager._on_config_change`` and ``_audit_governance_hook_decision`` read, and
#: ``test_services_config_hot_reload`` patches that one.
_PRE_EXISTING_LOCAL_SHADOWS: dict[str, frozenset[str]] = {
    "governance_gate": frozenset({"sel"}),  # _audit_governance, from hooks.py:2227
    "internal_reads": frozenset({"sel"}),  # _emit_internal_read_audit, from hooks.py:4277
}


def _runtime_shadows(source: str) -> dict[str, list[int]]:
    """Names an owner binds in a way that would shadow the facade's at call time.

    Two shapes, and only two. A FUNCTION-LOCAL import binds the name in the frame that
    runs, so it wins over the facade's global for that call. A MODULE-LEVEL assignment is
    owner state, which ``compose`` never moves, so a reader would see a dead copy.

    Deliberately NOT flagged: a module-level ``import`` (inert -- a rebound function's
    ``__globals__`` are the facade's, so the owner's binding is never read, and the import
    is there for mypy and the linter), and an assignment to a name the function declared
    ``global`` (that writes the facade's namespace, which is how ``set_builtin_app_names``
    and ``set_global_hook_store`` still work from an owner).
    """
    tree = ast.parse(source)
    inside_a_function = {
        id(sub)
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for sub in ast.walk(node)
    } - {
        id(node)
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    found: dict[str, list[int]] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)) and id(node) in inside_a_function:
            for alias in node.names:
                found.setdefault((alias.asname or alias.name).split(".")[0], []).append(node.lineno)
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else []
        if isinstance(node, ast.AnnAssign):
            targets = [node.target]
        for target in targets:
            if isinstance(target, ast.Name):
                found.setdefault(target.id, []).append(node.lineno)
    return found


def test_no_owner_binds_a_name_the_tests_patch_on_the_facade() -> None:
    """The seam the whole technique rests on. A patch of ``hooks.<name>`` reaches a
    moved call site only while that site reads the name out of the facade's globals, so
    no owner may bind one of those names at runtime -- not at module level, and not with
    a function-local ``import``.

    The two that would fail silently rather than loudly: the simulated-Windows ``os``
    and simulated-darwin ``sys`` swaps (``monkeypatch.setattr(hooks, "os", ...)``) reach
    more than twenty functions this split spreads across four owners, so one owner
    reading its own ``os`` would leave half the call graph believing it is on POSIX
    while the other half believes it is on Windows -- and the UNC and case-alias suites
    would stop exercising the gate instead of going red. A module-level ``import os``
    written for mypy and the linter is inert under ``compose``; a function-local one is
    not, which is why both are swept.
    """
    patched = _patched_facade_names()
    found = {
        stem: frozenset(name for name in _runtime_shadows(source) if name in patched)
        for stem, source in _owner_sources().items()
    }
    assert {stem: names for stem, names in found.items() if names} == _PRE_EXISTING_LOCAL_SHADOWS


def test_the_binding_sweep_reports_a_shadow_and_ignores_an_inert_import() -> None:
    """The sweep can fail, and does not fail on the two shapes that are safe."""
    assert "os" in _runtime_shadows("def f():\n    import os\n\n    return os.name\n")
    assert "CAP" in _runtime_shadows("CAP = 7\n")
    # Inert: a module-level import, and a ``global``-declared write.
    assert "os" not in _runtime_shadows("import os\n\n\ndef f():\n    return os.name\n")
    assert "X" not in _runtime_shadows("def f(v):\n    global X\n\n    X = v\n")


def test_no_owner_adds_a_gate_consultation_the_package_scan_would_meet() -> None:
    """``test_hooks.test_every_enforcing_caller_uses_the_shared_extraction`` scans the
    package for an assignment-shaped ``= X.on_tool_call(`` / ``= X.judge(`` and
    exempts only ``hooks.py``. A consultation added in an owner would be scanned and
    would have to use the one extraction; no owner consults the gate (a tier body is
    handed its row, it never calls back into ``judge``).

    Over the AST, not the source text: ``hook_gate_kwargs``'s own docstring quotes a
    dispatcher's call line, and a substring check would flag that prose."""
    calls = {
        stem: sorted(
            node.lineno
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("on_tool_call", "judge")
        )
        for stem, source in _owner_sources().items()
    }
    assert {stem: lines for stem, lines in calls.items() if lines} == {}
