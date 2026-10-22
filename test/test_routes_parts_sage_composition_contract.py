"""The composition contract of the Code Review Sage ``backend/routes_parts`` package.

``kiro_crew.apps.builtins.code_review_sage.backend.routes`` keeps the app's import header,
its process state, the follow-up endpoints and ``register_routes``. Every other handler and
helper lives in a part of ``backend/routes_parts`` and runs on the facade's globals through
``kiro_crew._facade_compose.compose``. The app's own tests load a private copy of
``backend/routes.py`` by file path for each test and rebind its names on that copy, so this
file pins what keeps that working:

* every function a part defines runs on the facade's globals and reports the facade as its
  module, every global it reads is bound there (a bytecode sweep with a planted
  self-test), and the facade binds every name a part defines;
* a part's top-level functions carry no decorator and close over nothing, because
  composition rebinds a function but never the function a wrapper holds; that is why the
  two ``_require_enabled`` follow-up handlers stay in the facade;
* a private copy loaded by file path composes the parts onto its own namespace, two copies
  share no state, and loading one leaves the canonical facade's functions alone;
* every name the test trees rebind or patch on the routes module is a facade global, and
  every composed function that reads one reads it through that namespace (a corpus scan
  with a planted self-test); a rebind on a copy reaches a moved function;
* nothing but the facade imports a part (with a planted-importer self-test), a part
  imports project names only for the type checker, and no part binds the facade's state;
* the routes log under the facade's logger name, and every import the facade binds is
  read by the family's code, which stands in for the F401 check ``setup.cfg`` switches
  off for it (with a planted self-test).
"""

from __future__ import annotations

import ast
import asyncio
import builtins
import dis
import importlib
import importlib.util
import itertools
import types
from pathlib import Path

import pytest
import source_corpus

pytestmark = pytest.mark.xdist_group(name="tree_scan_test_routes_parts_sage_composition_contract")

FACADE = "kiro_crew.apps.builtins.code_review_sage.backend.routes"
PARTS_PACKAGE = "kiro_crew.apps.builtins.code_review_sage.backend.routes_parts"
facade = importlib.import_module(FACADE)
FACADE_PATH = Path(facade.__file__).resolve()
PARTS_DIR = FACADE_PATH.with_name("routes_parts")
APP_TESTS_DIR = FACADE_PATH.parent.parent / "tests"
_THIS_FILE = Path(__file__).resolve()
#: The parts the facade composes, in the order it passes them.
PARTS = ("registry", "review_runs", "run_threads", "publishing", "repos", "settings", "namespaces")
#: The logger name log filters and ``caplog`` assertions use, as before the split.
LOGGER_NAME = "kirocrew.app.code-review-sage"

#: Where each unit the split moved out of ``backend/routes.py`` lives. A part may define
#: more; it must keep defining these.
_MOVED: dict[str, str] = {
    **dict.fromkeys(
        (
            "_make_progress _now _runs_file _write_runs _save_runs _load_runs _is_live _record"
            " _find_run _reap_orphan_run_dirs _stage_key _claim_changes_under_lock"
            " _release_claims"
        ).split(),
        "registry",
    ),
    **dict.fromkeys(
        (
            "_run_review_bg _first_change_failure _first_change_error _run_headline"
            " _notify_finished _handle_review _posting_expected _record_reviewed _list_repo_prs"
            " _handle_repo_prs _handle_review_repo _handle_runs"
        ).split(),
        "review_runs",
    ),
    **dict.fromkeys(
        (
            "_run_id_param _handle_run_detail _rebanded_report _handle_run_report"
            " _handle_run_cancel _handle_run_delete _handle_run_archive"
        ).split(),
        "run_threads",
    ),
    **dict.fromkeys(
        (
            "_post_comments_bg _notify_posted _handle_run_post _DISMISS_REASON_MAX"
            " _dismissed_keys _without_dismissed _handle_run_dismiss _collect_delivered"
            " _pending_comment_count"
        ).split(),
        "publishing",
    ),
    **dict.fromkeys(
        (
            "_handle_recent_repos _handle_my_repos _handle_review_queue _pull_request_ref"
            " _handle_repos"
        ).split(),
        "repos",
    ),
    **dict.fromkeys(
        (
            "_load_known_models _known_models _valid_model _load_review_section"
            " _write_review_section _handle_settings"
        ).split(),
        "settings",
    ),
    **dict.fromkeys(
        "_handle_namespaces _handle_learnings _consolidate_bg _handle_consolidate".split(),
        "namespaces",
    ),
}

