"""The fleet probe as a facade over two owner modules: its surface, its seams, its run.

``fleet_probe.py`` loads ``fleet_probe_sessions.py`` and ``fleet_probe_processes.py`` by
path and re-exports every name they define. The arrangement has to keep four things
true, and each section below pins one of them:

* every name a test replaces on the facade is defined there and read by no owner, so
  the replacement reaches every reader;
* every name the probe bound as a single file is still bound on the facade, and a
  re-export is the owner's own object -- the one module-level duplicate allowed;
* an owner imports only the standard library, is named under the facade's own
  package, and is never registered in ``sys.modules``;
* a bare copy of the three files runs as a script under ``-I -S`` and writes no
  bytecode.
"""

from __future__ import annotations

import ast
import collections
import functools
import importlib
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
import source_corpus
from pipeline_conductor_fleet_probe_helpers import LOAD_NAME, SCRIPT
from skill_script_helpers import load_skill_script

# One walk of the test tree, shared by this module's cases on one worker.
pytestmark = pytest.mark.xdist_group(name="tree_scan_test_fleet_probe_owners")

SCRIPTS = SCRIPT.parent
#: Owner stem -> the facade attribute its module is bound to.
OWNERS = {"fleet_probe_sessions": "_sessions", "fleet_probe_processes": "_processes"}
OWNER_PATHS = {stem: SCRIPTS / f"{stem}.py" for stem in OWNERS}
LOAD_PACKAGE = LOAD_NAME.rpartition(".")[0]

