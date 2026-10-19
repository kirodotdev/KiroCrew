"""``kiro_crew.cli_doctor`` composes its split-out rows onto one namespace.

The Model section and the model-pin audit, MCP Tools and MCP Governance, the host probes
and the KAS relay rows moved out of ``cli_doctor.py`` into the
``kiro_crew.cli_doctor_parts`` owners, and :func:`kiro_crew._facade_compose.compose` runs
every function they define on ``kiro_crew.cli_doctor``'s module globals. The facade's own
contract (``test/test_cli_doctor_refactor_facade.py``) passes because of that, with its two
universal source scans widened to the owner files; these tests pin what the composition
itself promises:

* every owner function runs on the facade's globals, names the facade as its module, is
  the object the facade binds, and every global it reads -- nested code included -- is
  bound there;
* every moved function keeps its kind, signature and defaults;
* every name the test trees patch on the facade reaches every owner function that reads
  it, and one patch per owner is exercised for real;
* an owner imports standard-library modules plainly and the facade only for type
  checking, defines no module-level value, carries no agent-SDK boundary edge, logs
  through the facade's logger, and nothing but the facade imports one;
* the facade composes through the shared helper, once, and the package defines nothing;
* the facade imports nothing its family does not account for, which stands in for the
  F401 check ``setup.cfg`` switches off for it; and
* a fresh ``import kiro_crew.cli`` composes every owner and still loads no
  ``doctor_checks`` family.
"""

from __future__ import annotations

import ast
import builtins
import dis
import functools
import importlib
import importlib.util
import inspect
import pkgutil
import subprocess
import sys
import types
from pathlib import Path

import facade_mirror_scan
import pytest
import source_corpus

from kiro_crew import _facade_mirror, cli_doctor, cli_doctor_parts
from kiro_crew._facade_compose import compose as facade_compose
from kiro_crew.subprocess_utf8 import UTF8_TEXT

pytestmark = pytest.mark.xdist_group(name="tree_scan_cli_doctor_parts")

FACADE = cli_doctor.__name__
PACKAGE = cli_doctor_parts.__name__
_PARTS_DIR = Path(cli_doctor_parts.__file__).resolve().parent
_SRC = Path(cli_doctor.__file__).resolve().parent
_GLOBAL_OPS = frozenset({"LOAD_GLOBAL", "LOAD_NAME", "STORE_GLOBAL", "DELETE_GLOBAL"})

#: The owners, by module stem.
_OWNER_STEMS = ("host_probes", "kas_relay", "mcp_sections", "model_section")

#: Each moved function's kind and signature in the one-module file, by owner.
_BASE_SURFACE: dict[str, dict[str, str]] = {
    "host_probes": {
        "_aws_auto_refreshes": "() -> 'bool | None'",
        "_aws_probe_env": "() -> 'dict[str, str]'",
        "_aws_profile_names": "() -> 'list[str] | None'",
        "_credential_vendor_line": "() -> 'str'",
        "_detect_userspace_oom_killer": "() -> 'str | bool | None'",
        "_discord_intent_grants": "(token: 'str') -> 'intent_probe.IntentGrants'",
        "_gateway_rss_bytes": "(pid: 'int') -> 'int | None'",
        "_git_line": "(repo: 'Path', *args: 'str') -> 'str | None'",
        "_kiro_cli_signed_in": "() -> 'bool | None'",
        "_linger_enabled": "(user: 'str') -> 'bool | None'",
        "_report_node": "(issues: 'list[str]') -> 'None'",
        "_venv_deps_ok": "(venv_py: 'Path') -> 'bool'",
    },
    "kas_relay": {
        "_doctor_kas": "(issues: 'list[str]') -> 'None'",
        "_kas_relay_help": "(binary: 'str') -> 'str | None'",
        "_report_kas_backend": "(issues: 'list[str]') -> 'None'",
        "_report_kas_bundle": "(issues: 'list[str]') -> 'None'",
        "_report_kas_spec_permissions": "(issues: 'list[str]') -> 'None'",
        "_report_kas_tool_vocabulary": "(issues: 'list[str]') -> 'None'",
    },
    "mcp_sections": {
        "_doctor_gated_off_mcps": "() -> 'frozenset[str]'",
        "_doctor_mcp_governance": "(agent_path: 'Path', issues: 'list[str]', *, gated_off: \"'frozenset[str] | None'\" = None) -> 'None'",
        "_doctor_mcp_tools": "(agent_path: 'Path', issues: 'list[str]', *, gated_off: \"'frozenset[str] | None'\" = None) -> 'None'",
        "_doctor_whoami_governance": "() -> 'bool | None'",
        "_spec_gate_closed": "(name: 'str') -> 'bool'",
    },
    "model_section": {
        "_agent_spec_model_problems": "(agents_dir: 'Path | None' = None, project_dir: 'str | Path | None' = None, provider: 'str' = 'acp') -> 'list[tuple[str, str, str]] | None'",
        "_doctor_effective_model": "(cfg: 'KiroCrewConfig', project_dir: 'str', issues: 'list[str]') -> 'None'",
        "_format_model_pin_problem": "(name: 'str', pin: 'str', correction: 'str') -> 'tuple[str, str]'",
        "_strict_agent_json_specs": "(directory: 'Path') -> 'list[Path]'",
    },
}