#: How many of the moved units are functions: the sweeps below must reach at least these.
_MOVED_FUNCTIONS = len(_MOVED) - 1  # every unit but the ``_DISMISS_REASON_MAX`` constant

#: The facade's process state. Each load of the facade -- the canonical module, and each
#: test's private copy -- binds its own, and every part reads it through those globals.
_PROCESS_STATE = frozenset(
    {
        "_RUNS",
        "_RUNS_MAX",
        "_LOCK",
        "_RUN_LOCK",
        "_NS_OPS_LOCK",
        "_INFLIGHT",
        "_STAGE_OWNER",
        "_CANCELLED",
        "_PROGRESS_LOCK",
        "_TASKS",
        "_APP_STATE",
        "_KNOWN_MODELS",
        "_CONSOLIDATING",
        "_CONSOLIDATE_STATE",
    }
)
#: The state a load creates afresh: two loads must never share one of these objects.
_FRESH_STATE = _PROCESS_STATE - {"_RUNS_MAX"}

#: What the facade keeps for itself: the follow-up endpoints, which ``_require_enabled``
#: wraps where they are defined, and the registration table.
_KEPT_IN_FACADE = (
    "_chat_params",
    "_require_enabled",
    "_followup_session_exists",
    "_ensure_followup_folder",
    "_change_title",
    "_followup_sweep_loop",
    "register_routes",
)
_WRAPPED_IN_FACADE = ("_handle_chat_get", "_handle_followup_start")

#: The spellings a test reaches the routes module by: the loader the app's tests share, a
#: local loader that calls ``spec_from_file_location`` on the routes path, the top-level
#: ``backend.routes`` import (the app root is on ``sys.path``), and the canonical import.
_LOADERS = frozenset({"_load_routes_module"})
_TOP_LEVEL = "backend.routes"
#: Text one of those spellings needs, so the corpus scan parses only the files that can hold one.
_REACH_MARKERS = (
    "_load_routes_module",
    "spec_from_file_location",
    "backend import routes",
    "backend.routes",
    "code_review_sage.backend",
)
_PATCHERS = frozenset({"setattr", "delattr", "object"})


def _part(stem: str) -> types.ModuleType:
    return importlib.import_module(f"{PARTS_PACKAGE}.{stem}")


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _is_type_checking(node: ast.stmt) -> bool:
    return isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING"


def _defined(tree: ast.Module) -> set[str]:
    """Names a module defines at its top level: a def, a class or an assignment."""
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names |= {n.id for t in targets for n in ast.walk(t) if isinstance(n, ast.Name)}
    return names


def _part_defined() -> dict[str, str]:
    return {name: stem for stem in PARTS for name in _defined(_tree(PARTS_DIR / f"{stem}.py"))}


def _load_copy(name: str) -> types.ModuleType:
    """A private copy of the facade, loaded by file path the way the app's tests load it."""
    spec = importlib.util.spec_from_file_location(name, str(FACADE_PATH))
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def part_functions(namespace: dict[str, object]) -> dict[str, types.FunctionType]:
    """``name -> function`` for every function *namespace* binds whose code is a part's."""
    return {
        name: value
        for name, value in namespace.items()
        if isinstance(value, types.FunctionType)
        and Path(value.__code__.co_filename).resolve().parent == PARTS_DIR
    }


def global_reads(code: types.CodeType) -> set[str]:
    """Every global *code* and the code nested in it loads, stores or deletes."""
    names: set[str] = set()
    for instruction in dis.get_instructions(code):
        if instruction.opname in {"LOAD_GLOBAL", "STORE_GLOBAL", "DELETE_GLOBAL"}:
            names.add(instruction.argval)
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            names |= global_reads(const)
    return names


def unbound_reads(functions: dict[str, types.FunctionType]) -> dict[str, list[str]]:
    """``name -> globals it reads that neither its globals nor the builtins bind``."""
    missing: dict[str, list[str]] = {}
    for name, fn in sorted(functions.items()):
        names = sorted(
            n
            for n in global_reads(fn.__code__)
            if n not in fn.__globals__ and n not in vars(builtins)
        )
        if names:
            missing[name] = names
    return missing


