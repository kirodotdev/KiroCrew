"""The update, log and status-stream handlers' facade keeps the patch seams the one module had.

``kiro_crew.dashboard.handlers.updates`` is a mirror facade (:mod:`kiro_crew._facade_mirror`)
over the ``kiro_crew.dashboard.handlers.updates_<role>`` owners and keeps no code of its own.
Callers, tests and documents still reach every name as
``kiro_crew.dashboard.handlers.updates.<name>``, and the moved code reads its names as plain
module globals, so a patch through the facade reaches that code only because the mirror writes
it into every module holding the name.

``test/test_facade_mirror_guards.py`` holds every mirrored facade to the shared rules (one
object per name, the manifest, owners never importing their facade, no shared global
rebound). These tests pin what is particular to this facade: every name the test trees patch
through it reaches every module holding it; the owners are exactly the ``updates_*.py``
siblings, loaded lowest layer first; each global a function rebinds lives in one owner with
every function that reads or rebinds it; the family logs under the facade's name; the moved
exception keeps the facade's module; and the facade still runs as ``python -m``.
"""

from __future__ import annotations

import ast
import dis
import functools
import importlib
import inspect
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import CodeType, ModuleType

import facade_mirror_scan
import pytest
import source_corpus

from kiro_crew import _facade_mirror
from kiro_crew.dashboard.handlers import updates
from kiro_crew.subprocess_utf8 import UTF8_TEXT

pytestmark = pytest.mark.xdist_group(name="tree_scan_updates_handlers_facade")

#: Checks ``test_facade_shared_checks.py`` runs against this module's helpers; each is
#: the same for every facade, so it is written once there and parametrized per module.
SHARED_FACADE_CHECKS = ("known_seams", "corpus_gate")

FACADE = "kiro_crew.dashboard.handlers.updates"
OWNER_PREFIX = "kiro_crew.dashboard.handlers.updates_"
HANDLERS_DIR = Path(updates.__file__).resolve().parent
_THIS_FILE = Path(__file__).resolve()

#: Names the test trees are known to patch through the facade, one of each seam the
#: handlers have: the release feed fetch the suite-wide floor refuses, the running version,
#: the update provider and install layout, the governance gate, the restart, the auto-update
#: effect and the check task state, the changelog cache and the log ring's installed flag.
#: The scan below must find all of them, so a scanner that stops matching goes red instead
#: of passing over nothing.
_KNOWN_SEAMS = frozenset(
    {
        "_fetch_feed_bytes",
        "_local_version",
        "resolve_provider",
        "detect_install_layout",
        "update_blocked_reason",
        "_restart_gateway",
        "_auto_effect",
        "_check_task",
        "_changelog_cache",
        "_log_ring_handler_installed",
    }
)

#: Every global a function of the family rebinds (``global name; name = ...``) -> the one
#: owner that binds it. A rebinding lands in one namespace only, so the name, its rebinders
#: and every function reading it share that owner, and the facade serves it from there.
_REBOUND_HOMES = {
    "_auto_effect": "updates_verdict",
    "_auto_effect_task": "updates_verdict",
    "_shape_effect": "updates_verdict",
    "_check_generation": "updates_check",
    "_last_update_check": "updates_check",
    "_check_task": "updates_check",
    "_check_task_generation": "updates_check",
    "_changelog_cache": "updates_channel",
    "_log_ring_handler_installed": "updates_streams",
    "_log_ring_handler": "updates_streams",
}

_GLOBAL_OPS = frozenset({"LOAD_GLOBAL", "STORE_GLOBAL", "DELETE_GLOBAL"})


def _index() -> _facade_mirror.MirrorIndex:
    return updates.__dict__[_facade_mirror.INDEX_ATTRIBUTE]


def _owner_paths() -> dict[str, Path]:
    return {
        f"{OWNER_PREFIX}{path.stem[len('updates_'):]}": path
        for path in sorted(HANDLERS_DIR.glob("updates_*.py"))
    }


def _family() -> list[ModuleType]:
    return [updates, *(sys.modules[name] for name in _index().owners)]


