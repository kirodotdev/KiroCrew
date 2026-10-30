"""The deploy routes' facade keeps the patch seams and the import surface the one module had.

``kiro_crew.deploy.handlers`` is a mirror facade (:mod:`kiro_crew._facade_mirror`) over the
``kiro_crew.deploy.handlers_<role>`` owners beside it. The dashboard server, the webapp preview
handler, the artifact tools and tests still reach every name as
``kiro_crew.deploy.handlers.<name>``, and the moved code reads its names as plain module
globals, so a patch through the facade reaches that code only because the mirror writes it into
every module holding the name.

``test/test_facade_mirror_guards.py`` holds every mirrored facade to the shared rules (one object
per name, the manifest, owners never importing their facade, a sample of the patched names
reaching their readers). These tests pin what is particular to this facade: every name the test
trees patch through it reaches every module holding it; the owners are exactly the
``handlers_*.py`` siblings, in layer order; a name the facade serves from outside the family
stays visible to type checkers; the owner that logs logs under the facade's name; the moved
exception classes keep the facade's module; and the names the production importers take from
the facade are the objects their owners define.
"""

from __future__ import annotations

import ast
import functools
import importlib
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import facade_mirror_scan
import pytest
import source_corpus

from kiro_crew import _facade_mirror
from kiro_crew.deploy import handlers

pytestmark = pytest.mark.xdist_group(name="tree_scan_handlers_deploy_facade")

#: Checks ``test_facade_shared_checks.py`` runs against this module's helpers; each is
#: the same for every facade, so it is written once there and parametrized per module.
SHARED_FACADE_CHECKS = ("known_seams", "corpus_gate")

FACADE = "kiro_crew.deploy.handlers"
DEPLOY_DIR = Path(handlers.__file__).resolve().parent
_THIS_FILE = Path(__file__).resolve()

#: The owners, lowest layer first: each imports only owners listed before it.
_OWNERS = (
    "kiro_crew.deploy.handlers_guards",
    "kiro_crew.deploy.handlers_content",
    "kiro_crew.deploy.handlers_sites",
)

#: Names the test trees are known to patch through the facade, one of each seam the family
#: has: a flag every layer reads, functions several owners call, a constant one owner reads,
#: a name the family only imports and a stdlib module the moved code reads as a global. The
#: scan below must find all of them, so a scanner that stops matching goes red instead of
#: passing over nothing.
_KNOWN_SEAMS = frozenset(
    {
        "_HAS_ARTIFACTS",
        "_SCAN_SIZE_LIMIT",
        "_allowed_local_roots",
        "_audit",
        "_do_deploy",
        "_resolve_profile",
        "_staging_root",
        "config_dir",
        "get_default_store",
        "os",
        "validate_field",
    }
)


def _index() -> _facade_mirror.MirrorIndex:
    return vars(handlers)[_facade_mirror.INDEX_ATTRIBUTE]


def _family() -> list[ModuleType]:
    return [handlers, *(sys.modules[name] for name in _index().owners)]


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
        if "deploy" not in text or "handlers" not in text:
            continue
        names = facade_mirror_scan.patched_names(text, {FACADE: FACADE})
        found |= names.get(FACADE, set())
    return frozenset(found)


def test_a_facade_patch_of_every_scanned_name_reaches_every_module_holding_it() -> None:
    """The reach the unsplit module had: a write to ``handlers.<name>`` was what every reader
    of that name read. Each module of the family that binds a patched name must see the
    patched object, and the undo must restore all of them."""
    family = _family()
    missed: dict[str, list[str]] = {}
    for name in sorted(_corpus_patched_names()):
        if not hasattr(handlers, name):
            continue
        sentinel = object()
        holders = [module for module in family if name in vars(module)]
        before = {module.__name__: vars(module)[name] for module in holders}
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(handlers, name, sentinel)
            stale = [m.__name__ for m in holders if vars(m).get(name) is not sentinel]
            if getattr(handlers, name) is not sentinel:
                stale.append(FACADE)
        restored = {module.__name__: vars(module).get(name) for module in holders}
        if stale or any(restored[m] is not before[m] for m in before):
            missed[name] = stale or ["undo"]
    assert missed == {}
    assert _index().divergent() == ()