def read_names(*sources: str) -> set[str]:
    """Every name the family's code loads, at any depth."""
    return {
        node.id
        for source in sources
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }


def unread_imports(
    source: str, part_names: dict[str, set[str]], family_reads: set[str]
) -> list[str]:
    """Imports *source* binds that nothing in the family reads, and part imports of names
    the part does not define. ``from __future__`` binds no name anyone reads."""
    found: list[str] = []
    for node in ast.parse(source).body:
        if isinstance(node, ast.Import):
            bound = [(alias.asname or alias.name.split(".")[0], None) for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module != "__future__":
            bound = [(alias.asname or alias.name, node.module) for alias in node.names]
        else:
            continue
        for name, module in bound:
            if module in part_names and name not in part_names[module]:
                found.append(f"{module}.{name}")
            elif module not in part_names and name not in family_reads:
                found.append(name)
    return found


def _family_sources() -> list[str]:
    paths = [FACADE_PATH, *sorted(PARTS_DIR.glob("*.py"))]
    return [path.read_text(encoding="utf-8") for path in paths]


def _part_names() -> dict[str, set[str]]:
    return {f"{PARTS_PACKAGE}.{stem}": _defined(_tree(PARTS_DIR / f"{stem}.py")) for stem in PARTS}


def _routes_paths(tree: ast.Module) -> set[str]:
    """Module-level names a file binds to the routes file's path (``... / "routes.py"``)."""
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign) and "'routes.py'" in ast.unparse(node.value):
            names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
    return names


def _loads_the_routes_file(fn: ast.FunctionDef | ast.AsyncFunctionDef, paths: set[str]) -> bool:
    """Whether *fn* calls ``spec_from_file_location`` on the routes file's path."""
    for node in ast.walk(fn):
        if (
            isinstance(node, ast.Call)
            and ast.unparse(node.func).rpartition(".")[2] == "spec_from_file_location"
        ):
            args = " ".join(ast.unparse(a) for a in [*node.args, *(k.value for k in node.keywords)])
            if "'routes.py'" in args or any(
                n.id in paths for a in node.args for n in ast.walk(a) if isinstance(n, ast.Name)
            ):
                return True
    return False


def _module_aliases(tree: ast.Module) -> set[str]:
    """The spellings a test file binds the routes module to.

    The result of a loader call assigned to a name or attribute, where a loader is the
    app suite's ``_load_routes_module``, a local function that calls
    ``spec_from_file_location`` on the routes path, or a local function returning either;
    and the name a ``backend.routes`` or canonical-module import binds.
    """
    paths = _routes_paths(tree)
    loaders = set(_LOADERS)
    functions = [
        node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    loaders |= {fn.name for fn in functions if _loads_the_routes_file(fn, paths)}
    for fn in functions:
        for sub in ast.walk(fn):
            if (
                isinstance(sub, ast.Return)
                and isinstance(sub.value, ast.Call)
                and ast.unparse(sub.value.func).rpartition(".")[2] in loaders
            ):
                loaders.add(fn.name)
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, ast.Call):
            if ast.unparse(node.value.func).rpartition(".")[2] in loaders:
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                aliases |= {ast.unparse(t) for t in targets}
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if f"{node.module}.{alias.name}" in {FACADE, _TOP_LEVEL}:
                    aliases.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in {FACADE, _TOP_LEVEL} and alias.asname:
                    aliases.add(alias.asname)
    return aliases


def patched_names(source: str) -> set[str]:
    """Names a test source rebinds, deletes or patches on the routes module.

    Seen: ``<alias>.<name> = ...`` and ``del``, ``setattr``/``delattr``/
    ``monkeypatch.setattr``/``patch.object`` with an alias and a literal name, and
    ``patch("<facade>.<name>")`` strings. Not seen, by design: ``patch.dict`` on the
    module's namespace, and a name the test computes (``patch.object(target, name)`` in a
    loop over a seam table); those reach the facade's globals the same way, but this scan
    does not claim them.
    """
    tree = ast.parse(source)
    aliases = _module_aliases(tree)
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign, ast.Delete)):
            targets = node.targets if isinstance(node, (ast.Assign, ast.Delete)) else [node.target]
            for target in targets:
                if isinstance(target, ast.Attribute) and ast.unparse(target.value) in aliases:
                    found.add(target.attr)
        elif isinstance(node, ast.Call):
            func = ast.unparse(node.func).rpartition(".")[2]
            args = node.args
            if (
                func in _PATCHERS
                and len(args) >= 2
                and ast.unparse(args[0]) in aliases
                and isinstance(args[1], ast.Constant)
                and isinstance(args[1].value, str)
            ):
                found.add(args[1].value)
            elif (
                func == "patch"
                and args
                and isinstance(args[0], ast.Constant)
                and isinstance(args[0].value, str)
                and args[0].value.startswith(FACADE + ".")
            ):
                found.add(args[0].value[len(FACADE) + 1 :].split(".")[0])
    return found


