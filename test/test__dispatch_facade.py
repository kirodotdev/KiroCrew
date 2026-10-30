"""``kiro_crew.acp._dispatch`` keeps the patch seams and the surface the one module had.

``kiro_crew.acp._dispatch`` is a mirror facade (:mod:`kiro_crew._facade_mirror`) over the
owners in ``kiro_crew.acp._dispatch_parts``. ``AcpClient``, ``AcpRuntime``, the tests and
the documents still reach every name as ``kiro_crew.acp._dispatch.<name>``, and the moved
code reads its names as plain module globals, so a patch through the facade reaches that
code only because the mirror writes it into every module holding the name.

``test/test_facade_mirror_guards.py`` holds every mirror facade to the shared contract.
These tests pin what is particular to this one: every name the test trees patch through
it reaches every module holding it; the owners are exactly the ``_dispatch_parts``
modules, each importing only the owners before it; the family logs through one logger
named for the facade; every project name the facade imported before its owners moved out
stays bound on it; and every name it serves from outside the family is visible to a type
checker through its ``TYPE_CHECKING`` imports.
"""

from __future__ import annotations

import ast
import importlib
import json
import sys
from pathlib import Path
from types import ModuleType

import facade_mirror_scan
import pytest
import source_corpus

from kiro_crew import _facade_mirror
from kiro_crew.acp import _dispatch

pytestmark = pytest.mark.xdist_group(name="tree_scan__dispatch_facade")

#: Checks ``test_facade_shared_checks.py`` runs against this module's helpers; each is
#: the same for every facade, so it is written once there and parametrized per module.
SHARED_FACADE_CHECKS = ("known_seams",)

FACADE = "kiro_crew.acp._dispatch"
PARTS_DIR = Path(_dispatch.__file__).resolve().parent / "_dispatch_parts"
FACADE_MODULE = _dispatch
INVENTORY = source_corpus.repo_root() / "test" / "campaign_facade_inventories" / f"{FACADE}.json"

#: Names the test trees are known to patch through the facade, one per kind of seam: a
#: cap an owner defines and reads, a redaction function several owners import, and a
#: logging helper the facade keeps. The scan must find all of them, so a scanner that
#: stops matching goes red instead of passing over nothing.
_KNOWN_SEAMS = frozenset(
    {
        "TOOL_CALL_CACHE_MAX_PAYLOAD_CHARS",
        "redact_text",
        "_log_unrecognized_metadata_fields",
    }
)


def _index() -> _facade_mirror.MirrorIndex:
    return _dispatch.__dict__[_facade_mirror.INDEX_ATTRIBUTE]


def _family() -> list[ModuleType]:
    return [_dispatch, *(sys.modules[name] for name in _index().owners)]


def _owner_sources() -> dict[str, str]:
    return {
        owner: Path(sys.modules[owner].__file__ or "").read_text(encoding="utf-8")
        for owner in _index().owners
    }


def _test_tree_files() -> list[Path]:
    root = source_corpus.repo_root()
    found = []
    for path in source_corpus.repo_files_named(".py"):
        rel = path.relative_to(root).as_posix()
        if rel == "conftest.py" or rel.startswith("test/"):
            found.append(path)
        elif rel.startswith("src/kiro_crew/apps/builtins/") and (
            "/tests/" in rel or path.name.startswith("test_") or path.name == "conftest.py"
        ):
            found.append(path)
    return found


def _corpus_patched_names() -> set[str]:
    spellings = {FACADE: FACADE}
    names: set[str] = set()
    for path in _test_tree_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        if not facade_mirror_scan._mentions(text, spellings):
            continue
        names |= facade_mirror_scan.text_patched_names(text, spellings).get(FACADE, set())
        if facade_mirror_scan.needs_syntax_tree(text, spellings):
            try:
                events = facade_mirror_scan.patch_events(text, spellings)
            except SyntaxError:
                continue
            names |= facade_mirror_scan.names_by_target(events, {FACADE}).get(FACADE, set())
    return names


def test_a_facade_patch_of_every_scanned_name_reaches_every_module_holding_it() -> None:
    """The reach the one module had: a write to ``_dispatch.<name>`` was what every reader
    of the name read. Every member of the family that binds a patched name sees the
    patched object, and the undo leaves the family one object per name again."""
    family = _family()
    missed: dict[str, list[str]] = {}
    checked = 0
    for name in sorted(_corpus_patched_names()):
        if not hasattr(_dispatch, name):
            continue  # a test that creates the name with raising=False
        holders = [module for module in family if name in vars(module)]
        sentinel = object()
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(_dispatch, name, sentinel)
            stale = [m.__name__ for m in holders if vars(m).get(name) is not sentinel]
            if getattr(_dispatch, name) is not sentinel:
                stale.append(FACADE)
        checked += 1
        if stale:
            missed[name] = stale
    assert missed == {}
    assert checked >= len(_KNOWN_SEAMS)
    assert _index().divergent() == ()


