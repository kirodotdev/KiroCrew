"""The directive application facade keeps the patch seams the one module had.

``kiro_crew.dashboard.session_directive_apply`` is a mirror facade (:mod:`kiro_crew._facade_mirror`) over the
``kiro_crew.dashboard.session_directive_apply_<role>`` owners, and keeps the dispatcher itself. Callers, tests and
documents still reach every name as ``kiro_crew.dashboard.session_directive_apply.<name>``, and the moved code
reads its names as plain module globals, so a patch through the facade reaches that code
only because the mirror writes it into every module holding the name.

``test/test_facade_mirror_guards.py`` holds every mirrored facade to the shared rules.
These tests pin what is particular to this facade: every name the test trees patch
through it reaches every module holding it; the owners are exactly the ``session_directive_apply_*.py``
siblings, loaded lowest layer first; the code the facade keeps stays bound on it; the
names it serves from outside the family are visible to type checkers; the family logs
under the facade's name; the moved classes keep the facade's module; and the facade
still runs as ``python -m``.
"""

from __future__ import annotations

import ast
import functools
import importlib
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import facade_mirror_scan
import pytest
import source_corpus

from kiro_crew import _facade_mirror
from kiro_crew.dashboard import session_directive_apply
from kiro_crew.subprocess_utf8 import UTF8_TEXT

pytestmark = pytest.mark.xdist_group(name="tree_scan_session_directive_apply_facade")

#: Checks ``test_facade_shared_checks.py`` runs against this module's helpers; each is
#: the same for every facade, so it is written once there and parametrized per module.
SHARED_FACADE_CHECKS = ("known_seams", "corpus_gate")

FACADE = "kiro_crew.dashboard.session_directive_apply"
OWNER_PREFIX = "kiro_crew.dashboard.session_directive_apply_"
DASHBOARD_DIR = Path(session_directive_apply.__file__).resolve().parent
_THIS_FILE = Path(__file__).resolve()

#: Names the test trees are known to patch through the facade, one of each seam it has:
#: the dispatcher's audit and the dispatcher itself (both kept here), the monitor binding (monitor) and the user-surface test (outcome). The scan below must find all of them, so a scanner that stops
#: matching goes red instead of passing over nothing.
_KNOWN_SEAMS = frozenset(
    {
        "_audit",
        "_binding",
        "_has_user_surface",
        "apply_session_directive_outcome",
    }
)

#: The code the facade keeps for itself: bound on it, never served from an owner.
_KEPT = frozenset(
    {
        "logger",
        "_audit",
        "apply_session_directive",
        "_outcome",
        "apply_session_directive_outcome",
        "_origin_arm_refusal",
    }
)

#: Moved classes whose module shows in tracebacks and reprs: they keep the facade's.
_FACADE_MODULE_CLASSES = ("_DirectiveDenied",)


def _index() -> _facade_mirror.MirrorIndex:
    return session_directive_apply.__dict__[_facade_mirror.INDEX_ATTRIBUTE]


def _owner_paths() -> dict[str, Path]:
    return {
        f"{OWNER_PREFIX}{path.stem[len('session_directive_apply_'):]}": path
        for path in sorted(DASHBOARD_DIR.glob("session_directive_apply_*.py"))
    }


def _family() -> list[ModuleType]:
    return [session_directive_apply, *(sys.modules[name] for name in _index().owners)]


def _test_tree_files() -> list[Path]:
    root = source_corpus.repo_root()
    tests = root / "test"
    out = []
    for path in source_corpus.repo_files_named(".py"):
        rel = path.relative_to(root).as_posix()
        if rel == "conftest.py" or path.is_relative_to(tests) or "/tests/" in rel:
            out.append(path)
    return out


@functools.lru_cache(maxsize=1)
def _corpus_patched_names() -> frozenset[str]:
    """Every name the test trees patch through the facade, scanned once per worker."""
    found: set[str] = set()
    for path in _test_tree_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        if "session_directive_apply" not in text:
            continue
        names = facade_mirror_scan.patched_names(text, {FACADE: FACADE})
        found |= names.get(FACADE, set())
    return frozenset(found)