def _test_tree_files() -> list[Path]:
    root = source_corpus.repo_root()
    out = []
    for path in source_corpus.repo_files_named(".py"):
        rel = path.relative_to(root).as_posix()
        if rel == "conftest.py" or rel.startswith("test/") or "/tests/" in rel:
            out.append(path)
    return out


@functools.lru_cache(maxsize=1)
def _corpus_patched_names() -> frozenset[str]:
    """Every name the test trees patch through the facade, scanned once per worker."""
    found: set[str] = set()
    for path in _test_tree_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        if "handlers" not in text or "updates" not in text:
            continue
        names = facade_mirror_scan.patched_names(text, {FACADE: FACADE})
        found |= names.get(FACADE, set())
    return frozenset(found)


def _codes(code: CodeType) -> Iterator[CodeType]:
    yield code
    for const in code.co_consts:
        if isinstance(const, CodeType):
            yield from _codes(const)


def _global_names(code: CodeType) -> set[str]:
    return {
        instr.argval
        for each in _codes(code)
        for instr in dis.get_instructions(each)
        if instr.opname in _GLOBAL_OPS
    }


def _functions_of(module: ModuleType) -> Iterator[object]:
    """Every function and method the module defines, nested code included through _codes."""
    for value in vars(module).values():
        if inspect.isfunction(value) and value.__module__ == module.__name__:
            yield value
        elif inspect.isclass(value) and value.__module__ in (module.__name__, FACADE):
            if inspect.getsourcefile(value) != inspect.getsourcefile(module):
                continue
            for member in vars(value).values():
                if inspect.isfunction(member):
                    yield member


def test_a_facade_patch_of_every_scanned_name_reaches_every_module_holding_it() -> None:
    """The reach the unsplit module had: a write to ``updates.<name>`` was what every
    reader of that name read. Each module of the family that binds a patched name must
    see the patched object, and the undo must restore all of them."""
    family = _family()
    missed: dict[str, list[str]] = {}
    for name in sorted(_corpus_patched_names()):
        if not hasattr(updates, name):
            continue
        sentinel = object()
        holders = [module for module in family if name in vars(module)]
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(updates, name, sentinel)
            stale = [m.__name__ for m in holders if vars(m).get(name) is not sentinel]
            if getattr(updates, name) is not sentinel:
                stale.append(FACADE)
        if stale:
            missed[name] = stale
    assert missed == {}
    assert _index().divergent() == ()


def test_the_owners_are_every_updates_sibling_in_layer_order() -> None:
    """``_OWNERS`` lists every ``updates_*.py`` sibling, and each owner imports only
    owners listed before it -- the order the facade loads them and a read resolves in."""
    owners = list(_index().owners)
    assert sorted(owners) == sorted(_owner_paths())
    late: dict[str, list[str]] = {}
    for position, owner in enumerate(owners):
        tree = ast.parse(_owner_paths()[owner].read_text(encoding="utf-8"))
        imported = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        later = sorted(m for m in imported if m in owners and owners.index(m) >= position)
        if later:
            late[owner] = later
    assert late == {}