def _corpus() -> list[Path]:
    """The test sources that can reach the routes module by one of the spellings above.

    Every file of the app's own suite that holds a spelling's text, and every file of the
    top-level test tree that holds one and names ``code_review_sage`` (there, ``backend``
    and ``routes.py`` are other apps' names as often as this one's).
    """
    root = source_corpus.repo_root()
    tests = root / "test"
    files = []
    for path in sorted(itertools.chain(tests.rglob("*.py"), APP_TESTS_DIR.rglob("*.py"))):
        if path.resolve() == _THIS_FILE:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        in_app = APP_TESTS_DIR in path.parents
        if any(m in text for m in _REACH_MARKERS) and (in_app or "code_review_sage" in text):
            files.append(path)
    return files


def _corpus_patched_names() -> dict[str, set[str]]:
    by_name: dict[str, set[str]] = {}
    for path in _corpus():
        for name in patched_names(path.read_text(encoding="utf-8")):
            by_name.setdefault(name, set()).add(path.name)
    return by_name


class TestPackage:
    def test_the_package_holds_exactly_the_composed_parts(self) -> None:
        assert {path.stem for path in PARTS_DIR.glob("*.py")} == {*PARTS, "__init__"}

    def test_the_package_init_holds_only_its_docstring(self) -> None:
        body = _tree(PARTS_DIR / "__init__.py").body
        assert len(body) == 1 and isinstance(body[0], ast.Expr)
        assert isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str)

    def test_the_facade_composes_every_part_once_through_the_shared_helper(self) -> None:
        tree = _tree(FACADE_PATH)
        helper = [
            node
            for node in tree.body
            if isinstance(node, ast.ImportFrom)
            and node.module == "kiro_crew._facade_compose"
            and [(a.name, a.asname) for a in node.names] == [("compose", "_facade_compose")]
        ]
        calls = [
            node.value
            for node in tree.body
            if isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Call)
            and ast.unparse(node.value.func) == "_facade_compose"
        ]
        assert len(helper) == 1 and len(calls) == 1
        args = calls[0].args
        assert ast.unparse(args[0]) == "globals()" and len(args) == 2 and not calls[0].keywords
        assert [ast.unparse(part) for part in args[1].elts] == [f"_parts.{p}" for p in PARTS]

    def test_the_facade_imports_its_parts_by_absolute_name(self) -> None:
        """A test's private copy of the facade has no parent package to be relative to."""
        relative = [
            ast.unparse(node)
            for node in _tree(FACADE_PATH).body
            if isinstance(node, ast.ImportFrom) and node.level
        ]
        assert relative == []


class TestComposedSurface:
    def test_every_part_keeps_the_units_the_split_moved_into_it(self) -> None:
        defined = _part_defined()
        assert {name: defined.get(name) for name in _MOVED} == _MOVED

    def test_the_facade_binds_every_name_a_part_defines(self) -> None:
        missing = []
        for name, stem in sorted(_part_defined().items()):
            ours, theirs = vars(facade).get(name), vars(_part(stem))[name]
            if isinstance(theirs, types.FunctionType):
                same = isinstance(ours, types.FunctionType) and ours.__code__ is theirs.__code__
            else:
                same = ours is theirs
            if not same:
                missing.append(name)
        assert missing == []

    def test_every_composed_function_runs_on_the_facade(self) -> None:
        functions = part_functions(vars(facade))
        assert set(functions) == {
            name
            for name, stem in _part_defined().items()
            if isinstance(vars(_part(stem))[name], types.FunctionType)
        }
        for name, fn in functions.items():
            assert fn.__globals__ is vars(facade), name
            assert fn.__module__ == FACADE, name
        assert len(functions) >= _MOVED_FUNCTIONS

    def test_a_part_function_carries_no_decorator_and_closes_over_nothing(self) -> None:
        for stem in PARTS:
            for node in _tree(PARTS_DIR / f"{stem}.py").body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    assert node.decorator_list == [], f"{stem}.{node.name}"
                    assert getattr(facade, node.name).__closure__ is None, f"{stem}.{node.name}"

    def test_the_wrapped_follow_up_handlers_stay_in_the_facade(self) -> None:
        for name in _KEPT_IN_FACADE:
            assert getattr(facade, name).__code__.co_filename == str(FACADE_PATH), name
        for name in _WRAPPED_IN_FACADE:
            handler = getattr(facade, name).__wrapped__
            assert handler.__code__.co_filename == str(FACADE_PATH), name
            assert handler.__globals__ is vars(facade), name


