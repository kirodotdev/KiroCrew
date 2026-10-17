"""``kiro_crew.taskq.adapters.runner`` stays the one import path of the runner adapters.

The lane, the claimed-row handle and the vocabulary every runner piece shares live in the
``runner_lane``, ``runner_handle`` and ``runner_vocab`` owners beside it. ``runner.py``
keeps the admission, the orphan-adoption sweep and the factory, and binds every name it
bound as one file. This file pins that plain-facade contract:

* the owners are exactly the ``runner_*`` siblings, listed lowest layer first in
  ``_RUNNER_OWNERS``; each imports only owners listed before it, none imports the facade
  at run time, and nothing else in ``src/`` imports an owner;
* an owner that logs does so under the ``kiro_crew.taskq.adapters.runner`` logger;
* the three refusals render under the facade path, and each class an owner defines
  carries its frozen ``__module__``;
* every module-level name the one-module file bound still resolves on the facade, to the
  object its owner holds, and the star export is the one-module file's;
* no name the test trees patch through the facade is read as a global by an owner: an
  owner reads its own namespace, so such a patch would not reach it.

Each structural check has a self-test that shows it can fail.
"""

from __future__ import annotations

import ast
import functools
import importlib
import sys
import traceback
from pathlib import Path
from types import ModuleType

import pytest
import source_corpus
from facade_mirror_scan import global_reads, patched_names

from kiro_crew.taskq.adapters import runner as runner_module

pytestmark = pytest.mark.xdist_group(name="tree_scan_runner_facade")

_FACADE = "kiro_crew.taskq.adapters.runner"
_PACKAGE = "kiro_crew.taskq.adapters"
_TEST_ROOT = Path(__file__).resolve().parent
_REPO_ROOT = _TEST_ROOT.parent

#: Every module-level name the one-module ``runner.py`` bound, less ``deque``: a
#: standard-library import only the lane reads, which the facade does not import.
_BASE_MODULE_NAMES = frozenset("""
    ADMITTED ADOPTED_RESUME_EVENT ANSWER_TRUNCATED Admitted AdoptReport Any Awaitable
    CANCELLED CLAIMABLE Callable DONE DependencyCoordinator DependencySignal FAILED
    INPUT_WAIT_ONLY Iterable KIND_TASKRUNNER_STEP KIND_WORKFLOW_AGENT L3_ACP_RUNTIME
    LANE_SYSTEM MODE_AIMD MODE_FIXED OWNER_TASKRUNNER OWNER_WORKFLOW PARAM_ACCEPTED_BY
    PARAM_LANE PARAM_SAFE_RETRY PARKED QUEUED RECOVERING RECOVERY_MAX_ATTEMPTS
    RUNNER_RECOVERY_ADAPTERS RUNNING RecoveryDecision RecoveryLadder RunnerAdmission
    RunnerAdmissionRefused RunnerLane RunnerLaneSelfBlocked RunnerTaskCancelled
    SIDE_EFFECT_IDEMPOTENT_KEY SIDE_EFFECT_NONE SIDE_EFFECT_UNKNOWN STARTING SYSTEM_LANE
    SYSTEM_SOURCES TASKRUNNER_ID_PREFIX TERMINAL TaskRecord TaskStore TaskStoreUnavailable
    UNCLAIMED UNKNOWN_SIDE_EFFECT WAITING WAITING_DEPENDENCY WAITING_INPUT
    WORKFLOW_ID_PREFIX WaitLedger WaitRecord _ADOPT_SCAN_PAGE _ADOPT_SCAN_PASSES
    _DEFAULT_ADMIT_WAIT_SECS _MAX_ADMIT_SLEEP_SECS _MAX_DURABLE_ANSWER_CHARS _NEVER_STARTED
    _answer_detail _orphaned_queued_rows adopt_orphaned_rows asyncio dataclass field
    is_safe_retry lane_for lane_key_for logger logging owner_of recovery_exhausted
    run_task_id runner_admission_for step_task_id time workflow_task_id
    """.split())

#: The one-module file's ``__all__``, which the facade keeps as it was.
_BASE_STAR_EXPORT = (
    "LANE_SYSTEM MODE_AIMD MODE_FIXED PARAM_ACCEPTED_BY PARAM_LANE PARAM_SAFE_RETRY"
    " RUNNER_RECOVERY_ADAPTERS SYSTEM_SOURCES WORKFLOW_ID_PREFIX AdoptReport Admitted"
    " RunnerAdmission RunnerAdmissionRefused RunnerLane RunnerTaskCancelled"
    " adopt_orphaned_rows is_safe_retry lane_for run_task_id runner_admission_for"
    " step_task_id workflow_task_id"
).split()


