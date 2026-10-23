"""``push_guard.py`` as a facade over five owners whose functions run on its globals.

The composition: the facade rebinds its owners through a local twin of
_facade_compose.rebind; owners define only functions. ``push_guard.py`` loads
``push_guard_index.py``, ``push_guard_record.py``, ``push_guard_history.py``,
``push_guard_squash.py`` and ``push_guard_commit.py`` by path, without registering them,
and binds every function they define on itself as a copy whose globals are the facade's.
A name a test replaces on the facade therefore reaches the moved code exactly as it
reached the one-file script. Each section below pins one property that keeps true:

* the local ``rebind`` is the shared helper's twin, and the loader is pr_status.py's;
* an owner holds only functions, reads nothing at import time and imports only the
  standard library, plus facade names under ``TYPE_CHECKING``;
* every owner function bound on the facade runs on the facade's globals and module name,
  each fresh copy composes its own, and a replaced name reaches the moved code;
* every name the one-file script bound is still bound, each defined in one file, and a
  name collision refuses at import;
* a bare copy of the scripts runs under ``-I -S``, ``preflight.py``'s by-name import
  still resolves, and no owner leaves bytecode behind.
"""

from __future__ import annotations

import ast
import builtins
import collections
import functools
import importlib
import importlib.util
import io
import os
import re
import subprocess
import symtable
import sys
import tokenize
from pathlib import Path
from types import FunctionType, ModuleType

import pytest
import source_corpus
from push_guard_helpers import PUSH_GUARD, PUSH_GUARD_DIR, REPO_ROOT
from skill_script_helpers import load_skill_script

# One walk of the test tree, shared by this module's cases on one worker.
pytestmark = pytest.mark.xdist_group(name="tree_scan_test_push_guard_owners")

SCRIPT = Path(PUSH_GUARD)
OWNERS = (
    "push_guard_index",
    "push_guard_record",
    "push_guard_history",
    "push_guard_squash",
    "push_guard_commit",
)
OWNER_PATHS = {stem: PUSH_GUARD_DIR / f"{stem}.py" for stem in OWNERS}
SHARED_REBIND = REPO_ROOT / "src" / "kiro_crew" / "_facade_compose.py"
PR_STATUS = PUSH_GUARD_DIR / "pr_status.py"
#: The loader units push_guard.py carries as a byte-identical twin of pr_status.py's.
LOADER_TWIN = ("_NoBytecodeSourceLoader", "_load_sibling", "_load_owner")
#: A dotted load name OUTSIDE ``kiro_crew``: a ``kiro_crew.`` prefix would make a
#: coverage run start measuring these files from this module's partial use of them.
DOTTED = "s56_probe.push_guard"