class TestBytecodeSweep:
    def test_every_global_a_composed_function_reads_is_bound_on_the_facade(self) -> None:
        functions = part_functions(vars(facade))
        assert len(functions) >= _MOVED_FUNCTIONS
        assert unbound_reads(functions) == {}

    def test_the_sweep_reports_a_planted_unbound_read_in_a_nested_function(self) -> None:
        code = compile(
            "def reads():\n"
            "    def nested():\n"
            "        return planted_name_nobody_binds\n"
            "    return nested\n"
            "def fine():\n"
            "    return _RUNS\n",
            "<planted_part>",
            "exec",
        )
        planted = {
            const.co_name: types.FunctionType(const, vars(facade))
            for const in code.co_consts
            if isinstance(const, types.CodeType)
        }
        assert unbound_reads(planted) == {"reads": ["planted_name_nobody_binds"]}


class TestPrivateCopies:
    """The app's tests load ``backend/routes.py`` by path, once per test."""

    def test_a_copy_composes_the_parts_onto_its_own_namespace(self) -> None:
        copy = _load_copy("sage_routes_contract_copy_one")
        functions = part_functions(vars(copy))
        assert set(functions) == set(part_functions(vars(facade)))
        for name, fn in functions.items():
            assert fn.__globals__ is vars(copy), name
            assert fn.__module__ == copy.__name__, name
        assert unbound_reads(functions) == {}

    def test_two_copies_share_no_state_and_leave_the_canonical_facade_alone(self) -> None:
        one = _load_copy("sage_routes_contract_copy_two_a")
        two = _load_copy("sage_routes_contract_copy_two_b")
        for name in sorted(_FRESH_STATE):
            assert getattr(one, name) is not getattr(two, name), name
            assert getattr(one, name) is not getattr(facade, name), name
        for name, fn in part_functions(vars(facade)).items():
            assert fn.__globals__ is vars(facade), name
            assert getattr(one, name) is not fn and getattr(two, name) is not fn, name

    def test_a_rebind_on_a_copy_reaches_a_moved_function_and_no_other_copy(self) -> None:
        one = _load_copy("sage_routes_contract_copy_three_a")
        two = _load_copy("sage_routes_contract_copy_three_b")
        run = {"run_id": "r1", "status": "done"}
        one._RUNS = [run]
        assert one._find_run("r1") is run
        assert two._find_run("r1") is None
        assert facade._find_run("r1") is not run

    def test_a_patched_function_on_a_copy_is_what_a_moved_caller_awaits(self) -> None:
        copy = _load_copy("sage_routes_contract_copy_four")
        saved: list[int] = []

        async def _fake_save() -> None:
            saved.append(len(copy._RUNS))

        copy._RUNS = []
        copy._save_runs = _fake_save
        asyncio.run(copy._record({"run_id": "r2", "status": "done"}))
        assert saved == [1]
        assert copy._RUNS[0]["run_id"] == "r2"

    def test_a_global_write_in_a_part_lands_on_the_copy(self, tmp_path, monkeypatch) -> None:
        copy = _load_copy("sage_routes_contract_copy_five")
        runs_file = tmp_path / "runs.json"
        runs_file.write_text('[{"run_id": "r3", "status": "done"}]', encoding="utf-8")
        monkeypatch.setattr(copy, "_runs_file", lambda: runs_file)
        before = facade._RUNS
        copy._RUNS = []
        copy._load_runs()
        assert [r["run_id"] for r in copy._RUNS] == ["r3"]
        assert facade._RUNS is before