def _owners() -> list[ModuleType]:
    return [importlib.import_module(name) for name in runner_module._RUNNER_OWNERS]


def _tree(module: ModuleType) -> ast.Module:
    return ast.parse(Path(module.__file__).read_text(encoding="utf-8"))


def test_the_owners_are_every_runner_sibling_in_layer_order() -> None:
    adapters = Path(runner_module.__file__).resolve().parent
    siblings = {f"{_PACKAGE}.{path.stem}" for path in adapters.glob("runner_*.py")}
    assert set(runner_module._RUNNER_OWNERS) == siblings
    assert runner_module._RUNNER_OWNERS == (
        f"{_PACKAGE}.runner_vocab",
        f"{_PACKAGE}.runner_lane",
        f"{_PACKAGE}.runner_handle",
    )


# --------------------------------------------------------------------------- #
# Owners never import the facade, and import each other in one direction.
# --------------------------------------------------------------------------- #


def _runtime_imports(tree: ast.Module, package: str = _PACKAGE) -> set[str]:
    """Dotted modules *tree* imports outside ``if TYPE_CHECKING:``, a relative import
    resolved against *package* (the owners' package)."""
    typing_only: set[int] = set()
    for node in tree.body:
        if (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Name)
            and node.test.id == "TYPE_CHECKING"
        ):
            # Only the guarded body: an ``else:`` branch runs at import time.
            for stmt in node.body:
                typing_only.update(id(n) for n in ast.walk(stmt))
    found: set[str] = set()
    for node in ast.walk(tree):
        if id(node) in typing_only:
            continue
        if isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parent = package.rsplit(".", node.level - 1)[0] if node.level > 1 else package
                base = f"{parent}.{base}" if base else parent
            found.add(base)
            found.update(f"{base}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return found


def test_the_runtime_import_scan_can_fail() -> None:
    tree = ast.parse(
        "from typing import TYPE_CHECKING\n"
        "from kiro_crew.taskq.adapters import runner_vocab\n"
        "if TYPE_CHECKING:\n"
        "    from .runner import RunnerAdmission\n"
        "from .runner_lane import RunnerLane\n"
        "def f():\n"
        "    import kiro_crew.taskq.adapters.runner\n"
        "def g():\n"
        "    from . import runner\n"
    )
    found = _runtime_imports(tree)
    assert f"{_PACKAGE}.runner_vocab" in found
    assert f"{_PACKAGE}.runner_lane" in found  # a relative import, resolved
    assert _FACADE in found  # the function-local imports are run-time imports
    typing_only = ast.parse(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from .runner import RunnerAdmission\n"
    )
    assert _FACADE not in _runtime_imports(typing_only)
    with_else = ast.parse(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from .runner import RunnerAdmission\n"
        "else:\n"
        "    from . import runner\n"
    )
    assert _FACADE in _runtime_imports(with_else)


@pytest.mark.parametrize("owner", runner_module._RUNNER_OWNERS)
def test_no_owner_imports_the_facade_at_run_time(owner: str) -> None:
    found = _runtime_imports(_tree(importlib.import_module(owner)))
    assert sorted(name for name in found if name == _FACADE or name.startswith(_FACADE + ".")) == []


def test_each_owner_imports_only_owners_listed_before_it() -> None:
    owners = runner_module._RUNNER_OWNERS
    later = {
        owner: sorted(
            name
            for name in _runtime_imports(_tree(sys.modules[owner]))
            if name in owners and name not in owners[:i]
        )
        for i, owner in enumerate(owners)
    }
    assert later == {owner: [] for owner in owners}


def _module_and_package(src: Path, path: Path) -> tuple[str, str]:
    """The dotted module *path* is under *src*, and the package its relative imports
    resolve against (a package ``__init__`` is its own package)."""
    parts = path.relative_to(src).with_suffix("").parts
    if parts[-1] == "__init__":
        dotted = ".".join(parts[:-1])
        return dotted, dotted
    dotted = ".".join(parts)
    return dotted, dotted.rpartition(".")[0]


def _src_files(src: Path) -> list[Path]:
    """The ``.py`` files under *src*: the ones git sees for the checkout's own tree
    (``source_corpus``, so an ignored or nested copy never answers), a walk for a
    self-test's tree under ``tmp_path``."""
    root = source_corpus.repo_root().resolve()
    if src.resolve().is_relative_to(root):
        return sorted(p for p in source_corpus.repo_files_named(".py") if p.is_relative_to(src))
    return sorted(src.rglob("*.py"))


def _owner_importers(src: Path, owners: frozenset[str]) -> list[str]:
    """Files under *src* that import one of *owners* at all, the owners and facade aside."""
    found = []
    for path in _src_files(src):
        text = path.read_text(encoding="utf-8", errors="replace")
        if "runner_" not in text:
            continue
        module, package = _module_and_package(src, path)
        if module in owners or module == _FACADE:
            continue
        for node in ast.walk(ast.parse(text)):
            names: set[str] = set()
            if isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    parent = package.rsplit(".", node.level - 1)[0] if node.level > 1 else package
                    base = f"{parent}.{base}" if base else parent
                names = {base, *(f"{base}.{alias.name}" for alias in node.names)}
            elif isinstance(node, ast.Import):
                names = {alias.name for alias in node.names}
            if names & owners:
                found.append(path.relative_to(src).as_posix())
                break
    return found


def test_the_owner_importer_scan_can_fail(tmp_path: Path) -> None:
    pkg = tmp_path / "kiro_crew" / "taskq" / "adapters"
    pkg.mkdir(parents=True)
    (pkg / "other.py").write_text("from .runner_lane import RunnerLane\n", encoding="utf-8")
    (pkg / "__init__.py").write_text("from . import runner_vocab\n", encoding="utf-8")
    (pkg / "fine.py").write_text(
        "from kiro_crew.taskq.adapters.runner import RunnerLane\n", encoding="utf-8"
    )
    owners = frozenset({f"{_PACKAGE}.runner_lane", f"{_PACKAGE}.runner_vocab"})
    assert _owner_importers(tmp_path, owners) == [
        "kiro_crew/taskq/adapters/__init__.py",
        "kiro_crew/taskq/adapters/other.py",
    ]


def test_nothing_else_in_src_imports_an_owner() -> None:
    """``kiro_crew.taskq.adapters.runner`` is the one import path of the runner adapters."""
    src = Path(runner_module.__file__).resolve().parents[3]
    assert _owner_importers(src, frozenset(runner_module._RUNNER_OWNERS)) == []


# --------------------------------------------------------------------------- #
# Owners log under the facade's logger; the refusals keep the facade's name.
# --------------------------------------------------------------------------- #


def _foreign_loggers(tree: ast.Module) -> list[str]:
    """Each ``getLogger(...)`` call in *tree* whose argument is not the facade's name."""
    found = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and (getattr(node.func, "attr", None) or getattr(node.func, "id", None)) == "getLogger"
        ):
            arg = node.args[0] if node.args else None
            if not (isinstance(arg, ast.Constant) and arg.value == _FACADE):
                found.append(f"{node.lineno}: {ast.unparse(node)}")
    return sorted(found, key=lambda entry: int(entry.split(":")[0]))