#: Names the test trees are known to patch on the facade, at least one per owner and per
#: kind of seam: an owner function a family calls, an owner function the orchestrator calls,
#: a name the facade only imports and an owner reads, and the override hook the conftest
#: floor sets. The scan below must find all of them, so a scanner that stops matching goes
#: red instead of passing over nothing.
_KNOWN_SEAMS = frozenset(
    {
        "KIRO_AGENTS_DIR",
        "_doctor_whoami_governance",
        "_git_line",
        "_kas_relay_help",
        "_strict_agent_json_specs",
        "mcp_governance_may_apply",
        "probe_server",
        "resolve_kiro_cli",
        "sel",
    }
)

#: Agent-SDK boundary edges ``cli_doctor.py`` carries (``.github/agent-sdk-boundary-baseline.txt``).
_FACADE_EDGES = 3

#: Every name the facade's runtime import statements bound before its rows moved out. They
#: stay bound because the composed rows read them as this module's globals, and callers and
#: tests read and patch them as its attributes.
_BASE_IMPORTS = frozenset("""
    _agent _ALWAYS_ON_MCPS _find_ffmpeg _LIB_PATH_ENV _load_llama_class _MANAGED_MCPS
    _mc_version _MOUNT_SOURCE_PREFIX _OPT_IN_MCPS _plat _platform_libs_dirname
    _read_agent_spec _read_gateway_pid _resolve_model_url _source_checkout_root
    _valid_override_home ACP_BACKEND_KAS acp_id_correction AGENT_FILENAME
    agent_spec_path agent_state Any apparmor asyncio atomic_write attribute_dump
    availability_detail bind_capability_manager build_kas_argv bundle_carries_decoder
    common_service config_dir contextlib CRED_DISCORD_BOT_TOKEN
    credential_vendor_server_ids cron_store_quarantine_copies CU_MCP_SERVER
    current_context data_home default_model_path DefaultCapabilityManager dep_sync
    describe diagnostics doctor_dead_paths dropped_inherited_lib_path dump_age_seconds
    dump_first_stack_lines dump_superseded dumps_with_stacks ensure_ffmpeg_in_path
    env_path format_node_version get_dumps_dir importlib install_url
    installed_kiro_cli_version intent_probe is_agent_spec_name is_claude_code
    is_dispatchable_member_name is_local_only is_registered_agent_name is_sensitive_path
    job_pause_state_from_disk json kas_engine_gap KAS_RELAY_ENGINE kas_relay_help
    kas_verified_releases kas_vocabulary_drift kiro_agents_dir KIRO_CLI_BIN
    KiroCrewConfig LEGACY_CONFIG_DIR_NAME logging machine_hostname may_skip_gate_now
    mcp_governance_may_apply mcp_ref_owned_by McpServerInfo MIN_NODE_VERSION
    model_file_present ModuleType NATIVE_SKILL_ALIAS_PREFIX newest_dump_with_stacks
    node_too_old_message node_version_meets_floor normalize_agent_model
    operator_lib_path_override os parse_dashboard_url parse_node_version Path
    PATH_ONLY_INSTALL_NOTE pip_install_channel_available pip_install_command
    pip_install_command_for platform_compat platform_context PlatformCompositionError
    probe_server project_agent_files project_agent_name project_agents_dir
    render_doctor_section resolve_agent_bindings resolve_custom_model
    resolve_effective_model resolve_kiro_cli safe_context_call sandbox sel
    service_controller service_linux session_pid shadowed_markdown_specs shlex shutil
    signing_health SKILL_VIEW_PROJECTION_CEILING SPEC_PERMISSIONS_MIN_VERSION
    spec_permissions_supported stdlib_shadow stt subprocess sweep_agents_dir sys
    tempfile textwrap TYPE_CHECKING unhealthy_jobs_from_disk unsandboxed_exec_declared
    urllib user_json UTF8_TEXT verify_vendored_libs warm_backend
    withheld_by_confirm_tools without_mcp_refs
    """.split())

