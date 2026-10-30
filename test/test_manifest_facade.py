"""The app manifest's facade keeps the seams the one module had.

``kiro_crew.apps.manifest`` is a plain re-export facade over the
``kiro_crew.apps.manifest_<role>`` owners: it imports every name they define, so callers,
tests and documents still reach each one as ``kiro_crew.apps.manifest.<name>`` and get
the owner's own object. A re-export is a binding, so a patch through the facade reaches
the facade's own readers and nothing else. These tests pin that this is enough: no owner
reads or binds a name the test trees patch through the facade (today the only patch goes
through ``manifest.sys``, the shared ``sys`` module); the owners are exactly the
``manifest_*.py`` siblings, layered, with no owner importing the facade; the
code the facade keeps stays defined there; and every owner keeps postponed annotations.
"""

from __future__ import annotations

import ast
import functools
import importlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import facade_mirror_scan
import pytest
import source_corpus

from kiro_crew.apps import manifest

pytestmark = pytest.mark.xdist_group(name="tree_scan_manifest_facade")

#: Checks ``test_facade_shared_checks.py`` runs against this module's helpers; each is
#: the same for every facade, so it is written once there and parametrized per module.
SHARED_FACADE_CHECKS = ("corpus_gate",)

FACADE = "kiro_crew.apps.manifest"
#: Every owner and the owners it may import; none may import the facade.
LAYERS: dict[str, frozenset[str]] = {
    "kiro_crew.apps.manifest_names": frozenset(),
    "kiro_crew.apps.manifest_containment": frozenset(),
    "kiro_crew.apps.manifest_sections": frozenset({"kiro_crew.apps.manifest_names"}),
    "kiro_crew.apps.manifest_contributes": frozenset(),
}
OWNERS = (
    "kiro_crew.apps.manifest_names",
    "kiro_crew.apps.manifest_containment",
    "kiro_crew.apps.manifest_sections",
    "kiro_crew.apps.manifest_contributes",
)
APPS_DIR = Path(manifest.__file__).resolve().parent
_THIS_FILE = Path(__file__).resolve()
#: The ``from __future__ import annotations`` feature, as a module binds it.
_POSTPONED = importlib.import_module("__future__").annotations

#: What the facade defines for itself rather than imports from an owner.
_KEPT = frozenset(
    {
        "CORE_APP_ROUTE_SEGMENTS",
        "_SEGMENT_BOUNDARY_RE",
        "_ENDPOINT_ALLOWED_RE",
        "app_endpoint_allowed",
        "_KNOWN_FIELDS",
        "AppManifest",
        "SCRIPT_ENTRY_POINT_SUFFIXES",
        "NODE_ENTRY_POINT_SUFFIXES",
        "SHELL_ENTRY_POINT_SUFFIX",
        "spawn_launches_entry_point_as_python",
        "is_module_style_entry_point",
        "has_stdio_mcp_server",
        "file_entry_point_refusal",
        "runtime_provisions_requirements",
        "REQUIREMENTS_TXT_MAX_BYTES",
        "requirements_in_tree",
    }
)


def _owners() -> list[ModuleType]:
    return [importlib.import_module(name) for name in OWNERS]


def _source(module: ModuleType) -> str:
    return Path(module.__file__).read_text(encoding="utf-8")