def test_the_logger_scan_can_fail() -> None:
    tree = ast.parse(
        "import logging\n"
        "logger = logging.getLogger('kiro_crew.taskq.adapters.runner')\n"
        "_log = logging.getLogger(__name__)\n"
        "from logging import getLogger\n"
        "other = getLogger('kiro_crew.taskq.adapters.runner_lane')\n"
    )
    assert [line.split(":")[0] for line in _foreign_loggers(tree)] == ["3", "5"]


@pytest.mark.parametrize("owner", runner_module._RUNNER_OWNERS)
def test_an_owner_that_logs_uses_the_runner_logger(owner: str) -> None:
    """The runner logs under ``kiro_crew.taskq.adapters.runner``, which log captures and
    filters name; an owner with a logger of its own would log where none of them look."""
    module = importlib.import_module(owner)
    assert _foreign_loggers(_tree(module)) == []
    assert getattr(module, "logger", runner_module.logger) is runner_module.logger
    assert runner_module.logger.name == _FACADE


#: Every class an owner defines -> the ``__module__`` it carries. The refusals keep the
#: facade's name, because a traceback and an error chain render a raised class as
#: ``module.qualname``.
_CLASS_MODULES = {
    "Admitted": f"{_PACKAGE}.runner_handle",
    "RunnerAdmissionRefused": _FACADE,
    "RunnerLane": f"{_PACKAGE}.runner_lane",
    "RunnerLaneSelfBlocked": _FACADE,
    "RunnerTaskCancelled": _FACADE,
}