#: The one name the facade's composition glue imports.
_GLUE_IMPORTS = frozenset({"_facade_compose"})


def _owners() -> list[types.ModuleType]:
    """Every owner module, imported through the package the facade composes."""
    return [
        importlib.import_module(f"{PACKAGE}.{info.name}")
        for info in pkgutil.iter_modules([str(_PARTS_DIR)])
    ]


def _owner_paths() -> dict[str, Path]:
    return {path.stem: path for path in sorted(_PARTS_DIR.glob("*.py")) if path.stem != "__init__"}


def _owner_functions() -> list[tuple[str, types.FunctionType]]:
    """``(owner.name, function)`` for every function an owner's file defines."""
    found = []
    for owner in _owners():
        short = owner.__name__.rpartition(".")[2]
        for name, value in vars(owner).items():
            if (
                isinstance(value, types.FunctionType)
                and value.__code__.co_filename == owner.__file__
            ):
                found.append((f"{short}.{name}", value))
    return found


def _global_names(code: types.CodeType):
    """Every global a code object and the code objects nested in it read or write."""
    for instruction in dis.get_instructions(code):
        if instruction.opname in _GLOBAL_OPS:
            yield instruction.argval
    for constant in code.co_consts:
        if isinstance(constant, types.CodeType):
            yield from _global_names(constant)


def _unbound_globals(fn: types.FunctionType) -> list[str]:
    namespace = fn.__globals__
    return sorted(
        {n for n in _global_names(fn.__code__) if n not in namespace and not hasattr(builtins, n)}
    )


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


# ── 1. every owner function runs on the facade's namespace ──────────────────


def test_the_facade_composes_every_module_of_the_package() -> None:
    """Every module of the package is composed, the four the split created among them."""
    stems = set(_owner_paths())
    assert set(_OWNER_STEMS) <= stems
    assert sorted(cli_doctor._PARTS) == sorted(f"{PACKAGE}.{stem}" for stem in stems)
    assert sorted(o.__name__ for o in _owners()) == sorted(cli_doctor._PARTS)


def test_every_owner_function_runs_on_the_facade_globals_as_the_facade_binds_it() -> None:
    functions = _owner_functions()
    assert len(functions) >= sum(len(names) for names in _BASE_SURFACE.values())
    wrong = [
        label
        for label, fn in functions
        if fn.__globals__ is not vars(cli_doctor)
        or fn.__module__ != FACADE
        or vars(cli_doctor).get(fn.__name__) is not fn
    ]
    assert wrong == []


def test_the_sweep_reports_a_global_the_namespace_does_not_bind() -> None:
    module_code = compile(
        "def f():\n    def g():\n        return absent\n    return present, g\n", "probe", "exec"
    )
    code = next(c for c in module_code.co_consts if isinstance(c, types.CodeType))
    probe = types.FunctionType(code, {"__name__": "probe", "present": 1})
    assert _unbound_globals(probe) == ["absent"]


def test_every_global_an_owner_function_reads_is_bound_on_the_facade() -> None:
    """Nested code included: ``_doctor_mcp_tools`` defines its probe coroutine inline."""
    missing = {label: names for label, fn in _owner_functions() if (names := _unbound_globals(fn))}
    assert missing == {}


def test_every_moved_function_keeps_its_base_kind_signature_and_defaults() -> None:
    """``inspect.signature`` carries the defaults and keyword-only defaults, so this is
    the BASE ``__defaults__`` and ``__kwdefaults__`` too; no moved function is a closure.
    Each moved function is checked where it landed; a function added later is not."""
    changed: dict[str, str] = {}
    for stem, names in _BASE_SURFACE.items():
        owner = importlib.import_module(f"{PACKAGE}.{stem}")
        for name, signature in names.items():
            value = vars(owner).get(name)
            if not isinstance(value, types.FunctionType):
                changed[f"{stem}.{name}"] = "missing"
            elif inspect.iscoroutinefunction(value) or value.__closure__ is not None:
                changed[f"{stem}.{name}"] = "kind"
            elif str(inspect.signature(value)) != signature:
                changed[f"{stem}.{name}"] = str(inspect.signature(value))
    assert changed == {}


