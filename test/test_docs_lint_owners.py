"""``scripts/docs_lint.py`` stays one gate while its fact checks and self-test live beside it.

CI runs ``python3 scripts/docs_lint.py --test`` and then the lint, both as a file, and
``test_docs_lint_fact_checks.py`` loads the script by path as ``docs_lint``. The fact
checks live in ``scripts/docs_lint_fact_checks.py`` and the planted-defect self-test in
``scripts/docs_lint_selftest.py``. These tests pin what keeps that split invisible to
both callers:

- every name the script bound before the split is still bound on it, and every name an
  owner defines is the owner's own object there;
- an owner imports only the standard library and the script's names that exist before
  the script imports that owner, and never the other owner;
- the script aliases itself in ``sys.modules`` and APPENDS its own directory to
  ``sys.path`` before those imports, so it runs as a file from a copy of its three files
  alone, with or without its directory first on the path;
- each load of the script gets its own owners, so two test modules that each load it by
  path in one process both see owners that read their own copy's names, and a load
  leaves ``sys.modules["docs_lint"]`` as it found it (an earlier copy, or nothing);
- run as ``__main__`` (``runpy``), the owners hold the running module's objects, and each
  owner also imports cold, alone or before the script;
- a name a test patches on the script is never held by an owner, so the patch reaches the
  moved code. No test patches the script today. The guarded names are read from the
  tests' own patch calls rather than kept here, and planted cases prove that both the scan
  and the guard fire.
"""

from __future__ import annotations

import ast
import functools
import importlib
import importlib.util
import json
import os
import shutil
import string
import subprocess
import sys
from pathlib import Path

import pytest

from kiro_crew.subprocess_utf8 import UTF8_TEXT

# The patch-reach scan reads every test module once; one group per file keeps that read
# on one worker.
pytestmark = pytest.mark.xdist_group(name="tree_scan_test_docs_lint_owners")

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = _REPO_ROOT / "scripts"
_SCRIPT_PATH = _SCRIPTS / "docs_lint.py"

FACADE = "docs_lint"
#: The owner modules beside the script, in the order the script imports them.
OWNERS = ("docs_lint_fact_checks", "docs_lint_selftest")

#: Every top-level name ``docs_lint.py`` defined before the split, frozen. The modules it
#: imported from the standard library are left out: they are not the script's surface.
BASE_NAMES = frozenset("""
    CHECK_COUPLING_COMPLETENESS CHECK_DEAD_IDENTIFIER CHECK_FENCED_PATH CHECK_LINE_REF
    CHECK_PATH_EXISTS CHECK_TABLE_ROW_MERGE CODE_COUPLED_DOCS CODE_ROOTS CODE_SUFFIXES
    DEFAULT_BASELINE DOC_ROOTS ENTRY_POINT_DOCS FactFinding Findings INDEX_NAMES
    SKIP_DIR_NAMES SKIP_DIR_PATHS SKIP_DIR_PREFIXES TIPS_ALLOWLIST_MODULE
    UNCURATED_PREFIXES _BASELINE_HEADER _CHANGELOG_LINE_RE _CITATION_MARKER_RE
    _CODE_DOC_REF_RE _CODE_REF_IGNORE_PATH_PARTS _CODE_REF_IGNORE_SUBSTRINGS
    _CONFLICT_MARKER_RE _COORDINATE_SUFFIX _COUPLING_AMBIGUOUS_NAMES _COUPLING_SCAN_ROOT
    _COUPLING_SUFFIXES _EXTERNAL_REPO_MARKERS _EXTERNAL_TARGET_COORDINATE_DOCS
    _FENCED_TASK_SPEC_RE _FENCE_RE _FORWARD_LOOKING_DOC_PREFIXES
    _HARNESS_INVARIANT_CITE_RE _HARNESS_INVARIANT_DOC _HARNESS_INVARIANT_ROW_RE
    _HTML_LINK_RE _IDENT_CAMEL_RE _IDENT_CODE_SUFFIXES _IDENT_CODE_TREES _IDENT_MIN_LEN
    _IDENT_PLACEHOLDER_PREFIXES _IDENT_SNAKE_RE _IDENT_STOPWORDS _IDENT_TOKEN_RE
    _INLINE_CODE_RE _INLINE_CODE_SPAN_RE _INVARIANT_SOURCE_SUFFIXES _KIROCREW_RUN_RE
    _LINK_RE _MAX_LINE_DIGITS _MAX_REPORTED_ADVISORIES _MAX_REPORTED_FINDINGS
    _MAX_REPORTED_STALE _MD_NAME_RE _NO_INDEX_REQUIRED _PACKAGED_DOCS_DIR
    _PATH_CITE_DOC_PREFIXES _PREAMBLE_SCAN_LINES _REPO_PATH_CITE_RE _SOURCE_CITE_RE
    _SOURCE_CITE_TREES _STRING_LITERAL_RE _TABLE_DELIMITER_RE _TABLE_LINK_CELL_RE
    _TS_COMMENT_LINE_RE _UNRESOLVABLE_REF_OK _check_invariant_citations _emit
    _emit_facts _identifier_index _index_for_dir _is_dir_link _is_external
    _is_forward_looking _is_regular_file _is_uncurated _iter_code_spans
    _iter_fenced_lines _iter_links _prune _read _read_baseline _rel _report
    _resolve_link _resolve_source_cite _self_test _source_file_index _strip_fences
    _walk_markdown _write_baseline apply_baseline check_changelog_preambles
    check_cited_paths check_code_citations check_code_coupled_docs
    check_conflict_markers check_coupling_completeness check_dead_identifiers
    check_directory_indexes check_fenced_paths check_invariant_citations check_line_refs
    check_links check_reachability check_source_citations check_table_rows main run
    """.split())