#: Every module-level name ``fleet_probe.py`` defined as one file, before its split:
#: each ``def`` and each assigned name, imports excluded. A caller of the probe reads
#: these off the module, so each must still be there.
BASE_NAMES = (
    "ARGV_RUNNER_RULE_LABEL",
    "DEFAULT_BANNED_RES",
    "DEFAULT_ERR_RES",
    "DEFAULT_INIT_TIMEOUT_RES",
    "DEFAULT_WATCHDOG_RES",
    "HEARTBEAT_TAGS",
    "IDLE_TAG",
    "NOPROGRESS_TAG",
    "PROTO",
    "PROTO_TAGS",
    "SHELL_PROGRAMS",
    "STICKY_TAGS",
    "TERMINAL_TAG",
    "TERMINAL_TAGS",
    "TOOL_ROLES",
    "_ADDOPTS_KEY",
    "_ALIAS_PROGRAM_LABELS",
    "_ALIAS_RUNNER_BASE_RE",
    "_ARGV_ONLY_RUNNER_BASES",
    "_BUDGETED_COUNT_WORDS",
    "_BUILTIN_SHAPES",
    "_CAP_FLAGS",
    "_CAP_SCAN",
    "_COMMAND_SEPARATORS",
    "_END_OF_OPTIONS",
    "_ENV_ASSIGNMENT_RE",
    "_EXPIRING_TAGS",
    "_FIRING",
    "_KEY_RE",
    "_LAUNCHER_BASES",
    "_LEADING_DECOR",
    "_MAX_CMD_TOKENS",
    "_MAX_CMD_TOKEN_CHARS",
    "_OVERRIDE_INI_FLAGS",
    "_OWN_ROW_NEEDLES",
    "_PAYLOAD_TAGS",
    "_PROCFS_DELETED_MARKER",
    "_PYTHON_BASE_RE",
    "_PYTHON_FLAG_LETTERS",
    "_PYTHON_LONG_VALUE_OPTS",
    "_PYTHON_VALUE_LETTERS",
    "_RUNNER_BASES",
    "_SAFE_LONG_FLAG_NAMES",
    "_SAFE_SHORT_FLAG_RE",
    "_SHELL_OPTS_WITH_OPERAND",
    "_SHELL_TOKEN_BOUNDARY",
    "_TRANSPARENT_LAUNCHER_BASES",
    "_TRUSTED_PROGRAM_DIRS",
    "_UNBUDGETED_COUNT",
    "_VALUE_TAKING_OPTS",
    "_VITEST_BANNED_RE",
    "_addopts_override",
    "_alias_program_label",
    "_argv_is_uncapped_argv_only_runner",
    "_argv_only_runner_indices",
    "_argv_worker_pool_is_budgeted",
    "_atomic_write",
    "_basename",
    "_classify",
    "_config_error",
    "_count_is_budgeted",
    "_count_own_rows",
    "_digest",
    "_handled_of",
    "_host_lines",
    "_invokes_bare_vitest_run",
    "_is_installed_entry_point",
    "_is_launcher_own_operand",
    "_is_runner_base",
    "_is_shell_command_wrapper",
    "_kernel_program_confirms",
    "_kernel_program_path",
    "_load_state",
    "_norm_path",
    "_owner_class",
    "_proc_age_secs",
    "_proc_starttime_ticks",
    "_program_class",
    "_program_path",
    "_proto_tag",
    "_python_execution_target",
    "_read_flag",
    "_recorded_proto",
    "_redacted_command",
    "_run_scope",
    "_runner_token_index",
    "_sessions_dir",
    "_stalled_since_disposition",
    "_stands_in_program_position",
    "_sticky_pending",
    "_suppressed",
    "_tail_entries",
    "_tail_matches",
    "_text_of",
    "_token_base",
    "_transcript_path",
    "_trusted_program_base",
    "_under",
    "_venv_root",
    "data_home",
    "main",
    "mark_handled",
    "run_probe",
)


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _module_level_bindings(tree: ast.Module) -> list[tuple[str, ast.stmt]]:
    """Every name a module binds at top level, the way the probe's own duplicate guard
    counts them: a ``def`` or ``class``, and each plain name an assignment targets."""
    bound: list[tuple[str, ast.stmt]] = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            bound += [(t.id, node) for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            bound.append((node.target.id, node))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.append((node.name, node))
    return bound


def _reexport_source(node: ast.stmt) -> str | None:
    """The owner alias a ``NAME = _owner.NAME`` binding reads from, or None."""
    if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
        return None
    target, value = node.targets[0], node.value
    if (
        isinstance(target, ast.Name)
        and isinstance(value, ast.Attribute)
        and isinstance(value.value, ast.Name)
        and value.attr == target.id
    ):
        return value.value.id
    return None


def _owner_defined(stem: str) -> set[str]:
    return {name for name, _node in _module_level_bindings(_tree(OWNER_PATHS[stem]))}


def _facade_defined() -> set[str]:
    """Names the facade defines itself: every binding that is not a re-export."""
    return {
        name
        for name, node in _module_level_bindings(_tree(SCRIPT))
        if _reexport_source(node) is None
    }


@pytest.fixture(name="facade")
def facade_fixture() -> ModuleType:
    return load_skill_script(LOAD_NAME, SCRIPT)


# --------------------------------------------------------------------------
# Patch reach: a replaced name is the facade's own and read by no owner
# --------------------------------------------------------------------------

_PATCH_CALLS = frozenset({"setattr", "delattr", "object"})
_IMPORTED_MODULE = re.compile(r"^[ \t]*(?:from[ \t]+(\w+)[ \t]+import|import[ \t]+(\w+))", re.M)


def patched_names_in(source: str) -> set[str]:
    """Names *source* replaces on a module held in a plain variable.

    ``monkeypatch.setattr(mod, "X", ...)``, ``monkeypatch.delattr(mod, "X")``,
    ``mock.patch.object(mod, "X", ...)``, ``monkeypatch.setitem(vars(mod), "X", ...)``
    and a plain ``mod.X = ...``. A first argument that is itself an attribute
    (``mod.os``) replaces something on ANOTHER module, which every reader sees, so it
    is not a facade seam and is left out.
    """
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Assign):
            found |= {
                target.attr
                for target in node.targets
                if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
            }
            continue
        if not isinstance(node, ast.Call) or len(node.args) < 2:
            continue
        target = node.args[0]
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "setitem"
            and isinstance(target, ast.Call)
            and getattr(target.func, "id", None) == "vars"
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            found.add(node.args[1].value)
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        target, attr = node.args[0], node.args[1]
        if (
            name in _PATCH_CALLS
            and isinstance(target, ast.Name)
            and isinstance(attr, ast.Constant)
            and isinstance(attr.value, str)
        ):
            found.add(attr.value)
    return found


def _test_tree_files() -> list[Path]:
    tree_root = source_corpus.repo_root() / "test"
    return [
        path for path in source_corpus.repo_files_named(".py") if path.is_relative_to(tree_root)
    ]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


@functools.lru_cache(maxsize=1)
def _corpus() -> tuple[frozenset[Path], dict[str, frozenset[Path]]]:
    """The test files that load the probe, and what each replaces on it.

    Read from the tests themselves, so a case added anywhere in the tree is covered
    without being registered here. A file is read when it names the probe, or imports
    a module that does: the conductor-agent suites reach the probe only through the
    harness in ``pipeline_conductor_agent_helpers.py``. A replaced attribute counts
    when the loaded facade has an attribute of that name -- a module-valued one
    included, since the owners import their own -- and the same spelling on an
    unrelated module is the safe over-inclusion, never a miss.
    """
    files = _test_tree_files()
    naming = {path for path in files if "fleet_probe" in _read(path)}
    stems = {path.stem for path in naming}
    scanned = set(naming)
    for path in files:
        if path not in naming:
            imported = {a or b for a, b in _IMPORTED_MODULE.findall(_read(path))}
            if imported & stems:
                scanned.add(path)
    probe = load_skill_script(LOAD_NAME, SCRIPT)
    patches: dict[str, set[Path]] = collections.defaultdict(set)
    for path in scanned:
        for name in patched_names_in(_read(path)):
            if hasattr(probe, name):
                patches[name].add(path)
    return frozenset(scanned), {name: frozenset(paths) for name, paths in patches.items()}