def _defined(source: str) -> set[str]:
    """Top-level names *source* binds by definition or assignment (not by import)."""
    names: set[str] = set()
    for node in ast.parse(source).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _imports(source: str) -> set[str]:
    """Every module *source* imports, anywhere in its body, relative imports resolved."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                module = importlib.util.resolve_name("." * node.level + module, "kiro_crew.apps")
            found.add(module)
            found.update(f"{module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return found


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
        # Every spelling of the facade, merged ``from kiro_crew.apps import a, manifest``
        # imports included, names the leaf, so this skips only files that cannot patch it.
        if "manifest" not in text:
            continue
        found |= facade_mirror_scan.patched_names(text, {FACADE: FACADE}).get(FACADE, set())
    return frozenset(found)


def test_the_patch_scan_reads_a_patch_through_the_facade() -> None:
    """Non-vacuity: no test patches a name through this facade today, so the scan is
    shown on a planted file to find both spellings a later test would use."""
    planted = (
        "from kiro_crew.apps import manifest\n"
        "def test_x(monkeypatch):\n"
        "    monkeypatch.setattr(manifest, 'KEBAB_RE', None)\n"
        "    monkeypatch.setattr('kiro_crew.apps.manifest._path_escapes_app_root', None)\n"
    )
    found = facade_mirror_scan.patched_names(planted, {FACADE: FACADE}).get(FACADE, set())
    assert found == {"KEBAB_RE", "_path_escapes_app_root"}


def test_no_owner_reads_or_binds_a_name_patched_through_the_facade() -> None:
    """The reach the unsplit module had. A write to ``manifest.<name>`` was what every
    reader of that name read; with plain re-exports that holds only while every reader is
    the facade, so a test that patches a name an owner reads must fail here first."""
    patched = _corpus_patched_names()
    stray = {
        owner.__name__: sorted(
            patched
            & (facade_mirror_scan.global_reads(_source(owner), owner.__file__) | set(vars(owner)))
        )
        for owner in _owners()
    }
    assert {name: names for name, names in stray.items() if names} == {}


def test_manifest_sys_is_the_sys_platform_config_reads() -> None:
    """``test_app_manifest.py`` patches ``manifest.sys.platform``. That reaches
    ``PlatformConfig.current_os`` in its owner only because both bind the one ``sys``."""
    sections = importlib.import_module("kiro_crew.apps.manifest_sections")
    assert manifest.sys is sys
    assert vars(sections)["sys"] is sys
    assert "sys" in facade_mirror_scan.global_reads(_source(sections), sections.__file__)


def test_the_owners_are_every_manifest_sibling_and_import_only_their_layer() -> None:
    """``OWNERS`` is every ``manifest_*.py`` beside the facade; ``manifest_sections`` imports
    ``manifest_names`` and no other owner imports a family member, the facade included,
    not even for type checking."""
    siblings = {f"kiro_crew.apps.{path.stem}" for path in APPS_DIR.glob("manifest_*.py")}
    assert sorted(siblings) == sorted(OWNERS) == sorted(LAYERS)
    family = {FACADE, *OWNERS}
    reached = {
        owner: sorted(
            name
            for name in _imports(_source(importlib.import_module(owner))) & family
            if name not in LAYERS[owner]
        )
        for owner in OWNERS
    }
    assert {owner: names for owner, names in reached.items() if names} == {}


def test_the_import_scan_resolves_relative_imports() -> None:
    """Non-vacuity for the layering scan: the spellings an owner could use all resolve."""
    planted = "from . import manifest\nfrom .manifest_names import KEBAB_RE\n"
    assert {FACADE, "kiro_crew.apps.manifest_names"} <= _imports(planted)


def test_every_name_an_owner_defines_is_the_facades_object() -> None:
    """Every owner name is bound on the facade, as the owner's own object."""
    wrong = [
        f"{owner.__name__}.{name}"
        for owner in _owners()
        for name in sorted(_defined(_source(owner)))
        if getattr(manifest, name, None) is not getattr(owner, name)
    ]
    assert wrong == []


def test_the_facade_defines_the_code_it_keeps() -> None:
    """``AppManifest``, the endpoint allowlist and the provisioning predicates stay
    defined in ``manifest.py``, and no owner binds any of them."""
    assert sorted(_KEPT - _defined(_source(manifest))) == []
    assert {o.__name__: sorted(_KEPT & set(vars(o))) for o in _owners()} == {
        name: [] for name in OWNERS
    }
    assert manifest.AppManifest.__module__ == FACADE


def test_the_moved_enum_keeps_the_facade_module() -> None:
    """A repr or a pickle names an enum as ``module.qualname``, so ``Containment`` keeps
    the path callers import it from, and that path resolves it."""
    cls = manifest.Containment
    assert cls.__module__ == FACADE
    assert getattr(importlib.import_module(cls.__module__), cls.__qualname__) is cls


def test_every_owner_keeps_postponed_annotations() -> None:
    """The dataclasses' annotations name each other across owners and the facade, so
    they must stay strings, as they were in the one module."""
    assert [o.__name__ for o in _owners() if vars(o).get("annotations") is not _POSTPONED] == []


def test_the_test_tree_walk_reaches_this_file() -> None:
    walked = {path.resolve() for path in _test_tree_files()}
    assert _THIS_FILE in walked
    assert source_corpus.repo_root() / "test" / "test_file_menu_items.py" in walked