def _load():
    spec = importlib.util.spec_from_file_location("docs_lint", _SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # test_docs_lint_fact_checks.py loads its own copy; a dotted patch target there
    # ("docs_lint.<name>", "docs_lint_fact_checks.<name>") must keep resolving to that
    # copy and its owners whichever file loads first.
    previous = {name: sys.modules.get(name) for name in ("docs_lint", *OWNERS)}
    sys.modules["docs_lint"] = module
    spec.loader.exec_module(module)
    if previous["docs_lint"] is not None:
        for name, held in previous.items():
            if held is not None:
                sys.modules[name] = held
    return module


gate = _load()


def _owner_path(owner: str) -> Path:
    return _SCRIPTS / f"{owner}.py"


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _defined_names(tree: ast.Module) -> list[str]:
    """Names a module's own top-level statements define (not the ones it imports)."""
    names: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(node.name)
        elif isinstance(node, ast.Assign):
            names.extend(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.append(node.target.id)
    return names


def _bound_names(nodes: list[ast.stmt]) -> set[str]:
    """Names a run of top-level statements binds, definitions and imports alike."""
    bound = set(_defined_names(ast.Module(body=nodes, type_ignores=[])))
    for node in nodes:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            bound.update((a.asname or a.name).split(".")[0] for a in node.names)
    return bound


# ── the script keeps every name, bound to the owner's object ─────────────────


def test_the_owner_files_beside_the_script_are_exactly_the_owners() -> None:
    assert {path.stem for path in _SCRIPTS.glob("docs_lint_*.py")} == set(OWNERS)


def test_every_name_the_script_bound_before_the_split_is_still_bound() -> None:
    assert sorted(name for name in BASE_NAMES if not hasattr(gate, name)) == []


def _owner_namespace(owner: str) -> dict[str, object]:
    """The namespace of the owner module THIS copy of the script imported.

    Read through a function the script re-exports, not through ``sys.modules``: another
    test module loads its own copy of the script, and with it its own owners.
    """
    first = next(
        node.name
        for node in _tree(_owner_path(owner)).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    )
    namespace = getattr(gate, first).__globals__
    assert namespace["__name__"] == owner, first
    return namespace


def test_every_owner_name_is_the_owners_own_object_on_the_script() -> None:
    for owner in OWNERS:
        namespace = _owner_namespace(owner)
        defined = _defined_names(_tree(_owner_path(owner)))
        assert defined, owner
        for name in defined:
            assert getattr(gate, name) is namespace[name], f"{owner}.{name}"


def test_every_name_an_owner_shares_with_the_script_is_the_scripts_object() -> None:
    for owner in OWNERS:
        for name, value in _owner_namespace(owner).items():
            if not name.startswith("__") and hasattr(gate, name):
                assert getattr(gate, name) is value, f"{owner}.{name}"


def test_the_finding_shapes_stay_defined_on_the_script() -> None:
    # The report prints them and the baseline stores their fields; the owners take
    # them from the script rather than defining their own.
    for name in ("FactFinding", "Findings"):
        assert getattr(gate, name).__module__ == FACADE
        for owner in OWNERS:
            assert name not in _defined_names(_tree(_owner_path(owner))), owner
            held = _owner_namespace(owner).get(name)
            assert held is None or held is getattr(gate, name), f"{owner}.{name}"


# ── import direction ──────────────────────────────────────────────────────────


def test_an_owner_imports_only_the_standard_library_and_the_script() -> None:
    for owner in OWNERS:
        for node in ast.walk(_tree(_owner_path(owner))):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                assert node.level == 0, f"{owner} uses a relative import"
                modules = [node.module or ""]
            else:
                continue
            for name in modules:
                top = name.split(".")[0]
                allowed = top == FACADE or top in sys.stdlib_module_names
                assert allowed, f"{owner} imports {name}"


def _import_line(tree: ast.Module, owner: str) -> int:
    lines = [
        node.lineno
        for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
        and owner
        in ([node.module] if isinstance(node, ast.ImportFrom) else [a.name for a in node.names])
    ]
    assert len(lines) == 1, f"the script imports {owner} {len(lines)} times"
    return lines[0]


def late_takes(script: ast.Module, owners: dict[str, ast.Module]) -> dict[str, list[str]]:
    """Per owner, the names it takes from the script that the script has not bound yet
    when it imports that owner. Such an import fails while the script is half-loaded."""
    late: dict[str, list[str]] = {}
    for owner, tree in owners.items():
        cut = _import_line(script, owner)
        bound_before = _bound_names([node for node in script.body if node.lineno < cut])
        taken = {
            alias.name
            for node in tree.body
            if isinstance(node, ast.ImportFrom) and node.module == FACADE
            for alias in node.names
        }
        if missing := sorted(taken - bound_before):
            late[owner] = missing
    return late


def test_the_late_take_check_catches_a_name_bound_after_the_owner_import() -> None:
    script = ast.parse(
        "A = 1\n"
        "from docs_lint_fact_checks import B\n"
        "from docs_lint_selftest import t\n"
        "C = 2\n"
    )
    owners = {
        "docs_lint_fact_checks": ast.parse("from docs_lint import A\n"),
        "docs_lint_selftest": ast.parse("from docs_lint import A, B, C\n"),
    }
    assert late_takes(script, owners) == {"docs_lint_selftest": ["C"]}


def test_an_owner_takes_from_the_script_only_what_exists_before_it_is_imported() -> None:
    owners = {owner: _tree(_owner_path(owner)) for owner in OWNERS}
    assert late_takes(_tree(_SCRIPT_PATH), owners) == {}


def test_the_script_aliases_itself_and_appends_its_directory_before_its_owners() -> None:
    script = _tree(_SCRIPT_PATH)
    cut = min(_import_line(script, owner) for owner in OWNERS)
    before = ast.Module(body=[n for n in script.body if n.lineno < cut], type_ignores=[])
    after = ast.Module(body=[n for n in script.body if n.lineno > cut], type_ignores=[])
    calls = [ast.unparse(n.func) for n in ast.walk(before) if isinstance(n, ast.Call)]
    assigned = {
        (ast.unparse(target), ast.unparse(n.value))
        for tree in (before, after)
        for n in ast.walk(tree)
        if isinstance(n, ast.Assign)
        for target in n.targets
    }
    assert "sys.path.append" in calls
    assert ("sys.modules['docs_lint']", "sys.modules[__name__]") in assigned
    assert ("sys.modules['docs_lint']", "_OUTER_ALIAS") in assigned
    for path in [_SCRIPT_PATH, *(_owner_path(owner) for owner in OWNERS)]:
        assert "sys.path.insert" not in path.read_text(encoding="utf-8"), path.name


# ── the script still runs as a file, from its own three files ────────────────


def _sparse_copy(tmp_path: Path) -> Path:
    """A tree holding the script, its two owners, and a two-doc tree to lint."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in (FACADE, *OWNERS):
        shutil.copy2(_SCRIPTS / f"{name}.py", scripts / f"{name}.py")
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "README.md").write_text("# Docs\n\n- [Ok](ok.md)\n", encoding="utf-8")
    (docs / "ok.md").write_text("# Ok\n\nBody.\n", encoding="utf-8")
    baseline = tmp_path / gate.DEFAULT_BASELINE
    baseline.parent.mkdir(parents=True)
    baseline.write_text(gate._BASELINE_HEADER, encoding="utf-8")
    return scripts / "docs_lint.py"


def _run_script(
    script: Path, flags: tuple[str, ...], *argv: str
) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONSAFEPATH")}
    # argparse wraps the help to the terminal width, and from Python 3.14 colours it when
    # the host forces colour; a fixed width and no colour keep the text plain.
    env["COLUMNS"] = "200"
    env["PYTHON_COLORS"] = "0"
    return subprocess.run(
        [sys.executable, "-B", *flags, str(script), *argv],
        capture_output=True,
        cwd=script.parent.parent,
        env=env,
        timeout=60,
        **UTF8_TEXT,
    )


@pytest.mark.parametrize("flags", [(), ("-P",)], ids=["script-dir-first", "safe-path"])
def test_the_script_runs_as_a_file_from_a_copy_of_its_three_files(
    tmp_path: Path, flags: tuple[str, ...]
) -> None:
    """Run as a file from a copy holding only its three files, the script imports its
    owners whether or not its directory is first on ``sys.path``: under ``-P`` it is not
    on the path at all until the script appends it. The lint over the copy also proves
    the two owners are exempt from the doc-citation scan the way the script is: the
    self-test plants a citation of a doc that does not exist. That the owners bind to the
    running ``__main__`` rather than to a second copy is pinned by the ``runpy`` probe
    below."""
    script = _sparse_copy(tmp_path)
    shown = _run_script(script, flags, "--help")
    assert shown.returncode == 0, shown.stderr
    assert "usage: docs_lint.py" in shown.stdout
    tested = _run_script(script, flags, "--test")
    assert tested.returncode == 0, tested.stdout + tested.stderr
    assert "Self-test passed" in tested.stdout
    linted = _run_script(script, flags)
    assert linted.returncode == 0, linted.stdout + linted.stderr
    assert "All documentation checks passed" in linted.stdout


#: Shared by the probes below: a load by path as the tests do it, and the owner names
#: that point anywhere but at the given copy of the script.
_PROBE_HELPERS = """
import ast, importlib, importlib.util, json, os, runpy, sys
script = sys.argv[1]
here = os.path.dirname(os.path.realpath(script))

def load(name="docs_lint"):
    spec = importlib.util.spec_from_file_location(name, script)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module

def mixed(copy):
    names = []
    for probe in ("check_cited_paths", "_self_test"):
        namespace = getattr(copy, probe).__globals__
        names += sorted(
            namespace["__name__"] + "." + name
            for name, value in namespace.items()
            if not name.startswith("__") and getattr(copy, name, value) is not value
        )
    return names

def taken(owner):
    tree = ast.parse(open(os.path.join(here, owner + ".py"), encoding="utf-8").read())
    return [
        alias.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module == "docs_lint"
        for alias in node.names
    ]
"""

_TWO_LOADS = _PROBE_HELPERS + """
first, second = load(), load()
other = load("_docs_lint_other")
print(json.dumps({
    "first": mixed(first),
    "second": mixed(second),
    "other": mixed(other),
    "alias_kept": sys.modules["docs_lint"] is second,
}))
"""

_ANOTHER_NAME_ALONE = _PROBE_HELPERS + """
alone = load("_docs_lint_alone")
print(json.dumps({"mixed": mixed(alone), "alias": "docs_lint" in sys.modules}))
"""

_RUN_AS_MAIN = _PROBE_HELPERS + """
sys.argv = [script, "--test"]
code = None
try:
    runpy.run_path(script, run_name="__main__")
except SystemExit as stop:
    code = stop.code
fact = sys.modules["docs_lint_fact_checks"]
selftest = sys.modules["docs_lint_selftest"]
facade = selftest.run.__globals__
print(json.dumps({
    "code": code,
    "main": facade["__name__"] == "__main__",
    "fact_checks": [n for n in taken("docs_lint_fact_checks") if getattr(fact, n) is not facade[n]],
    "selftest": [n for n in taken("docs_lint_selftest") if getattr(selftest, n) is not facade[n]],
    "reexported": facade["check_cited_paths"] is fact.check_cited_paths
    and facade["_self_test"] is selftest._self_test,
    "alias": "docs_lint" in sys.modules,
}))
"""

_OWNER_FIRST = _PROBE_HELPERS + """
owner = sys.argv[2]
sys.path.append(os.path.dirname(script))
first = importlib.import_module(owner)
import docs_lint
print(json.dumps({
    "taken": len(taken(owner)),
    "foreign": [n for n in taken(owner) if getattr(first, n) is not getattr(docs_lint, n)],
    "defined_shared": all(
        getattr(docs_lint, name) is value
        for name, value in vars(first).items()
        if callable(value) and getattr(value, "__module__", None) == owner
    ),
}))
"""

_COLD_OWNER = _PROBE_HELPERS + """
owner = sys.argv[2]
gate = load()
del sys.modules[owner]
fresh = importlib.import_module(owner)
print(json.dumps({
    "appended": sys.path[-1] == here and sys.path[0] != here,
    "taken": len(taken(owner)),
    "foreign": [n for n in taken(owner) if getattr(fresh, n) is not getattr(gate, n)],
}))
"""


def _run_python(program: str, *argv: str) -> object:
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONSAFEPATH")}
    out = subprocess.run(
        [sys.executable, "-B", "-c", program, str(_SCRIPT_PATH), *argv],
        capture_output=True,
        check=True,
        cwd=_REPO_ROOT,
        env=env,
        timeout=60,
        **UTF8_TEXT,
    )
    return json.loads(out.stdout.splitlines()[-1])


def test_each_load_of_the_script_gets_owners_bound_to_its_own_names() -> None:
    """Two loads by path in one process (two test modules that each load the script),
    and a third under another module name, all keep every owner name pointing at their
    own copy, as each load of the one-file script did; the third leaves the alias the
    second set."""
    expected = {"first": [], "second": [], "other": [], "alias_kept": True}
    assert _run_python(_TWO_LOADS) == expected


def test_a_load_under_another_name_leaves_no_docs_lint_entry_behind() -> None:
    """With no earlier ``docs_lint`` entry, a load under another name ends as the
    one-file script did: nothing registered as ``docs_lint``, and owners bound to it."""
    assert _run_python(_ANOTHER_NAME_ALONE) == {"mixed": [], "alias": False}


def test_run_as_main_the_owners_hold_the_running_modules_objects() -> None:
    """``runpy.run_path(..., run_name="__main__")`` with ``--test`` exits 0, every name an
    owner takes from the script is the running ``__main__`` module's object, the script
    re-exports the owners' own functions, and no ``docs_lint`` entry stays behind."""
    assert _run_python(_RUN_AS_MAIN) == {
        "code": 0,
        "main": True,
        "fact_checks": [],
        "selftest": [],
        "reexported": True,
        "alias": False,
    }


@pytest.mark.parametrize("owner", OWNERS)
def test_an_owner_imported_before_the_script_ends_up_sharing_its_objects(owner: str) -> None:
    """With the directory on the path, importing an owner first still leaves the owner
    and the script holding the same objects."""
    result = _run_python(_OWNER_FIRST, owner)
    assert result["taken"] > 0, result
    assert result == {"taken": result["taken"], "foreign": [], "defined_shared": True}


@pytest.mark.parametrize("owner", OWNERS)
def test_each_owner_imports_cold_once_the_script_is_loaded_by_path(owner: str) -> None:
    """A fresh interpreter loads the script the way the tests do, then imports the owner
    again from nothing: the script's directory was APPENDED to ``sys.path`` (never put
    first), and every name the owner takes from the script is that copy's object."""
    result = _run_python(_COLD_OWNER, owner)
    assert result["taken"] > 0, result
    assert result == {"appended": True, "taken": result["taken"], "foreign": []}


# ── patch reach: what the tests patch on the script ─────────────────────────


#: The calls that load a module from a file: a test that loads the script spells its file
#: name in the location, whatever module name it registers it under.
_FILE_LOADERS = frozenset({"spec_from_file_location", "SourceFileLoader"})


def _text(node: ast.expr | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _verb(node: ast.Call) -> str | None:
    func = node.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)


def _argument(node: ast.Call, index: int, *keywords: str) -> ast.expr | None:
    if len(node.args) > index:
        return node.args[index]
    return next((k.value for k in node.keywords if k.arg in keywords), None)


class _ScriptRefs:
    """How one test module refers to the script: the module-level names bound to its
    file path, the module names it loads it under, and the names bound to the module."""

    def __init__(self, tree: ast.Module) -> None:
        self.tree = tree
        self.path_names: set[str] = set()
        self._grow_path_names()
        self.module_names = self._module_names()
        self.spec_names = {
            target.id
            for node in ast.walk(tree)
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and self._loads_the_file(node.value)
            for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
            if isinstance(target, ast.Name)
        }
        self.loaders = {
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and any(self.loads(inner) for inner in ast.walk(node))
        }
        self.aliases = self._aliases()

    def spells_the_file(self, node: ast.AST) -> bool:
        return any(
            (
                isinstance(n, ast.Constant)
                and isinstance(n.value, str)
                and n.value.endswith(f"{FACADE}.py")
            )
            or (isinstance(n, ast.Name) and n.id in self.path_names)
            for n in ast.walk(node)
        )

    def _grow_path_names(self) -> None:
        """Module-level names bound to an expression that spells the script's file name
        (``_SCRIPT = ROOT / "scripts" / "docs_lint.py"``), followed through copies."""
        grew = True
        while grew:
            grew = False
            for node in self.tree.body:
                if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
                    continue
                if not self.spells_the_file(node.value):
                    continue
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name) and target.id not in self.path_names:
                        self.path_names.add(target.id)
                        grew = True

    def _module_names(self) -> set[str]:
        names = {FACADE}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call) and _verb(node) in _FILE_LOADERS:
                location = _argument(node, 1, "location", "path")
                name = _text(_argument(node, 0, "name", "fullname"))
                if location is not None and name and self.spells_the_file(location):
                    names.add(name)
        return names

    def is_module(self, node: ast.AST | None) -> bool:
        """``sys.modules["<name>"]`` or ``import_module("<name>")`` for a name the
        script is loaded under: an expression whose value IS the script."""
        if isinstance(node, ast.Subscript):
            return ast.unparse(node.value) == "sys.modules" and (
                _text(node.slice) in self.module_names
            )
        return (
            isinstance(node, ast.Call)
            and _verb(node) == "import_module"
            and _text(_argument(node, 0, "name")) in self.module_names
        )

    def _loads_the_file(self, node: ast.AST | None) -> bool:
        """A ``spec_from_file_location`` / ``SourceFileLoader`` call whose location
        spells the script's file."""
        if not (isinstance(node, ast.Call) and _verb(node) in _FILE_LOADERS):
            return False
        location = _argument(node, 1, "location", "path")
        return location is not None and self.spells_the_file(location)

    def loads(self, node: ast.AST) -> bool:
        return self._loads_the_file(node) or self.is_module(node)

    def _gives_the_script(self, node: ast.expr | None, aliases: set[str]) -> bool:
        """The script itself: an alias, a loader function's result,
        ``module_from_spec(<spec of the file>)``, ``<loader of the file>.load_module()``,
        or ``sys.modules[...]`` / ``import_module(...)`` under a name it is loaded as."""
        if isinstance(node, ast.Name):
            return node.id in aliases
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in self.loaders:
                return True
            if _verb(node) == "module_from_spec":
                spec = _argument(node, 0, "spec")
                named = isinstance(spec, ast.Name) and spec.id in self.spec_names
                return named or self._loads_the_file(spec)
            if _verb(node) == "load_module" and isinstance(node.func, ast.Attribute):
                return self._loads_the_file(node.func.value)
        return self.is_module(node)

    def _aliases(self) -> set[str]:
        """``import docs_lint`` (under any alias), a name assigned the script or a copy of
        one, and a ``pytest`` fixture that loads it or returns one (under its own or its
        ``name=`` name)."""
        aliases: set[str] = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                aliases.update(
                    a.asname or a.name for a in node.names if a.name in self.module_names
                )
        grew = True
        while grew:
            before = len(aliases)
            for node in ast.walk(self.tree):
                if isinstance(node, (ast.Assign, ast.AnnAssign)) and self._gives_the_script(
                    node.value, aliases
                ):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    aliases.update(t.id for t in targets if isinstance(t, ast.Name))
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    fixture = self._fixture_name(node)
                    returns = [
                        n.value
                        for n in ast.walk(node)
                        if isinstance(n, (ast.Return, ast.Yield)) and n.value is not None
                    ]
                    loads_itself = node.name in self.loaders
                    if fixture and (
                        loads_itself or any(self._gives_the_script(r, aliases) for r in returns)
                    ):
                        aliases.add(fixture)
            grew = len(aliases) > before
        return aliases

    @staticmethod
    def _fixture_name(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
        for decorator in node.decorator_list:
            call = decorator if isinstance(decorator, ast.Call) else None
            if ast.unparse(call.func if call else decorator) in {"pytest.fixture", "fixture"}:
                keywords = call.keywords if call else []
                named = _text(next((k.value for k in keywords if k.arg == "name"), None))
                return named or node.name
        return None

    def is_facade(self, node: ast.expr | None) -> bool:
        return (isinstance(node, ast.Name) and node.id in self.aliases) or self.is_module(node)

    def is_facade_namespace(self, node: ast.expr | None) -> bool:
        """``gate.__dict__`` or ``vars(gate)``."""
        if isinstance(node, ast.Attribute) and node.attr == "__dict__":
            return self.is_facade(node.value)
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "vars"
            and len(node.args) == 1
            and self.is_facade(node.args[0])
        )