class TestPatchReach:
    """Every name the test trees rebind on the routes module is one the composed code reads
    through the namespace it was rebound in."""

    def test_every_name_the_tests_patch_is_a_facade_global_the_parts_read_through_it(self) -> None:
        patched = _corpus_patched_names()
        assert {
            "_RUNS",
            "_save_runs",
            "_KNOWN_MODELS",
            "is_app_enabled",
            "_post_comments_bg",
        } <= set(patched)
        missing = sorted(name for name in patched if name not in vars(facade))
        assert missing == []
        readers = {
            (name, fn_name)
            for fn_name, fn in part_functions(vars(facade)).items()
            for name in global_reads(fn.__code__) & set(patched)
            if fn.__globals__ is not vars(facade)
        }
        assert readers == set()
        reached = {
            name
            for fn in part_functions(vars(facade)).values()
            for name in global_reads(fn.__code__) & set(patched)
        }
        assert {
            "_RUNS",
            "_save_runs",
            "_KNOWN_MODELS",
            "_write_runs",
            "_post_comments_bg",
        } <= reached

    def test_the_corpus_scan_reads_every_patch_shape_it_claims(self) -> None:
        planted = (
            "from kiro_crew.apps.builtins.code_review_sage.tests.backend_routes_helpers import (\n"
            "    _load_routes_module,\n"
            ")\n"
            "from kiro_crew.apps.builtins.code_review_sage.backend import routes as R\n"
            "def _routes():\n"
            "    return _load_routes_module()\n"
            "class T:\n"
            "    def setUp(self):\n"
            "        self.mod = _load_routes_module()\n"
            "        self.other = _routes()\n"
            "    def test(self, monkeypatch):\n"
            "        self.mod._RUNS = []\n"
            "        self.other._first = 1\n"
            "        monkeypatch.setattr(self.mod, '_second', None)\n"
            "        with unittest.mock.patch.object(R, '_third'):\n"
            "            pass\n"
            "        with mock.patch('kiro_crew.apps.builtins.code_review_sage.backend.routes"
            "._fourth.x'):\n"
            "            pass\n"
            "        self.mod.review_pool.get_pool = None\n"
            "        unrelated._fifth = 1\n"
        )
        assert patched_names(planted) == {"_RUNS", "_first", "_second", "_third", "_fourth"}

    def test_the_corpus_scan_reads_the_top_level_import_and_a_local_spec_loader(self) -> None:
        planted = (
            "import importlib.util\n"
            "from pathlib import Path\n"
            "from backend import routes\n"
            "import backend.routes as top\n"
            "_APP_ROOT = Path(__file__).resolve().parent.parent\n"
            '_ROUTES = _APP_ROOT / "backend" / "routes.py"\n'
            '_OTHER = _APP_ROOT / "sage_lib" / "report.py"\n'
            "def _load_routes():\n"
            '    spec = importlib.util.spec_from_file_location("x", str(_ROUTES))\n'
            "    mod = importlib.util.module_from_spec(spec)\n"
            "    spec.loader.exec_module(mod)\n"
            "    return mod\n"
            "def _load_other():\n"
            '    spec = importlib.util.spec_from_file_location("y", str(_OTHER))\n'
            "    return importlib.util.module_from_spec(spec)\n"
            "def test(monkeypatch):\n"
            "    r = _load_routes()\n"
            "    o = _load_other()\n"
            "    with mock.patch.object(routes, '_first'):\n"
            "        pass\n"
            "    top._second = 1\n"
            "    mock.patch.object(r, '_third', None)\n"
            "    o._not_the_routes = 1\n"
            "    for name in ('_computed',):\n"
            "        mock.patch.object(r, name, None)\n"
        )
        assert patched_names(planted) == {"_first", "_second", "_third"}

    def test_the_corpus_is_the_suites_that_reach_the_routes(self) -> None:
        names = {path.name for path in _corpus()}
        assert {
            "test_backend_routes.py",
            "test_run_endpoints.py",
            "test_owner_gate.py",
            "test_post_comments.py",
            "test_consolidate.py",
            "test_paste_pr_link.py",
            "test_no_auto_post.py",
            "test_sage_backend_routes_coverage.py",
        } <= names

    def test_leaf_test_scope_derives_this_file_as_a_corpus_gate(self, monkeypatch) -> None:
        """It reads the whole test tree, so a leaf-only diff must still run it."""
        root = source_corpus.repo_root()
        monkeypatch.syspath_prepend(str(root / "scripts"))
        leaf_test_scope = importlib.import_module("leaf_test_scope")
        assert _THIS_FILE.relative_to(root).as_posix() in leaf_test_scope.corpus_gates(root)


