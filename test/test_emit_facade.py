"""The session crew-log emitter facade keeps the patch seams the one module had.

``kiro_crew.crew_log.emit`` is a mirror facade (:mod:`kiro_crew._facade_mirror`) over the
``kiro_crew.crew_log.emit_<role>`` owners. Callers, tests and documents still reach every
name as ``kiro_crew.crew_log.emit.<name>``, and the moved code reads its names as plain
module globals, so a patch through the facade reaches that code only because the mirror
writes it into every module holding the name.

``test/test_facade_mirror_guards.py`` holds every mirrored facade to the shared rules (one
object per name, the manifest, owners never importing their facade, no shared global
rebound). These tests pin what is particular to this facade: every name the test trees
patch through it reaches every module holding it, and the scan finds the seams it is known
to have; the owners are exactly the ``emit_*.py`` siblings, layered lowest first; the
facade binds every name its owners define but the nine module globals they rebind; the
names served from outside the family stay visible to type checkers; the emitter keeps
logging under the facade's name; its refusal classes keep the facade's module name; and a
rebound slot written through the facade is the one the owner reads.
"""

from __future__ import annotations

import ast
import functools
import logging
import sys
from pathlib import Path
from types import ModuleType

import facade_mirror_scan
import pytest
import source_corpus

from kiro_crew import _facade_mirror
from kiro_crew.crew_log import emit

pytestmark = pytest.mark.xdist_group(name="tree_scan_emit_facade")

#: Checks ``test_facade_shared_checks.py`` runs against this module's helpers; each is
#: the same for every facade, so it is written once there and parametrized per module.
SHARED_FACADE_CHECKS = ("known_seams", "corpus_gate")

FACADE = "kiro_crew.crew_log.emit"
OWNER_PREFIX = "kiro_crew.crew_log.emit_"
CREW_LOG_DIR = Path(emit.__file__).resolve().parent
_THIS_FILE = Path(__file__).resolve()

#: The module globals a ``global`` statement rebinds. Each lives in the one owner that
#: rebinds it, beside every function reading it, and the facade serves it from there.
_REBOUND = (
    "_child_liveness",
    "_lost_child_origins",
    "_lost_origin_reported",
    "_next_limits",
    "_shutdown_hook_registered",
    "_subsystem",
    "_tracker_instance",
    "_warnings_instance",
    "_writer_instance",
)

#: Names the test trees are known to patch through the facade, one from each kind of seam
#: the emitter has: a helper several owners call, the switch, the retry schedule the
#: installed writer reads by name, a rebound slot, a mutable map, a constant, a name the
#: emitter only imports, and a public entry point. The scan below must find all of them, so
#: a scanner that stops matching goes red instead of passing over nothing.
_KNOWN_SEAMS = frozenset(
    {
        "_REPAIR_WALK_HOPS",
        "_child_liveness",
        "_growth_listeners",
        "_retry_delay",
        "_submit",
        "enabled",
        "redact_exfiltration_urls",
        "slot_previous_store",
    }
)


def _index() -> _facade_mirror.MirrorIndex:
    return emit.__dict__[_facade_mirror.INDEX_ATTRIBUTE]


def _family() -> list[ModuleType]:
    return [emit, *(sys.modules[name] for name in _index().owners)]


def _owner_paths() -> dict[str, Path]:
    return {
        f"{OWNER_PREFIX}{path.stem[len('emit_'):]}": path
        for path in sorted(CREW_LOG_DIR.glob("emit_*.py"))
    }


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
        if "crew_log" not in text or "emit" not in text:
            continue
        try:
            found |= facade_mirror_scan.patched_names(text, {FACADE: FACADE}).get(FACADE, set())
        except SyntaxError:
            continue
    return frozenset(found)


def test_the_test_tree_walk_reaches_this_file() -> None:
    walked = {path.resolve() for path in _test_tree_files()}
    assert _THIS_FILE in walked
    assert source_corpus.repo_root() / "test" / "test_crew_log_emit.py" in walked


def test_a_facade_patch_of_every_scanned_name_reaches_every_module_holding_it() -> None:
    """The reach the unsplit module had: a write to ``emit.<name>`` was what every
    reader of that name read. Each module of the family that binds a patched name must
    see the patched object, and the undo must restore all of them."""
    family = _family()
    missed: dict[str, list[str]] = {}
    for name in sorted(_corpus_patched_names()):
        if not hasattr(emit, name):
            continue
        sentinel = object()
        holders = [module for module in family if name in vars(module)]
        before = {module.__name__: vars(module)[name] for module in holders}
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(emit, name, sentinel)
            stale = [m.__name__ for m in holders if vars(m).get(name) is not sentinel]
            if getattr(emit, name) is not sentinel:
                stale.append(FACADE)
        restored = {module.__name__: vars(module).get(name) for module in holders}
        if stale or restored != before:
            missed[name] = stale or ["undo"]
    assert missed == {}
    assert _index().divergent() == ()