def _reexported() -> set[str]:
    """Names the facade binds as ``NAME = _owner.NAME``."""
    return {
        name
        for name, node in _module_level_bindings(_tree(SCRIPT))
        if _reexport_source(node) is not None
    }


def test_the_corpus_scan_finds_the_seams_the_suite_is_known_to_patch():
    """Non-vacuity: the derivation sees, in the suite itself, the two replacements it
    makes today -- ``_proc_starttime_ticks`` to splice a recycled pid between two
    reads, and ``_SAFE_LONG_FLAG_NAMES`` to plant an over-long vocabulary entry. This
    file's own replacements of the same names do not count toward it."""
    this_file = Path(__file__).resolve()
    _scanned, patches = _corpus()
    for name in ("_proc_starttime_ticks", "_SAFE_LONG_FLAG_NAMES"):
        assert patches.get(name, frozenset()) - {this_file}, name


def test_the_corpus_reaches_suites_that_load_the_probe_only_through_a_helper():
    """A suite whose own text never names the probe still loads it, through the
    conductor-agent harness, so the scan has to follow the import to read it."""
    scanned, _patches = _corpus()
    assert [path for path in scanned if "fleet_probe" not in _read(path)], sorted(scanned)


def test_the_patch_reader_recognises_each_spelling_and_skips_another_modules_attribute():
    planted = (
        'monkeypatch.setattr(mod, "_a", 1)\n'
        "monkeypatch.setattr(\n    mod,\n    '_b',\n    2,\n)\n"
        'monkeypatch.delattr(probe, "_c")\n'
        'mock.patch.object(probe, "_d", 3)\n'
        'monkeypatch.setattr(mod.os, "readlink", 4)\n'
        'monkeypatch.setattr("pkg.mod._e", 5)\n'
        'monkeypatch.setitem(vars(mod), "_f", 6)\n'
        "mod._g = 7\n"
        "mod.os.environ = {}\n"
    )
    assert patched_names_in(planted) == {"_a", "_b", "_c", "_d", "_f", "_g"}


def test_no_patched_name_is_a_re_export_or_read_by_an_owner():
    """A replacement rebinds the facade's own global, and only code reading THAT global
    sees it. A re-exported name leaves the owner's original in force for every owner
    caller, and an owner that reads the name -- its own import of a module included --
    never sees the replacement, so both are refused."""
    patched = set(_corpus()[1])
    reexported = sorted(patched & _reexported())
    assert not reexported, f"replaced on the facade but defined in an owner: {reexported}"
    for stem, path in OWNER_PATHS.items():
        read = {node.id for node in ast.walk(_tree(path)) if isinstance(node, ast.Name)}
        assert not patched & read, f"{stem} reads a replaced name: {sorted(patched & read)}"


def test_leaf_test_scope_derives_this_file_as_a_corpus_gate(monkeypatch):
    """It reads the whole test tree, so a leaf-only diff must still run it."""
    root = source_corpus.repo_root()
    this_file = (root / "test" / Path(__file__).name).relative_to(root).as_posix()
    monkeypatch.syspath_prepend(str(root / "scripts"))
    leaf_test_scope = importlib.import_module("leaf_test_scope")
    assert this_file in leaf_test_scope.corpus_gates(root)


def test_a_replaced_incarnation_reader_reaches_the_age(facade, monkeypatch, tmp_path):
    """The behaviour the placement buys: replacing ``_proc_starttime_ticks`` on the
    facade changes what the facade's own ``_proc_age_secs`` computes. With the real
    reader there is no ``stat`` file here and the age is unknown."""
    (tmp_path / "uptime").write_text("1000.0 0.0\n", encoding="ascii")
    monkeypatch.setattr(os, "sysconf", lambda _name: 100, raising=False)
    assert facade._proc_age_secs(tmp_path, "7", 100) is None
    monkeypatch.setattr(facade, "_proc_starttime_ticks", lambda *_args: 100)
    assert facade._proc_age_secs(tmp_path, "7", 100) == 999