def test_a_facade_patch_of_every_scanned_name_reaches_every_module_holding_it() -> None:
    """The reach the unsplit module had: a write to ``session_directive_apply.<name>`` was what every
    reader of that name read. Each module of the family that binds a patched name must
    see the patched object, and the undo must restore all of them."""
    family = _family()
    missed: dict[str, list[str]] = {}
    for name in sorted(_corpus_patched_names()):
        if not hasattr(session_directive_apply, name):
            continue
        sentinel = object()
        holders = [module for module in family if name in vars(module)]
        originals = {module.__name__: vars(module)[name] for module in holders}
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(session_directive_apply, name, sentinel)
            stale = [m.__name__ for m in holders if vars(m).get(name) is not sentinel]
            if getattr(session_directive_apply, name) is not sentinel:
                stale.append(FACADE)
        stale += [
            f"{m.__name__} (undo)"
            for m in holders
            if vars(m).get(name) is not originals[m.__name__]
        ]
        if stale:
            missed[name] = stale
    assert missed == {}
    assert _index().divergent() == ()


def test_the_owners_are_every_session_directive_apply_sibling_in_layer_order() -> None:
    """``_OWNERS`` lists every ``session_directive_apply_*.py`` sibling, and each owner imports only
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


def test_the_facade_keeps_the_default_read_path_and_forwards_no_table() -> None:
    """``session_directive_apply.py`` was a plain module, so no reader ever fetched a name of it through
    ``importlib.import_module``: the facade forwards no legacy table and keeps the default
    ``forward_reads``, and every served name comes from the index."""
    index = _index()
    assert index.forward_reads == "sys_modules_first"
    assert dict(index.forwards) == {}
    assert dict(index.served) == dict(index.exports)


def test_the_facade_binds_the_code_it_keeps_for_itself() -> None:
    """The kept code stays defined in ``session_directive_apply.py``: bound there, served by no owner."""
    assert sorted(_KEPT - set(vars(session_directive_apply))) == []
    assert sorted(_KEPT & set(_index().exports)) == []
    defined = {
        getattr(vars(session_directive_apply)[name], "__module__", FACADE)
        for name in _KEPT
        if name != "logger"
    }
    assert defined == {FACADE}


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
    tree = ast.parse(Path(session_directive_apply.__file__).read_text(encoding="utf-8"))
    out: dict[str, tuple[str, str]] = {}
    for node in tree.body:
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            for stmt in node.body:
                if isinstance(stmt, ast.ImportFrom) and stmt.module not in family:
                    for alias in stmt.names:
                        out[alias.asname or alias.name] = (stmt.module or "", alias.name)
    return out


def test_every_name_served_from_outside_the_family_is_visible_to_type_checkers() -> None:
    """At BASE the module bound these by import, so ``from kiro_crew.dashboard.session_directive_apply import
    <name>`` type-checked. Now an owner holds them, so the facade names each one from its
    home module under ``TYPE_CHECKING``, and exactly those."""
    served = set(_index().exports)
    expected = {name: home for name, home in _home_imports().items() if name in served}
    assert expected, "no served name comes from outside the family -- the scan went blind"
    assert _home_manifest() == expected


def test_the_family_logs_under_the_facade_name() -> None:
    """One logger, still named for the facade module, held by every module that logs."""
    assert session_directive_apply.logger.name == FACADE
    others = [
        m.__name__
        for m in _family()
        if vars(m).get("logger", session_directive_apply.logger)
        is not session_directive_apply.logger
    ]
    assert others == []


def test_the_moved_classes_keep_the_facade_module() -> None:
    """A moved class still names the facade as its module, and that name resolves to it."""
    for name in _FACADE_MODULE_CLASSES:
        cls = getattr(session_directive_apply, name)
        assert cls.__module__ == FACADE, name
        assert getattr(importlib.import_module(cls.__module__), cls.__qualname__) is cls


def test_the_facade_runs_as_a_module(tmp_path: Path) -> None:
    """``python -m kiro_crew.dashboard.session_directive_apply`` imported cleanly before the split; the mirror
    installs on ``__main__`` too, because every member binds one logger by name."""
    env = dict(os.environ)
    src = str(Path(session_directive_apply.__file__).resolve().parents[2])
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
    assert source_corpus.repo_root() / "test" / "test_monitor_directive_apply.py" in walked