def test_each_class_an_owner_defines_carries_its_frozen_module() -> None:
    found = {
        node.name: vars(module)[node.name].__module__
        for module in _owners()
        for node in _tree(module).body
        if isinstance(node, ast.ClassDef)
    }
    assert found == _CLASS_MODULES


@pytest.mark.parametrize(
    "name", ["RunnerAdmissionRefused", "RunnerLaneSelfBlocked", "RunnerTaskCancelled"]
)
def test_a_refusal_renders_under_the_facade_path(name: str) -> None:
    refusal = getattr(runner_module, name)("no slot")
    text = "".join(traceback.format_exception_only(refusal))
    assert text == f"{_FACADE}.{name}: no slot\n"


# --------------------------------------------------------------------------- #
# The facade keeps every name the one-module file bound.
# --------------------------------------------------------------------------- #


def test_every_base_module_name_still_resolves_on_the_facade() -> None:
    assert sorted(name for name in _BASE_MODULE_NAMES if not hasattr(runner_module, name)) == []


@pytest.mark.parametrize("owner", runner_module._RUNNER_OWNERS)
def test_a_module_name_an_owner_binds_is_the_same_object_on_the_facade(owner: str) -> None:
    """A moved definition, or an import both bind, is one object in both namespaces."""
    module = importlib.import_module(owner)
    shared = sorted(_BASE_MODULE_NAMES & set(vars(module)))
    assert shared
    assert [name for name in shared if getattr(runner_module, name) is not vars(module)[name]] == []


def test_the_star_export_is_the_one_module_files() -> None:
    assert runner_module.__all__ == _BASE_STAR_EXPORT
    assert [name for name in runner_module.__all__ if not hasattr(runner_module, name)] == []


# --------------------------------------------------------------------------- #
# No name a test patches through the facade is read by an owner.
# --------------------------------------------------------------------------- #


def _test_tree_files() -> list[Path]:
    """Every file pytest collects tests or fixtures from, as git sees the checkout: the
    root conftest, ``test/``, and the test modules and conftests under the builtin apps.
    """
    root = source_corpus.repo_root()
    test_root = root / "test"
    files = []
    for path in source_corpus.repo_files_named(".py"):
        rel = path.relative_to(root).as_posix()
        name = path.name
        if rel == "conftest.py" or path.is_relative_to(test_root):
            files.append(path)
        elif rel.startswith("src/kiro_crew/apps/builtins/") and (
            "/tests/" in rel
            or name.startswith("test_")
            or name.endswith("_test.py")
            or name == "conftest.py"
        ):
            files.append(path)
    return sorted(files)


def _facade_spellings(src: Path) -> dict[str, str]:
    """The facade's own name, and every src module's module-level binding of it: a patch
    through either reaches the facade."""
    spellings = {_FACADE: _FACADE}
    for path in _src_files(src):
        text = path.read_text(encoding="utf-8", errors="replace")
        # A binding of the facade names ``adapters``, or sits in the package as ``. runner``.
        if "adapters" not in text and not ("runner" in text and path.parent.name == "adapters"):
            continue
        module, package = _module_and_package(src, path)
        for node in ast.parse(text).body:
            if isinstance(node, ast.ImportFrom) and (node.module is not None or node.level):
                base = node.module or ""
                if node.level:
                    parent = package.rsplit(".", node.level - 1)[0] if node.level > 1 else package
                    base = f"{parent}.{base}" if base else parent
                for alias in node.names:
                    if f"{base}.{alias.name}" == _FACADE and module != _FACADE:
                        spellings[f"{module}.{alias.asname or alias.name}"] = _FACADE
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == _FACADE and alias.asname:
                        spellings[f"{module}.{alias.asname}"] = _FACADE
    return spellings


def test_the_spelling_scan_sees_an_aliased_binding(tmp_path: Path) -> None:
    pkg = tmp_path / "kiro_crew" / "dashboard"
    pkg.mkdir(parents=True)
    (pkg / "tasks.py").write_text(
        "from kiro_crew.taskq.adapters import runner as taskq_runner\n", encoding="utf-8"
    )
    adapters = tmp_path / "kiro_crew" / "taskq" / "adapters"
    adapters.mkdir(parents=True)
    (adapters / "rel.py").write_text("from . import runner\n", encoding="utf-8")
    assert _facade_spellings(tmp_path) == {
        _FACADE: _FACADE,
        "kiro_crew.dashboard.tasks.taskq_runner": _FACADE,
        "kiro_crew.taskq.adapters.rel.runner": _FACADE,
    }