def test_the_owners_are_every_emit_module_in_layer_order() -> None:
    """``_OWNERS`` lists every ``emit_*.py`` sibling, and each owner imports only owners
    listed before it -- the order a read of a served name resolves in."""
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


def test_the_facade_binds_every_name_its_owners_define_but_the_rebound_globals() -> None:
    """``emit.py`` held its definitions in its own namespace, and still does: each name an
    owner defines is bound here too, so ``vars(emit)`` reads as it did and a
    ``mock.patch.object(emit, ...)`` of one of them is undone by one write, never by deleting
    the name from every owner first. The rebound globals are the exception, because a
    ``global`` statement in their owner rebinds them and a copy here would go stale; they
    are served instead."""
    defined = {
        name
        for path in _owner_paths().values()
        for name in _facade_mirror._defined_names(ast.parse(path.read_text(encoding="utf-8")))
    }
    assert defined, "no owner defines anything -- the scan read nothing"
    assert sorted(name for name in defined if name not in vars(emit)) == sorted(_REBOUND)
    assert sorted(name for name in _index().exports if name in defined) == sorted(_REBOUND)


def test_the_facade_keeps_the_default_read_path_and_forwards_no_table() -> None:
    """``emit.py`` was a plain module, so no reader ever fetched a name of it through
    ``importlib.import_module``: the facade forwards no legacy table and keeps the default
    ``forward_reads``, and every served name comes from the index."""
    index = _index()
    assert index.forward_reads == "sys_modules_first"
    assert dict(index.forwards) == {}
    assert dict(index.served) == dict(index.exports)


def _home_imports() -> dict[str, tuple[str, str]]:
    """Served names an owner imports from a project module outside the family."""
    found: dict[str, tuple[str, str]] = {}
    for path in _owner_paths().values():
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.ImportFrom) and node.module and not node.level:
                if not node.module.startswith("kiro_crew") or node.module in _owner_paths():
                    continue
                for alias in node.names:
                    found.setdefault(alias.asname or alias.name, (node.module, alias.name))
    return found


def _home_manifest() -> dict[str, tuple[str, str]]:
    """The facade's ``TYPE_CHECKING`` imports from outside the family: name -> (home, original)."""
    tree = ast.parse(Path(emit.__file__).read_text(encoding="utf-8"))
    out: dict[str, tuple[str, str]] = {}
    for node in tree.body:
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            for stmt in node.body:
                if isinstance(stmt, ast.ImportFrom) and stmt.module not in _owner_paths():
                    for alias in stmt.names:
                        out[alias.asname or alias.name] = (stmt.module or "", alias.name)
    return out


def test_every_name_served_from_outside_the_family_is_visible_to_type_checkers() -> None:
    """At BASE the emitter bound these by import, so ``from kiro_crew.crew_log.emit import
    crew_log_enabled`` type-checked. Now an owner holds them, so the facade names each one
    from its home module under ``TYPE_CHECKING``, and exactly those."""
    served = set(_index().exports)
    expected = {name: home for name, home in _home_imports().items() if name in served}
    assert expected, "no served name comes from outside the family -- the scan read nothing"
    assert _home_manifest() == expected


def test_the_emitter_logs_under_the_facade_name() -> None:
    """One logger, still named for the facade module, held by every module that logs."""
    assert emit.logger is logging.getLogger(FACADE)
    others = [
        m.__name__ for m in _family() if vars(m).get("logger", emit.logger) is not emit.logger
    ]
    assert others == []


@pytest.mark.parametrize(
    "name", ["WorkFieldError", "WorkFieldCollisionError", "WorkFieldTooDeepError"]
)
def test_a_refusal_class_keeps_the_facade_module_name(name: str) -> None:
    """A work-field refusal reaches tracebacks and refusal text under its class's module,
    which named the facade before the split and still does."""
    cls = getattr(emit, name)
    assert cls.__module__ == FACADE
    assert issubclass(cls, emit.WorkFieldError)


def test_a_rebound_slot_written_through_the_facade_is_the_one_the_runtime_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_child_liveness`` is rebound by ``set_child_liveness`` in its owner, and read there
    by the resume repair's probe. A write through either door is what the other reads: a
    copy bound on the facade would answer a stale probe."""
    assert "_child_liveness" not in vars(emit)
    monkeypatch.setattr(emit, "_child_liveness", None)
    assert emit._child_gone_probe("s-facade") is None

    def running(agent_id: str) -> bool:
        return True

    emit.set_child_liveness(running)
    assert emit._child_liveness is running
    gone = emit._child_gone_probe("s-facade")
    assert gone is not None and gone("a-child") is False
    monkeypatch.setattr(emit, "_child_liveness", None)
    assert emit._child_gone_probe("s-facade") is None