def test_a_replaced_flag_vocabulary_reaches_the_redactor(facade, monkeypatch):
    """Replacing ``_SAFE_LONG_FLAG_NAMES`` on the facade changes what the facade's own
    ``_redacted_command`` prints: an unknown long flag is withheld until the
    vocabulary names it."""
    cmd = "pytest --zzz"
    assert facade._redacted_command(cmd, (0, len(cmd))) == "pytest,+1"
    monkeypatch.setattr(
        facade, "_SAFE_LONG_FLAG_NAMES", frozenset(facade._SAFE_LONG_FLAG_NAMES | {"--zzz"})
    )
    assert facade._redacted_command(cmd, (0, len(cmd))) == "pytest,--zzz"


# --------------------------------------------------------------------------
# Surface: the single file's names, and re-exports that are the owner's objects
# --------------------------------------------------------------------------


def test_every_name_the_single_file_bound_is_still_bound_on_the_facade(facade):
    missing = [name for name in BASE_NAMES if not hasattr(facade, name)]
    assert not missing, missing


def test_every_single_file_name_is_defined_in_exactly_one_of_the_three_files():
    """Nothing was dropped in the move and nothing was copied: each name the single
    file defined is now defined once, in the facade or in one owner. Names added
    later are free to land in any of the three."""
    defined = collections.Counter(
        [*_facade_defined(), *(name for stem in OWNERS for name in _owner_defined(stem))]
    )
    wrong = {name: defined[name] for name in BASE_NAMES if defined[name] != 1}
    assert not wrong, wrong


def test_every_moved_name_is_re_exported_as_the_owners_own_object(facade):
    """A caller reads a moved name off the facade, so the facade's binding must be the
    owner's object itself, not a copy that the owner's own callers would not see."""
    for stem, alias in OWNERS.items():
        owner = getattr(facade, alias)
        for name in sorted(_owner_defined(stem) & set(BASE_NAMES)):
            assert vars(facade)[name] is vars(owner)[name], (stem, name)


def test_no_owner_defines_a_module_level_name_twice():
    """The single file's own guard, applied to each owner: a second binding leaves the
    first one dead, docstring and all, while every check reading the import still
    passes because only the survivor is visible."""
    for stem, path in OWNER_PATHS.items():
        counts = collections.Counter(name for name, _node in _module_level_bindings(_tree(path)))
        assert counts, f"{stem}: parsed no definitions, so this guard proves nothing"
        assert not {n: c for n, c in counts.items() if c > 1}, stem


def test_the_only_name_bound_in_two_files_is_a_re_export_of_the_owners_definition():
    """Across the three files a name may be bound twice in exactly one way: the owner
    defines it and the facade binds ``NAME = <that owner's alias>.NAME``."""
    by_alias = {alias: stem for stem, alias in OWNERS.items()}
    owners_of: dict[str, list[str]] = collections.defaultdict(list)
    for stem in OWNERS:
        for name in _owner_defined(stem):
            owners_of[name].append(stem)
    assert not {n: s for n, s in owners_of.items() if len(s) > 1}, "defined in both owners"
    facade_bindings = _module_level_bindings(_tree(SCRIPT))
    seen = collections.Counter(name for name, _node in facade_bindings)
    assert not {n: c for n, c in seen.items() if c > 1}, "bound twice in the facade"
    for name, node in facade_bindings:
        alias = _reexport_source(node)
        if name in owners_of:
            assert alias is not None and by_alias.get(alias) == owners_of[name][0], name
        else:
            assert alias is None, f"{name} re-exports a name no owner defines"


# --------------------------------------------------------------------------
# Loading: standard library only, named after the facade, never registered
# --------------------------------------------------------------------------


@pytest.mark.parametrize("stem", sorted(OWNERS))
def test_an_owner_imports_only_the_standard_library(stem):
    """The probe is run by a bare interpreter, so an owner can lean on nothing the
    package or a sibling script provides -- and importing a sibling would bypass the
    by-path load the facade does for it."""
    imported = set()
    for node in ast.walk(_tree(OWNER_PATHS[stem])):
        if isinstance(node, ast.ImportFrom):
            assert node.level == 0, f"{stem}: relative import"
            imported.add((node.module or "").split(".")[0])
        elif isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
    assert imported, f"{stem}: found no imports, so this guard proves nothing"
    assert imported <= set(sys.stdlib_module_names) | {"__future__"}, sorted(imported)