#: Every module-level name ``push_guard.py`` defined as one file, before its split: each
#: ``def``, ``class`` and assigned name, imports excluded. A caller reads these off the
#: module, so each must still be there.
BASE_NAMES = (
    "ChildResult",
    "DEFAULT_MAX_AHEAD",
    "EXIT_CODES",
    "EXIT_ENV",
    "EXIT_FOREIGN_INDEX",
    "EXIT_REFUSED",
    "EXIT_SAFE",
    "EXIT_USAGE",
    "FETCH_STALL_S",
    "GIT_TIMEOUT_S",
    "HOOK_TIMEOUT_S",
    "NO_PROMPT_ENV",
    "REPLAY_HISTORY_WINDOW",
    "Refused",
    "UsageError",
    "_BUILT_DIR",
    "_BUILT_MAX_AGE_S",
    "_CMD_META",
    "_CONSUMED_HEADS",
    "_FETCH_ERROR_CLASSES",
    "_GIT_CMD",
    "_HOOK_RUN_GIT",
    "_INTERRUPTED",
    "_KILL_GRACE_S",
    "_LISTED_PATHS",
    "_MESSAGE_PREFIX",
    "_Parser",
    "_REFLOG_TIPS",
    "_RELOCATION_VARS",
    "_RESET_ENTRIES",
    "_SAFETY_ENV",
    "_STAGED_LIST_MAX_AGE_S",
    "_STAGED_LIST_PREFIX",
    "_STDERR_CAP",
    "_TODO_HEAD_CHARS",
    "_TOP_LITERAL",
    "_Terminated",
    "_UNRECORDED_LIST_PREFIX",
    "_ask",
    "_branch_ref",
    "_branch_tips",
    "_built",
    "_built_dir",
    "_carry_remedy",
    "_changed",
    "_check_index",
    "_check_index_for_commit",
    "_check_pre_squash",
    "_check_single_on_base",
    "_classify_fetch_error",
    "_cleanup_mode",
    "_commit_by_name",
    "_commit_info",
    "_common_dir",
    "_covered",
    "_diff_against",
    "_display",
    "_empty_tree",
    "_failed",
    "_fetch_base",
    "_follow_renames",
    "_git_argv",
    "_git_paths",
    "_git_version",
    "_head_ref",
    "_hook_message",
    "_hooks_runnable",
    "_ident",
    "_in_progress",
    "_indented",
    "_index_entries",
    "_interactive",
    "_is_sha",
    "_literal_relative",
    "_local_base",
    "_local_email",
    "_main",
    "_move_ref",
    "_named_paths",
    "_origin_head",
    "_outcome",
    "_outside",
    "_own_diff",
    "_parse_args",
    "_patch_ids",
    "_private_index",
    "_published",
    "_raise_terminated",
    "_read_message",
    "_refuse_unrecorded",
    "_remote_ref",
    "_report_staged",
    "_resolve_base",
    "_resquash_remedy",
    "_sequencer_op",
    "_sha",
    "_squash",
    "_squash_message_file",
    "_ssh_with_batch_mode",
    "_staged",
    "_stop",
    "_stripspace",
    "_tail",
    "_take_back",
    "_taskkill",
    "_top",
    "_top_relative",
    "_unlink",
    "_unmerged",
    "_unrecorded",
    "_wait",
    "_which_git",
    "_worktree_matches",
    "_write_git_file",
    "_write_list",
    "child_env",
    "err",
    "fetch_base_ref",
    "fetch_diagnostic",
    "install_termination_handlers",
    "main",
    "parse_origin_head",
    "record_built",
    "restore_handlers",
    "run",
    "run_child",
    "squash_message_path",
    "ssh_batch_env",
)


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    found = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name]
    assert len(found) == 1, f"{name}: {len(found)} top-level definitions"
    return found[0]


def _top_level_defs(path: Path) -> set[str]:
    return {
        n.name
        for n in _tree(path).body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }


def _facade_bindings() -> set[str]:
    """Every name the facade's own text binds at top level, imports excluded."""
    bound = set()
    for node in _tree(SCRIPT).body:
        if isinstance(node, ast.Assign):
            bound |= {t.id for t in node.targets if isinstance(t, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            bound.add(node.target.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
    return bound


@pytest.fixture(name="facade")
def facade_fixture() -> ModuleType:
    return load_skill_script("push_guard_owners_facade", SCRIPT)


# --------------------------------------------------------------------------
# Twins: the shared helper's rebind, and pr_status.py's loader
# --------------------------------------------------------------------------


def _strip_docstring(node: ast.FunctionDef) -> ast.FunctionDef:
    body = list(node.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]
    return ast.FunctionDef(
        name=node.name,
        args=node.args,
        body=body,
        decorator_list=node.decorator_list,
        returns=node.returns,
        type_comment=node.type_comment,
        **({"type_params": node.type_params} if hasattr(node, "type_params") else {}),
    )


#: The one statement the twin spells differently: this script still runs on
#: interpreters older than 3.12, whose functions have no ``__type_params__``.
_TYPE_PARAMS_SHARED = "new.__type_params__ = fn.__type_params__"
_TYPE_PARAMS_LOCAL = "new.__type_params__ = getattr(fn, '__type_params__', ())"


def rebind_differences(shared_source: str, local_source: str) -> list[tuple[str, str]]:
    """Each place the local ``rebind`` differs from the shared one, docstrings aside.

    A statement pair per differing body statement, plus one ``("<signature>", ...)``
    entry when the name, parameters, annotations or decorators differ.
    """
    shared = _strip_docstring(_function(ast.parse(shared_source), "rebind"))
    local = _strip_docstring(_function(ast.parse(local_source), "rebind"))
    found = [
        (ast.unparse(a), ast.unparse(b))
        for a, b in zip(shared.body, local.body)
        if ast.dump(a) != ast.dump(b)
    ]
    if len(shared.body) != len(local.body):
        found.append(("<body length>", f"{len(shared.body)} != {len(local.body)}"))
    shared.body, local.body = [], []
    if ast.dump(shared) != ast.dump(local):
        found.append(("<signature>", "name, parameters, annotations or decorators"))
    return found


def test_the_local_rebind_is_the_shared_helpers_twin():
    """The local ``rebind`` is ``kiro_crew._facade_compose.rebind``, docstring aside, but
    for one statement: it reads ``__type_params__`` with a default. Either side drifting
    in any other way fails here."""
    differences = rebind_differences(
        SHARED_REBIND.read_text(encoding="utf-8"), SCRIPT.read_text(encoding="utf-8")
    )
    assert differences == [(_TYPE_PARAMS_SHARED, _TYPE_PARAMS_LOCAL)], differences


def test_the_rebind_twin_check_sees_a_second_difference_and_a_signature_change():
    """Non-vacuity: one more changed statement, or a changed parameter, is told apart."""
    shared = SHARED_REBIND.read_text(encoding="utf-8")
    local = SCRIPT.read_text(encoding="utf-8")
    body_drift = local.replace("    new.__doc__ = fn.__doc__\n", "    new.__doc__ = None\n", 1)
    assert len(rebind_differences(shared, body_drift)) == 2
    signature_drift = local.replace(
        "def rebind(fn: FunctionType,", "def rebind(func: FunctionType,", 1
    )
    assert "<signature>" in {kind for kind, _ in rebind_differences(shared, signature_drift)}


def test_the_loader_is_a_byte_identical_twin_of_pr_statuss():
    """The script loads its owners the way pr_status.py loads its siblings, and two
    copies of one loader may only ever be the same text."""
    ours, theirs = SCRIPT.read_text(encoding="utf-8"), PR_STATUS.read_text(encoding="utf-8")
    our_nodes = {getattr(n, "name", ""): n for n in ast.parse(ours).body}
    their_nodes = {getattr(n, "name", ""): n for n in ast.parse(theirs).body}
    for name in LOADER_TWIN:
        assert ast.get_source_segment(ours, our_nodes[name]) == ast.get_source_segment(
            theirs, their_nodes[name]
        ), name


# --------------------------------------------------------------------------
# Owner shape: functions only, nothing read at import time
# --------------------------------------------------------------------------


def _is_type_checking_block(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.If)
        and isinstance(node.test, ast.Name)
        and node.test.id == "TYPE_CHECKING"
        and not node.orelse
        and all(isinstance(n, ast.ImportFrom) for n in node.body)
    )


def owner_shape_violations(source: str) -> list[str]:
    """What, in an owner's text, is not a docstring, an import or a def.

    The rule the rebind relies on: an owner's top level holds its docstring,
    ``from __future__ import annotations`` (required), standard-library imports, one
    ``if TYPE_CHECKING:`` block of imports and plain functions -- no module state, no
    class, and no default or decorator that reads a name when the owner's ``def`` runs.
    """
    stdlib = set(sys.stdlib_module_names) | {"__future__"}
    found = []
    body = ast.parse(source).body
    if not any(
        isinstance(n, ast.ImportFrom)
        and n.module == "__future__"
        and any(a.name == "annotations" for a in n.names)
        for n in body
    ):
        # The annotations name facade types under TYPE_CHECKING only; evaluated, they
        # would fail, and on 3.9 a PEP 604 annotation fails on its own.
        found.append("no `from __future__ import annotations`")
    for index, node in enumerate(body):
        if index == 0 and isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue
        if isinstance(node, ast.Import):
            bad = [a.name for a in node.names if a.name.split(".")[0] not in stdlib]
            found += [f"import {name}" for name in bad]
        elif isinstance(node, ast.ImportFrom):
            if node.level or (node.module or "").split(".")[0] not in stdlib:
                found.append(f"from {node.module} import")
        elif _is_type_checking_block(node):
            continue
        elif isinstance(node, ast.FunctionDef):
            evaluated = [*node.args.defaults, *filter(None, node.args.kw_defaults)]
            evaluated += node.decorator_list
            for sub in evaluated:
                names = {n.id for n in ast.walk(sub) if isinstance(n, ast.Name)}
                found += [
                    f"{node.name} reads {name} at import"
                    for name in sorted(names - set(dir(builtins)))
                ]
        else:
            found.append(f"{type(node).__name__} at line {node.lineno}")
    return found


@pytest.mark.parametrize("stem", OWNERS)
def test_an_owner_holds_only_functions_and_reads_nothing_at_import(stem):
    source = OWNER_PATHS[stem].read_text(encoding="utf-8")
    assert _top_level_defs(OWNER_PATHS[stem]), f"{stem}: defines nothing"
    assert not owner_shape_violations(source), owner_shape_violations(source)


def test_the_owner_shape_check_names_each_kind_of_violation():
    """Non-vacuity: state, a class, a non-standard import and an import-time read."""
    planted = (
        '"""doc"""\n'
        "from __future__ import annotations\n"
        "import os\n"
        "import kiro_crew\n"
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from push_guard import err\n"
        "STATE = 1\n"
        "class Box:\n"
        "    pass\n"
        "def f(x=DEFAULT):\n"
        "    return x\n"
        "def g(y=1):\n"
        "    return os.sep\n"
    )
    assert owner_shape_violations(planted) == [
        "import kiro_crew",
        "Assign at line 8",
        "ClassDef at line 9",
        "f reads DEFAULT at import",
    ]


def test_the_owner_shape_check_requires_the_annotations_future_import():
    """Without it an owner's annotations are evaluated when its defs run: they name
    facade types imported only under ``TYPE_CHECKING``, and 3.9 rejects ``X | None``."""
    planted = '"""doc"""\nimport os\n\n\ndef f(path: str | None) -> str:\n    return os.sep\n'
    assert owner_shape_violations(planted) == ["no `from __future__ import annotations`"]
    with_future = planted.replace('"""\n', '"""\nfrom __future__ import annotations\n', 1)
    assert owner_shape_violations(with_future) == []


# --------------------------------------------------------------------------
# Rebinding: owner functions run on the facade's globals
# --------------------------------------------------------------------------


def _owner_functions(owner: ModuleType) -> dict[str, FunctionType]:
    return {
        name: value
        for name, value in vars(owner).items()
        if isinstance(value, FunctionType) and value.__code__.co_filename == owner.__file__
    }


def test_every_owner_function_runs_on_the_facades_globals_and_module_name(facade):
    """Bound on the facade as a copy with the facade's globals and ``__module__``: the
    original's code, name, qualname and defaults, read against the facade's names."""
    assert len(facade._OWNERS) == len(OWNERS)
    seen = 0
    for owner in facade._OWNERS:
        for name, original in _owner_functions(owner).items():
            bound = vars(facade)[name]
            assert bound is not original, name
            assert bound.__globals__ is vars(facade), name
            assert bound.__module__ == facade.__name__, name
            assert bound.__code__ is original.__code__, name
            assert bound.__qualname__ == original.__qualname__, name
            assert bound.__defaults__ == original.__defaults__, name
            seen += 1
    assert seen == sum(len(_top_level_defs(path)) for path in OWNER_PATHS.values())


def test_each_fresh_copy_composes_its_own_owners():
    """Tests load a private copy per run; a copy's owners are its own, so a name one
    copy replaces is invisible to another."""
    first = load_skill_script("push_guard_owners_first", SCRIPT)
    second = load_skill_script("push_guard_owners_second", SCRIPT)
    assert first._check_index is not second._check_index
    assert first._check_index.__globals__ is vars(first)
    assert second._check_index.__globals__ is vars(second)
    assert not {id(o) for o in first._OWNERS} & {id(o) for o in second._OWNERS}


def test_a_replaced_runner_reaches_moved_code_through_the_facades_ask(facade):
    """Two layers: the moved ``_head_ref`` calls the facade's ``_ask``, which calls the
    replaced ``run``."""
    facade.run = lambda args, **_kw: (0, "refs/heads/feature", "")
    assert facade._head_ref() == "refs/heads/feature"
    facade.run = lambda args, **_kw: (1, "", "")
    assert facade._head_ref() == "HEAD"


def test_a_replaced_moved_function_reaches_its_moved_caller(facade):
    """The squash suite replaces ``_git_version``, and the moved ``_hooks_runnable``
    reads it through the facade's globals."""
    facade._git_version = lambda: (9, 0)
    assert facade._hooks_runnable() is True


def _function_globals(source: str) -> dict[str, set[str]]:
    """Per top-level function, the global names its body (nested code included) reads."""
    table = symtable.symtable(source, "<owner>", "exec")

    def collect(tab: symtable.SymbolTable, into: set[str]) -> None:
        for symbol in tab.get_symbols():
            if symbol.is_referenced() and symbol.is_global():
                into.add(symbol.get_name())
        for child in tab.get_children():
            collect(child, into)

    out = {}
    for child in table.get_children():
        if child.get_type() == "function":
            names: set[str] = set()
            collect(child, names)
            out[child.get_name()] = names
    return out


@pytest.mark.parametrize("stem", OWNERS)
def test_every_name_an_owner_function_reads_is_bound_on_the_facade(stem, facade):
    """A rebound function looks every global up on the facade, so a name only the owner
    imported would fail at call time; the facade must bind each one itself."""
    reads = _function_globals(OWNER_PATHS[stem].read_text(encoding="utf-8"))
    assert reads, f"{stem}: found no functions"
    missing = {
        name: sorted(n for n in names if not hasattr(facade, n) and not hasattr(builtins, n))
        for name, names in reads.items()
    }
    assert not {k: v for k, v in missing.items() if v}, missing


# --------------------------------------------------------------------------
# Patch reach: the corpus the suites replace on the facade
# --------------------------------------------------------------------------

_PATCH_CALLS = frozenset({"setattr", "delattr", "object"})
_IMPORTED_MODULE = re.compile(r"^[ \t]*(?:from[ \t]+(\w+)[ \t]+import|import[ \t]+(\w+))", re.M)


def patched_names_in(source: str) -> set[str]:
    """Names *source* replaces on a module held in a plain variable: ``setattr``,
    ``delattr``, ``patch.object``, ``setitem(vars(mod), ...)`` and ``mod.X = ...``; an
    ``overrides={...}`` mapping the in-process runner applies counts too."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Assign):
            found |= {
                t.attr
                for t in node.targets
                if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name)
            }
            continue
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg == "overrides" and isinstance(keyword.value, ast.Dict):
                found |= {
                    k.value
                    for k in keyword.value.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)
                }
        if len(node.args) < 2:
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


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def names_the_script(source: str, filename: str) -> bool:
    """Whether *source* spells the script's file as a string of its own: ``"push_guard.py"``
    or a path ending in it, as a loader writes it -- never a mention inside prose."""
    if filename not in source:
        return False
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, SyntaxError):
        return False
    for token in tokens:
        if token.type != tokenize.STRING:
            continue
        try:
            value = ast.literal_eval(token.string)
        except (ValueError, SyntaxError):
            continue
        if isinstance(value, str) and (value == filename or value.endswith("/" + filename)):
            return True
    return False


@functools.lru_cache(maxsize=1)
def _corpus() -> tuple[frozenset[Path], dict[str, frozenset[Path]]]:
    """The test files that load the script, and the facade names each replaces.

    A file is read when it spells the script's file, or imports a module that does: the
    push_guard suites reach it through ``push_guard_helpers.py``.
    """
    tree_root = source_corpus.repo_root() / "test"
    files = [p for p in source_corpus.repo_files_named(".py") if p.is_relative_to(tree_root)]
    naming = {path for path in files if names_the_script(_read(path), "push_guard.py")}
    stems = {path.stem for path in naming}
    scanned = set(naming)
    for path in files:
        if path not in naming:
            imported = {a or b for a, b in _IMPORTED_MODULE.findall(_read(path))}
            if imported & stems:
                scanned.add(path)
    facade = load_skill_script("push_guard_owners_corpus", SCRIPT)
    patches: dict[str, set[Path]] = collections.defaultdict(set)
    for path in scanned:
        for name in patched_names_in(_read(path)):
            if hasattr(facade, name):
                patches[name].add(path)
    return frozenset(scanned), {name: frozenset(paths) for name, paths in patches.items()}


def test_the_corpus_scan_finds_the_seams_the_suite_is_known_to_patch():
    """Non-vacuity: the suites replace the runner, the fake-git hook, the bounds, the
    Windows taskkill lookup and, through the in-process runner's overrides, a moved
    function."""
    this_file = Path(__file__).resolve()
    _scanned, patches = _corpus()
    for name in ("run", "_GIT_CMD", "GIT_TIMEOUT_S", "FETCH_STALL_S", "_taskkill", "_git_version"):
        assert patches.get(name, frozenset()) - {this_file}, name


def test_the_corpus_reaches_suites_that_load_the_script_only_through_a_helper():
    """Some push_guard suites spell no path of the script at all and load it through
    ``push_guard_helpers.py``, so the scan has to follow the import to read them."""
    scanned, _patches = _corpus()
    helper_only = {p.name for p in scanned if not names_the_script(_read(p), "push_guard.py")}
    assert {"test_push_guard_diagnostics.py", "test_push_guard_squash.py"} <= helper_only, sorted(
        helper_only
    )


def test_every_reader_of_a_replaced_name_resolves_it_on_the_facade(facade):
    """Patch reach, from the corpus: for each name the suites replace on the facade, every
    function that reads it as a global -- in the facade or in an owner -- runs on the
    facade's globals, so the replacement reaches it. Moved readers of the runner, the
    interactive child and the git version check are among them."""
    _scanned, patches = _corpus()
    readers: dict[str, list[tuple[str, str]]] = collections.defaultdict(list)
    for path in (SCRIPT, *OWNER_PATHS.values()):
        for function, names in _function_globals(path.read_text(encoding="utf-8")).items():
            for name in names & set(patches):
                readers[name].append((path.name, function))
    read_by_owners = {
        name for name, found in readers.items() if any(p != SCRIPT.name for p, _f in found)
    }
    assert {"run", "_interactive", "_git_version"} <= read_by_owners, sorted(read_by_owners)
    for name, found in sorted(readers.items()):
        for path_name, function in found:
            assert vars(facade)[function].__globals__ is vars(facade), (name, path_name, function)


def test_leaf_test_scope_derives_this_file_as_a_corpus_gate(monkeypatch):
    """It reads the whole test tree, so a leaf-only diff must still run it."""
    root = source_corpus.repo_root()
    this_file = (root / "test" / Path(__file__).name).relative_to(root).as_posix()
    monkeypatch.syspath_prepend(str(root / "scripts"))
    leaf_test_scope = importlib.import_module("leaf_test_scope")
    assert this_file in leaf_test_scope.corpus_gates(root)


# --------------------------------------------------------------------------
# Surface: the one file's names, each defined once; a collision refuses
# --------------------------------------------------------------------------


def test_every_name_the_one_file_script_bound_is_still_bound_on_the_facade(facade):
    missing = [name for name in BASE_NAMES if not hasattr(facade, name)]
    assert not missing, missing


def test_every_one_file_name_is_defined_in_exactly_one_file():
    """Each name the one file defined is defined once, in the facade or in one owner;
    the facade binds owner functions at run time only. Names added later are free to
    land in any file."""
    defined = collections.Counter(
        [*_facade_bindings(), *(n for path in OWNER_PATHS.values() for n in _top_level_defs(path))]
    )
    wrong = {name: defined[name] for name in BASE_NAMES if defined[name] != 1}
    assert not wrong, wrong


def test_the_type_checking_imports_name_what_each_module_defines():
    """The imports the type checker reads stay true: the facade's name owner functions
    by their owner, and an owner's name the facade's own definitions or another owner's."""
    facade_names = _facade_bindings()
    for path in (SCRIPT, *OWNER_PATHS.values()):
        for node in _tree(path).body:
            if not _is_type_checking_block(node):
                continue
            for imp in node.body:
                assert isinstance(imp, ast.ImportFrom)
                if imp.module == "push_guard":
                    defines = facade_names
                else:
                    defines = _top_level_defs(PUSH_GUARD_DIR / f"{imp.module}.py")
                stray = [a.name for a in imp.names if a.name not in defines]
                assert not stray, (path.name, imp.module, stray)


def _copy_scripts(tmp_path: Path) -> Path:
    """Every script of the skill, the way the installed skill holds them."""
    scripts = tmp_path / "skills" / "kirocrew-prepare-pr" / "scripts"
    scripts.mkdir(parents=True)
    for path in PUSH_GUARD_DIR.glob("*.py"):
        (scripts / path.name).write_bytes(path.read_bytes())
    return scripts


def test_an_owner_name_the_facade_already_binds_refuses_at_import(tmp_path):
    """Binding over a facade name would silently change what every caller runs."""
    scripts = _copy_scripts(tmp_path)
    owner = scripts / "push_guard_history.py"
    owner.write_text(
        owner.read_text(encoding="utf-8") + "\n\ndef run(args):\n    return 0, '', ''\n",
        encoding="utf-8",
    )
    with pytest.raises(ImportError, match="push_guard_history.py defines run"):
        load_skill_script("push_guard_collision", scripts / "push_guard.py")


def test_only_functions_compiled_from_an_owner_are_bound(tmp_path):
    """Ruling condition 2: an owner's namespace also holds what it imports, and a
    function imported from the standard library is not the owner's to bind. Without the
    ``co_filename`` test it would land on the facade."""
    scripts = _copy_scripts(tmp_path)
    owner = scripts / "push_guard_history.py"
    source = owner.read_text(encoding="utf-8")
    anchor = "from typing import TYPE_CHECKING\n"
    assert source.count(anchor) == 1
    owner.write_text(
        source.replace(anchor, anchor + "from os.path import join\n"), encoding="utf-8"
    )
    facade = load_skill_script("push_guard_imported_function", scripts / "push_guard.py")
    history = next(o for o in facade._OWNERS if o.__name__.endswith("push_guard_history"))
    assert isinstance(vars(history)["join"], FunctionType)
    assert "join" not in vars(facade)
    assert facade._check_pre_squash.__globals__ is vars(facade)


def test_two_owners_defining_one_name_refuse_at_import(tmp_path):
    scripts = _copy_scripts(tmp_path)
    owner = scripts / "push_guard_commit.py"
    owner.write_text(
        owner.read_text(encoding="utf-8") + "\n\ndef _check_index(git_dir):\n    return 0\n",
        encoding="utf-8",
    )
    with pytest.raises(ImportError, match="push_guard_commit.py defines _check_index"):
        load_skill_script("push_guard_collision_owners", scripts / "push_guard.py")


def test_the_owners_are_bound_at_the_foot_before_any_caller():
    """The binding is the last statement before the ``__main__`` guard, so every name is
    bound before ``main`` or an importer can call one."""
    body = _tree(SCRIPT).body
    guard, binding = body[-1], body[-2]
    assert isinstance(guard, ast.If) and "__main__" in ast.unparse(guard.test)
    assert isinstance(binding, ast.Assign) and ast.unparse(binding.targets[0]) == "_OWNERS"
    assert ast.unparse(binding.value).startswith("_bind_owners(")


# --------------------------------------------------------------------------
# Loading and the script run
# --------------------------------------------------------------------------


def test_a_dotted_load_names_the_owners_under_the_same_package_and_registers_nothing():
    facade = load_skill_script(DOTTED, SCRIPT)
    package = DOTTED.rpartition(".")[0]
    assert [o.__name__ for o in facade._OWNERS] == [f"{package}.{stem}" for stem in OWNERS]
    assert [Path(o.__file__) for o in facade._OWNERS] == list(OWNER_PATHS.values())
    assert not {o.__name__ for o in facade._OWNERS} & set(sys.modules)
    assert not set(OWNERS) & set(sys.modules)


def test_loading_the_owners_writes_no_bytecode(tmp_path, monkeypatch):
    """The facade goes through the ORDINARY loader with writing on, so only the
    facade's own loader decides whether an owner leaves bytecode behind."""
    scripts = _copy_scripts(tmp_path)
    monkeypatch.setattr(sys, "dont_write_bytecode", False)
    monkeypatch.setattr(sys, "pycache_prefix", None)
    spec = importlib.util.spec_from_file_location("push_guard_copy", scripts / "push_guard.py")
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(importlib.util.module_from_spec(spec))
    cached = sorted(p.name for p in (scripts / "__pycache__").glob("*"))
    assert [name for name in cached if name.startswith("push_guard.")], cached
    assert not [name for name in cached if name.startswith(OWNERS)], cached


def _bare_env(tmp_path: Path) -> dict[str, str]:
    empty = tmp_path / "empty-path"
    empty.mkdir(exist_ok=True)
    env = {"PATH": str(empty)}
    if "SYSTEMROOT" in os.environ:
        env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    return env


def test_a_bare_copy_runs_as_a_script_without_the_package_and_writes_no_bytecode(tmp_path):
    """The installed shape, run by an interpreter with no site directory and no
    environment-driven import path: ``-I`` leaves the script's own directory off
    ``sys.path``, so the owners are reachable only through the facade's by-path load."""
    scripts = _copy_scripts(tmp_path)
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(scripts / "push_guard.py"), "--help"],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        env=_bare_env(tmp_path),
        cwd=str(tmp_path),
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("usage: push_guard.py"), result.stdout
    assert not list(scripts.rglob("__pycache__")), sorted(scripts.rglob("*"))


def test_preflights_import_by_name_still_resolves_every_name_it_takes(tmp_path):
    """preflight.py imports the facade by NAME from its own directory and takes twelve
    names, two of them private; with git off the path it gets as far as its own check."""
    scripts = _copy_scripts(tmp_path)
    env = _bare_env(tmp_path)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [sys.executable, "-S", str(scripts / "preflight.py")],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        cwd=str(tmp_path),
        timeout=60,
        check=False,
    )
    assert "Traceback" not in result.stderr, result.stderr
    assert result.returncode == 2, result.stdout + result.stderr