# ── 2. a patch of the facade reaches every owner reader ─────────────────────


def _spellings() -> dict[str, str]:
    """The facade's dotted name, plus ``<module>.cli_doctor`` for each src module that binds
    it at module level -- another spelling of the same object a test can patch through."""
    spellings = {FACADE: FACADE}
    for path, text in source_corpus.iter_candidate_sources(require_all=("cli_doctor",)):
        if not path.is_relative_to(_SRC):
            continue
        module = ".".join(path.relative_to(_SRC.parent).with_suffix("").parts)
        for node in ast.parse(text).body:
            if isinstance(node, ast.ImportFrom) and node.module == "kiro_crew" and not node.level:
                for alias in node.names:
                    if alias.name == "cli_doctor":
                        spellings[f"{module}.{alias.asname or alias.name}"] = FACADE
    return spellings


def _test_tree_files() -> list[Path]:
    root = source_corpus.repo_root()
    test_root = root / "test"
    out = []
    for path in source_corpus.repo_files_named(".py"):
        rel = path.relative_to(root).as_posix()
        if rel == "conftest.py" or path.is_relative_to(test_root) or "/tests/" in rel:
            out.append(path)
    return out


@functools.lru_cache(maxsize=1)
def _corpus_patched_names() -> frozenset[str]:
    """Every name the test trees patch on the facade, scanned once per worker."""
    spellings = _spellings()
    found: set[str] = set()
    for path in _test_tree_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        if "cli_doctor" not in text:
            continue
        try:
            found |= facade_mirror_scan.patched_names(text, spellings).get(FACADE, set())
        except SyntaxError:
            continue
    return frozenset(found)