def test_a_facade_patch_of_redact_text_reaches_every_owner_that_imports_it() -> None:
    """``redact_text`` is defined by one owner and imported by three more; a patch of
    ``_dispatch.redact_text`` must be what each of them calls, as in the one module."""
    holders = [m.__name__ for m in _family() if "redact_text" in vars(m)]
    assert len(holders) >= 4, holders
    sentinel = object()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(_dispatch, "redact_text", sentinel)
        assert [m for m in holders if vars(sys.modules[m])["redact_text"] is not sentinel] == []
    assert len({id(vars(sys.modules[m])["redact_text"]) for m in holders}) == 1


def test_the_owners_are_the_parts_package_in_layer_order() -> None:
    """``_OWNERS`` names every module of ``_dispatch_parts``, and each owner imports only
    owners listed before it: the order the facade loads them and a read resolves in."""
    owners = list(_index().owners)
    on_disk = {
        f"kiro_crew.acp._dispatch_parts.{p.stem}": p
        for p in PARTS_DIR.glob("*.py")
        if p.name != "__init__.py"
    }
    assert len(on_disk) == 7
    assert sorted(owners) == sorted(on_disk)
    late: dict[str, list[str]] = {}
    for position, owner in enumerate(owners):
        tree = ast.parse(on_disk[owner].read_text(encoding="utf-8"))
        imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        later = sorted(m for m in imported if m in owners and owners.index(m) >= position)
        if later:
            late[owner] = later
    assert late == {}


def test_the_facade_keeps_the_default_read_path_and_forwards_no_table() -> None:
    """``_dispatch.py`` was a plain module: the facade forwards no legacy table and keeps
    the default ``forward_reads``, and every served name comes from the index."""
    index = _index()
    assert index.forward_reads == "sys_modules_first"
    assert dict(index.forwards) == {}
    assert dict(index.served) == dict(index.exports)


def test_the_family_logs_through_one_logger_named_for_the_facade() -> None:
    """``caplog`` filters keyed on ``kiro_crew.acp._dispatch`` keep seeing the moved code."""
    assert _dispatch.logger.name == FACADE
    loggers = [m for m in _family() if "logger" in vars(m)]
    assert len(loggers) >= 4
    assert [m.__name__ for m in loggers if vars(m)["logger"] is not _dispatch.logger] == []


def _static_bindings(path: Path) -> set[str]:
    """Names a module binds at import time with a module-level ``import``, outside
    ``if TYPE_CHECKING:`` -- the bindings a type checker reads off the module."""
    found: set[str] = set()
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            found |= {(a.asname or a.name).split(".")[0] for a in node.names}
    return found


def _type_checking_bindings(path: Path) -> dict[str, tuple[str, str | None]]:
    """Name -> (module, attribute) a top-level ``if TYPE_CHECKING:`` block binds it from:
    the attribute is ``None`` for a plain ``import <module>``."""
    found: dict[str, tuple[str, str | None]] = {}
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            for sub in node.body:
                if isinstance(sub, ast.ImportFrom):
                    found.update(
                        {a.asname or a.name: (sub.module or "", a.name) for a in sub.names}
                    )
                elif isinstance(sub, ast.Import):
                    found.update({a.asname or a.name: (a.name, None) for a in sub.names})
    return found


def _home(value: object) -> str:
    if isinstance(value, ModuleType):
        return value.__name__
    return getattr(value, "__module__", "") or ""


def test_every_project_name_the_facade_imported_stays_bound_on_it() -> None:
    """A name the facade imported from the rest of ``kiro_crew`` before its owners moved
    out is bound on it still, statically and as its home module's object, so a reader that
    spells it through ``kiro_crew.acp._dispatch`` keeps type-checking."""
    rebound = json.loads(INVENTORY.read_text(encoding="utf-8"))["rebound"]
    assert "ACP_BACKENDS_META_IDENTITY" in rebound, "the inventory went blind"
    bound = _static_bindings(Path(_dispatch.__file__))
    assert sorted(name for name in rebound if name not in bound) == []
    for name, home in rebound.items():
        assert vars(_dispatch)[name] is getattr(sys.modules[home], name), name


def test_every_name_served_from_outside_the_family_is_visible_to_a_type_checker() -> None:
    """A name the mirror serves that no owner defines is an owner's import: it comes from
    outside the family, so the facade's ``TYPE_CHECKING`` imports name it, from the
    module that holds it, and none of them is a project module the facade stopped
    binding."""
    defined = {
        name
        for text in _owner_sources().values()
        for name in _facade_mirror._defined_names(ast.parse(text))
    }
    imported_by_owners = sorted(set(_index().exports) - defined)
    assert "difflib" in imported_by_owners, "no served name is an owner's import"
    manifest = _type_checking_bindings(Path(_dispatch.__file__))
    assert sorted(name for name in imported_by_owners if name not in manifest) == []
    wrong_home = []
    for name in imported_by_owners:
        module, attribute = manifest[name]
        home = importlib.import_module(module)
        if (home if attribute is None else getattr(home, attribute)) is not getattr(
            FACADE_MODULE, name
        ):
            wrong_home.append(name)
    assert wrong_home == []
    family = {FACADE, *_index().owners}
    served_from_outside = {
        name: _home(getattr(_dispatch, name))
        for name in imported_by_owners
        if _home(getattr(_dispatch, name)).startswith("kiro_crew")
        and _home(getattr(_dispatch, name)) not in family
    }
    assert served_from_outside == {}