#: ``mock.patch.multiple`` keywords that configure the patch rather than name a target.
_MULTIPLE_OPTIONS = frozenset({"spec", "create", "spec_set", "autospec", "new_callable"})


def _patch_targets(refs: _ScriptRefs) -> set[str]:
    """Every name a module patches on the script, in each spelling a test can use."""
    found: set[str] = set()
    for node in ast.walk(refs.tree):
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign, ast.Delete)):
            targets = node.targets if isinstance(node, (ast.Assign, ast.Delete)) else [node.target]
            found.update(
                t.attr for t in targets if isinstance(t, ast.Attribute) and refs.is_facade(t.value)
            )
            continue
        if not isinstance(node, ast.Call):
            continue
        verb = _verb(node)
        first = _argument(node, 0, "target", "in_dict", "dic")
        if verb in {"setattr", "delattr", "object"} and refs.is_facade(first):
            name = _text(_argument(node, 1, "name", "attribute"))
            if name is not None:
                found.add(name)
        elif verb == "multiple" and refs.is_facade(first):
            found.update(k.arg for k in node.keywords if k.arg and k.arg not in _MULTIPLE_OPTIONS)
        elif verb in {"setattr", "delattr", "patch"} and _text(first) is not None:
            module, _, rest = str(_text(first)).partition(".")
            if rest and module in refs.module_names:
                found.add(rest.split(".")[0])
        elif verb in {"setitem", "delitem"} and refs.is_facade_namespace(first):
            name = _text(_argument(node, 1, "name"))
            if name is not None:
                found.add(name)
        elif verb == "dict" and refs.is_facade_namespace(first):
            values = _argument(node, 1, "values")
            if isinstance(values, ast.Dict):
                found.update(n for n in map(_text, values.keys) if n is not None)
            found.update(k.arg for k in node.keywords if k.arg and k.arg not in {"clear"})
    return found