def test_leaf_test_scope_derives_this_file_as_a_corpus_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It reads the whole test tree, so a leaf-only diff must still run it."""
    root = source_corpus.repo_root()
    monkeypatch.syspath_prepend(str(root / "scripts"))
    leaf_test_scope = importlib.import_module("leaf_test_scope")
    this_file = Path(__file__).resolve().relative_to(root).as_posix()
    assert this_file in leaf_test_scope.corpus_gates(root)


def test_the_spellings_include_a_family_binding_of_the_facade() -> None:
    assert f"{cli_doctor_parts.__name__.rpartition('.')[0]}.doctor_checks.access.cli_doctor" in (
        _spellings()
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "from kiro_crew import cli_doctor\n"
            "def t(monkeypatch):\n    monkeypatch.setattr(cli_doctor, 'A', 1)\n",
            {"A"},
        ),
        (
            "from unittest.mock import patch\n"
            "def t():\n    with patch('kiro_crew.cli_doctor.B'):\n        pass\n",
            {"B"},
        ),
        (
            "from kiro_crew.doctor_checks import access\n"
            "def t(monkeypatch):\n    monkeypatch.setattr(access.cli_doctor, 'C', 1)\n",
            {"C"},
        ),
        (
            "from unittest.mock import patch\n"
            "def t():\n    with patch('kiro_crew.cli_doctor_parts.host_probes.D'):\n        pass\n",
            set(),
        ),
    ],
    ids=["monkeypatch", "mock-patch-string", "family-binding", "owner-string"],
)
def test_the_patch_scan_reads_every_spelling_and_nothing_else(
    source: str, expected: set[str]
) -> None:
    assert facade_mirror_scan.patched_names(source, _spellings()).get(FACADE, set()) == expected


def test_the_patch_scan_finds_every_known_seam() -> None:
    """Non-vacuity: a scan that found nothing would pass the reach test over nothing."""
    assert sorted(_KNOWN_SEAMS - _corpus_patched_names()) == []


def test_every_name_the_tests_patch_on_the_facade_reaches_every_owner_reader() -> None:
    """A patch of the facade reaches owner code only where that code runs on its globals."""
    patched = _corpus_patched_names()
    readers = 0
    missed = []
    for label, fn in _owner_functions():
        loaded = set(_global_names(fn.__code__)) & patched
        readers += bool(loaded)
        if loaded and fn.__globals__ is not vars(cli_doctor):
            missed.append(f"{label}: {sorted(loaded)}")
    assert readers >= len(_OWNER_STEMS), "no owner reads a patched name -- the sweep went blind"
    assert missed == []


def test_a_facade_patch_reaches_the_model_section(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert cli_doctor._agent_spec_model_problems(agents_dir=tmp_path) == []

    def refuse(directory: Path) -> list[Path]:
        raise RuntimeError(f"refused {directory}")

    monkeypatch.setattr(cli_doctor, "_strict_agent_json_specs", refuse)
    assert cli_doctor._agent_spec_model_problems(agents_dir=tmp_path) is None


def test_a_facade_patch_reaches_the_mcp_sections(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_doctor, "_MANAGED_MCPS", ("planted-open", "planted-closed"))
    monkeypatch.setattr(cli_doctor, "_spec_gate_closed", lambda name: name == "planted-closed")
    assert cli_doctor._doctor_gated_off_mcps() == frozenset({"planted-closed"})


def test_a_facade_patch_reaches_the_host_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Path, str]] = []

    class _Done:
        returncode = 0

    class _DepSync:
        @staticmethod
        def _probe_interpreter(venv_py: Path, code: str, timeout: float) -> _Done:
            calls.append((venv_py, code))
            return _Done()

    monkeypatch.setattr(cli_doctor, "dep_sync", _DepSync)
    assert cli_doctor._venv_deps_ok(Path("planted-python")) is True
    assert [venv for venv, _code in calls] == [Path("planted-python")]


def test_a_facade_patch_reaches_the_kas_relay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_doctor, "kas_relay_help", lambda binary: f"help for {binary}")
    assert cli_doctor._kas_relay_help("planted-kiro-cli") == "help for planted-kiro-cli"


# ── 3. what an owner may hold and import ────────────────────────────────────


def _runtime_imports(tree: ast.Module) -> list[tuple[int, str]]:
    """``(line, module)`` of each import an owner executes when it is imported.

    Module level only: a row that imported a name inside its own body did so in the
    one-module file too, and that import still runs when the row does.
    """
    found = []
    pending = list(tree.body)
    while pending:
        node = pending.pop()
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            pending += node.orelse
        elif isinstance(node, (ast.If, ast.Try, ast.With)):
            pending += [n for f in ("body", "orelse", "finalbody") for n in getattr(node, f, [])]
            pending += [n for h in getattr(node, "handlers", []) for n in h.body]
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.append((node.lineno, node.module))
        elif isinstance(node, ast.Import):
            found += [(node.lineno, a.name) for a in node.names]
    return sorted(found)


def _bad_owner_imports(tree: ast.Module) -> list[str]:
    """Runtime project imports, and type-only imports from anywhere but the facade."""
    bad = [
        f"{line}: {mod}"
        for line, mod in _runtime_imports(tree)
        if mod == "kiro_crew" or mod.startswith("kiro_crew.")
    ]
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            for stmt in ast.walk(node):
                if isinstance(stmt, ast.ImportFrom) and stmt.module != FACADE:
                    bad.append(f"{stmt.lineno}: type-only {stmt.module}")
                elif isinstance(stmt, ast.Import):
                    bad.append(f"{stmt.lineno}: type-only import")
    return bad


@pytest.mark.parametrize(
    ("source", "flagged"),
    [
        ("import os\nfrom pathlib import Path\n", False),
        ("if TYPE_CHECKING:\n    from kiro_crew.cli_doctor import sel\n", False),
        ("from kiro_crew.cli_doctor import sel\n", True),
        ("from kiro_crew import sandbox\n", True),
        ("import kiro_crew.cli_doctor\n", True),
        ("if TYPE_CHECKING:\n    from kiro_crew.sandbox import warm_backend\n", True),
    ],
)
def test_the_owner_import_check_sees_every_spelling(source: str, flagged: bool) -> None:
    assert bool(_bad_owner_imports(ast.parse(source))) is flagged


def test_an_owner_imports_the_facade_only_for_type_checking_and_no_project_module() -> None:
    bad = {
        stem: found
        for stem, path in _owner_paths().items()
        if (found := _bad_owner_imports(_tree(path)))
    }
    assert bad == {}


def _module_values(tree: ast.Module) -> list[str]:
    """Top-level statements that are neither the docstring, an import, the
    ``TYPE_CHECKING`` block nor a function definition."""
    found = []
    for index, node in enumerate(tree.body):
        if index == 0 and isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef)):
            continue
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            continue
        found.append(f"{node.lineno}: {type(node).__name__}")
    return found


def test_the_value_check_can_fail() -> None:
    assert _module_values(ast.parse('"""doc"""\nimport os\nLIMIT = 3\nclass K: ...\n')) == [
        "3: Assign",
        "4: ClassDef",
    ]


def test_no_owner_defines_a_module_level_value() -> None:
    """The doctor's constants stay on the facade: a test that rebinds one there rebinds
    what every composed row reads, and an owner copy would read stale."""
    found = {
        stem: values
        for stem, path in _owner_paths().items()
        if (values := _module_values(_tree(path)))
    }
    assert found == {}


def test_the_package_init_defines_nothing() -> None:
    init = _PARTS_DIR / "__init__.py"
    assert _facade_mirror._defined_names(_tree(init)) == set()
    assert [n for n in _tree(init).body if not isinstance(n, ast.Expr)] == []


def _imports_an_owner(source: str) -> bool:
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            mod = "." * node.level + (node.module or "")
            if "cli_doctor_parts" in mod or any(a.name == "cli_doctor_parts" for a in node.names):
                return True
        elif isinstance(node, ast.Import) and any("cli_doctor_parts" in a.name for a in node.names):
            return True
    return False


@pytest.mark.parametrize(
    ("source", "flagged"),
    [
        ("from kiro_crew.cli_doctor_parts import host_probes\n", True),
        ("from kiro_crew.cli_doctor_parts.kas_relay import _doctor_kas\n", True),
        ("import kiro_crew.cli_doctor_parts.mcp_sections\n", True),
        ("from kiro_crew import cli_doctor_parts\n", True),
        ("from kiro_crew.cli_doctor import _doctor_kas\n", False),
    ],
)
def test_the_importer_check_sees_every_spelling(source: str, flagged: bool) -> None:
    assert _imports_an_owner(source) is flagged


def test_nothing_but_the_facade_imports_an_owner() -> None:
    importers = sorted(
        str(path.relative_to(_SRC.parent))
        for path in sorted(_SRC.rglob("*.py"))
        if path.parent != _PARTS_DIR
        and path.name != "cli_doctor.py"
        and "cli_doctor_parts" in (text := path.read_text(encoding="utf-8"))
        and _imports_an_owner(text)
    )
    assert importers == []


def _rel(path: Path) -> str:
    """*path* as the boundary gate's own scan names it: relative to the repository."""
    return path.resolve().relative_to(source_corpus.repo_root()).as_posix()