class TestPlacement:
    def test_nothing_but_the_facade_imports_a_part(self) -> None:
        importers = _part_importers(source_corpus.repo_root() / "src" / "kiro_crew")
        assert importers == ["apps/builtins/code_review_sage/backend/routes.py"]

    def test_the_importer_scan_reports_planted_importers(self, tmp_path) -> None:
        (tmp_path / "facade.py").write_text(f"from {PARTS_PACKAGE} import registry\n")
        (tmp_path / "direct.py").write_text(f"import {PARTS_PACKAGE}.repos\n")
        (tmp_path / "hidden.py").write_text(
            f"if False:\n    from {PARTS_PACKAGE}.settings import _valid_model\n"
        )
        (tmp_path / "unrelated.py").write_text("import json\n")
        assert _part_importers(tmp_path) == ["direct.py", "facade.py", "hidden.py"]

    def test_a_part_imports_project_names_only_for_the_type_checker(self) -> None:
        for stem in PARTS:
            for node in _tree(PARTS_DIR / f"{stem}.py").body:
                if _is_type_checking(node):
                    for sub in node.body:
                        assert isinstance(sub, ast.ImportFrom) and sub.module == FACADE, stem
                elif isinstance(node, ast.ImportFrom):
                    assert node.module in {"__future__", "typing"} or (
                        node.module.split(".")[0] in {"collections", "functools", "pathlib"}
                    ), (stem, node.module)
                elif isinstance(node, ast.Import):
                    assert all("." not in a.name and a.name != "kiro_crew" for a in node.names)

    def test_no_part_binds_the_facades_process_state_or_its_logger(self) -> None:
        assert _PROCESS_STATE <= set(vars(facade))
        for stem in PARTS:
            assert (_PROCESS_STATE | {"logger"}) & set(vars(_part(stem))) == set(), stem
            rebound = {
                name
                for node in ast.walk(_tree(PARTS_DIR / f"{stem}.py"))
                if isinstance(node, ast.Global)
                for name in node.names
            }
            assert rebound <= _PROCESS_STATE, stem

    def test_the_routes_log_under_the_facade_logger_name(self) -> None:
        assert facade.logger.name == LOGGER_NAME
        readers = [
            name
            for name, fn in part_functions(vars(facade)).items()
            if "logger" in global_reads(fn.__code__)
        ]
        assert readers and all(
            getattr(facade, n).__globals__["logger"] is facade.logger for n in readers
        )


class TestImportInventory:
    def test_every_import_the_facade_binds_is_read_by_the_family(self) -> None:
        source = FACADE_PATH.read_text(encoding="utf-8")
        family_reads = read_names(*_family_sources())
        assert unread_imports(source, _part_names(), family_reads) == []

    def test_the_inventory_check_reports_a_planted_import(self) -> None:
        """The F401 stand-in must be able to fail: an import nothing reads, and a part
        import of a name that part does not define, are both reported."""
        source = FACADE_PATH.read_text(encoding="utf-8")
        planted = source + (
            "\nimport shutil\nimport fnmatch\n"
            f"from {PARTS_PACKAGE}.registry import _handle_settings\n"
        )
        family_reads = read_names(*_family_sources(), "_uses = fnmatch.fnmatch\n")
        assert unread_imports(planted, _part_names(), family_reads) == [
            "shutil",
            f"{PARTS_PACKAGE}.registry._handle_settings",
        ]


def _part_importers(root: Path) -> list[str]:
    """Files under *root* that import the parts package or one of its modules."""
    importers = []
    for path in sorted(root.rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        if "routes_parts" not in text:
            continue
        for node in ast.walk(ast.parse(text)):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module, *(f"{node.module}.{a.name}" for a in node.names)]
            if any(m == PARTS_PACKAGE or m.startswith(PARTS_PACKAGE + ".") for m in modules):
                importers.append(path.relative_to(root).as_posix())
                break
    return importers