def test_a_facade_patch_of_the_audit_reaches_the_owner_that_denies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``handlers._audit`` is patched through the facade by the profile suites; the
    restricted-session refusal in ``handlers_guards`` reads it as a global of its own module,
    and so does ``handlers_sites``, and each sees the patch."""
    calls: list[tuple[str, str, str, str]] = []

    def _record(action: str, site_id: str, outcome: str, *, error: str = "") -> None:
        calls.append((action, site_id, outcome, error))

    monkeypatch.setattr(handlers, "_audit", _record)
    holders = _index().holders["_audit"]
    assert set(holders) == {"kiro_crew.deploy.handlers_guards", "kiro_crew.deploy.handlers_sites"}
    assert all(vars(sys.modules[owner])["_audit"] is _record for owner in holders)
    response = handlers._deny_restricted(SimpleNamespace(app=None), "deploy")
    assert response is not None and response.status == 403
    assert calls == [("deploy", "", "denied", "no app context")]


def test_the_owners_are_every_handlers_sibling_in_layer_order() -> None:
    """A ``handlers_*.py`` module the facade does not list would hold names a patch through
    the facade never reaches, so the sibling set and the owner tuple agree, and each owner
    imports only owners listed before it."""
    siblings = {f"kiro_crew.deploy.{path.stem}" for path in DEPLOY_DIR.glob("handlers_*.py")}
    assert set(_OWNERS) == siblings
    assert _index().owners == _OWNERS
    for position, owner in enumerate(_OWNERS):
        tree = ast.parse(Path(sys.modules[owner].__file__).read_text(encoding="utf-8"))
        imported = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module in _OWNERS
        }
        assert imported <= set(_OWNERS[:position]), owner


def _home_imports() -> dict[str, tuple[str, str]]:
    """Served names an owner imports from a project module outside the family."""
    found: dict[str, tuple[str, str]] = {}
    for owner in _OWNERS:
        tree = ast.parse(Path(sys.modules[owner].__file__).read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module and not node.level:
                if not node.module.startswith("kiro_crew") or node.module in _OWNERS:
                    continue
                for alias in node.names:
                    found.setdefault(alias.asname or alias.name, (node.module, alias.name))
    return found


def _home_manifest() -> dict[str, tuple[str, str]]:
    """The facade's ``TYPE_CHECKING`` imports from outside the family."""
    tree = ast.parse(Path(handlers.__file__).read_text(encoding="utf-8"))
    out: dict[str, tuple[str, str]] = {}
    for node in tree.body:
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            for stmt in node.body:
                if isinstance(stmt, ast.ImportFrom) and stmt.module not in _OWNERS:
                    for alias in stmt.names:
                        out[alias.asname or alias.name] = (stmt.module or "", alias.name)
    return out


def test_every_name_served_from_outside_the_family_is_visible_to_type_checkers() -> None:
    """An owner holds these and the facade serves them, so ``from kiro_crew.deploy.handlers
    import scan_content`` type-checks only because the facade names each one from its home
    under ``TYPE_CHECKING``; exactly those."""
    served = set(_index().exports)
    expected = {name: home for name, home in _home_imports().items() if name in served}
    assert expected, "no served name comes from outside the family -- the scan went blind"
    assert _home_manifest() == expected


def test_the_owner_that_logs_logs_under_the_facade_name() -> None:
    """One logger, still named for the facade module, held by the facade and every owner
    that logs."""
    assert handlers.logger.name == FACADE
    loggers = {
        owner: vars(sys.modules[owner])["logger"]
        for owner in _OWNERS
        if "logger" in vars(sys.modules[owner])
    }
    assert "kiro_crew.deploy.handlers_sites" in loggers
    assert all(found is handlers.logger for found in loggers.values())


@pytest.mark.parametrize("name", ["WebAppRootError", "_ProfileResolveError"])
def test_a_moved_exception_class_keeps_the_facade_module(name: str) -> None:
    """An error chain renders a raised class as ``module.qualname``; the owner the class moved
    into stays out of that text, so a traceback reads as it did before the split."""
    cls = getattr(handlers, name)
    assert issubclass(cls, Exception)
    assert cls.__module__ == FACADE
    assert cls.__qualname__ == name


def test_the_names_production_imports_from_the_facade_are_their_owners_objects() -> None:
    """``webapp_preview`` imports ``WebAppRootError`` and ``resolve_webapp_public_dir``, the
    artifact tools ``_redact_text``, and the dashboard server ``register_routes`` from this
    facade; each is the one object its owner defines, and a name the family only imports is
    served as its home's object."""
    content = importlib.import_module("kiro_crew.deploy.handlers_content")
    guards = importlib.import_module("kiro_crew.deploy.handlers_guards")
    paths = importlib.import_module("kiro_crew.config.paths")
    assert handlers.WebAppRootError is content.WebAppRootError
    assert handlers.resolve_webapp_public_dir is content.resolve_webapp_public_dir
    assert handlers._redact_text is guards._redact_text
    assert "register_routes" in vars(handlers)
    assert "config_dir" in _index().exports
    assert handlers.config_dir is paths.config_dir


def test_the_test_tree_walk_reaches_this_file() -> None:
    walked = {path.resolve() for path in _test_tree_files()}
    assert _THIS_FILE in walked
    assert source_corpus.repo_root() / "test" / "test_deploy_web_handlers.py" in walked