def test_the_facade_keeps_the_default_read_path_and_no_code_of_its_own() -> None:
    """Every name is served from the index, through the default read path, and the facade
    defines no function or class: new update code goes to the owner of its responsibility."""
    index = _index()
    assert index.forward_reads == "sys_modules_first"
    assert dict(index.forwards) == {}
    assert dict(index.served) == dict(index.exports)
    tree = ast.parse(Path(updates.__file__).read_text(encoding="utf-8"))
    defined = [
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    assert defined == []


def test_each_rebound_global_lives_in_one_owner_with_its_readers() -> None:
    """``global name; name = ...`` writes one namespace, so the owner that binds the name
    must also hold every function that reads or rebinds it, and the facade must not bind
    it. A read through the facade then sees the value every rebinder wrote."""
    family = _family()
    misplaced: dict[str, list[str]] = {}
    for name, stem in _REBOUND_HOMES.items():
        home = sys.modules[f"kiro_crew.dashboard.handlers.{stem}"]
        binders = sorted(m.__name__ for m in family if name in vars(m))
        if binders != [home.__name__]:
            misplaced[name] = [f"bound by {binders}"]
            continue
        assert getattr(updates, name) is vars(home)[name]
        readers = [
            f"{module.__name__}.{fn.__qualname__}"
            for module in family
            for fn in _functions_of(module)
            if name in _global_names(fn.__code__) and module is not home
        ]
        if readers:
            misplaced[name] = readers
    assert misplaced == {}


def test_the_rebound_scan_sees_the_known_rebinders() -> None:
    """Non-vacuity for the scan above: it finds the functions known to rebind each global."""
    known = {
        "_check_task": ("updates_check", "_do_update_check"),
        "_auto_effect": ("updates_verdict", "record_auto_update_effect"),
        "_changelog_cache": ("updates_channel", "_read_changelog"),
        "_log_ring_handler_installed": ("updates_streams", "install_log_ring_handler"),
    }
    for name, (stem, function) in known.items():
        fn = vars(sys.modules[f"kiro_crew.dashboard.handlers.{stem}"])[function]
        assert name in _global_names(fn.__code__), (name, function)


def _home_imports() -> dict[str, tuple[str, str]]:
    """Served names an owner imports from a project module outside the family."""
    family = set(_index().owners)
    found: dict[str, tuple[str, str]] = {}
    for path in _owner_paths().values():
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.ImportFrom) and node.module and not node.level:
                if not node.module.startswith("kiro_crew") or node.module in family:
                    continue
                for alias in node.names:
                    if f"{node.module}.{alias.name}" not in family:
                        found.setdefault(alias.asname or alias.name, (node.module, alias.name))
    return found


def _home_manifest() -> dict[str, tuple[str, str]]:
    """The facade's ``TYPE_CHECKING`` imports from outside the family: name -> (home, original)."""
    family = set(_index().owners)
    tree = ast.parse(Path(updates.__file__).read_text(encoding="utf-8"))
    out: dict[str, tuple[str, str]] = {}
    for node in tree.body:
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            for stmt in node.body:
                if isinstance(stmt, ast.ImportFrom) and stmt.module not in family:
                    for alias in stmt.names:
                        out[alias.asname or alias.name] = (stmt.module or "", alias.name)
    return out


def test_every_name_served_from_outside_the_family_is_visible_to_type_checkers() -> None:
    """An owner binds these by import, so the facade names each one from its home module
    under ``TYPE_CHECKING`` and ``from kiro_crew.dashboard.handlers.updates import
    resolve_provider`` type-checks."""
    served = set(_index().exports)
    expected = {name: home for name, home in _home_imports().items() if name in served}
    assert expected, "no served name comes from outside the family -- the scan went blind"
    assert _home_manifest() == expected


def test_the_family_logs_under_the_facade_name() -> None:
    """One logger, still named for the facade module, held by every module that logs."""
    assert updates.logger.name == FACADE
    others = [
        m.__name__ for m in _family() if vars(m).get("logger", updates.logger) is not updates.logger
    ]
    assert others == []


def test_the_moved_exception_keeps_the_facade_module() -> None:
    """``_PolicyApplyStopped`` is named in tracebacks by the path callers import it from,
    and that path still resolves it."""
    cls = updates._PolicyApplyStopped
    assert cls.__module__ == FACADE
    assert getattr(importlib.import_module(cls.__module__), cls.__qualname__) is cls


def test_the_facade_runs_as_a_module(tmp_path: Path) -> None:
    """``python -m kiro_crew.dashboard.handlers.updates`` imports cleanly: the mirror
    installs on ``__main__`` too, because every member binds one logger by name."""
    env = dict(os.environ)
    src = str(Path(updates.__file__).resolve().parents[3])
    env["PYTHONPATH"] = os.pathsep.join(p for p in (src, env.get("PYTHONPATH", "")) if p)
    out = subprocess.run(
        [sys.executable, "-m", FACADE],
        capture_output=True,
        check=False,
        cwd=tmp_path,
        env=env,
        timeout=90,
        **UTF8_TEXT,
    )
    assert out.returncode == 0, out.stdout + out.stderr


def test_the_test_tree_walk_reaches_this_file() -> None:
    walked = {path.resolve() for path in _test_tree_files()}
    assert _THIS_FILE in walked
    assert source_corpus.repo_root() / "test" / "test_dashboard_updates_coverage.py" in walked