def _boundary_gate() -> types.ModuleType:
    root = source_corpus.repo_root()
    spec = importlib.util.spec_from_file_location(
        "_s27_agent_sdk_boundary", root / "scripts" / "check_agent_sdk_boundary.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_agent_sdk_boundary_edges_stay_in_the_facade() -> None:
    gate = _boundary_gate()
    edges = {
        path.name: len(gate._violations_in_source(_rel(path), path.read_text(encoding="utf-8")))
        for path in sorted(_PARTS_DIR.glob("*.py"))
    }
    assert edges == dict.fromkeys(edges, 0)
    facade = Path(cli_doctor.__file__)
    edges_here = gate._violations_in_source(_rel(facade), facade.read_text(encoding="utf-8"))
    assert len(edges_here) == _FACADE_EDGES


@pytest.mark.parametrize(
    "source", ["from ..acp.client import KIRO_CLI_BIN\n", "from kiro_crew.acp import types\n"]
)
def test_the_boundary_check_sees_an_owner_import_of_the_acp_layer(source: str) -> None:
    """The gate resolves a relative import against the repository path it is given."""
    gate = _boundary_gate()
    assert gate._violations_in_source(_rel(_PARTS_DIR / "kas_relay.py"), source) != {}


def test_the_owners_log_through_the_facade_logger() -> None:
    assert cli_doctor.logger.name == FACADE
    assert [o.__name__ for o in _owners() if "logger" in vars(o)] == []
    assert any("logger" in set(_global_names(fn.__code__)) for _label, fn in _owner_functions())


# ── 4. the composition and the facade's imports ─────────────────────────────


def test_the_facade_composes_through_the_shared_helper_once() -> None:
    assert cli_doctor._facade_compose is facade_compose
    source = Path(cli_doctor.__file__).read_text(encoding="utf-8")
    assert source.count("\n_facade_compose(") == 1


def unexpected_imports(source: str, owner_names: dict[str, set[str]], read: set[str]) -> list[str]:
    """Names *source* binds by a runtime import that nothing accounts for: the F401 check
    ``setup.cfg`` switches off for this module, kept as a test.

    A name is accounted for when the module bound it before its rows moved out (BASE),
    the composition glue imports it, an owner module defines it (for an import from that
    owner), or the family reads it: the facade's own code, a composed owner function, or
    a ``doctor_checks`` family through ``cli_doctor.<name>``. Imports under
    ``if TYPE_CHECKING:`` bind nothing at run time and are the facade contract's business.
    """
    found: list[str] = []
    for node in ast.parse(source).body:
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            continue
        if isinstance(node, ast.Import):
            bound = [(alias.asname or alias.name.split(".")[0], None) for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module != "__future__":
            bound = [(alias.asname or alias.name, node.module) for alias in node.names]
        else:
            continue
        for name, module in bound:
            if module in owner_names:
                if name not in owner_names[module]:
                    found.append(f"{module}.{name}")
            elif name not in _BASE_IMPORTS | _GLUE_IMPORTS | read:
                found.append(name)
    return found


def _owner_names() -> dict[str, set[str]]:
    return {
        f"{PACKAGE}.{stem}": _facade_mirror._defined_names(_tree(path))
        for stem, path in _owner_paths().items()
    }


def _family_reads() -> set[str]:
    """Every global the facade's code and the composed owner functions read, and every
    ``cli_doctor.<name>`` a ``doctor_checks`` family reads."""
    facade = Path(cli_doctor.__file__)
    read = set(_global_names(compile(facade.read_text(encoding="utf-8"), str(facade), "exec")))
    read |= {n for _label, fn in _owner_functions() for n in _global_names(fn.__code__)}
    families = Path(cli_doctor.__file__).resolve().parent / "doctor_checks"
    for path in sorted(families.glob("*.py")):
        for node in ast.walk(_tree(path)):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "cli_doctor"
            ):
                read.add(node.attr)
    return read


def test_the_facade_imports_nothing_the_family_does_not_account_for() -> None:
    source = Path(cli_doctor.__file__).read_text(encoding="utf-8")
    assert unexpected_imports(source, _owner_names(), _family_reads()) == []


def test_the_inventory_check_reports_a_planted_import() -> None:
    """The F401 stand-in must be able to fail: an import nothing reads, and an owner import
    of a name that owner does not define, are both reported."""
    planted = (
        Path(cli_doctor.__file__).read_text(encoding="utf-8")
        + "\nimport pickle\nfrom kiro_crew.cli_doctor_parts.kas_relay import _git_line\n"
    )
    assert unexpected_imports(planted, _owner_names(), _family_reads()) == [
        "pickle",
        "kiro_crew.cli_doctor_parts.kas_relay._git_line",
    ]


def test_a_fresh_cli_import_composes_every_owner_and_loads_no_family() -> None:
    """``cli.py`` imports ``_doctor`` for every command, so the doctor's owners load then,
    composed; the ``doctor_checks`` families still load only when a report runs."""
    probe = (
        "import sys, kiro_crew.cli\n"
        "from kiro_crew import cli_doctor\n"
        "loaded = sorted(m for m in sys.modules if m.startswith('kiro_crew.cli_doctor_parts.'))\n"
        "families = sorted(m for m in sys.modules if m.startswith('kiro_crew.doctor_checks'))\n"
        "composed = all(getattr(cli_doctor, n).__globals__ is vars(cli_doctor)\n"
        "    for m in loaded for n, v in vars(sys.modules[m]).items()\n"
        "    if type(v).__name__ == 'function'\n"
        "    and v.__code__.co_filename == sys.modules[m].__file__)\n"
        "print(repr((loaded, families, composed)))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=source_corpus.repo_root(),
        capture_output=True,
        check=False,
        timeout=120,
        **UTF8_TEXT,
    )
    assert done.returncode == 0, done.stderr
    loaded, families, composed = ast.literal_eval(done.stdout.strip().splitlines()[-1])
    assert loaded == sorted(f"{PACKAGE}.{stem}" for stem in _OWNER_STEMS)
    assert families == []
    assert composed is True
