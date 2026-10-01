"""``kiro_crew.cli_doctor`` stays the doctor's one import path and patch target.

The report's sections moved into the :mod:`kiro_crew.doctor_checks` families. What
every caller, test and doc still reaches is this module, so these tests pin what
that promise needs:

* every name the module held before the move still resolves here, and a moved name
  resolves to the object its family holds;
* a write here reaches the code that reads it -- in the family that defines a moved
  name, and in every family that reads a name this module binds -- and every
  patching idiom the suite uses restores exactly what it found;
* a star import still carries every public name;
* the families read what this module binds through it, never through a second
  binding of their own, which is what makes the previous property structural; and
* the families stay off the ``kiro_crew.cli`` import every command pays.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import inspect
import pkgutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from unittest import mock

import pytest

from kiro_crew import cli_doctor, doctor_checks
from kiro_crew.subprocess_utf8 import UTF8_TEXT

_SRC = Path(cli_doctor.__file__).resolve().parent
_REPO = _SRC.parents[1]

#: Every name ``kiro_crew.cli_doctor`` bound before the sections moved, with what it
#: was: a module, a function or class imported from elsewhere (and from where), a
#: value, or a function the doctor defined itself.
_BASE_NAMES: dict[str, str] = {
    "ACP_BACKEND_KAS": "value:str",
    "AGENT_FILENAME": "value:str",
    "CRED_DISCORD_BOT_TOKEN": "value:str",
    "CU_MCP_SERVER": "value:str",
    "DefaultCapabilityManager": "class:kiro_crew.platform.defaults",
    "KAS_RELAY_ENGINE": "value:str",
    "KAS_RELAY_ENGINE_FLAG": "value:str",
    "KIRO_AGENTS_DIR": "value:NoneType",
    "KIRO_CLI_BIN": "value:str",
    "KiroCrewConfig": "class:kiro_crew.config.loader",
    "LEGACY_CONFIG_DIR_NAME": "value:str",
    "MIN_NODE_VERSION": "value:tuple",
    "McpServerInfo": "class:kiro_crew.mcp_discovery",
    "NATIVE_SKILL_ALIAS_PREFIX": "value:str",
    "PATH_ONLY_INSTALL_NOTE": "value:str",
    "Path": "class:pathlib",
    "PlatformCompositionError": "class:kiro_crew.platform.context",
    "SPEC_PERMISSIONS_MIN_VERSION": "value:tuple",
    "UTF8_TEXT": "value:mappingproxy",
    "_ALWAYS_ON_MCPS": "value:tuple",
    "_AWS_PROBE_TIMEOUT_SECS": "value:int",
    "_BLOCKED_COMMANDS_DOC_URL": "value:str",
    "_CLAUDE_ACP_BIN": "value:str",
    "_CLI_INSTALLER_GLOB": "value:str",
    "_CLI_INSTALLER_RESIDUE_MIN": "value:int",
    "_CLI_INSTALLER_SCAN_CAP": "value:int",
    "_CRON_REPORT_CAP": "value:int",
    "_FFMPEG_LINUX_HINT": "value:str",
    "_INDENT": "value:str",
    "_KAS_ENGINE_FLAG_NAME": "value:str",
    "_LEGACY_VENV_DIR_NAMES": "value:tuple",
    "_LIB_PATH_ENV": "value:str",
    "_MAIN_AGENT_NAME": "value:str",
    "_MANAGED_MCPS": "value:tuple",
    "_MEMBER_NAMES_NOT_CHECKED": "value:str",
    "_MOUNT_SOURCE_PREFIX": "value:str",
    "_NO_BLANKET_ALLOW_MCPS": "value:frozenset",
    "_OOM_KILLER_UNITS": "value:tuple",
    "_OPT_IN_MCPS": "value:tuple",
    "_PROC_MEMINFO": "value:PosixPath",
    "_RUN_DIR_BACKLOG_WARN": "value:int",
    "_SKILL_VIEW_BACKLOG_WARN": "value:int",
    "_STALL_CURRENT_SECS": "value:int",
    "_STRICT_IDENTITY_SERVERS": "value:tuple",
    "_TMPFS_FREE_INODES_FLOOR": "value:int",
    "_TMPFS_FREE_PCT_WARN": "value:float",
    "_agent": "module:kiro_crew.agent",
    "_agent_spec_model_problems": "own-func",
    "_agents_dir": "own-func",
    "_aws_auto_refreshes": "own-func",
    "_aws_probe_env": "own-func",
    "_aws_profile_names": "own-func",
    "_backend_policy_label": "own-func",
    "_credential_vendor_line": "own-func",
    "_detect_userspace_oom_killer": "own-func",
    "_discord_install_line": "own-func",
    "_discord_intent_grants": "own-func",
    "_discord_live_state": "own-func",
    "_discord_msg_content_line": "own-func",
    "_discord_unused_intent_line": "own-func",
    "_doctor": "own-func",
    "_doctor_agent_auth": "own-func",
    "_doctor_agents_janitor": "own-func",
    "_doctor_backend_ability_cards": "own-func",
    "_doctor_claude_backend": "own-func",
    "_doctor_cli_installer_residue": "own-func",
    "_doctor_credentials": "own-func",
    "_doctor_cron_health": "own-func",
    "_doctor_cron_script_sources": "own-func",
    "_doctor_data_home": "own-func",
    "_doctor_deprecated_agent_specs": "own-func",
    "_doctor_discord": "own-func",
    "_doctor_effective_model": "own-func",
    "_doctor_gated_off_mcps": "own-func",
    "_doctor_headless_auth": "own-func",
    "_doctor_import_path": "own-func",
    "_doctor_kas": "own-func",
    "_doctor_kiro_internal_sandbox": "own-func",
    "_doctor_live_target_pointer": "own-func",
    "_doctor_managed_service_policy": "own-func",
    "_doctor_masked_credential_aliases": "own-func",
    "_doctor_mcp_gateway_daemon": "own-func",
    "_doctor_mcp_governance": "own-func",
    "_doctor_mcp_tools": "own-func",
    "_doctor_member_dispatchability": "own-func",
    "_doctor_member_memory_bindings": "own-func",
    "_doctor_memory_pressure": "own-func",
    "_doctor_model_url_reachable": "own-func",
    "_doctor_name_grant_platform_scope": "own-func",
    "_doctor_overload_resilience": "own-func",
    "_doctor_path_launcher": "own-func",
    "_doctor_pod_session_bus": "own-func",
    "_doctor_run_dirs": "own-func",
    "_doctor_runtime_tmpfs": "own-func",
    "_doctor_sandbox": "own-func",
    "_doctor_sandbox_apparmor": "own-func",
    "_doctor_sandbox_backend": "own-func",
    "_doctor_selected_backend_projection": "own-func",
    "_doctor_skill_currency": "own-func",
    "_doctor_skill_view_census": "own-func",
    "_doctor_source_checkout": "own-func",
    "_doctor_strict_identity": "own-func",
    "_doctor_task_store": "own-func",
    "_doctor_trust_root": "own-func",
    "_doctor_unresolved_mcp_refs": "own-func",
    "_doctor_whatsapp": "own-func",
    "_find_ffmpeg": "func:kiro_crew.transcribe",
    "_format_job_labels": "own-func",
    "_format_model_pin_problem": "own-func",
    "_gateway_memory_lines": "own-func",
    "_gateway_rss_bytes": "own-func",
    "_git_line": "own-func",
    "_kas_relay_help": "own-func",
    "_kiro_cli_signed_in": "own-func",
    "_legacy_venv_entries": "own-func",
    "_linger_enabled": "own-func",
    "_liveness_platform_line": "own-func",
    "_load_llama_class": "value:_lru_cache_wrapper",
    "_mc_version": "value:str",
    "_member_dispatchability": "own-func",
    "_open_slot_agent_names": "own-func",
    "_os_fix_hint": "own-func",
    "_plat": "module:platform",
    "_platform_libs_dirname": "func:kiro_crew.embeddings",
    "_print_wrapped": "own-func",
    "_process_apparmor_confinement": "own-func",
    "_process_userns_vantage_confined": "own-func",
    "_read_agent_spec": "func:kiro_crew.agent_discovery",
    "_read_gateway_pid": "func:kiro_crew.cli_perf",
    "_read_linux_proc_self": "own-func",
    "_report_kas_backend": "own-func",
    "_report_kas_spec_permissions": "own-func",
    "_report_node": "own-func",
    "_resolve_model_url": "func:kiro_crew.embeddings",
    "_runtime_tmpfs_roots": "own-func",
    "_safe_display": "own-func",
    "_scan_cli_installer_residue": "own-func",
    "_service_profile_applies": "own-func",
    "_source_checkout_root": "func:kiro_crew._bootstrap",
    "_spec_gate_closed": "own-func",
    "_strict_agent_json_specs": "own-func",
    "_swap_total_kib": "own-func",
    "_tmpfs_usage": "own-func",
    "_valid_override_home": "func:kiro_crew.config.paths",
    "_venv_deps_ok": "own-func",
    "acp_id_correction": "func:kiro_crew.model_registry",
    "agent_spec_path": "func:kiro_crew.agent",
    "agent_state": "module:kiro_crew.agent_state",
    "annotations": "value:_Feature",
    "apparmor": "module:kiro_crew.service.apparmor",
    "asyncio": "module:asyncio",
    "atomic_write": "func:kiro_crew.atomic_write",
    "attribute_dump": "func:kiro_crew.stall_attribution",
    "availability_detail": "func:kiro_crew.transcribe",
    "bind_capability_manager": "func:kiro_crew.platform.capability_bound",
    "build_kas_argv": "func:kiro_crew.acp.kas_transport",
    "common_service": "module:kiro_crew.service.common",
    "config_dir": "func:kiro_crew.config.paths",
    "credential_vendor_server_ids": "func:kiro_crew.deny_guidance",
    "current_context": "func:kiro_crew.platform.context",
    "data_home": "func:kiro_crew.config.paths",
    "default_model_path": "func:kiro_crew.embeddings",
    "dep_sync": "module:kiro_crew.dep_sync",
    "describe": "func:kiro_crew.stall_attribution",
    "diagnostics": "module:kiro_crew.diagnostics",
    "doctor_dead_paths": "func:kiro_crew.doctor_deadpath",
    "dump_age_seconds": "func:kiro_crew.dashboard.crash_dump_store",
    "dump_first_stack_lines": "func:kiro_crew.dashboard.crash_dump_store",
    "dump_superseded": "func:kiro_crew.dashboard.crash_dump_store",
    "dumps_with_stacks": "func:kiro_crew.dashboard.crash_dump_store",
    "ensure_ffmpeg_in_path": "func:kiro_crew.transcribe",
    "env_path": "func:kiro_crew.config.loader",
    "format_node_version": "func:kiro_crew.constants",
    "get_dumps_dir": "func:kiro_crew.dashboard.crash_dump_store",
    "install_url": "module:kiro_crew.discord.install_url",
    "installed_kiro_cli_version": "func:kiro_crew.kiro_cli",
    "intent_probe": "module:kiro_crew.discord.intent_probe",
    "is_agent_spec_name": "func:kiro_crew.agent_spec_format",
    "is_claude_code": "func:kiro_crew.agent_sdk.provider_identity",
    "is_dispatchable_member_name": "func:kiro_crew.members",
    "is_local_only": "func:kiro_crew.dashboard.urls",
    "is_registered_agent_name": "func:kiro_crew.validation",
    "is_sensitive_path": "func:kiro_crew.security.paths",
    "job_pause_state_from_disk": "func:kiro_crew.cron",
    "json": "module:json",
    "kiro_agents_dir": "func:kiro_crew.config.paths",
    "logger": "value:Logger",
    "logging": "module:logging",
    "machine_hostname": "func:kiro_crew.dashboard.urls",
    "may_skip_gate_now": "func:kiro_crew.platform.governance",
    "mcp_governance_may_apply": "func:kiro_crew.kiro_cli",
    "model_file_present": "func:kiro_crew.embeddings",
    "newest_dump_with_stacks": "func:kiro_crew.dashboard.crash_dump_store",
    "node_too_old_message": "func:kiro_crew.constants",
    "node_version_meets_floor": "func:kiro_crew.constants",
    "normalize_agent_model": "func:kiro_crew.config.sections",
    "os": "module:os",
    "parse_dashboard_url": "func:kiro_crew.dashboard.urls",
    "parse_node_version": "func:kiro_crew.constants",
    "pip_install_channel_available": "func:kiro_crew.extras",
    "pip_install_command": "func:kiro_crew.extras",
    "pip_install_command_for": "func:kiro_crew.extras",
    "platform_compat": "module:kiro_crew.platform_compat",
    "platform_context": "module:kiro_crew.platform.context",
    "probe_server": "func:kiro_crew.mcp_discovery",
    "project_agent_files": "func:kiro_crew.agent_discovery",
    "project_agent_name": "func:kiro_crew.agent_discovery",
    "project_agents_dir": "func:kiro_crew.config.paths",
    "render_doctor_section": "func:kiro_crew.config.superseded_defaults",
    "resolve_agent_bindings": "func:kiro_crew.config.loader",
    "resolve_custom_model": "func:kiro_crew.embeddings",
    "resolve_effective_model": "func:kiro_crew.config.loader",
    "resolve_kiro_cli": "func:kiro_crew.kiro_cli",
    "safe_context_call": "func:kiro_crew.platform.context",
    "sandbox": "module:kiro_crew.sandbox",
    "sel": "func:kiro_crew.sel",
    "service_controller": "module:kiro_crew.service.controller",
    "service_linux": "module:kiro_crew.service.linux",
    "shlex": "module:shlex",
    "shutil": "module:shutil",
    "signing_health": "func:kiro_crew.session_pid_sig",
    "spec_permissions_supported": "func:kiro_crew.kiro_cli",
    "stdlib_shadow": "module:kiro_crew.stdlib_shadow",
    "stt": "module:kiro_crew.stt",
    "subprocess": "module:subprocess",
    "sweep_agents_dir": "func:kiro_crew.agents_janitor",
    "sys": "module:sys",
    "tempfile": "module:tempfile",
    "textwrap": "module:textwrap",
    "unhealthy_jobs_from_disk": "func:kiro_crew.cron",
    "unsandboxed_exec_declared": "func:kiro_crew.config.loader",
    "urllib": "module:urllib",
    "verify_vendored_libs": "func:kiro_crew.embeddings",
    "warm_backend": "func:kiro_crew.sandbox",
}


def _family_modules() -> list[ModuleType]:
    return [
        importlib.import_module(f"{doctor_checks.__name__}.{info.name}")
        for info in pkgutil.iter_modules(doctor_checks.__path__)
    ]


def _top_level_names(tree: ast.Module) -> set[str]:
    """Names a module DEFINES at its top level (not the ones it imports)."""
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _runtime_imports(tree: ast.Module) -> list[ast.ImportFrom]:
    """Every ``from ... import`` that binds at runtime: module or function scope,
    but not inside an ``if TYPE_CHECKING:`` block."""
    skipped: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Name)
            and node.test.id == "TYPE_CHECKING"
        ):
            skipped.update(id(child) for stmt in node.body for child in ast.walk(stmt))
    return [n for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and id(n) not in skipped]


# ── Every name still resolves, to the object its owner holds ─────────────────


@pytest.mark.parametrize("name", sorted(_BASE_NAMES))
def test_every_name_the_module_held_still_resolves(name: str) -> None:
    kind = _BASE_NAMES[name]
    value = getattr(cli_doctor, name)
    if name in cli_doctor._EXPORTS:
        family = importlib.import_module(cli_doctor._EXPORTS[name])
        assert value is getattr(family, name)
        # Held only by the family: a copy here would shadow it for every later read.
        assert name not in vars(cli_doctor)
    else:
        assert name in vars(cli_doctor)
    category, _, origin = kind.partition(":")
    if category == "module":
        assert isinstance(value, ModuleType) and value.__name__ == origin
    elif category in ("func", "class"):
        assert value.__module__ == origin
    elif category == "own-func":
        assert inspect.isfunction(value)
        assert value.__module__ in (cli_doctor.__name__, cli_doctor._EXPORTS.get(name))
    elif name == "KIRO_AGENTS_DIR":
        # The override hook: ``None`` until a caller (the suite's floor) points it.
        assert value is None or isinstance(value, Path)
    elif origin in ("PosixPath", "WindowsPath"):
        # A concrete path's class is the host's flavour, recorded here on Linux.
        assert isinstance(value, Path)
    else:
        assert type(value).__name__ == origin


def test_the_table_names_exactly_the_moved_names() -> None:
    """The table re-exports exactly the names this module held that a family now
    defines, each pointing at the family that defines it -- no section added since
    the move rides along, so the facade forwards only what callers already knew."""
    defined: dict[str, str] = {}
    for module in _family_modules():
        tree = ast.parse(inspect.getsource(module))
        for name in _top_level_names(tree):
            assert name not in defined, f"{name} is defined in two families"
            defined[name] = module.__name__
    moved = {name: owner for name, owner in defined.items() if name in _BASE_NAMES}
    assert moved == cli_doctor._EXPORTS


def test_a_name_outside_the_table_is_still_an_attribute_error() -> None:
    assert not hasattr(cli_doctor, "_no_such_doctor_section")
    with pytest.raises(AttributeError):
        cli_doctor.__getattr__("_no_such_doctor_section")
    assert set(cli_doctor._EXPORTS) <= set(dir(cli_doctor))


# ── A write here reaches the reader, and every idiom restores it ─────────────


@pytest.mark.parametrize("name", sorted(cli_doctor._EXPORTS))
def test_a_patch_here_lands_on_the_family_and_is_undone(name: str) -> None:
    family = importlib.import_module(cli_doctor._EXPORTS[name])
    original = getattr(family, name)
    outer, inner = object(), object()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(cli_doctor, name, outer)
        assert getattr(family, name) is outer
        assert name not in vars(cli_doctor)
        with mock.patch.object(cli_doctor, name, inner):
            assert getattr(family, name) is inner
        assert getattr(family, name) is outer
        with mock.patch(f"{cli_doctor.__name__}.{name}", inner):
            assert getattr(family, name) is inner
        assert getattr(family, name) is outer
    assert getattr(family, name) is original
    assert getattr(cli_doctor, name) is original
    assert name not in vars(cli_doctor)


def test_a_patch_of_a_name_this_module_binds_reaches_a_family_reader(capsys) -> None:
    """The other direction: a family reads what this module binds through it."""
    from kiro_crew.doctor_checks import access

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(cli_doctor, "signing_health", lambda: (True, Path("/pinned/key")))
        access._doctor_trust_root()

    assert capsys.readouterr().out == f"  trust root:  ✅ {Path('/pinned/key')}\n"


# A ``create=True`` patch of a re-exported name is refused by the scan in
# ``test_cli_doctor_refactor_create_guard.py``.


def test_a_loaded_family_is_read_without_calling_import_module() -> None:
    """``sys.modules`` answers; ``importlib.import_module`` only fills it.

    ``importlib.import_module`` is an attribute any caller can rebind, and tests do.
    With it refused outright, a name whose family is already loaded still reads,
    writes and restores through the facade.
    """
    from kiro_crew.doctor_checks import render

    name = "_safe_display"
    original = getattr(render, name)
    sentinel = object()
    real = importlib.import_module

    def refuse(target: str, package: str | None = None) -> ModuleType:
        raise AssertionError(f"the facade called import_module for {target!r}")

    try:
        with mock.patch("importlib.import_module", side_effect=refuse):
            assert getattr(cli_doctor, name) is original
            setattr(cli_doctor, name, sentinel)
            assert getattr(render, name) is sentinel
            delattr(cli_doctor, name)
            assert name not in vars(render)
            setattr(cli_doctor, name, original)
    finally:
        render._safe_display = original
    assert importlib.import_module is real
    assert getattr(cli_doctor, name) is original


def _bare_loads(source: str) -> list[tuple[int, str]]:
    """Loads of a re-exported name as a bare global, outside the import lines."""
    tree = ast.parse(source)
    import_lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            import_lines.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    return [
        (node.lineno, node.id)
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
        and isinstance(node.ctx, ast.Load)
        and node.id in cli_doctor._EXPORTS
        and node.lineno not in import_lines
    ]


def test_the_facade_reads_no_reexported_name_as_a_bare_global() -> None:
    """A function defined here resolves a bare global through this module's own
    namespace, which ``__getattr__`` never sees; such a read would ``NameError``."""
    source = Path(cli_doctor.__file__).read_text(encoding="utf-8")
    assert _bare_loads(source) == []


def test_the_bare_global_scan_can_fail() -> None:
    sample = sorted(cli_doctor._EXPORTS)[0]
    assert _bare_loads(f"def f():\n    return {sample}\n") == [(2, sample)]
    assert _bare_loads(f"from kiro_crew.doctor_checks.x import (\n    {sample},\n)\n") == []


# ── One home per name ─────────────────────────────────────────────────────────


def test_no_family_holds_a_second_binding_of_a_facade_name() -> None:
    """A family reads what this module binds as ``cli_doctor.<name>`` and a sibling
    family's definition as ``<family>.<name>``. A ``from ... import`` of either
    would be a second binding a patch here does not reach."""
    facade_names = {
        name for name, value in vars(cli_doctor).items() if not isinstance(value, ModuleType)
    }
    # Imports the sections made inside their own bodies before the move, kept as
    # they were: at the time a patch on this module did not reach them either.
    kept_as_they_were = {("kiro_crew.doctor_checks.workload", "data_home")}
    offenders: list[str] = []
    for module in _family_modules():
        tree = ast.parse(inspect.getsource(module))
        for name in _top_level_names(tree):
            if name in facade_names:
                offenders.append(f"{module.__name__} defines {name}")
        for node in _runtime_imports(tree):
            source = node.module or ""
            for alias in node.names:
                bound = alias.asname or alias.name
                if (module.__name__, bound) in kept_as_they_were:
                    continue
                imported = getattr(importlib.import_module(source), alias.name, None)
                if isinstance(imported, ModuleType):
                    continue
                if source == cli_doctor.__name__ or source.startswith(doctor_checks.__name__):
                    offenders.append(f"{module.__name__} imports {bound} from {source}")
                elif source.startswith("kiro_crew") and bound in facade_names:
                    offenders.append(f"{module.__name__} imports {bound} from {source}")
    assert offenders == []


def test_the_facade_imports_no_family_at_module_scope() -> None:
    """The families load when a report runs, not when ``cli_doctor`` does; the
    ``TYPE_CHECKING`` block names every re-export for the type checker."""
    tree = ast.parse(Path(cli_doctor.__file__).read_text(encoding="utf-8"))
    runtime = [
        node.module
        for node in tree.body
        if isinstance(node, ast.ImportFrom)
        and (node.module or "").startswith(doctor_checks.__name__)
    ]
    assert runtime == []
    typed: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.If) and getattr(node.test, "id", "") == "TYPE_CHECKING":
            for stmt in node.body:
                if isinstance(stmt, ast.ImportFrom):
                    typed.update({alias.name: stmt.module or "" for alias in stmt.names})
    assert typed == cli_doctor._EXPORTS


def test_a_star_import_still_carries_every_public_name(tmp_path: Path) -> None:
    """Through a real module doing the star import, as any importer would."""
    probe = tmp_path / "doctor_star_probe.py"
    probe.write_text(f"from {cli_doctor.__name__} import *  # noqa: F401,F403\n", encoding="utf-8")
    spec = importlib.util.spec_from_file_location("doctor_star_probe", probe)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    namespace = vars(module)
    public = [name for name in _BASE_NAMES if not name.startswith("_")]
    missing = [name for name in public if name not in namespace]
    assert missing == []
    assert all(namespace[name] is getattr(cli_doctor, name) for name in public)


def test_the_package_names_every_family() -> None:
    doc = doctor_checks.__doc__ or ""
    for module in _family_modules():
        assert f":mod:`~{module.__name__}`" in doc, module.__name__


# ── Import weight ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("entry", ["kiro_crew.cli", "kiro_crew.cli_doctor"])
def test_importing_the_cli_loads_no_family(entry: str, tmp_path: Path) -> None:
    """``cli.py`` imports ``_doctor`` at module scope for every command, including
    the long-lived MCP stdio servers, so a family imported from here would be paid
    by all of them."""
    code = (
        f"import sys, {entry}\n"
        "loaded = sorted(m for m in sys.modules if m.startswith('kiro_crew.doctor_checks'))\n"
        "print(','.join(loaded))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        capture_output=True,
        timeout=120,
        check=False,
        **UTF8_TEXT,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""