def _patched_through_the_facade(files: list[Path], spellings: dict[str, str]) -> set[str]:
    names: set[str] = set()
    # A test that imports the facade itself names ``adapters``, and a patch through another
    # module's binding of it names that binding (``tasks_mod.taskq_runner``) whichever way
    # the test imported the module that holds it.
    needles = {"adapters"} | {s.rpartition(".")[2] for s in spellings if s != _FACADE}
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        if not any(needle in text for needle in needles):
            continue
        try:
            found = patched_names(text, spellings)
        except SyntaxError:
            continue
        names |= found.get(_FACADE, set())
    return names


@functools.lru_cache(maxsize=1)
def _corpus_patched_names() -> frozenset[str]:
    """Every name the test trees patch through any spelling of the facade, read once."""
    spellings = _facade_spellings(_REPO_ROOT / "src")
    return frozenset(_patched_through_the_facade(_test_tree_files(), spellings))


def _owner_reads() -> dict[str, set[str]]:
    return {
        owner.__name__: global_reads(
            Path(owner.__file__).read_text(encoding="utf-8"), owner.__file__
        )
        for owner in _owners()
    }


def _reach_gaps(patched: set[str], reads: dict[str, set[str]]) -> list[str]:
    return sorted(f"{owner}.{name}" for owner, names in reads.items() for name in patched & names)


def test_the_test_tree_walk_reaches_every_collected_tree() -> None:
    files = _test_tree_files()
    assert Path(__file__).resolve() in files
    assert _REPO_ROOT / "conftest.py" in files
    assert any("/apps/builtins/" in path.as_posix() for path in files)
    assert len(files) > 1000


def test_the_patch_scan_sees_the_facade_patches_it_must() -> None:
    # test_taskq_runner_adapter.py pages the adoption sweep through _ADOPT_SCAN_PAGE, and
    # test_taskrunner_taskq.py counts adopt_orphaned_rows calls; both are read by kept code.
    assert {"_ADOPT_SCAN_PAGE", "adopt_orphaned_rows"} <= _corpus_patched_names()


def test_no_name_patched_through_the_facade_is_read_by_an_owner() -> None:
    assert _reach_gaps(set(_corpus_patched_names()), _owner_reads()) == []


def test_the_reach_scan_can_fail(tmp_path: Path) -> None:
    probe = tmp_path / "test_probe.py"
    probe.write_text(
        "from kiro_crew.taskq.adapters import runner as runner_mod\n"
        "def test_x(monkeypatch):\n"
        "    monkeypatch.setattr(runner_mod, 'MODE_FIXED', 'pinned')\n"
        "def test_y():\n"
        "    from unittest import mock\n"
        "    with mock.patch('kiro_crew.taskq.adapters.runner.TaskStoreUnavailable', OSError):\n"
        "        pass\n",
        encoding="utf-8",
    )
    via_handler = tmp_path / "test_probe_handler.py"
    via_handler.write_text(
        "from kiro_crew.dashboard.handlers import tasks as tasks_mod\n"
        "def test_z(monkeypatch):\n"
        "    monkeypatch.setattr(tasks_mod.taskq_runner, 'deque', list)\n",
        encoding="utf-8",
    )
    spellings = _facade_spellings(_REPO_ROOT / "src")
    assert "kiro_crew.dashboard.handlers.tasks.taskq_runner" in spellings
    patched = _patched_through_the_facade([probe, via_handler], spellings)
    assert patched == {"MODE_FIXED", "TaskStoreUnavailable", "deque"}
    gaps = _reach_gaps(patched, _owner_reads())
    assert f"{_PACKAGE}.runner_lane.MODE_FIXED" in gaps
    assert f"{_PACKAGE}.runner_handle.TaskStoreUnavailable" in gaps
    assert f"{_PACKAGE}.runner_lane.deque" in gaps


def test_leaf_test_scope_derives_this_file_as_a_corpus_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It reads the whole test tree, so a leaf-only diff must still run it."""
    monkeypatch.syspath_prepend(str(_REPO_ROOT / "scripts"))
    leaf_test_scope = importlib.import_module("leaf_test_scope")
    rel = Path(__file__).resolve().relative_to(_REPO_ROOT).as_posix()
    assert rel in leaf_test_scope.corpus_gates(_REPO_ROOT)