def _patch_targets_in(source: str) -> set[str]:
    return _patch_targets(_ScriptRefs(ast.parse(source)))


@functools.cache
def _corpus() -> tuple[tuple[Path, str], ...]:
    """Every test module that names the script, with its text."""
    found: list[tuple[Path, str]] = []
    for path in sorted((_REPO_ROOT / "test").rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        if FACADE in text:
            found.append((path, text))
    return tuple(found)


@functools.cache
def guarded_names() -> frozenset[str]:
    """Every name any test patches on the script, read from the test tree itself."""
    names: set[str] = set()
    for _path, text in _corpus():
        names |= _patch_targets_in(text)
    return frozenset(names)


_PLANTED_PATCHES = """
import importlib
import importlib.machinery
import importlib.util
import sys
import types
import unittest.mock as _mock
from pathlib import Path
from unittest import mock

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "docs_lint.py"


def _load():
    spec = importlib.util.spec_from_file_location("docs_lint", "scripts/docs_lint.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["docs_lint"] = module
    spec.loader.exec_module(module)
    return module


def _load_under_another_name():
    spec = importlib.util.spec_from_file_location("_docs_lint_under_test", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = _load()
copy = gate
direct = sys.modules["docs_lint"]
other_name = _load_under_another_name()
typed: types.ModuleType = _load()
_SPEC: object = importlib.util.spec_from_file_location("_docs_lint_inline", _SCRIPT)
inline = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(inline)
by_loader = importlib.machinery.SourceFileLoader("_docs_lint_by_loader", str(_SCRIPT)).load_module()
nested = importlib.util.module_from_spec(
    importlib.util.spec_from_file_location("_docs_lint_nested", _SCRIPT)
)


@pytest.fixture
def lint():
    return gate


@pytest.fixture(name="named_lint")
def _named():
    yield gate


@pytest.fixture
def fresh():
    held = {}
    held["copy"] = importlib.util.module_from_spec(
        importlib.util.spec_from_file_location("docs_lint", _SCRIPT)
    )
    return held["copy"]


def test(monkeypatch, other, lint, named_lint, fresh):
    monkeypatch.setattr(gate, "A", 1)
    _mock.patch.object(copy, "B")
    monkeypatch.setattr("docs_lint.C", 1)
    mock.patch("docs_lint.D.attr")
    monkeypatch.delattr(direct, "E")
    _mock.patch.multiple(gate, F=1, G=2, create=True)
    monkeypatch.setitem(vars(gate), "H", 1)
    monkeypatch.setitem(gate.__dict__, "I", 1)
    monkeypatch.setattr(gate, name="J", value=1)
    _mock.patch.object(target=gate, attribute="K")
    _mock.patch.dict(vars(gate), {"L": 1})
    monkeypatch.setattr(sys.modules["docs_lint"], "M", 1)
    monkeypatch.setattr(importlib.import_module("docs_lint"), "N", 1)
    monkeypatch.setattr(lint, "O", 1)
    monkeypatch.setattr(named_lint, "P", 1)
    monkeypatch.setattr(other_name, "Q", 1)
    monkeypatch.setattr("_docs_lint_under_test.R", 1)
    gate.S = 1
    del gate.T
    gate.U += 1
    gate.V: int = 1
    monkeypatch.setattr(by_loader, "W", 1)
    monkeypatch.setattr(fresh, "X", 1)
    monkeypatch.setattr(typed, "Y", 1)
    monkeypatch.setattr(inline, "Z", 1)
    monkeypatch.setattr(nested, "AA", 1)
    spec = importlib.util.spec_from_file_location("docs_lint", "scripts/docs_lint.py")
    monkeypatch.setattr(spec, "NOT_THE_SCRIPT", 1)
    monkeypatch.setattr(other, "NOT_THE_SCRIPT", 1)
    monkeypatch.setattr("elsewhere.NOT_THE_SCRIPT", 1)
"""


def test_the_corpus_scan_finds_every_patch_spelling_and_nothing_else() -> None:
    assert _patch_targets_in(_PLANTED_PATCHES) == {*string.ascii_uppercase, "AA"}
    unloaded = (
        _PLANTED_PATCHES.replace('"scripts/docs_lint.py"', '"scripts/elsewhere.py"')
        .replace('/ "docs_lint.py"', '/ "elsewhere.py"')
        .replace('sys.modules["docs_lint"]', 'sys.modules["elsewhere"]')
        .replace('import_module("docs_lint")', 'import_module("elsewhere")')
    )
    # Nothing loads the script any more: only the dotted targets that name it remain.
    assert _patch_targets_in(unloaded) == set("CD")


def _unbound_loads(text: str) -> list[str]:
    """Where a module gets hold of the script without the scan binding a name to it: an
    assignment of the script to anything but a plain name (``self.lint = ...``), or a
    module that loads it and binds no name at all."""
    refs = _ScriptRefs(ast.parse(text))
    sites = [
        f"line {node.lineno}"
        for node in ast.walk(refs.tree)
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        and refs._gives_the_script(node.value, refs.aliases)
        and any(
            not isinstance(target, ast.Name)
            and not (
                isinstance(target, ast.Subscript) and ast.unparse(target.value) == "sys.modules"
            )
            for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        )
    ]
    if any(refs.loads(node) for node in ast.walk(refs.tree)) and not refs.aliases:
        sites.append("no name is bound to the script")
    return sites


def test_a_load_the_scan_cannot_bind_is_reported() -> None:
    later = (
        "class TestLint:\n"
        "    def setup_method(self):\n"
        '        self.lint = importlib.import_module("docs_lint")\n'
    )
    alone = "import importlib\n" + later
    beside_a_bound_one = 'import importlib\ngate = importlib.import_module("docs_lint")\n' + later
    assert _unbound_loads(alone) == ["line 4", "no name is bound to the script"]
    assert _unbound_loads(beside_a_bound_one) == ["line 5"]
    assert _unbound_loads(alone.replace('"docs_lint"', '"elsewhere"')) == []


def _loads_the_script(text: str) -> bool:
    refs = _ScriptRefs(ast.parse(text))
    return any(refs.loads(node) for node in ast.walk(refs.tree))


def test_every_test_module_that_loads_the_script_is_seen_by_the_scan() -> None:
    """A load the scan cannot bind would leave a patch added there later unguarded."""
    loading = [path for path, text in _corpus() if _loads_the_script(text)]
    assert loading, "no test loads the script: the patch-reach scan reads nothing"
    unbound = {
        path.relative_to(_REPO_ROOT).as_posix(): sites
        for path, text in _corpus()
        if (sites := _unbound_loads(text))
    }
    assert unbound == {}


def test_every_patched_name_is_one_the_script_binds() -> None:
    assert sorted(name for name in guarded_names() if not hasattr(gate, name)) == []


# ── patch reach: how the owners read those names ───────────────────────────


def reach_violations(source: str, guarded: frozenset[str]) -> list[str]:
    """Each way *source*, an owner module, could hold a guarded name a patch misses.

    A guarded name must be read as ``docs_lint.<name>`` inside a function body. Reading
    it bare, binding it (an import under any name, an assignment, a parameter), reading it
    through another module, aliasing or rebinding the script, or reading it while the
    module is imported (at module or class level, in a default, a decorator or an
    annotation) keeps an object the patch does not replace.
    """
    tree = ast.parse(source)
    problems: list[str] = []
    other_modules = {
        alias.asname or alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
        if alias.name != FACADE
    }

    def visit(node: ast.AST, call_time: bool) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            args = node.args
            outer = [*args.defaults, *[d for d in args.kw_defaults if d is not None]]
            if not isinstance(node, ast.Lambda):
                outer += node.decorator_list
                outer += [a.annotation for a in (*args.posonlyargs, *args.args) if a.annotation]
                outer += [a.annotation for a in args.kwonlyargs if a.annotation]
                outer += [node.returns] if node.returns else []
            for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]:
                if arg is not None and arg.arg in guarded:
                    problems.append(f"line {node.lineno}: parameter {arg.arg} binds a guarded name")
            for child in outer:
                visit(child, call_time)
            for child in node.body if isinstance(node.body, list) else [node.body]:
                visit(child, True)
            return
        if isinstance(node, ast.Name) and node.id in guarded:
            how = "reads" if isinstance(node.ctx, ast.Load) else "binds"
            problems.append(f"line {node.lineno}: {how} {node.id} bare")
        elif (
            isinstance(node, ast.Name) and node.id == FACADE and not isinstance(node.ctx, ast.Load)
        ):
            problems.append(f"line {node.lineno}: rebinds {FACADE}")
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".")[0]
                if bound in guarded or alias.name in guarded:
                    problems.append(f"line {node.lineno}: imports {alias.name} as {bound}")
                if isinstance(node, ast.Import) and alias.name == FACADE and alias.asname:
                    problems.append(f"line {node.lineno}: aliases {FACADE} as {alias.asname}")
        elif isinstance(node, ast.ExceptHandler) and node.name in guarded:
            problems.append(f"line {node.lineno}: binds {node.name}")
        elif isinstance(node, ast.Attribute) and node.attr in guarded:
            owner = node.value.id if isinstance(node.value, ast.Name) else None
            if owner == FACADE and not call_time:
                problems.append(f"line {node.lineno}: reads {FACADE}.{node.attr} at import time")
            elif owner in other_modules:
                problems.append(f"line {node.lineno}: reads {node.attr} through {owner}")
        for child in ast.iter_child_nodes(node):
            visit(child, call_time)

    # A patched function DEFINED in an owner (a ``def``, which is not a Name) and
    # re-exported by the script may keep that one binding, as long as every caller goes
    # through the script.
    for statement in tree.body:
        visit(statement, False)
    return problems