def test_a_dotted_load_names_the_owners_under_the_same_package_and_registers_nothing():
    """The name is what a coverage run attributes executed lines by, so an owner loaded
    under a bare stem by the dotted harness would drop out of the measurement."""
    facade = load_skill_script(LOAD_NAME, SCRIPT)
    for stem, alias in OWNERS.items():
        owner = getattr(facade, alias)
        assert owner.__name__ == f"{LOAD_PACKAGE}.{stem}"
        assert owner.__file__ == str(OWNER_PATHS[stem])
        assert owner.__name__ not in sys.modules and stem not in sys.modules


def _copy_scripts(tmp_path: Path) -> Path:
    scripts = tmp_path / "skills" / "pipeline-conductor" / "scripts"
    scripts.mkdir(parents=True)
    for path in (SCRIPT, *OWNER_PATHS.values()):
        (scripts / path.name).write_bytes(path.read_bytes())
    return scripts


def _load_copy(scripts: Path) -> ModuleType:
    """Import the copied facade with the ORDINARY loader, so only the facade's own
    loader decides whether an owner leaves bytecode behind."""
    spec = importlib.util.spec_from_file_location("fleet_probe_copy", scripts / "fleet_probe.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_bare_load_names_the_owners_by_their_bare_stem(tmp_path):
    """Loaded from a COPY: coverage decides once per file whether to measure it, so a
    bare-name load of the real files could leave them unmeasured for this worker."""
    facade = load_skill_script("fleet_probe", _copy_scripts(tmp_path) / "fleet_probe.py")
    assert {getattr(facade, alias).__name__ for alias in OWNERS.values()} == set(OWNERS)
    assert not set(OWNERS) & set(sys.modules)


def test_loading_the_owners_writes_no_bytecode_and_restores_the_flag(tmp_path, monkeypatch):
    scripts = _copy_scripts(tmp_path)
    monkeypatch.setattr(sys, "dont_write_bytecode", False)
    monkeypatch.setattr(sys, "pycache_prefix", None)
    _load_copy(scripts)
    assert sys.dont_write_bytecode is False
    cached = sorted(p.name for p in (scripts / "__pycache__").glob("*"))
    # The control: the facade went through the ordinary loader with writing on, and
    # was cached -- so the owners' absence below is the facade's loader at work.
    assert [name for name in cached if name.startswith("fleet_probe.")], cached
    assert not [name for name in cached if name.startswith(tuple(OWNERS))], cached


def test_an_owner_that_raises_on_load_still_restores_the_flag(tmp_path, monkeypatch):
    scripts = _copy_scripts(tmp_path)
    (scripts / "fleet_probe_sessions.py").write_text(
        'raise RuntimeError("owner failed to load")\n', encoding="utf-8"
    )
    monkeypatch.setattr(sys, "dont_write_bytecode", False)
    with pytest.raises(RuntimeError, match="owner failed to load"):
        _load_copy(scripts)
    assert sys.dont_write_bytecode is False


# --------------------------------------------------------------------------
# Script run: a bare copy of the three files, as the synced skill runs
# --------------------------------------------------------------------------


def test_a_bare_copy_runs_as_a_script_without_the_package_and_writes_no_bytecode(tmp_path):
    """The installed shape: the skill directory synced out of the package, run by an
    interpreter with no site directory and no environment-driven import path. ``-I``
    also leaves the script's own directory off ``sys.path``, so the owners are
    reachable only through the facade's by-path load.

    One pid running an unbudgeted pytest exercises the process owner, and one
    heartbeat session the session owner; both print ASCII, so the child's console
    encoding cannot decide the outcome.
    """
    scripts = _copy_scripts(tmp_path)
    crew = tmp_path / "crew"
    (crew / "sessions").mkdir(parents=True)
    (crew / "sessions" / "worker.jsonl").write_text(
        json.dumps({"role": "assistant", "content": "WORKING: on it"}) + "\n", encoding="utf-8"
    )
    proc = tmp_path / "proc"
    (proc / "4242").mkdir(parents=True)
    (proc / "4242" / "cmdline").write_bytes(b"pytest\0-n4\0")
    config = tmp_path / "probe-config.json"
    config.write_text(json.dumps({"sessions": ["worker"]}), encoding="utf-8")
    env = {
        "PATH": os.environ.get("PATH", ""),
        "KIROCREW_HOME": str(crew),
        "KIROCREW_PROBE_PROC_ROOT": str(proc),
    }
    if "SYSTEMROOT" in os.environ:
        env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(scripts / "fleet_probe.py"), "--config", str(config)],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert any(line.startswith("BANNED pid=4242 ") for line in lines), lines
    assert lines[-1].startswith("OK 1 watched, 0 fired"), lines
    assert not list(scripts.rglob("__pycache__")), sorted(scripts.rglob("*"))