def test_the_reach_guard_catches_each_way_an_owner_could_miss_a_patch() -> None:
    guarded = frozenset({"KNOB", "seam"})
    clean = "import docs_lint\n\ndef f():\n    return docs_lint.KNOB, docs_lint.seam()\n"
    assert reach_violations(clean, guarded) == []
    planted = {
        "a bare read": "def f():\n    return KNOB\n",
        "a bare call": "def f():\n    return seam()\n",
        "a from-import": "from docs_lint import KNOB\n",
        "an aliased from-import": "from docs_lint import KNOB as _KNOB\n",
        "a module-level copy": "import docs_lint\nKNOB = docs_lint.KNOB\n",
        "a default": "import docs_lint\ndef f(x=docs_lint.KNOB):\n    return x\n",
        "a decorator": "import docs_lint\n@docs_lint.seam\ndef f():\n    pass\n",
        "a class-level read": "import docs_lint\nclass C:\n    k = docs_lint.KNOB\n",
        "an aliased script": "import docs_lint as dl\ndef f():\n    return dl.KNOB\n",
        "a rebound script": "import docs_lint\ndocs_lint = None\n",
        "a parameter": "def f(KNOB):\n    return 1\n",
        "a read through another owner": (
            "import docs_lint_fact_checks as f\ndef g():\n    return f.seam()\n"
        ),
    }
    missed = [label for label, source in planted.items() if not reach_violations(source, guarded)]
    assert missed == []


def test_no_owner_holds_a_name_a_test_patches_on_the_script() -> None:
    guarded = guarded_names()
    found = {
        owner: problems
        for owner in OWNERS
        if (problems := reach_violations(_owner_path(owner).read_text(encoding="utf-8"), guarded))
    }
    assert found == {}, (
        "an owner holds a name a test patches on docs_lint, so the patch misses it: import "
        f"the script and read the name as docs_lint.<name> inside the function: {found}"
    )


def test_leaf_test_scope_derives_this_file_as_a_corpus_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The patch-reach scan reads every test module, so a leaf-only diff must still run it."""
    monkeypatch.syspath_prepend(str(_REPO_ROOT / "scripts"))
    leaf_test_scope = importlib.import_module("leaf_test_scope")
    this_file = (_REPO_ROOT / "test" / Path(__file__).name).relative_to(_REPO_ROOT).as_posix()
    assert this_file in leaf_test_scope.corpus_gates(_REPO_ROOT)
